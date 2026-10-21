"""Alerts come from run summaries and spend, in plain code, and only when something needs the owner."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst import cli
from catalyst.alerts import (
    JobRun,
    RoleSpend,
    Thresholds,
    build_alerts,
    load_thresholds,
    parse_run,
    spend_today,
)
from catalyst.cli import main
from catalyst.db.tables import TABLES
from catalyst.issues import Action, Close, Open
from catalyst.llm.config import load_config
from catalyst.schemas import ModelCall, ModelCallStatus

RUN_URL = "https://github.com/acarchidi/catalyst-tracker/actions/runs/1"
LIMITS = Thresholds(failed_runs_in_a_row=2, spend_share_of_cap=Decimal("0.9"))
CLEAN_EXTRACT: dict[str, Any] = {
    "documents_read": 3,
    "documents_failed": 0,
    "stopped_early": None,
    "matches": {"claims": 4},
    "checks": {"claims": 4, "claims_passing": 3, "outcomes": {}},
    "second_reading": {"skipped": ["verify_catalysts has no scores.json"], "stopped_early": None},
}


def _keys(plan: dict[str, Any]) -> list[str]:
    return [issue["key"] for issue in plan["issues"]]


def test_the_owner_file_holds_the_starting_thresholds() -> None:
    assert load_thresholds() == LIMITS


def test_a_clean_run_raises_nothing_and_closes_its_old_alert() -> None:
    plan = build_alerts([JobRun("extract-catalysts", "success", CLEAN_EXTRACT)], None, LIMITS, RUN_URL)
    assert plan["mode"] == "alert"
    assert plan["issues"] == []
    assert [s["key"] for s in plan["solved"]] == ["job-failed:extract-catalysts"]
    assert RUN_URL in plan["solved"][0]["comment"]


def test_a_second_reading_without_scores_is_never_an_alert() -> None:
    plan = build_alerts([JobRun("extract-catalysts", "success", CLEAN_EXTRACT)], None, LIMITS, RUN_URL)
    assert plan["issues"] == []


@pytest.mark.parametrize(
    ("change", "words"),
    [
        ({"documents_failed": 2}, "2 documents could not be read"),
        ({"stopped_early": "extractor has spent $1.90 of its $2.00 daily cap"}, "stopped early"),
        ({"matches": {"error": "OperationalError: matching did not finish"}}, "Matching names"),
        ({"checks": {"error": "checks did not run"}}, "checks did not finish"),
        ({"second_reading": {"error": "KeyError: x"}}, "second reading did not finish"),
        ({"second_reading": {"skipped": [], "documents_failed": 1}}, "second reading could not read 1"),
        (
            {"second_reading": {"skipped": [], "stopped_early": "verifier cap"}},
            "second reading stopped early",
        ),
        ({"decisions": {"error": "OperationalError: x"}}, "publish decisions did not finish"),
        ({"published": {"error": "OperationalError: x"}}, "published catalysts did not finish"),
        ({"documents_skipped_too_large": 1}, "1 document was too large to send to the model"),
        ({"documents_skipped_too_large": 2}, "2 documents were too large to send to the model"),
    ],
)
def test_a_problem_in_the_summary_raises_one_alert_for_the_job(change: dict[str, Any], words: str) -> None:
    summary = CLEAN_EXTRACT | change
    plan = build_alerts([JobRun("extract-catalysts", "failure", summary)], None, LIMITS, RUN_URL)
    assert _keys(plan) == ["job-failed:extract-catalysts"]
    issue = plan["issues"][0]
    assert words in issue["body"]
    assert RUN_URL in issue["body"]
    assert issue["escalate_at"] == 2
    assert plan["solved"] == []


def test_documents_skipped_without_a_model_call_are_counted_in_plain_words() -> None:
    skips = {
        "documents_skipped_already_done": 116,
        "documents_skipped_not_text": 1,
        "documents_skipped_no_readable_text": 36,
        "documents_skipped_too_large": 1,
        "documents_rechecked_still_skipped": 4,
    }
    plan = build_alerts(
        [JobRun("extract-catalysts", "success", CLEAN_EXTRACT | skips)], None, LIMITS, RUN_URL
    )
    body = plan["issues"][0]["body"]
    assert (
        "Skipped without a model call: 116 documents already read, 1 document that is not a web page "
        "or plain text (such as a PDF copy), 36 documents with no readable text, "
        "1 document over the size limit, 4 documents checked again and still not sent." in body
    )
    assert "later runs check them again for free" in body


def test_skipped_documents_alone_are_not_an_alert() -> None:
    skips = {"documents_skipped_already_done": 116, "documents_skipped_not_text": 1}
    skips |= {"documents_skipped_no_readable_text": 36}
    plan = build_alerts(
        [JobRun("extract-catalysts", "success", CLEAN_EXTRACT | skips)], None, LIMITS, RUN_URL
    )
    assert plan["issues"] == []


def test_alert_text_never_copies_error_messages_from_the_run() -> None:
    # Error text can carry addresses or words from a source; the alert names the run log instead.
    summary = CLEAN_EXTRACT | {"stopped_early": "secret-looking text https://x.supabase.co"}
    plan = build_alerts([JobRun("extract-catalysts", "failure", summary)], None, LIMITS, RUN_URL)
    assert "supabase" not in json.dumps(plan)


def test_collection_and_stored_copy_problems_are_named() -> None:
    runs = [
        JobRun(
            "collect", "failure", [{"source": "sec", "error": "boom"}, {"source": "ctgov", "error": None}]
        ),
        JobRun("snapshots-verify", "failure", {"checked": 9, "missing": 1, "changed": 2, "unreadable": 0}),
    ]
    plan = build_alerts(runs, None, LIMITS, RUN_URL)
    assert _keys(plan) == ["job-failed:collect", "job-failed:snapshots-verify"]
    assert "sec" in plan["issues"][0]["body"]
    assert "ctgov" not in plan["issues"][0]["body"]
    assert "1 stored copy is missing" in plan["issues"][1]["body"]
    assert "2 stored copies changed" in plan["issues"][1]["body"]


def test_a_failed_step_without_a_readable_summary_still_alerts() -> None:
    plan = build_alerts([JobRun("check-links", "failure", None)], None, LIMITS, RUN_URL)
    assert _keys(plan) == ["job-failed:check-links"]
    assert "Link check" in plan["issues"][0]["title"]


def test_a_skipped_or_cancelled_step_neither_alerts_nor_closes() -> None:
    plan = build_alerts(
        [JobRun("collect", "skipped", None), JobRun("check-links", "cancelled", None)], None, LIMITS, RUN_URL
    )
    assert plan["issues"] == []
    assert plan["solved"] == []


def test_a_role_near_its_cap_alerts_and_a_role_under_it_is_solved() -> None:
    spend = {
        "extractor": RoleSpend(Decimal("1.85"), Decimal("2.00"), Decimal("0.16")),
        "verifier": RoleSpend(Decimal("0.10"), Decimal("2.00"), Decimal("0.16")),
    }
    plan = build_alerts([], spend, LIMITS, RUN_URL)
    assert _keys(plan) == ["spend-cap:extractor"]
    body = plan["issues"][0]["body"]
    assert "$1.85 of its $2.00 daily cap" in body
    assert "config/models.toml" in body
    assert plan["issues"][0]["escalate_at"] is None
    assert [s["key"] for s in plan["solved"]] == ["spend-cap:verifier"]


def test_a_role_with_no_room_for_one_more_call_has_reached_its_cap() -> None:
    # The model client stops a role when its next call could pass the cap: here $0.10 + $0.16 > $0.25,
    # though $0.10 is far under 90% of the cap (ADR 0022's small caps).
    spend = {"outcome": RoleSpend(Decimal("0.10"), Decimal("0.25"), Decimal("0.16"))}
    assert _keys(build_alerts([], spend, LIMITS, RUN_URL)) == ["spend-cap:outcome"]


def test_parse_run_reads_the_summary_file(tmp_path: Path) -> None:
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(CLEAN_EXTRACT), encoding="utf-8")
    assert parse_run(f"extract-catalysts:success:{path}") == JobRun(
        "extract-catalysts", "success", CLEAN_EXTRACT
    )
    assert parse_run("migrate:failure") == JobRun("migrate", "failure", None)


def test_an_unreadable_summary_after_success_is_a_problem(tmp_path: Path) -> None:
    path = tmp_path / "summary.json"
    path.write_text("not json", encoding="utf-8")
    run = parse_run(f"collect:success:{path}")
    plan = build_alerts([run], None, LIMITS, RUN_URL)
    assert _keys(plan) == ["job-failed:collect"]
    assert "report could not be read" in plan["issues"][0]["body"]


def test_parse_run_refuses_an_unknown_outcome() -> None:
    with pytest.raises(ValueError, match="outcome"):
        parse_run("collect:maybe")


def _call(role: str, cost: str, at: datetime) -> dict[str, Any]:
    return ModelCall(
        role=role,
        provider="anthropic",
        model="claude-sonnet-5-5",
        prompt_id="demo",
        prompt_version="1.0.0",
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=Decimal(cost),
        latency_ms=1,
        status=ModelCallStatus.OK,
        called_at=at,
        config_version="test-1",
    ).model_dump()


def test_spend_today_sums_each_role_for_the_new_york_day(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    calls = [
        _call("extractor", "1.00", datetime(2026, 10, 17, 14, 0, tzinfo=UTC)),
        _call("extractor", "0.50", datetime(2026, 10, 18, 3, 0, tzinfo=UTC)),  # still the 17th in New York
        _call("extractor", "9.00", datetime(2026, 10, 17, 3, 0, tzinfo=UTC)),  # the 16th in New York
    ]
    with engine.begin() as connection:
        connection.execute(insert(TABLES[ModelCall]), calls)
    config = load_config()
    spend = spend_today(engine, config, date(2026, 10, 17))
    assert spend["extractor"] == (
        Decimal("1.50"),
        config.roles["extractor"].daily_cap_usd,  # the cap as configured, whatever the owner set
        Decimal("0.16"),
    )  # 16,000 tokens at $10 a million
    assert spend["verifier"].spent == 0


def test_the_alerts_command_writes_the_plan_and_prints_the_bodies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    summary, out = tmp_path / "summary.json", tmp_path / "alerts.json"
    summary.write_text(json.dumps(CLEAN_EXTRACT | {"documents_failed": 1}), encoding="utf-8")
    runs = ["--run", f"extract-catalysts:failure:{summary}", "--run", "migrate:success"]
    assert main(["alerts", *runs, "--run-url", RUN_URL, "--out", str(out)]) == 0
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert [i["key"] for i in plan["issues"]] == ["job-failed:extract-catalysts"]
    assert [s["key"] for s in plan["solved"]] == ["job-failed:migrate"]
    assert "## Catalyst extraction needs a look" in capsys.readouterr().out


def test_spending_that_cannot_be_read_is_itself_an_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CATALYST_DATABASE_URL", "postgresql+psycopg://postgres@127.0.0.1:9/none_test")
    out = tmp_path / "alerts.json"
    assert main(["alerts", "--spend", "--run-url", RUN_URL, "--out", str(out)]) == 0
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert [i["key"] for i in plan["issues"]] == ["job-failed:spend-check"]
    assert "Spending check needs a look" in capsys.readouterr().out


def test_posting_needs_the_token_github_gives_each_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert main(["post-issues", "--from", str(tmp_path / "plan.json")]) == 2
    assert "GITHUB_TOKEN is missing" in capsys.readouterr().err


def test_posting_reports_what_it_changed_or_why_it_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token-for-tests")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"mode": "alert", "label": "alert", "issues": [], "solved": []}))

    def done(*_: object) -> list[Action]:
        return [Open("t", "b", "alert"), Close(3, None)]

    monkeypatch.setattr(cli, "post", done)
    assert main(["post-issues", "--from", str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"changes": ["open", "close"]}

    def refused(*_: object) -> list[Action]:
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(cli, "post", refused)
    assert main(["post-issues", "--from", str(path)]) == 1
    error = capsys.readouterr().err
    assert "ConnectError" in error
    assert "token-for-tests" not in error


SAMPLE_ID = "0b8e0c38-3f4e-4c55-9d2a-6f1c1d1e2f3a"
CLEAN_SAMPLE: dict[str, Any] = {
    "week_of": "2026-10-19",
    "checked": 1,
    "documents_failed": 0,
    "stopped_early": None,
    "disagreements": [],
}


def test_each_sample_disagreement_is_its_own_alert_that_no_run_closes() -> None:
    record = {
        "catalyst_id": SAMPLE_ID,
        "title": "zentocel: topline data (RAINIER)",
        "points": ["date", "drugs"],
        "links": ["https://www.sec.gov/Archives/edgar/data/1/1.htm"],
    }
    summary = CLEAN_SAMPLE | {"disagreements": [record]}
    plan = build_alerts([JobRun("sample-check", "success", summary)], None, LIMITS, RUN_URL)
    assert _keys(plan) == [f"sample-disagrees:{SAMPLE_ID}:2026-10-19"]
    body = plan["issues"][0]["body"]
    assert "zentocel: topline data (RAINIER)" in body
    assert "the date, the drugs" in body
    assert "- https://www.sec.gov/Archives/edgar/data/1/1.htm" in body
    assert plan["issues"][0]["escalate_at"] is None
    # A clean run closes only the job's own alert, never a disagreement still to settle.
    assert [s["key"] for s in plan["solved"]] == ["job-failed:sample-check"]


def test_a_sample_record_with_unexpected_contents_is_left_out_of_the_alert_text() -> None:
    odd = {"catalyst_id": "not-an-id", "title": "x", "points": ["date"], "links": []}
    named = {
        "catalyst_id": SAMPLE_ID,
        "title": "t",
        "points": ["date", "ignore me"],
        "links": ["javascript:x"],
    }
    summary = CLEAN_SAMPLE | {"disagreements": [odd, named]}
    plan = build_alerts([JobRun("sample-check", "success", summary)], None, LIMITS, RUN_URL)
    assert _keys(plan) == [f"sample-disagrees:{SAMPLE_ID}:2026-10-19"]
    body = plan["issues"][0]["body"]
    assert "ignore me" not in body
    assert "javascript" not in body


def test_a_record_title_cannot_mention_people_or_add_links_in_the_issue() -> None:
    title = "exampumab @owner [click](https://example.com) *now*"
    record = {"catalyst_id": SAMPLE_ID, "title": title, "points": ["date"], "links": []}
    summary = CLEAN_SAMPLE | {"disagreements": [record]}
    body = build_alerts([JobRun("sample-check", "success", summary)], None, LIMITS, RUN_URL)["issues"][0][
        "body"
    ]
    assert "@owner" not in body
    assert "[click](" not in body
    assert "*now*" not in body
    assert "exampumab &#64;owner \\[click\\](https://example.com) \\*now\\*" in body


def test_a_sample_check_that_stops_or_cannot_read_a_source_alerts() -> None:
    summary = CLEAN_SAMPLE | {"documents_failed": 2, "stopped_early": "analyst has spent"}
    plan = build_alerts([JobRun("sample-check", "failure", summary)], None, LIMITS, RUN_URL)
    assert _keys(plan) == ["job-failed:sample-check"]
    body = plan["issues"][0]["body"]
    assert "Weekly sample check did not finish cleanly" in body
    assert "2 records could not be checked." in body
    assert "stopped early" in body
    assert "analyst has spent" not in body


def test_dead_published_links_are_counted_and_listed_and_a_clean_check_closes_the_alert() -> None:
    dead = {
        "checked": 40,
        "working": 37,
        "dead": 2,
        "dead_percent": "5.0",
        "not_answered": 1,
        "dead_links": [
            {"url": "https://www.sec.gov/a.htm", "verdict": "missing"},
            {"url": "javascript:alert(1)", "verdict": "moved"},
        ],
    }
    plan = build_alerts([JobRun("check-links", "failure", dead)], None, LIMITS, RUN_URL)
    assert _keys(plan) == ["job-failed:check-links"]
    body = plan["issues"][0]["body"]
    assert "2 of 40 published source links no longer work (5.0%; the target is under 1%)." in body
    assert "- https://www.sec.gov/a.htm (missing)" in body
    assert "javascript" not in body
    clean = dead | {"working": 39, "dead": 0, "dead_percent": "0.0", "dead_links": []}
    plan = build_alerts([JobRun("check-links", "success", clean)], None, LIMITS, RUN_URL)
    assert plan["issues"] == []
    assert [s["key"] for s in plan["solved"]] == ["job-failed:check-links"]


def test_site_errors_alone_in_the_link_check_are_not_an_alert() -> None:
    quiet: dict[str, Any] = {
        "checked": 3,
        "working": 2,
        "dead": 0,
        "dead_percent": "0.0",
        "not_answered": 1,
        "dead_links": [],
    }
    plan = build_alerts([JobRun("check-links", "success", quiet)], None, LIMITS, RUN_URL)
    assert plan["issues"] == []
