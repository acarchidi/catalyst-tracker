"""Command line entry point: `uv run catalyst <command>`."""

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from defusedxml.ElementTree import ParseError
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from catalyst import settings
from catalyst.alerts import RoleSpend, build_alerts, load_thresholds, parse_run, spend_today
from catalyst.api.http import create_app
from catalyst.api.mcp_server import build_server
from catalyst.collectors.coverage import build_coverage, coverage_toml, load_overrides, unconfirmed_markdown
from catalyst.collectors.report import latest_documents_report
from catalyst.collectors.run import build_clients, collect_all, load_companies
from catalyst.collectors.sec.edgar import EdgarClient
from catalyst.collectors.sec.press_release import NoPressReleaseError, collect_latest_press_release
from catalyst.extract.agent import EXTRACTOR_PROMPT, ROLE
from catalyst.extract.evaluate import EVALS, evaluate, load_gold, scores_record
from catalyst.extract.run import extract_documents
from catalyst.gate1 import companies_toml, sample_markdown, sample_records, select_companies
from catalyst.issues import API, plan_markdown, post
from catalyst.links import check_links, link_client, links_markdown
from catalyst.llm import anthropic_adapter
from catalyst.llm.client import ModelClient
from catalyst.llm.config import load_config
from catalyst.llm.ledger import DatabaseLedger
from catalyst.llm.prompts import load_prompt
from catalyst.logs import configure_logging
from catalyst.outreach import prospects
from catalyst.publish.run import decide_claims
from catalyst.resolve_claims import resolve_claims
from catalyst.snapshots import (
    LocalSnapshotStore,
    SnapshotStore,
    SnapshotStoreError,
    SupabaseSnapshotStore,
    VerifyReport,
    verify_snapshots,
)
from catalyst.verify.advisory import check_advisory_meetings
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
    verify_claims = commands.add_parser(
        "verify-claims", help="Run the rule checks on every extracted claim and store the outcomes (free)."
    )
    _store_arguments(verify_claims)
    decide = commands.add_parser(
        "decide-claims",
        help="Decide, from the stored checks and copies, which extracted claims may publish (free).",
    )
    _store_arguments(decide)
    commands.add_parser(
        "resolve-claims", help="Match the names in extracted claims to companies, drugs, and trials (free)."
    )
    api = commands.add_parser(
        "api", help="Serve the read-only data API on this computer only, at http://127.0.0.1:<port>/v1."
    )
    api.add_argument("--port", type=int, default=8000, help="Port on this computer (default 8000).")
    commands.add_parser("mcp", help="Run the read-only MCP server over standard input and output.")
    evaluate = commands.add_parser(
        "eval-extractor", help="Score the extractor on the gold set (costs money; about 12 model calls)."
    )
    evaluate.add_argument("--out", type=Path, required=True, help="Write the scores record (JSON) here.")
    evaluate.add_argument(
        "--prompt",
        choices=sorted(_READERS),
        default=EXTRACTOR_PROMPT[0],
        help="Which reader to score: the extractor (default) or the verifier's second reading.",
    )
    planted = commands.add_parser(
        "planted-errors",
        help="Plant known errors in gold-set claims and report what the checks catch (free).",
    )
    planted.add_argument("--out", type=Path, required=True, help="Write the one-page report (Markdown) here.")
    planted.add_argument("--scores", type=Path, help="Also write the counts (JSON) here.")
    _issue_arguments(commands)
    check = commands.add_parser(
        "prospects-check",
        help="Check the outreach prospect list and print its counts (ADR 0020; sends nothing).",
    )
    check.add_argument("--file", type=Path, default=prospects.DEFAULT_PATH, help="Prospect list (CSV).")
    check.add_argument(
        "--previous", type=Path, help="An earlier copy; fail if it had a do_not_contact row now missing."
    )
    return parser


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
        "verify-claims": _verify_claims,
        "alerts": _alerts,
        "post-issues": _post_issues,
        "decide-claims": _decide_claims,
        "api": _serve,
        "mcp": _serve,
        "planted-errors": _planted_errors,
        "prospects-check": _prospects_check,
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
        config = load_config()
        client = ModelClient(config, {adapter.name: adapter}, DatabaseLedger(engine))
        report = extract_documents(sessionmaker(engine), store, client, limit=args.limit, since=args.since)
        # The extraction stands if a later step fails; each re-runs next time.
        checks: dict[str, Any] = {"error": "checks did not run, because matching did not finish"}
        try:
            matches: dict[str, Any] = resolve_claims(sessionmaker(engine), _utc_now).summary()
        except SQLAlchemyError as error:
            matches = {"error": f"{type(error).__name__}: matching did not finish"}
        else:
            try:
                checks = run_checks(sessionmaker(engine), _utc_now).summary()
            except SQLAlchemyError as error:
                checks = {"error": f"{type(error).__name__}: checks did not finish"}
            else:
                # Its own step, so a problem in it keeps the rule checks' summary (Opus review of PR #49).
                try:
                    checks["advisory"] = check_advisory_meetings(
                        sessionmaker(engine), store, _utc_now
                    ).summary()
                except (SQLAlchemyError, ValueError, OSError) as error:
                    checks["advisory"] = {
                        "error": f"{type(error).__name__}: the advisory check did not finish"
                    }
        # Last, so a problem in it never holds up matching or the rule checks (review of PR #34).
        try:
            second: dict[str, Any] = run_second_reading(
                sessionmaker(engine), store, client, config, limit=args.limit, now=_utc_now
            ).summary()
        except (SQLAlchemyError, ValueError, KeyError, OSError) as error:  # a bad scores file, too
            second = {"error": f"{type(error).__name__}: the second reading did not finish"}
        decisions = _decide_after(sessionmaker(engine), store, checks)
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
    }
    print(json.dumps(summary, indent=2))
    second_failed = "error" in second or second.get("documents_failed") or second.get("stopped_early")
    failed = summary["documents_failed"] or summary["stopped_early"] or second_failed or "error" in decisions
    return 1 if failed else 0


def _decide_after(
    session_factory: sessionmaker[Session], store: SnapshotStore, checks: dict[str, Any]
) -> dict[str, Any]:
    """Decide every claim after every check; earlier steps stand if this fails.

    Skipped when a check step failed, so no decision rests on outcomes this run did not renew.
    """
    if "error" in checks or "error" in checks.get("advisory", {}):
        return {"skipped": "the checks did not finish, so nothing was decided"}
    try:
        return decide_claims(session_factory, store, _utc_now).summary()
    except (SQLAlchemyError, ValueError) as error:
        return {"error": f"{type(error).__name__}: the publish decisions did not finish"}


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
    finally:
        engine.dispose()
    print(json.dumps(report, indent=2))
    return 0


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


def _evaluate(args: argparse.Namespace) -> int:
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
