"""The weekly summary for the owner: the week's numbers in short plain sentences (ADR 0023).

Plain code over stored rows covers the 7 days before the run; no model writes it. It also lists
the open alerts and anything low-urgency that is not worth an alert, such as a second reading
that is not switched on yet.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.extract.agent import ROLE
from catalyst.llm.config import ModelConfig
from catalyst.llm.ledger import NEW_YORK
from catalyst.schemas import Claim, ClaimCheck, ModelCall, SourceDocument

CHECK_WORDS = {
    "extractor_checks": "The extractor's own checks",
    "date_words_in_quote": "The date words are in the quote",
    "window_matches_words": "The date window matches the date words",
    "has_window": "The claim has a date window",
    "window_not_past": "The date has not passed",
    "names_matched": "Every name matched one record",
    "phase_matches_registry": "The trial phase matches the registry",
    "pdufa_date_plausible": "The FDA decision date is possible",
    "second_reading_agrees": "The second reading agrees",
}
_CLAIM, _CHECK, _CALL = TABLES[Claim], TABLES[ClaimCheck], TABLES[ModelCall]


def _plural(count: int, word: str) -> str:
    return f"{count} {word if count == 1 else word + 's'}"


def _work(session: Session, start: datetime, end: datetime) -> list[str]:
    document = TABLES[SourceDocument]
    documents = session.execute(
        select(func.count()).where(document.c.retrieved_at >= start, document.c.retrieved_at < end)
    ).scalar_one()
    week = select(_CLAIM.c.id).where(
        _CLAIM.c.agent == ROLE, _CLAIM.c.extracted_at >= start, _CLAIM.c.extracted_at < end
    )
    claims = session.execute(select(func.count()).select_from(week.subquery())).scalar_one()
    failing = func.count().filter(_CHECK.c.outcome == "fail")
    rows = session.execute(
        select(_CHECK.c.claim_id, failing).where(_CHECK.c.claim_id.in_(week)).group_by(_CHECK.c.claim_id)
    ).all()
    passing = sum(fails == 0 for _, fails in rows)
    unchecked = claims - len(rows)
    lines = [
        f"- Documents collected: {documents}.",
        f"- Claims extracted: {claims}.",
        f"- Claims passing every check so far: {passing} of {claims}."
        + (f" {unchecked} {'has' if unchecked == 1 else 'have'} not been checked yet." if unchecked else ""),
    ]
    failures = session.execute(
        select(_CHECK.c.check_name, func.count())
        .where(_CHECK.c.claim_id.in_(week), _CHECK.c.outcome == "fail")
        .group_by(_CHECK.c.check_name)
        .order_by(func.count().desc(), _CHECK.c.check_name)
    ).all()
    checks = [f"- {CHECK_WORDS.get(name, name)}: failed on {_plural(n, 'claim')}." for name, n in failures]
    return [*lines, "", "## Checks that failed", "", *(checks or ["No check failed this week."])]


def _spend(session: Session, config: ModelConfig, start: datetime, end: datetime) -> list[str]:
    day = func.date(func.timezone("America/New_York", _CALL.c.called_at))
    rows = session.execute(
        select(_CALL.c.role, day, func.sum(_CALL.c.cost_usd))
        .where(_CALL.c.called_at >= start, _CALL.c.called_at < end)
        .group_by(_CALL.c.role, day)
    ).all()
    days: defaultdict[str, list[Decimal]] = defaultdict(list)
    for role, _, total in rows:
        days[role].append(Decimal(total))
    lines: list[str] = []
    for name, role in config.roles.items():
        spent = sum(days[name], Decimal(0))
        line = f"- {name.replace('_', ' ').capitalize()}: ${spent:.2f} this week."
        if days[name]:
            line += f" Most in one day: ${max(days[name]):.2f} of its ${role.daily_cap_usd:.2f} daily cap."
        lines.append(line)
    return lines


def build_weekly(
    session_factory: sessionmaker[Session],
    config: ModelConfig,
    now: datetime,
    open_alerts: list[dict[str, Any]],
    second_reading: list[str],
) -> dict[str, Any]:
    """Return the plan for this week's summary issue, for the 7 days before `now`.

    `open_alerts` lists the open alert issues (number, title, url); `second_reading` lists why the
    second reading may not run yet, empty once it may.
    """
    start, end = now - timedelta(days=7), now
    day = now.astimezone(NEW_YORK).date()
    with session_factory() as session:
        work, spend = _work(session, start, end), _spend(session, config, start, end)
    alerts = [f"- [{alert['title']}]({alert['url']})" for alert in open_alerts]
    body = [
        f"Numbers for the 7 days to {day:%A} {day.day} {day:%B %Y} (New York time).",
        "",
        "## Work done",
        "",
        *work,
        "",
        "## Spending by role",
        "",
        "Each role has a daily cap in config/models.toml; it stops at the cap.",
        "",
        *spend,
        "",
        "## Open alerts",
        "",
        f"Open alerts: {len(alerts)}." if alerts else "No open alerts.",
        *alerts,
        "",
        "## Error rate",
        "",
        "No sample yet: the weekly sample checker is not built yet, so there is no measured error rate.",
    ]
    if second_reading:
        body += [
            "",
            "## Worth knowing (no action needed now)",
            "",
            "The second reading is not switched on yet, so no claim can pass that check. Why:",
            *(f"- {problem}" for problem in second_reading),
        ]
    title = f"Weekly summary: the week to {day.day} {day:%B %Y}"
    issue = {"key": f"weekly-summary:{day.isoformat()}", "title": title, "body": "\n".join(body)}
    return {"mode": "weekly", "label": "weekly-summary", "issues": [issue], "solved": []}
