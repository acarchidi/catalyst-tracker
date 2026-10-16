"""Command line entry point: `uv run catalyst <command>`."""

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import httpx
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from catalyst import settings
from catalyst.collectors.report import latest_documents_report
from catalyst.collectors.run import build_clients, collect_all, load_companies
from catalyst.collectors.sec.edgar import EdgarClient
from catalyst.collectors.sec.press_release import NoPressReleaseError, collect_latest_press_release
from catalyst.logs import configure_logging
from catalyst.snapshots import (
    LocalSnapshotStore,
    SnapshotStore,
    SupabaseSnapshotStore,
    VerifyReport,
    verify_snapshots,
)

DEFAULT_SNAPSHOT_DIR = Path("var/snapshots")
DEFAULT_COMPANIES = Path("config/pilot_companies.toml")


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
    verify = commands.add_parser(
        "snapshots-verify", help="Re-read every stored source copy and check it against its recorded hash."
    )
    _store_arguments(verify)
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
    engine = create_engine(settings.database_url())
    try:
        if args.command == "snapshots-verify":
            return _verify(store, sessionmaker(engine))
        if args.command == "collect":
            return _collect(args, email, store, sessionmaker(engine))
        return _press_release(args, email, store, sessionmaker(engine))
    finally:
        engine.dispose()


def _verify(store: SnapshotStore, session_factory: sessionmaker[Session]) -> int:
    report: VerifyReport = verify_snapshots(session_factory, store)
    summary = {"checked": report.checked, "missing": report.missing, "changed": report.changed}
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
