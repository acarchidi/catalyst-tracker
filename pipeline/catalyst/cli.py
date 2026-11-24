"""Command line entry point: `uv run catalyst <command>`."""

import argparse
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from defusedxml.ElementTree import ParseError
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from catalyst import settings
from catalyst.alerts import RoleSpend, build_alerts, load_thresholds, parse_run, spend_today
from catalyst.api.access import Access
from catalyst.api.http import create_app
from catalyst.api.mcp_server import build_server
from catalyst.api.read import published, read_session
from catalyst.api.views import CatalystDetail
from catalyst.audit.sample import SAMPLE_SIZE, run_sample_check, week_of
from catalyst.budget import TimeBudget
from catalyst.collectors.coverage import build_coverage, coverage_toml, load_overrides, unconfirmed_markdown
from catalyst.collectors.report import latest_documents_report
from catalyst.collectors.run import ON_REQUEST, SOURCES, build_clients, collect_all, load_companies
from catalyst.collectors.sec.backfill import backfill_press_releases
from catalyst.collectors.sec.edgar import EdgarClient
from catalyst.collectors.sec.press_release import NoPressReleaseError, collect_latest_press_release
from catalyst.collectors.sources.mesh import stored_index
from catalyst.company_names import recompute_display_names
from catalyst.copy_cache import CachedStore, claim_copy_keys
from catalyst.drug_list_store import SOURCES as DRUG_SOURCES
from catalyst.drug_list_store import load_drugs
from catalyst.export import build_export, write_export
from catalyst.extract.agent import EXTRACTOR_PROMPT, ROLE
from catalyst.extract.backfill import backfill_status
from catalyst.extract.disease import (
    CHECKER_PROMPT,
    READER_PROMPT,
    evaluate_checker,
    evaluate_readers,
    load_diseases,
    score_record,
)
from catalyst.extract.evaluate import EVALS, evaluate, load_gold, scores_record
from catalyst.extract.indicate import indicate_catalysts
from catalyst.extract.run import extract_documents
from catalyst.gate1 import companies_toml, sample_markdown, sample_records, select_companies
from catalyst.issues import API, plan_markdown, post
from catalyst.links import DEAD, check_links, link_client, link_summary, links_markdown, published_urls
from catalyst.llm import anthropic_adapter
from catalyst.llm.client import ModelClient
from catalyst.llm.config import load_config
from catalyst.llm.ledger import DatabaseLedger
from catalyst.llm.prompts import load_prompt
from catalyst.logs import configure_logging
from catalyst.outreach import prospects
from catalyst.outreach.digest import (
    DEFAULT_CALENDAR_LIMIT,
    PAID_CALENDAR_LIMIT,
    build_digest,
    details_from_export,
    latest_issue_date,
    render_html,
    render_page,
)
from catalyst.publish.indication import publish_indications
from catalyst.publish.run import decide_claims
from catalyst.publish.sanity import check_safe_to_write
from catalyst.publish.write import publish_claims
from catalyst.resolve_claims import resolve_claims
from catalyst.sealed import SealError, generate_keypair, seal, unseal
from catalyst.snapshot_space import DEFAULT_WORKERS, compress_snapshots, snapshot_sources, snapshot_usage
from catalyst.snapshots import (
    LocalSnapshotStore,
    SnapshotStore,
    SnapshotStoreError,
    SupabaseSnapshotStore,
    VerifyReport,
    verify_snapshots,
)
from catalyst.verify.advisory import check_advisory_meetings
from catalyst.verify.company import check_company_named
from catalyst.verify.drug import check_drug_named
from catalyst.verify.event_type import check_type_named
from catalyst.verify.planted import load_corpus
from catalyst.verify.planted_report import render_report
from catalyst.verify.planted_report import run as run_planted
from catalyst.verify.planted_report import scores as planted_scores
from catalyst.verify.run import run_checks
from catalyst.verify.second import VERIFIER_PROMPT, VERIFIER_ROLE
from catalyst.verify.second_run import run_second_reading

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
    collect.add_argument(
        "--sources",
        type=_sources,
        default=None,
        help=(
            f"Only these collectors, comma-separated, from: {', '.join(SOURCES)} (default: all); "
            "also 'ctgov-linked', only the trials our claims and catalysts name (ADR 0040), and "
            "'drugs', the drug list (ADR 0035), and 'mesh', NLM's disease names (ADR 0053); "
            "these run only when named."
        ),
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
    links.add_argument(
        "--published",
        action="store_true",
        help=(
            "Only the links published records cite; fail only on a missing or moved one, "
            "unless --strict (weekly check)."
        ),
    )
    links.add_argument(
        "--strict",
        action="store_true",
        help="Pass only when every link checked works (HTTP 200), as Gate 1 asks (ADR 0010, ADR 0054).",
    )
    links.add_argument("--summary", type=Path, default=None, help="Also write the counts (JSON) here.")
    verify = commands.add_parser(
        "snapshots-verify", help="Re-read every stored source copy and check it against its recorded hash."
    )
    _store_arguments(verify)
    verify.add_argument(
        "--workers", type=int, default=1, help="Copies read at the same time (default 1; ADR 0054)."
    )
    _snapshot_space_arguments(commands)
    extract = commands.add_parser(
        "extract-catalysts",
        help="Ask the extractor for the catalysts in stored press releases (costs money).",
    )
    extract.add_argument(
        "--limit", type=_at_least_one, default=50, help="Most documents to read in this run."
    )
    extract.add_argument(
        "--since", type=date.fromisoformat, default=None, help="Earliest publication date, YYYY-MM-DD."
    )
    extract.add_argument("--out", type=Path, default=None, help="Write the full results (JSON) here.")
    extract.add_argument(
        "--max-minutes",
        type=_at_least_one,
        default=None,
        help=(
            "Start no new document after this many minutes; the rest wait for the next run (ADR 0065). "
            "The steps after reading keep their share of the time budget."
        ),
    )
    extract.add_argument(
        "--stop-is-expected",
        action="store_true",
        help="A stop at a daily cap or the time limit is this run's normal end, not a failure (ADR 0065).",
    )
    extract.add_argument(
        "--time-budget-minutes",
        type=_positive_minutes,
        default=TIME_BUDGET_MINUTES,
        help=(
            f"Stop starting new work after this many minutes, so the run ends cleanly and the next "
            f"one carries on (default {TIME_BUDGET_MINUTES:g}; ADR 0064)."
        ),
    )
    _store_arguments(extract)
    backfill = commands.add_parser(
        "backfill-collect",
        help="Store the 8-K press releases filed since 2025 that are not stored yet (free; ADR 0065).",
    )
    backfill.add_argument("--companies", type=Path, default=DEFAULT_COVERAGE, help="Company list (TOML).")
    backfill.add_argument(
        "--max-minutes",
        type=_at_least_one,
        default=None,
        help="Start no new company after this many minutes.",
    )
    _store_arguments(backfill)
    commands.add_parser(
        "backfill-status",
        help="Say whether the backfill's model reads may run today, and how many releases wait (free).",
    )
    verify_claims = commands.add_parser(
        "verify-claims", help="Run the rule checks on every extracted claim and store the outcomes (free)."
    )
    _store_arguments(verify_claims)
    decide = commands.add_parser(
        "decide-claims",
        help=(
            "Decide, from the stored checks and copies, which extracted claims may publish (free). "
            "Its publish count is not final: publish-claims checks each claim again before it writes."
        ),
    )
    _store_arguments(decide)
    publish = commands.add_parser(
        "publish-claims",
        help="Check again each claim marked to publish, and write the live catalysts (free; no model calls).",
    )
    _store_arguments(publish)
    commands.add_parser(
        "resolve-claims", help="Match the names in extracted claims to companies, drugs, and trials (free)."
    )
    names = commands.add_parser(
        "company-names",
        help="Store each company's own spelling of its name, from its stored releases (free; ADR 0068).",
    )
    _store_arguments(names)
    drugs = commands.add_parser(
        "load-drugs",
        help=(
            "Fill the drug list from Drugs@FDA and ClinicalTrials.gov for collected companies "
            "(free; about 550 requests, 13 minutes; ADR 0035)."
        ),
    )
    drugs.add_argument("--companies", type=Path, default=DEFAULT_COVERAGE, help="Company list (TOML).")
    drugs.add_argument(
        "--sources",
        type=_drug_sources,
        default=frozenset(DRUG_SOURCES),
        help=f"Comma-separated, from: {', '.join(DRUG_SOURCES)} (default: both; ctgov needs openfda).",
    )
    api = commands.add_parser(
        "api", help="Serve the read-only data API on this computer only, at http://127.0.0.1:<port>/v1."
    )
    api.add_argument("--port", type=int, default=8000, help="Port on this computer (default 8000).")
    commands.add_parser("mcp", help="Run the read-only MCP server over standard input and output.")
    export = commands.add_parser(
        "export-catalysts",
        help="Write every published catalyst, each value with its source, to one JSON file (read-only).",
    )
    export.add_argument("--out", type=Path, required=True, help="Write the export (JSON) here.")
    export.add_argument(
        "--include-held",
        action="store_true",
        help="Also list, apart, the claims the publish gate holds for review, with its reasons.",
    )
    _store_arguments(export)  # the held claims' paragraphs come from the stored copies
    digest = commands.add_parser(
        "digest",
        help="Build the weekly digest of published changes (read-only; plain code; sends nothing).",
    )
    digest.add_argument(
        "--issue-date",
        type=date.fromisoformat,
        help="The Monday the issue is dated, YYYY-MM-DD (default: the latest Monday in New York).",
    )
    digest.add_argument("--out", type=Path, required=True, help="Write the digest (JSON) here.")
    digest.add_argument(
        "--from-export",
        type=Path,
        help="Build from this file written by export-catalysts, not from the database (ADR 0049).",
    )
    digest.add_argument("--html", type=Path, help="Also write the digest as an HTML fragment here.")
    digest.add_argument(
        "--page", type=Path, help="Also write the digest as a complete web page, to copy from a browser."
    )
    digest.add_argument(
        "--access",
        choices=[access.value for access in Access],
        default=Access.FREE.value,
        help="free: the next 30 days, the rest counted. paid: the full calendar and date histories; "
        "for paid subscribers only, so never publish it openly (ADR 0049).",
    )
    digest.add_argument(
        "--calendar-limit",
        type=int,
        help=f"Most other catalysts to list (free {DEFAULT_CALENDAR_LIMIT}, paid {PAID_CALENDAR_LIMIT}).",
    )
    keygen = commands.add_parser(
        "seal-keygen",
        help="Make a one-off key pair for a sealed export: save the private key, print the public line.",
    )
    keygen.add_argument("--private-key", type=Path, required=True, help="New file for the private key.")
    seal_cmd = commands.add_parser(
        "seal", help="Print a file sealed to a public line, safe for a public job log (ADR 0034)."
    )
    seal_cmd.add_argument("--to", dest="public", required=True, help="The public line from seal-keygen.")
    seal_cmd.add_argument("--in", dest="source", type=Path, required=True, help="The file to seal.")
    unseal_cmd = commands.add_parser("unseal", help="Open a sealed file, or a job log that holds one.")
    unseal_cmd.add_argument("--private-key", type=Path, required=True, help="The private key file.")
    unseal_cmd.add_argument("--in", dest="source", type=Path, required=True, help="Sealed text or job log.")
    unseal_cmd.add_argument("--out", type=Path, required=True, help="Write the opened file here.")
    evaluate = commands.add_parser(
        "eval-extractor", help="Score the extractor on the gold set (costs money; about 12 model calls)."
    )
    evaluate.add_argument("--out", type=Path, required=True, help="Write the scores record (JSON) here.")
    evaluate.add_argument(
        "--prompt",
        choices=sorted([*_READERS, *_INDICATION_READERS]),
        default=EXTRACTOR_PROMPT[0],
        help=(
            "Which reader to score: the extractor (default), the verifier's second reading, "
            "or either reader of indications (ADR 0053)."
        ),
    )
    planted = commands.add_parser(
        "planted-errors",
        help="Plant known errors in gold-set claims and report what the checks catch (free).",
    )
    planted.add_argument("--out", type=Path, required=True, help="Write the one-page report (Markdown) here.")
    planted.add_argument("--scores", type=Path, help="Also write the counts (JSON) here.")
    _issue_arguments(commands)
    indicate = commands.add_parser(
        "indicate-catalysts",
        help="Ask both indication readers about published catalysts without an indication (costs money).",
    )
    indicate.add_argument(
        "--limit",
        type=_at_least_one,
        default=10,
        help="Most catalysts to ask about in this run (default 10).",
    )
    _store_arguments(indicate)
    sample_check = commands.add_parser(
        "sample-check",
        help="Check the week's random draw of published catalysts against their sources (costs money).",
    )
    sample_check.add_argument(
        "--limit", type=int, default=1, help="Most records to check in this run (default 1, inside the cap)."
    )
    sample_check.add_argument(
        "--week-of",
        type=date.fromisoformat,
        default=None,
        help="Any day of the week to check (default: today).",
    )
    sample_check.add_argument("--size", type=int, default=SAMPLE_SIZE, help="Records in the week's draw.")
    _store_arguments(sample_check)
    check = commands.add_parser(
        "prospects-check",
        help="Check the outreach prospect list and print its counts (ADR 0020; sends nothing).",
    )
    check.add_argument("--file", type=Path, default=prospects.DEFAULT_PATH, help="Prospect list (CSV).")
    check.add_argument(
        "--previous", type=Path, help="An earlier copy; fail if it had a do_not_contact row now missing."
    )
    return parser


def _drug_sources(value: str) -> frozenset[str]:
    """Parse `load-drugs --sources`; registry names need Drugs@FDA for the comparator rule."""
    names = frozenset(name.strip() for name in value.split(","))
    if unknown := names - set(DRUG_SOURCES):
        raise argparse.ArgumentTypeError(
            f"unknown source {', '.join(sorted(unknown))}; valid: openfda, ctgov"
        )
    if "ctgov" in names and "openfda" not in names:
        raise argparse.ArgumentTypeError("ctgov needs openfda: Drugs@FDA tells approved comparators apart")
    return names


def _sources(value: str) -> frozenset[str]:
    """Parse `--sources`: comma-separated names from `SOURCES`; refuse any other, naming the valid ones."""
    names = [name.strip() for name in value.split(",")]
    unknown = [name for name in names if name not in (*SOURCES, *ON_REQUEST)]
    if unknown:
        shown = ", ".join(repr(name) for name in unknown)
        valid = ", ".join((*SOURCES, *ON_REQUEST))
        raise argparse.ArgumentTypeError(f"unknown source {shown}; valid sources: {valid}")
    return frozenset(names)


def _issue_arguments(commands: Any) -> None:
    alerts = commands.add_parser(
        "alerts", help="Build the alerts a run raises or solves, from its job summaries (free; no model)."
    )
    alerts.add_argument(
        "--run",
        action="append",
        default=[],
        type=parse_run,
        help="A job in this run as NAME:OUTCOME[:FILE], FILE being the JSON the job printed. Repeatable.",
    )
    alerts.add_argument("--spend", action="store_true", help="Also check today's spend per role (database).")
    alerts.add_argument("--run-url", required=True, help="Link to this run's log, for the alert text.")
    alerts.add_argument("--out", type=Path, required=True, help="Write the plan (JSON) here.")
    issues = commands.add_parser("post-issues", help="Post a plan from `alerts` to GitHub as issues.")
    issues.add_argument("--from", dest="plan", type=Path, required=True, help="The plan (JSON).")


def _snapshot_space_arguments(commands: Any) -> None:
    compress = commands.add_parser(
        "snapshots-compress",
        help="Replace each plain copy in the Supabase bucket with its checked compressed copy (ADR 0046).",
    )
    compress.add_argument(
        "--dry-run", action="store_true", help="Only count the copies and the bytes before and after."
    )
    compress.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS, help="Copies handled at the same time."
    )
    compress.add_argument(
        "--max-minutes",
        type=float,
        default=None,
        help="Start no new copy after this many minutes; a later run finishes the rest.",
    )
    usage = commands.add_parser(
        "snapshots-usage", help="Print the Supabase bucket's size in total, by format, and by source."
    )
    usage.add_argument("--by-source", action="store_true", help="Also total by source (reads the database).")


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
        "resolve-claims": _resolve,
        "load-drugs": _load_drugs,
        "verify-claims": _verify_claims,
        "company-names": _company_names,
        "alerts": _alerts,
        "post-issues": _post_issues,
        "decide-claims": _decide_claims,
        "publish-claims": _publish_claims,
        "api": _serve,
        "mcp": _serve,
        "export-catalysts": _export,
        "digest": _digest,
        "seal-keygen": _seal_keygen,
        "seal": _seal,
        "unseal": _unseal,
        "planted-errors": _planted_errors,
        "sample-check": _sample_check,
        "indicate-catalysts": _indicate,
        "prospects-check": _prospects_check,
        "snapshots-compress": _snapshots_space,
        "snapshots-usage": _snapshots_space,
        "backfill-status": _backfill_status,
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
            return _verify(store, sessionmaker(engine), args.workers)
        if args.command == "collect":
            return _collect(args, email, store, sessionmaker(engine))
        if args.command == "backfill-collect":
            return _backfill_collect(args, email, store, sessionmaker(engine))
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
        if args.published:
            urls = published_urls(sessionmaker(engine))
            report = check_links(sessionmaker(engine), client, args.sample, args.seed, urls=urls)
        else:
            report = check_links(sessionmaker(engine), client, args.sample, args.seed)
        if args.summary is not None:
            args.summary.write_text(json.dumps(link_summary(report), indent=2) + "\n", encoding="utf-8")
        print(links_markdown(report), end="")
        if args.strict and not report.results:  # a check of nothing proves nothing (review of PR 125)
            print("No link was checked.", file=sys.stderr)
            return 1
        if args.published and not args.strict:  # a refusal says nothing about the document (ADR 0043)
            return 1 if any(r.status in DEAD for r in report.results) else 0
        return 0 if report.ok else 1
    finally:
        engine.dispose()


