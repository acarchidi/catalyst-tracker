"""Command line entry point: `uv run catalyst <command>`."""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from defusedxml.ElementTree import ParseError
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from catalyst import settings
from catalyst.collectors.coverage import build_coverage, coverage_toml, load_overrides, unconfirmed_markdown
from catalyst.collectors.report import latest_documents_report
from catalyst.collectors.run import build_clients, collect_all, load_companies
from catalyst.collectors.sec.edgar import EdgarClient
from catalyst.collectors.sec.press_release import NoPressReleaseError, collect_latest_press_release
from catalyst.extract.agent import EXTRACTOR_PROMPT, ROLE
from catalyst.extract.evaluate import EVALS, evaluate, load_gold, scores_record
from catalyst.extract.run import extract_documents
from catalyst.gate1 import companies_toml, sample_markdown, sample_records, select_companies
from catalyst.links import check_links, link_client, links_markdown
from catalyst.llm import anthropic_adapter
from catalyst.llm.client import ModelClient
from catalyst.llm.config import load_config
from catalyst.llm.ledger import DatabaseLedger
from catalyst.logs import configure_logging
from catalyst.snapshots import (
    LocalSnapshotStore,
    SnapshotStore,
    SnapshotStoreError,
    SupabaseSnapshotStore,
    VerifyReport,
    verify_snapshots,
)

NEW_YORK = ZoneInfo("America/New_York")
DEFAULT_SNAPSHOT_DIR = Path("var/snapshots")
DEFAULT_COMPANIES = Path("config/pilot_companies.toml")
DEFAULT_COVERAGE = Path("config/coverage_companies.toml")
DEFAULT_OVERRIDES = Path("config/coverage_overrides.toml")
DEFAULT_REVIEW_LIST = Path("docs/coverage-review.md")
DEFAULT_GATE1 = Path("config/gate1_companies.toml")
GATE1_SEED = 2026


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="catalyst", description="Catalyst tracker pipeline commands.")
    commands = parser.add_subparsers(dest="command", required=True)
    press = commands.add_parser(
        "sec-press-release", help="Fetch a company's latest 8-K press release and record it."
    )
    press.add_argument("--cik", type=int, required=True, help="SEC company number, for example 875320.")
    _store_arguments(press)
    collect = commands.add_parser("collect", help="Run every collector for a company list.")
    collect.add_argument("--companies", type=Path, default=DEFAULT_COMPANIES, help="Company list (TOML).")
    collect.add_argument(
        "--since", type=date.fromisoformat, required=True, help="Earliest SEC filing date, YYYY-MM-DD."
    )
    _store_arguments(collect)
    commands.add_parser(
        "report", help="Print each company's latest document per source, with links (Markdown)."
    )
    coverage = commands.add_parser(
        "coverage-build", help="Build the coverage list from SEC industry codes and trial evidence."
    )
    coverage.add_argument("--out", type=Path, default=DEFAULT_COVERAGE, help="Company list to write.")
    coverage.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES, help="Hand corrections.")
    coverage.add_argument("--review", type=Path, default=DEFAULT_REVIEW_LIST, help="Review list to write.")
    select = commands.add_parser(
        "gate1-select", help="Choose the Gate 1 companies: the pilot ten plus a random draw."
    )
    select.add_argument("--count", type=int, default=50, help="How many companies in all.")
    select.add_argument("--seed", type=int, default=GATE1_SEED, help="Seed for the repeatable draw.")
    select.add_argument("--out", type=Path, default=DEFAULT_GATE1, help="Company list to write.")
    sample = commands.add_parser("sample-records", help="Print a repeatable random sample of stored records.")
    sample.add_argument("--count", type=int, default=10, help="How many records.")
    sample.add_argument("--seed", type=int, default=GATE1_SEED, help="Seed for the repeatable sample.")
    links = commands.add_parser("check-links", help="Check that stored source links still work.")
    links.add_argument(
        "--sample", type=int, default=None, help="Check a random sample instead of every link."
    )
    links.add_argument("--seed", type=int, default=GATE1_SEED, help="Seed for the repeatable sample.")
    verify = commands.add_parser(
        "snapshots-verify", help="Re-read every stored source copy and check it against its recorded hash."
    )
    _store_arguments(verify)
    extract = commands.add_parser(
        "extract-catalysts",
        help="Ask the extractor for the catalysts in stored press releases (costs money).",
    )
    extract.add_argument("--limit", type=int, default=50, help="Most documents to read in this run.")
    extract.add_argument(
        "--since", type=date.fromisoformat, default=None, help="Earliest publication date, YYYY-MM-DD."
    )
    extract.add_argument("--out", type=Path, default=None, help="Write the full results (JSON) here.")
    _store_arguments(extract)
    evaluate = commands.add_parser(
        "eval-extractor", help="Score the extractor on the gold set (costs money; about 12 model calls)."
    )
    evaluate.add_argument("--out", type=Path, required=True, help="Write the scores record (JSON) here.")
    return parser


