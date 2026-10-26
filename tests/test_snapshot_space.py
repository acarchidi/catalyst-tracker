"""Recompressing the stored copies, and reporting what fills the bucket (ADR 0046).

Every step of `compress_snapshots` must leave each copy readable as its exact original bytes:
a plain copy is deleted only after its compressed copy has been read back and checked.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from catalyst import cli
from catalyst.cli import main
from catalyst.db.repository import save_source_document
from catalyst.schemas import SourceDocument, SourceTier
from catalyst.snapshot_space import (
    NO_ROW,
    compress_snapshots,
    snapshot_sources,
    snapshot_usage,
)
from catalyst.snapshots import COMPRESSED_SUFFIX, SupabaseSnapshotStore, compress_copy, snapshot_key
from fake_storage import BUCKET, FakeStorage, store_for

CONTENTS = [
    b'{"protocolSection":{"identificationModule":{"nctId":"NCT01"}}}' * 40,
    b"<html><body>" + b"Topline data are expected in mid-2027. " * 300 + b"</body></html>",
    b"<rss><item>short wire item</item></rss>",
]


def _key(content: bytes) -> str:
    return snapshot_key(hashlib.sha256(content).hexdigest())


def _plain_fake(contents: list[bytes] = CONTENTS) -> FakeStorage:
    """Return a bucket as it is today: every copy stored plain."""
    fake = FakeStorage()
    for content in contents:
        fake.objects[f"{BUCKET}/{_key(content)}"] = content
    return fake


def _writes(fake: FakeStorage) -> list[str]:
    return [
        f"{r.method} {r.url.path}"
        for r in fake.requests
        if r.method == "DELETE" or (r.method == "POST" and "/object/list/" not in r.url.path)
    ]


def test_a_dry_run_reports_the_sizes_and_changes_nothing() -> None:
    fake = _plain_fake()
    before = dict(fake.objects)
    report = compress_snapshots(store_for(fake), dry_run=True)
    assert fake.objects == before
    assert _writes(fake) == []
    assert report.dry_run
    assert (report.objects, report.plain, report.compressed) == (3, 3, 3)
    assert report.bytes_before == sum(map(len, CONTENTS))
    assert report.bytes_after == sum(len(compress_copy(c)) for c in CONTENTS)
    assert report.ok


def test_compressing_keeps_every_copy_readable_as_its_exact_bytes() -> None:
    fake = _plain_fake()
    store = store_for(fake)
    report = compress_snapshots(store)
    assert report.ok
    assert (report.plain, report.compressed, report.problems) == (3, 3, 0)
    assert sorted(fake.objects) == sorted(f"{BUCKET}/{_key(c)}{COMPRESSED_SUFFIX}" for c in CONTENTS)
    for content in CONTENTS:
        assert store.get(_key(content)) == content
    assert report.bytes_after == sum(len(o) for o in fake.objects.values())
    assert report.bytes_after < report.bytes_before


def test_each_plain_copy_is_deleted_only_after_its_compressed_copy_was_read_back() -> None:
    fake = _plain_fake(CONTENTS[:1])
    compress_snapshots(store_for(fake))
    key = _key(CONTENTS[0])
    steps = [
        f"{r.method} {r.url.path.removeprefix('/storage/v1')}"
        for r in fake.requests
        if "/object/list/" not in r.url.path
    ]
    assert steps == [
        f"GET /object/authenticated/{BUCKET}/{key}",
        f"POST /object/{BUCKET}/{key}{COMPRESSED_SUFFIX}",
        f"GET /object/authenticated/{BUCKET}/{key}{COMPRESSED_SUFFIX}",
        f"DELETE /object/{BUCKET}/{key}",
    ]


def test_a_copy_listed_twice_is_counted_once() -> None:
    fake = _plain_fake()
    store = store_for(fake)
    listed = list(store.list_objects())
    store.list_objects = lambda *_args, **_kwargs: iter(listed + listed)  # type: ignore[method-assign]
    report = compress_snapshots(store, dry_run=True)
    assert (report.objects, report.plain, report.compressed) == (3, 3, 3)
    assert report.bytes_before == sum(map(len, CONTENTS))


def test_running_it_again_changes_nothing() -> None:
    fake = _plain_fake()
    store = store_for(fake)
    compress_snapshots(store)
    after_first = dict(fake.objects)
    fake.requests.clear()
    report = compress_snapshots(store)
    assert fake.objects == after_first
    assert _writes(fake) == []
    assert (report.plain, report.already_compressed, report.compressed) == (0, 3, 0)
    assert report.bytes_before == report.bytes_after


def test_a_run_stopped_after_the_upload_is_finished_by_the_next_run() -> None:
    fake = _plain_fake(CONTENTS[:1])
    key = _key(CONTENTS[0])
    fake.objects[f"{BUCKET}/{key}{COMPRESSED_SUFFIX}"] = compress_copy(
        CONTENTS[0]
    )  # the earlier run stopped here
    dry = compress_snapshots(store_for(fake), dry_run=True)
    assert dry.bytes_after == len(compress_copy(CONTENTS[0]))  # the duplicate is not counted twice
    report = compress_snapshots(store_for(fake))
    assert (report.compressed, report.problems) == (1, 0)
    assert list(fake.objects) == [f"{BUCKET}/{key}{COMPRESSED_SUFFIX}"]
    assert report.bytes_after == dry.bytes_after


def test_a_plain_copy_that_no_longer_matches_its_hash_is_left_alone() -> None:
    fake = _plain_fake(CONTENTS[:1])
    key = _key(CONTENTS[0])
    fake.objects[f"{BUCKET}/{key}"] = b"edited"
    report = compress_snapshots(store_for(fake))
    assert (report.changed, report.compressed) == (1, 0)
    assert not report.ok
    assert fake.objects == {f"{BUCKET}/{key}": b"edited"}


def test_a_compressed_copy_that_reads_back_wrong_keeps_the_plain_copy() -> None:
    fake = _plain_fake(CONTENTS[:1])
    fake.on_upload = lambda _name, _content: compress_copy(b"not what was sent")
    store = store_for(fake)
    report = compress_snapshots(store)
    assert (report.check_failed, report.compressed) == (1, 0)
    assert not report.ok
    # The bad compressed copy is removed again, so reads still find the good plain copy.
    assert fake.objects == {f"{BUCKET}/{_key(CONTENTS[0])}": CONTENTS[0]}
    assert store.get(_key(CONTENTS[0])) == CONTENTS[0]


def test_one_unreadable_copy_does_not_stop_the_others() -> None:
    fake = _plain_fake()
    fake.failing_reads.add(f"{BUCKET}/{_key(CONTENTS[1])}")
    report = compress_snapshots(store_for(fake), workers=2)
    assert (report.errors, report.compressed) == (1, 2)
    assert fake.objects[f"{BUCKET}/{_key(CONTENTS[1])}"] == CONTENTS[1]


def test_a_copy_over_the_size_cap_stays_plain() -> None:
    fake = _plain_fake()
    limit = len(CONTENTS[1]) - 1
    store = SupabaseSnapshotStore(
        "https://example.supabase.co",
        "sb_secret_test-only-not-a-real-key",
        transport=httpx.MockTransport(fake),
        max_snapshot_bytes=limit,
    )
    report = compress_snapshots(store)
    assert (report.too_large, report.compressed) == (1, 2)
    assert fake.objects[f"{BUCKET}/{_key(CONTENTS[1])}"] == CONTENTS[1]
    assert store.get(_key(CONTENTS[1])) == CONTENTS[1]


def test_other_files_in_the_bucket_are_not_touched() -> None:
    fake = _plain_fake(CONTENTS[:1])
    fake.objects[f"{BUCKET}/notes/readme.txt"] = b"not a copy"
    misfiled = f"sha256/00/{'a' * 64}"  # its folder is not its hash's first two digits
    fake.objects[f"{BUCKET}/{misfiled}"] = b"misfiled"
    report = compress_snapshots(store_for(fake))
    assert (report.other, report.compressed, report.problems) == (2, 1, 0)
    assert fake.objects[f"{BUCKET}/notes/readme.txt"] == b"not a copy"
    assert fake.objects[f"{BUCKET}/{misfiled}"] == b"misfiled"


def test_a_time_limit_stops_before_starting_more_copies() -> None:
    fake = _plain_fake()
    before = dict(fake.objects)
    report = compress_snapshots(store_for(fake), deadline=0.0, clock=lambda: 1.0)
    assert (report.not_started, report.compressed) == (3, 0)
    assert report.ok  # nothing went wrong; the next run carries on
    assert fake.objects == before


def test_the_log_holds_counts_and_never_content(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    compress_snapshots(store_for(_plain_fake()))
    assert "snapshots_compress_finished" in caplog.text
    assert "Topline" not in caplog.text
    assert "NCT01" not in caplog.text


def test_usage_totals_the_bucket_by_format() -> None:
    fake = _plain_fake(CONTENTS[:2])
    store = store_for(fake)
    store.put(CONTENTS[2])
    usage = snapshot_usage(store)
    assert usage.objects == 3
    assert usage.bytes == sum(len(o) for o in fake.objects.values())
    assert usage.by_format == {
        "plain": {"objects": 2, "bytes": len(CONTENTS[0]) + len(CONTENTS[1])},
        "gzip": {"objects": 1, "bytes": len(compress_copy(CONTENTS[2]))},
    }
    assert usage.by_source == {}


def test_usage_by_source_counts_each_copy_once() -> None:
    fake = _plain_fake()
    owners = {_key(CONTENTS[0]): "ctgov", _key(CONTENTS[1]): "sec 10-Q"}
    usage = snapshot_usage(store_for(fake), owners)
    assert usage.by_source == {
        "ctgov": {"objects": 1, "bytes": len(CONTENTS[0])},
        "sec 10-Q": {"objects": 1, "bytes": len(CONTENTS[1])},
        NO_ROW: {"objects": 1, "bytes": len(CONTENTS[2])},
    }


def _document(source_key: str, document_type: str, content: bytes) -> SourceDocument:
    return SourceDocument(
        source_key=source_key,
        url="https://example.org/" + source_key.replace(":", "/"),
        publisher="test",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type=document_type,
        published_at=datetime(2026, 10, 1, tzinfo=UTC),
        retrieved_at=datetime(2026, 10, 2, tzinfo=UTC),
        sha256=hashlib.sha256(content).hexdigest(),
        snapshot_key=_key(content),
        content_type="text/html",
    )


def test_sources_are_named_by_collector_and_sec_form(session_factory: sessionmaker[Session]) -> None:
    rows = [
        _document("ctgov:NCT01:2026-09-01", "Registry record", CONTENTS[0]),
        _document("sec:0000875320-26-000259:vrtx-10q.htm", "10-Q", CONTENTS[1]),
        _document("wire:prn-biotech:abc", "Press release (wire)", CONTENTS[2]),
        _document("wire:prn-biotech:def", "Press release (wire)", CONTENTS[2]),  # same bytes, one copy
    ]
    with session_factory.begin() as session:
        for row in rows:
            save_source_document(session, row)
    with session_factory() as session:
        assert snapshot_sources(session) == {
            _key(CONTENTS[0]): "ctgov",
            _key(CONTENTS[1]): "sec 10-Q",
            _key(CONTENTS[2]): "wire",
        }


class _FakeEngine:
    def dispose(self) -> None:
        pass


def _use_fake(monkeypatch: pytest.MonkeyPatch, fake: FakeStorage) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test-only")
    monkeypatch.setattr(cli, "SupabaseSnapshotStore", lambda *_args, **_kwargs: store_for(fake))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]


@pytest.mark.parametrize("command", ["snapshots-compress", "snapshots-usage"])
@pytest.mark.parametrize("missing", ["SUPABASE_URL", "SUPABASE_SECRET_KEY"])
def test_the_commands_need_the_supabase_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str, missing: str
) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test-only")
    monkeypatch.delenv(missing)
    assert main([command]) == 2
    err = capsys.readouterr().err
    assert missing in err
    assert "sb_secret_test-only" not in err


def test_the_compress_command_prints_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _plain_fake()
    _use_fake(monkeypatch, fake)
    assert main(["snapshots-compress", "--dry-run"]) == 0
    printed: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert printed["dry_run"] is True
    assert printed["plain"] == 3
    assert printed["bytes_before"] == sum(map(len, CONTENTS))
    assert _writes(fake) == []
    assert main(["snapshots-compress", "--workers", "2", "--max-minutes", "60"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (printed["dry_run"], printed["compressed"]) == (False, 3)


def test_the_compress_command_fails_when_a_copy_needs_attention(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _plain_fake(CONTENTS[:1])
    fake.objects[f"{BUCKET}/{_key(CONTENTS[0])}"] = b"edited"
    _use_fake(monkeypatch, fake)
    assert main(["snapshots-compress"]) == 1
    assert json.loads(capsys.readouterr().out)["changed"] == 1


def test_the_usage_command_prints_totals_and_reads_sources_only_when_asked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_fake(monkeypatch, _plain_fake())
    monkeypatch.setattr(cli, "create_engine", lambda *_args: pytest.fail("no database without --by-source"))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    assert main(["snapshots-usage"]) == 0
    printed: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert printed["objects"] == 3
    assert printed["bytes"] == sum(map(len, CONTENTS))
    assert "by_source" not in printed

    owners = {_key(CONTENTS[0]): "ctgov"}
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "snapshot_sources", lambda _session: owners)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "sessionmaker", lambda _engine: _FakeSessions())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    assert main(["snapshots-usage", "--by-source"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["by_source"]["ctgov"] == {"objects": 1, "bytes": len(CONTENTS[0])}
    assert printed["by_source"][NO_ROW]["objects"] == 2


class _FakeSessions:
    def __call__(self) -> _FakeSessions:
        return self

    def __enter__(self) -> _FakeSessions:
        return self

    def __exit__(self, *_args: object) -> None:
        pass