# Shares of a run's time budget (ADR 0064). Reading new releases stops first, so the checks and
# the publish gate always have time; the second reading stops next; the checks that read stored
# copies stop at 90%, and then nothing is decided that run; past the whole budget nothing is
# decided or published; past 85% no indication is asked for. Every step is safe to stop and run
# again, so the next run carries on.
TIME_BUDGET_MINUTES = 35.0  # 10 minutes inside the workflow step's 45, for a call already under way
EXTRACT_SHARE = 0.45
SECOND_SHARE = 0.7
CHECKS_SHARE = 0.9
INDICATION_SHARE = 0.85
OUT_OF_TIME = "the run's time ran out first; the next run goes on"


@dataclass(frozen=True)
class RunPlan:
    """When each step of one run must stop starting new work, in minutes from the run's start.

    Reading new releases gets its share of the budget, or the backfill's `--max-minutes` (ADR
    0065); every later step keeps the same time after reading as on a nightly run (ADR 0064).
    """

    budget: TimeBudget
    reading: float
    rest: Callable[[float], float]  # minutes from the start at which a share of the budget is spent

    @classmethod
    def of(cls, args: argparse.Namespace) -> "RunPlan":
        """Build the plan from `--time-budget-minutes` and `--max-minutes`."""
        minutes: float = args.time_budget_minutes
        reading = float(args.max_minutes) if args.max_minutes is not None else minutes * EXTRACT_SHARE
        return cls(TimeBudget(minutes), reading, lambda share: reading + minutes * (share - EXTRACT_SHARE))

    def by(self, share: float) -> float:
        """Return the clock reading at which a step with this share of the budget stops."""
        return self.budget.at(self.rest(share))

    def past(self, share: float) -> bool:
        """Say whether a step with this share of the budget is already out of time."""
        return self.budget.clock() > self.by(share)