def _store_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--snapshot-store",
        choices=("local", "supabase"),
        default="local",
        help="Where source copies are kept: local disk, or the project's private Supabase Storage bucket.",
    )
    parser.add_argument(
        "--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR, help="Folder for the local store."
    )


def _open_store(args: argparse.Namespace, *, create_bucket: bool) -> SnapshotStore:
    """Open the chosen store; raises MissingSettingError when Supabase settings are absent."""
    if args.snapshot_store == "local":
        return LocalSnapshotStore(args.snapshot_dir)
    store = SupabaseSnapshotStore(settings.supabase_url(), settings.supabase_secret_key())
    if create_bucket:
        store.ensure_bucket()
    return store


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command; results go to standard output, logs to standard error."""
    args = _parser().parse_args(argv)
    configure_logging()
    standalone = {
        "coverage-build": _coverage,
        "gate1-select": _gate1_select,
        "sample-records": _gate1_checks,
        "check-links": _gate1_checks,
        "extract-catalysts": _extract,
        "eval-extractor": _evaluate,
    }
    if args.command in standalone:
        return standalone[args.command](args)
    if args.command == "report":
        engine = create_engine(settings.database_url())
        try:
            print(latest_documents_report(engine))
        finally:
            engine.dispose()
        return 0
    try:
        email = "" if args.command == "snapshots-verify" else settings.sec_contact_email()
        store = _open_store(args, create_bucket=args.command != "snapshots-verify")
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    except (SnapshotStoreError, httpx.HTTPError) as error:
        print(f"Nothing stored: the snapshot store is not usable ({error}).", file=sys.stderr)
        return 1
    engine = create_engine(settings.database_url())
    try:
        if args.command == "snapshots-verify":
            return _verify(store, sessionmaker(engine))
        if args.command == "collect":
            return _collect(args, email, store, sessionmaker(engine))
        return _press_release(args, email, store, sessionmaker(engine))
    finally:
        engine.dispose()


def _coverage(args: argparse.Namespace) -> int:
    try:
        clients = build_clients(settings.sec_contact_email())
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    try:
        entries = build_coverage(clients.sec, clients.web, load_overrides(args.overrides))
    except httpx.HTTPError as error:
        print(f"Nothing written: a request failed ({error}).", file=sys.stderr)
        return 1
    except (ValueError, ParseError) as error:  # malformed JSON or XML from a source
        print(f"Nothing written: a source sent an answer that could not be read ({error}).", file=sys.stderr)
        return 1
    today = datetime.now(NEW_YORK).date()
    args.out.write_text(coverage_toml(entries, today), encoding="utf-8")
    args.review.write_text(unconfirmed_markdown(entries, today), encoding="utf-8")
    included = sum(entry.included for entry in entries)
    print(json.dumps({"listed": len(entries), "included": included, "review": len(entries) - included}))
    return 0


def _gate1_select(args: argparse.Namespace) -> int:
    pilot = load_companies(DEFAULT_COMPANIES)
    try:
        chosen = select_companies(pilot, load_companies(DEFAULT_COVERAGE), args.count, args.seed)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    header = (
        f"Gate 1 companies: the {len(pilot)} pilot companies plus a random draw from "
        f"config/coverage_companies.toml (seed {args.seed}), written by `catalyst gate1-select` (ADR 0010)."
    )
    args.out.write_text(companies_toml(chosen, header), encoding="utf-8")
    print(json.dumps({"companies": len(chosen), "out": str(args.out)}))
    return 0


def _gate1_checks(args: argparse.Namespace) -> int:
    engine = create_engine(settings.database_url())
    try:
        if args.command == "sample-records":
            print(sample_markdown(sample_records(sessionmaker(engine), args.count, args.seed)), end="")
            return 0
        try:
            client = link_client(settings.sec_contact_email())
        except settings.MissingSettingError as error:
            print(error, file=sys.stderr)
            return 2
        report = check_links(sessionmaker(engine), client, args.sample, args.seed)
        print(links_markdown(report), end="")
        return 0 if report.ok else 1
    finally:
        engine.dispose()


def _extract(args: argparse.Namespace) -> int:
    try:
        adapter = anthropic_adapter.from_settings()
        store = _open_store(args, create_bucket=False)
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    engine = create_engine(settings.database_url())
    try:
        client = ModelClient(load_config(), {adapter.name: adapter}, DatabaseLedger(engine))
        report = extract_documents(sessionmaker(engine), store, client, limit=args.limit, since=args.since)
    finally:
        engine.dispose()
    if args.out is not None:
        documents = [asdict(d) | {"published": d.published.isoformat()} for d in report.documents]
        args.out.write_text(json.dumps(documents, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = report.summary()
    print(json.dumps(summary, indent=2))
    return 1 if summary["documents_failed"] or summary["stopped_early"] else 0


def _evaluate(args: argparse.Namespace) -> int:
    try:
        adapter = anthropic_adapter.from_settings()
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    config = load_config()
    engine = create_engine(settings.database_url())
    try:
        client = ModelClient(config, {adapter.name: adapter}, DatabaseLedger(engine))
        gold = load_gold(EVALS / "gold.jsonl")
        scores, _ = evaluate(client, gold, EVALS / "documents")
    finally:
        engine.dispose()
    model = config.roles[ROLE].model
    record = scores_record(scores, EXTRACTOR_PROMPT, model, EVALS / "gold.jsonl", len(gold))
    args.out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2))
    return 0


def _verify(store: SnapshotStore, session_factory: sessionmaker[Session]) -> int:
    report: VerifyReport = verify_snapshots(session_factory, store)
    summary = {
        "checked": report.checked,
        "missing": report.missing,
        "changed": report.changed,
        "unreadable": report.unreadable,
    }
    print(json.dumps(summary, indent=2))
    return 0 if report.ok else 1


def _collect(
    args: argparse.Namespace, email: str, store: SnapshotStore, session_factory: sessionmaker[Session]
) -> int:
    stats = collect_all(
        load_companies(args.companies),
        build_clients(email),
        store,
        session_factory,
        args.since,
    )
    summary = [
        {"source": s.source, "created": s.created, "existing": s.existing, "error": s.error} for s in stats
    ]
    print(json.dumps(summary, indent=2))
    return 1 if any(s.error for s in stats) else 0


def _press_release(
    args: argparse.Namespace, email: str, store: SnapshotStore, session_factory: sessionmaker[Session]
) -> int:
    try:
        document, created = collect_latest_press_release(EdgarClient(email), store, session_factory, args.cik)
    except NoPressReleaseError as error:
        print(f"Nothing stored: {error}.", file=sys.stderr)
        return 1
    except httpx.HTTPError as error:
        print(f"Nothing stored: the SEC request failed ({error}).", file=sys.stderr)
        return 1
    except OperationalError:
        print("Nothing stored: cannot reach the database. Is it running? See README.", file=sys.stderr)
        return 1
    print(json.dumps({"created": created, **document.model_dump(mode="json")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
