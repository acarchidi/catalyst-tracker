"""Command line entry point: `uv run catalyst <command>`."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from catalyst import settings
from catalyst.collectors.sec.edgar import EdgarClient
from catalyst.collectors.sec.press_release import collect_latest_press_release
from catalyst.logs import configure_logging
from catalyst.snapshots import LocalSnapshotStore

DEFAULT_SNAPSHOT_DIR = Path("var/snapshots")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="catalyst", description="Catalyst tracker pipeline commands.")
    commands = parser.add_subparsers(dest="command", required=True)
    press = commands.add_parser(
        "sec-press-release", help="Fetch a company's latest 8-K press release and record it."
    )
    press.add_argument("--cik", type=int, required=True, help="SEC company number, for example 875320.")
    press.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR, help="Where copies go.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and print its result as JSON on standard output."""
    args = _parser().parse_args(argv)
    configure_logging()
    try:
        client = EdgarClient(settings.sec_contact_email())
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    engine = create_engine(settings.database_url())
    try:
        document, created = collect_latest_press_release(
            client, LocalSnapshotStore(args.snapshot_dir), sessionmaker(engine), args.cik
        )
    finally:
        engine.dispose()
    print(json.dumps({"created": created, **document.model_dump(mode="json")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