def _extract(args: argparse.Namespace) -> int:
    plan = RunPlan.of(args)
    try:
        adapter = anthropic_adapter.from_settings()
        # Every step reads the same stored copies: each is read once a run (ADR 0064).
        store = CachedStore(_open_store(args, create_bucket=False))
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    engine = create_engine(settings.database_url())
    try:
        config = load_config()
        client = ModelClient(config, {adapter.name: adapter}, DatabaseLedger(engine))
        report = extract_documents(
            sessionmaker(engine),
            store,
            client,
            limit=args.limit,
            since=args.since,
            deadline=_utc_now() + timedelta(minutes=plan.reading),
        )
        # The extraction stands if a later step fails; each re-runs next time.
        _prefetch(sessionmaker(engine), store)
        matches, checks = _match_and_check(sessionmaker(engine), store, plan.by(CHECKS_SHARE))
        # Last, so a problem in it never holds up matching or the rule checks (review of PR #34).
        try:
            second: dict[str, Any] = run_second_reading(
                sessionmaker(engine),
                store,
                client,
                config,
                limit=args.limit,
                now=_utc_now,
                deadline=plan.by(SECOND_SHARE),
            ).summary()
        except (SQLAlchemyError, ValueError, KeyError, OSError) as error:  # a bad scores file, too
            second = {"error": f"{type(error).__name__}: the second reading did not finish"}
        decisions = (
            {"skipped": OUT_OF_TIME}
            if plan.past(1.0)
            else decide_after_checks(sessionmaker(engine), store, checks)
        )
        published = _publish_after(sessionmaker(engine), store, decisions, config.roles[VERIFIER_ROLE].model)
        indications = (
            {"skipped": OUT_OF_TIME}
            if plan.past(INDICATION_SHARE)
            else _indications_after(sessionmaker(engine), store, client, published, args.limit)
        )
        # Last and free: each company's own spelling of its name, for the issue (ADR 0068).
        names = (
            {"skipped": OUT_OF_TIME}
            if plan.past(1.0)
            else _display_names(sessionmaker(engine), store, plan.by(1.0))
        )
    finally:
        engine.dispose()
    if args.out is not None:
        documents = [asdict(d) | {"published": d.published.isoformat()} for d in report.documents]
        args.out.write_text(json.dumps(documents, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = report.summary() | {
        "matches": matches,
        "checks": checks,
        "second_reading": second,
        "decisions": decisions,
        "published": published,
        "indications": indications,
        "display_names": names,
        "minutes_used": round(plan.budget.minutes_used(), 1),
    }
    print(json.dumps(summary, indent=2))
    # A backfill run reads until a cap or its time limit stops it; that stop is its normal end (ADR
    # 0065). A stop at the time limit is never a failure: the next run carries on (ADR 0064).
    cap_stop = summary["stopped_early"] and not report.stopped_for_time
    stopped = not args.stop_is_expected and (cap_stop or second.get("stopped_early"))
    second_failed = "error" in second or second.get("documents_failed")
    gate_failed = "error" in decisions or "error" in published or "error" in indications
    failed = summary["documents_failed"] or stopped or second_failed or gate_failed
    return 1 if failed else 0


def _prefetch(session_factory: sessionmaker[Session], store: CachedStore) -> None:
    """Read every claim's stored copy, eight at a time, before the steps that read them (ADR 0064).

    A copy that cannot be read here is read again by the step that needs it, which reports it.
    """
    try:
        with session_factory() as session:
            keys = claim_copy_keys(session)
    except SQLAlchemyError:
        return  # matching reports the database problem itself
    store.prefetch(keys)


def _match_and_check(
    session_factory: sessionmaker[Session], store: SnapshotStore, deadline: float
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Match every claim's names, then run every check; return both summaries.

    The checks that read stored copies stop past `deadline` and say so (ADR 0064).
    """
    checks: dict[str, Any] = {"error": "checks did not run, because matching did not finish"}
    try:
        # With the stored copies, so a name its own document writes can be matched (ADR 0060).
        matches: dict[str, Any] = resolve_claims(session_factory, _utc_now, store).summary()
    except SQLAlchemyError as error:
        return {"error": f"{type(error).__name__}: matching did not finish"}, checks
    try:
        checks = run_checks(session_factory, _utc_now).summary()
    except SQLAlchemyError as error:
        return matches, {"error": f"{type(error).__name__}: checks did not finish"}
    # Each its own step, so a problem in one keeps the others' summaries (Opus review of PR #49).
    try:
        checks["advisory"] = check_advisory_meetings(session_factory, store, _utc_now).summary()
    except (SQLAlchemyError, ValueError, OSError) as error:
        checks["advisory"] = {"error": f"{type(error).__name__}: the advisory check did not finish"}
    checks |= _copy_checks(session_factory, store, deadline)
    checks["safe_to_write"] = _safe_to_write(session_factory)
    return matches, checks


def _copy_checks(
    session_factory: sessionmaker[Session], store: SnapshotStore, deadline: float | None
) -> dict[str, Any]:
    """Run the checks that read stored copies, each as its own step; each stops past `deadline`."""
    found: dict[str, Any] = {}
    steps = {"company": check_company_named, "drug": check_drug_named, "type": check_type_named}
    for name, check in steps.items():
        try:
            found[name] = check(session_factory, store, _utc_now, deadline).summary()
        except (SQLAlchemyError, ValueError, OSError) as error:
            found[name] = {"error": f"{type(error).__name__}: the {name} check did not finish"}
    return found


def decide_after_checks(
    session_factory: sessionmaker[Session], store: SnapshotStore, checks: dict[str, Any]
) -> dict[str, Any]:
    """Decide every claim after every check; earlier steps stand if this fails.

    Skipped when a check step failed or ran out of time, so no decision rests on outcomes this
    run did not renew (ADR 0064).
    """
    steps = ("advisory", "company", "drug", "type", "safe_to_write")
    if "error" in checks or any("error" in checks.get(step, {}) for step in steps):
        return {"skipped": "the checks did not finish, so nothing was decided"}
    if any(checks.get(step, {}).get("stopped_for_time") for step in steps):
        return {"skipped": "a check ran out of time, so nothing was decided; the next run goes on"}
    try:
        return decide_claims(session_factory, store, _utc_now).summary()
    except (SQLAlchemyError, ValueError) as error:
        return {"error": f"{type(error).__name__}: the publish decisions did not finish"}


def _safe_to_write(session_factory: sessionmaker[Session]) -> dict[str, Any]:
    """Run the writer's own check before the decisions, so they see every claim it would hold."""
    try:
        return check_safe_to_write(session_factory, _utc_now).summary()
    except (SQLAlchemyError, ValueError) as error:
        return {"error": f"{type(error).__name__}: the safe-to-write check did not finish"}


def _publish_after(
    session_factory: sessionmaker[Session],
    store: SnapshotStore,
    decisions: dict[str, Any],
    verifier_model: str,
) -> dict[str, Any]:
    """Write the live catalysts last; every earlier summary stands if this fails (ADR 0018, part 5b).

    Skipped when the decisions did not finish, so nothing is written from decisions this run did
    not renew.
    """
    if "error" in decisions or "skipped" in decisions:
        return {"skipped": "the publish decisions did not finish, so nothing was published"}
    try:
        return publish_claims(session_factory, store, _utc_now, verifier_model=verifier_model).summary()
    except (SQLAlchemyError, ValueError, OSError) as error:
        return {"error": f"{type(error).__name__}: publishing did not finish"}


def _indications_after(
    session_factory: sessionmaker[Session],
    store: SnapshotStore,
    client: ModelClient,
    published: dict[str, Any],
    limit: int,
) -> dict[str, Any]:
    """Find newly published catalysts' indications, then write the confirmed ones (ADR 0053).

    After the writer, so new catalysts are asked about the same day; skipped when publishing did
    not finish. A blocked gate or a role at its cap is reported, not an error: the catalysts
    simply wait for a later run.
    """
    if "error" in published or "skipped" in published:
        return {"skipped": "publishing did not finish, so no indication was asked for"}
    failures = (SQLAlchemyError, SnapshotStoreError, httpx.HTTPError, SyntaxError, ValueError, OSError)
    result: dict[str, Any] = {}
    try:
        result["read"] = indicate_catalysts(
            session_factory, store, client, limit=limit, now=_utc_now
        ).summary()
    except failures as error:
        result["read"] = {"error": f"{type(error).__name__}: the indication readers did not finish"}
    try:  # its own step, so finds stored by earlier runs are written even when the readers fail
        result |= publish_indications(session_factory, store, _utc_now).summary()
    except failures as error:
        result["write"] = {"error": f"{type(error).__name__}: the indication pass did not finish"}
    if "error" in result["read"] or "write" in result:
        result["error"] = "the indication step did not finish"
    return result


def _resolve(_args: argparse.Namespace) -> int:
    try:
        database = settings.database_url()
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    engine = create_engine(database)
    try:
        report = resolve_claims(sessionmaker(engine), _utc_now)
    finally:
        engine.dispose()
    print(json.dumps(report.summary(), indent=2))
    return 0


def _load_drugs(args: argparse.Namespace) -> int:
    try:
        database = settings.database_url()
        email = settings.sec_contact_email()
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    engine = create_engine(database)
    try:
        report = load_drugs(
            sessionmaker(engine),
            build_clients(email).web,
            load_companies(args.companies),
            _utc_now,
            args.sources,
        )
    except httpx.HTTPError as error:
        print(f"Nothing stored: a request failed ({error}).", file=sys.stderr)
        return 1
    except (
        ValueError
    ) as error:  # an answer that could not be read, or more trials than a search pages through
        print(f"Nothing stored: {error}.", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(json.dumps(report.summary(), indent=2))
    return 0


def _verify_claims(args: argparse.Namespace) -> int:
    try:
        database = settings.database_url()
        store = _open_store(args, create_bucket=False)
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    engine = create_engine(database)
    try:
        report = run_checks(sessionmaker(engine), _utc_now).summary()
        try:
            report["advisory"] = check_advisory_meetings(sessionmaker(engine), store, _utc_now).summary()
        except (SQLAlchemyError, ValueError, OSError) as error:
            report["advisory"] = {"error": f"{type(error).__name__}: the advisory check did not finish"}
        report |= _copy_checks(sessionmaker(engine), store, None)
        report["safe_to_write"] = _safe_to_write(sessionmaker(engine))
    finally:
        engine.dispose()
    print(json.dumps(report, indent=2))
    return 0


def _company_names(args: argparse.Namespace) -> int:
    try:
        database = settings.database_url()
        store = _open_store(args, create_bucket=False)
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    engine = create_engine(database)
    try:
        report = _display_names(sessionmaker(engine), store, None)
    finally:
        engine.dispose()
    print(json.dumps(report, indent=2))
    return 1 if "error" in report else 0


def _display_names(
    session_factory: sessionmaker[Session], store: SnapshotStore, deadline: float | None
) -> dict[str, Any]:
    """Store each company's display name (ADR 0068); a problem here never fails the steps before it."""
    try:
        return recompute_display_names(session_factory, store, deadline).summary()
    except (SQLAlchemyError, ValueError, OSError) as error:
        return {"error": f"{type(error).__name__}: the display names did not finish"}


def _decide_claims(args: argparse.Namespace) -> int:
    try:
        database = settings.database_url()
        store = _open_store(args, create_bucket=False)
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    engine = create_engine(database)
    try:
        report = decide_claims(sessionmaker(engine), store, _utc_now)
    finally:
        engine.dispose()
    print(json.dumps(report.summary(), indent=2))
    return 0


def _publish_claims(args: argparse.Namespace) -> int:
    try:
        database = settings.database_url()
        store = _open_store(args, create_bucket=False)
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    engine = create_engine(database)
    try:
        model = load_config().roles[VERIFIER_ROLE].model
        report = publish_claims(sessionmaker(engine), store, _utc_now, verifier_model=model)
        indications = publish_indications(sessionmaker(engine), store, _utc_now)
    finally:
        engine.dispose()
    print(json.dumps(report.summary() | indications.summary(), indent=2))
    return 0


def _serve(args: argparse.Namespace) -> int:
    """Serve published data read-only: the HTTP API on localhost, or MCP over stdio (ADR 0025)."""
    try:
        database = settings.database_url()
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    engine = create_engine(database)
    try:
        if args.command == "api":
            uvicorn.run(create_app(sessionmaker(engine)), host="127.0.0.1", port=args.port, log_config=None)
        else:
            build_server(sessionmaker(engine)).run("stdio")
    finally:
        engine.dispose()
    return 0


def _export(args: argparse.Namespace) -> int:
    """Write the prototype export; print only its counts, never a claim or quote (ADR 0034)."""
    try:
        database = settings.database_url()
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    store = None
    if args.include_held:
        try:
            store = _open_store(args, create_bucket=False)
        except settings.MissingSettingError as error:
            print(error, file=sys.stderr)
            return 2
    engine = create_engine(database)
    try:
        with read_session(sessionmaker(engine)) as session:
            export = build_export(session, _utc_now(), include_held=args.include_held, store=store)
    except OperationalError:
        print("Nothing written: cannot reach the database. Is it running? See README.", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    write_export(args.out, export)
    print(json.dumps(export["counts"]))
    return 0


def _digest(args: argparse.Namespace) -> int:
    """Write the weekly digest; print only its counts (ADR 0047). Nothing is sent or posted."""
    latest = latest_issue_date(_utc_now())
    issue_date: date = args.issue_date or latest
    try:
        access = Access(args.access)
        build_digest([], issue_date, calendar_limit=args.calendar_limit)  # refuse a bad date before any read
        if issue_date > latest:
            # A later issue would list days past today's free 30-day calendar (ADR 0025, decision 6).
            raise ValueError(
                f"the latest issue is dated {latest.isoformat()}; a later one cannot be built yet"
            )
        outputs = [path.resolve() for path in (args.out, args.html, args.page) if path]
        if len(set(outputs)) < len(outputs):
            raise ValueError("--out, --html, and --page must name different files")
        details = _export_details(args.from_export, issue_date) if args.from_export else None
        database = "" if details is not None else settings.database_url()
    except ValueError as error:
        print(f"Nothing written: {error}.", file=sys.stderr)
        return 1
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    if details is None:
        engine = create_engine(database)
        try:
            with read_session(sessionmaker(engine)) as session:
                details = published(session)
        except OperationalError:
            print("Nothing written: cannot reach the database. Is it running? See README.", file=sys.stderr)
            return 1
        finally:
            engine.dispose()
    issue = build_digest(details, issue_date, access=access, calendar_limit=args.calendar_limit)
    write_export(args.out, issue.model_dump(mode="json"))
    if args.html:
        args.html.write_text(render_html(issue), encoding="utf-8")
    if args.page:
        args.page.write_text(render_page(issue), encoding="utf-8")
    counts = {"issue_date": issue_date.isoformat(), "access": issue.access, **issue.counts.model_dump()}
    print(json.dumps(counts))
    return 0


def _export_details(path: Path, issue_date: date) -> list[CatalystDetail]:
    """Read published catalysts from an export file; a ValueError says plainly what is wrong."""
    try:
        export = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {path} as an export ({type(error).__name__})") from error
    if not isinstance(export, dict):
        raise ValueError("the file is not an export: it is not a JSON object")
    return details_from_export(cast(dict[str, object], export), issue_date=issue_date)


def _seal_keygen(args: argparse.Namespace) -> int:
    """Save a new private key where only this user can read it; print its public line."""
    private, public = generate_keypair()
    try:
        descriptor = os.open(args.private_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"Nothing written: {args.private_key} already exists.", file=sys.stderr)
        return 1
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(private)
    print(public)
    return 0


def _seal(args: argparse.Namespace) -> int:
    """Print the file sealed to the public line; on any problem print only why, never the file."""
    try:
        print(seal(args.source.read_bytes(), args.public), end="")
    except SealError as error:
        print(f"Nothing sealed: {error}.", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"Nothing sealed: the file could not be read ({type(error).__name__}).", file=sys.stderr)
        return 1
    return 0


def _unseal(args: argparse.Namespace) -> int:
    """Write the opened file; refuse to write over an existing one."""
    try:
        opened = unseal(args.source.read_text(encoding="utf-8"), args.private_key.read_bytes())
        with args.out.open("xb") as stream:
            stream.write(opened)
    except SealError as error:
        print(f"Nothing written: {error}.", file=sys.stderr)
        return 1
    except FileExistsError:
        print(f"Nothing written: {args.out} already exists.", file=sys.stderr)
        return 1
    print(json.dumps({"out": str(args.out), "bytes": len(opened)}))
    return 0


def _alerts(args: argparse.Namespace) -> int:
    runs = list(args.run)
    spend = None
    if args.spend:
        try:
            spend = _spend_today()
        except (SQLAlchemyError, settings.MissingSettingError):
            runs.append(parse_run("spend-check:failure"))  # the alert says spending could not be read
        else:
            runs.append(parse_run("spend-check:success"))  # closes that alert once spending reads
    plan = build_alerts(runs, spend, load_thresholds(), args.run_url)
    args.out.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    print(plan_markdown(plan), end="")
    return 0


def _spend_today() -> dict[str, RoleSpend]:
    engine = create_engine(settings.database_url())
    try:
        return spend_today(engine, load_config(), datetime.now(NEW_YORK).date())
    finally:
        engine.dispose()


def _post_issues(args: argparse.Namespace) -> int:
    try:
        token, repo = settings.github_token(), settings.github_repository()
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    with httpx.Client(base_url=API, timeout=30) as client:
        try:
            actions = post(plan, client, repo, token)
        except httpx.HTTPError as error:
            print(f"Posting stopped: a GitHub request failed ({type(error).__name__}).", file=sys.stderr)
            return 1
    print(json.dumps({"changes": [type(action).__name__.lower() for action in actions]}))
    return 0


def _utc_now() -> datetime:
    return datetime.now(UTC)


# Each reader scored on the gold set: its role and its prompt. The verifier's scores go in
# evals/verify_catalysts/ (ADR 0018, part 2).
_READERS = {
    EXTRACTOR_PROMPT[0]: (ROLE, EXTRACTOR_PROMPT),
    VERIFIER_PROMPT[0]: (VERIFIER_ROLE, VERIFIER_PROMPT),
}


_INDICATION_READERS = {READER_PROMPT[0], CHECKER_PROMPT[0]}
INDICATION_EVALS = EVALS.parent


def _evaluate(args: argparse.Namespace) -> int:
    if args.prompt in _INDICATION_READERS:
        return _evaluate_indication(args)
    try:
        adapter = anthropic_adapter.from_settings()
        database = settings.database_url()
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    config = load_config()
    engine = create_engine(database)
    try:
        client = ModelClient(config, {adapter.name: adapter}, DatabaseLedger(engine))
        role_name, prompt_ref = _READERS[args.prompt]
        gold = load_gold(EVALS / "gold.jsonl")
        scores, _ = evaluate(client, gold, EVALS / "documents", role=role_name, prompt_ref=prompt_ref)
    finally:
        engine.dispose()
    role = config.roles[role_name]
    record = scores_record(scores, load_prompt(*prompt_ref), role, EVALS / "gold.jsonl", len(gold))
    args.out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2))
    return 0


def _evaluate_indication(args: argparse.Namespace) -> int:
    """Score a reader of indications: both readers on the gold events, or the checker on its cases."""
    try:
        adapter = anthropic_adapter.from_settings()
        database = settings.database_url()
        # Only the first reader's path reads the stored MeSH file (Opus review of PR 124).
        store = (
            None
            if args.prompt == CHECKER_PROMPT[0]
            else SupabaseSnapshotStore(settings.supabase_url(), settings.supabase_secret_key())
        )
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    config = load_config()
    engine = create_engine(database)
    try:
        client = ModelClient(config, {adapter.name: adapter}, DatabaseLedger(engine))
        if args.prompt == CHECKER_PROMPT[0]:
            cases = INDICATION_EVALS / "check_indication" / "cases.jsonl"
            checked, answers = evaluate_checker(client, cases)
            record = score_record(
                checked, [load_prompt(*CHECKER_PROMPT)], [config.roles[VERIFIER_ROLE]], [cases], answers
            )
        else:
            if store is None:  # never: the store is opened above for this path
                return 2
            with Session(engine) as session:
                index = stored_index(session, store)
            if index is None:
                print("no MeSH descriptor file is stored yet; run the mesh collector first", file=sys.stderr)
                return 2
            labels = INDICATION_EVALS / "indicate_catalysts" / "indications.jsonl"
            gold = load_gold(EVALS / "gold.jsonl")
            read, answers = evaluate_readers(client, gold, load_diseases(labels), EVALS / "documents", index)
            record = score_record(
                read,
                [load_prompt(*READER_PROMPT), load_prompt(*CHECKER_PROMPT)],
                [config.roles[ROLE], config.roles[VERIFIER_ROLE]],
                [EVALS / "gold.jsonl", labels],
                answers,
            )
    finally:
        engine.dispose()
    args.out.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0


def _positive_minutes(text: str) -> float:
    """Parse a number of minutes that must be more than zero."""
    minutes = float(text)
    if not minutes > 0:
        raise argparse.ArgumentTypeError(f"must be more than 0 minutes, not {text}")
    return minutes


def _at_least_one(text: str) -> int:
    """Parse a count that must be 1 or more, so a slip like -1 never means "all but one"."""
    number = int(text)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, not {number}")
    return number


def _indicate(args: argparse.Namespace) -> int:
    """Ask both readers about published catalysts' indications; fail if blocked, stopped, or a read failed."""
    try:
        adapter = anthropic_adapter.from_settings()
        store = _open_store(args, create_bucket=False)
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    engine = create_engine(settings.database_url())
    try:
        client = ModelClient(load_config(), {adapter.name: adapter}, DatabaseLedger(engine))
        report = indicate_catalysts(sessionmaker(engine), store, client, limit=args.limit, now=_utc_now)
    finally:
        engine.dispose()
    summary = report.summary()
    print(json.dumps(summary, indent=2))
    return 1 if summary["blocked"] or summary["stopped"] or summary["failed"] else 0


def _sample_check(args: argparse.Namespace) -> int:
    """Check part of the week's draw; fail only when a record could not be checked (ADR 0041)."""
    try:
        adapter = anthropic_adapter.from_settings()
        store = _open_store(args, create_bucket=False)
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    week = week_of(
        datetime.combine(args.week_of, datetime.min.time(), NEW_YORK) if args.week_of else _utc_now()
    )
    engine = create_engine(settings.database_url())
    try:
        client = ModelClient(load_config(), {adapter.name: adapter}, DatabaseLedger(engine))
        report = run_sample_check(
            sessionmaker(engine), store, client, week=week, limit=args.limit, now=_utc_now, size=args.size
        )
    finally:
        engine.dispose()
    summary = report.summary()
    print(json.dumps(summary, indent=2))
    return 1 if summary["documents_failed"] or summary["records_too_large"] or summary["stopped_early"] else 0


def _planted_errors(args: argparse.Namespace) -> int:
    corpus = load_corpus()
    results = run_planted(corpus)
    args.out.write_text(render_report(results, corpus), encoding="utf-8")
    counted = planted_scores(results)
    if args.scores:
        args.scores.write_text(json.dumps(counted, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(counted["errors"], indent=2))
    return 0


def _prospects_check(args: argparse.Namespace) -> int:
    try:
        rows = prospects.load_prospects(args.file)
        dropped = (
            prospects.dropped_do_not_contact(prospects.load_prospects(args.previous), rows)
            if args.previous
            else []
        )
    except prospects.ProspectsError as error:
        print("\n".join(error.problems), file=sys.stderr)
        return 1
    except FileNotFoundError as error:
        if args.previous and str(error.filename) == str(args.previous):
            print(
                f"No earlier copy at {error.filename}; check the path given with --previous.", file=sys.stderr
            )
        else:
            print(
                f"No prospect list at {error.filename}. The list is kept outside the repository "
                "(ADR 0048); save a copy there, or name one with --file.",
                file=sys.stderr,
            )
        return 2
    for row in dropped:
        print(
            f"{row.organization}, {row.name}: a do_not_contact row was removed; put it back.", file=sys.stderr
        )
    counts = {
        "rows": len(rows),
        "with_email": sum(row.email is not None for row in rows),
        "categories": dict(sorted(Counter(row.category for row in rows).items())),
        "statuses": {status: sum(row.status == status for row in rows) for status in prospects.STATUSES},
    }
    print(json.dumps(counts, indent=2))
    return 1 if dropped else 0


def _snapshots_space(args: argparse.Namespace) -> int:
    """Compress the bucket's plain copies, or report its size; both only on the Supabase bucket."""
    try:
        store = SupabaseSnapshotStore(settings.supabase_url(), settings.supabase_secret_key())
        if args.command == "snapshots-usage":
            return _usage(store, by_source=args.by_source)
        deadline = None if args.max_minutes is None else time.monotonic() + args.max_minutes * 60
        report = compress_snapshots(store, dry_run=args.dry_run, workers=args.workers, deadline=deadline)
    except settings.MissingSettingError as error:
        print(error, file=sys.stderr)
        return 2
    except (SnapshotStoreError, httpx.HTTPError) as error:
        print(f"The snapshot bucket could not be read ({error}).", file=sys.stderr)
        return 1
    print(json.dumps(report.summary(), indent=2))
    return 0 if report.ok else 1


def _usage(store: SupabaseSnapshotStore, *, by_source: bool) -> int:
    sources: dict[str, str] | None = None
    if by_source:
        engine = create_engine(settings.database_url())
        try:
            with sessionmaker(engine)() as session:
                sources = snapshot_sources(session)
        finally:
            engine.dispose()
    usage = snapshot_usage(store, sources)
    summary: dict[str, Any] = {"objects": usage.objects, "bytes": usage.bytes, "by_format": usage.by_format}
    if by_source:
        summary["by_source"] = usage.by_source
    print(json.dumps(summary, indent=2))
    return 0


def _verify(store: SnapshotStore, session_factory: sessionmaker[Session], workers: int = 1) -> int:
    report: VerifyReport = verify_snapshots(session_factory, store, workers=workers)
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
    # Without --sources, collect_all runs with its own default, every source, as before.
    chosen: dict[str, Any] = {} if args.sources is None else {"sources": args.sources}
    stats = collect_all(
        load_companies(args.companies), build_clients(email), store, session_factory, args.since, **chosen
    )
    summary = [
        {"source": s.source, "created": s.created, "existing": s.existing, "error": s.error} for s in stats
    ]
    print(json.dumps(summary, indent=2))
    return 1 if any(s.error for s in stats) else 0


def _backfill_collect(
    args: argparse.Namespace, email: str, store: SnapshotStore, session_factory: sessionmaker[Session]
) -> int:
    report = backfill_press_releases(
        load_companies(args.companies),
        EdgarClient(email),
        store,
        session_factory,
        today=datetime.now(NEW_YORK).date(),
        max_minutes=args.max_minutes,
    )
    print(json.dumps(report.summary(), indent=2))
    return 1 if report.failed else 0


def _backfill_status(args: argparse.Namespace) -> int:
    engine = create_engine(settings.database_url())
    try:
        with Session(engine) as session:
            status = backfill_status(session, datetime.now(NEW_YORK).date())
    finally:
        engine.dispose()
    print(json.dumps(status.summary(), indent=2))
    return 0


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
