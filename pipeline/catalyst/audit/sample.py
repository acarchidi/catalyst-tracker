"""The weekly sample checker (spec: Weekly random sample; ADR 0040).

Each week plain code draws 30 published catalysts at random, from those published before the
week began, with the week's Monday as the seed, so anyone can repeat the draw. A stronger model
than the readers (the Opus `analyst` role) reads each source with the record's points beside it
and answers, per point, supported, contradicted, or not stated. It has no tools and writes
nothing. Plain code then decides: a point agrees only when the answer is "supported" and its
quote is really in the source. A record agrees only when every point agrees; anything else is a
disagreement for the owner to settle.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.api.read import published, read_session
from catalyst.api.views import CatalystDetail, SourceView
from catalyst.collectors.documents import new_york_midnight
from catalyst.db.tables import TABLES
from catalyst.extract.run import NEW_YORK
from catalyst.extract.text import find_quote, visible_text
from catalyst.llm.client import ModelClient, ModelError, SpendCapReachedError
from catalyst.llm.prompts import load_prompt
from catalyst.logs import log_event
from catalyst.schemas import Claim, SampleCheck, SampleVerdict, SourceDocument
from catalyst.snapshots import SnapshotStore, SnapshotStoreError

ROLE = "analyst"  # the Opus role, stronger than the Sonnet readers it checks
PROMPT = ("check_sample", "1.0.0")
SAMPLE_SIZE = 30
_log = logging.getLogger(__name__)

# Which point each published field belongs to. The title is composed in code from checked fields,
# and the date label and status follow from the type and the date, so none is a point of its own.
_POINT_OF_FIELD = {
    "catalyst_type": "type",
    "company_id": "company",
    "drugs": "drugs",
    "indication_id": "indication",
    "window_start": "date",
    "window_end": "date",
    "window_precision": "date",
    "window_raw_text": "date",
    "trials": "trials",
}
_REASONS = {
    "contradicted": "the checker says the source states something else",
    "not_stated": "the checker says the source does not state it",
}


def week_of(moment: datetime) -> date:
    """Return the Monday, New York time, of the week `moment` falls in."""
    day = moment.astimezone(NEW_YORK).date()
    return day - timedelta(days=day.weekday())


def draw(details: Sequence[CatalystDetail], week: date, size: int = SAMPLE_SIZE) -> list[CatalystDetail]:
    """Return the week's repeatable random draw from the records published before the week began."""
    start = new_york_midnight(week)
    population = sorted(
        (d for d in details if d.history and min(c.learned_at for c in d.history) < start),
        key=lambda d: str(d.id),
    )
    chooser = random.Random(f"sample:{week.isoformat()}")  # noqa: S311 - a repeatable draw, not security
    return chooser.sample(population, min(size, len(population)))


def points_for(detail: CatalystDetail, source: SourceView) -> dict[str, str]:
    """Return the points this source states for the record, each as the record states it."""
    window = detail.window
    stated = {
        "type": detail.catalyst_type.value.replace("_", " "),
        "company": detail.company.name,
        "drugs": "; ".join(f"{d.name} ({d.role.value})" for d in detail.drugs) or "none",
        "indication": detail.indication.name if detail.indication else "none",
        "date": (
            f"{window.start.isoformat()} to {window.end.isoformat()} ({window.precision.value}), "
            f"written in the source as '{window.raw_text}'"
        ),
        "trials": "; ".join(f"{t.study_name or t.nct_id} ({t.nct_id})" for t in detail.trials) or "none",
    }
    points = dict.fromkeys(_POINT_OF_FIELD[f] for f in source.fields if f in _POINT_OF_FIELD)
    return {point: stated[point] for point in points}


def checker_input(points: Mapping[str, str], published_on: date, text: str) -> str:
    """Return what the checker reads: the date, the numbered points, then the document."""
    numbered = "\n".join(f"{n}. {point}: {value}" for n, (point, value) in enumerate(points.items(), 1))
    return (
        f"Publication date: {published_on.isoformat()}\n\nRecord to check:\n{numbered}\n\nDocument:\n\n{text}"
    )


def _point_verdict(
    point: str, stated: str, answers: list[Mapping[str, Any]], text: str
) -> dict[str, JsonValue]:
    verdict: dict[str, JsonValue] = {"point": point, "stated": stated, "agrees": False}
    if not answers:
        return verdict | {"answer": None, "reason": "the checker gave no answer for this point"}
    worst = next((a for a in answers if a["answer"] != "supported"), answers[0])
    verdict |= {
        "answer": str(worst["answer"]),
        "checker_quote": str(worst["quote"]),
        "checker_reason": str(worst["reason"]),
    }
    if worst["answer"] != "supported":
        return verdict | {"reason": _REASONS[str(worst["answer"])]}
    if any(not str(a["quote"]).strip() or find_quote(text, str(a["quote"])) is None for a in answers):
        return verdict | {"reason": "the checker's quote is not in the source"}
    return verdict | {"agrees": True, "reason": None}


def judge(
    points: Mapping[str, str], answers: Sequence[Mapping[str, Any]], text: str
) -> list[dict[str, JsonValue]]:
    """Decide each asked point from the checker's answers, in the order asked; other answers are ignored."""
    return [
        _point_verdict(point, stated, [a for a in answers if a["point"] == point], text)
        for point, stated in points.items()
    ]


@dataclass
class SampleReport:
    """What one run did; `disagreements` holds only names and links chosen in code."""

    week: date
    drawn: int = 0
    already_checked: int = 0
    checked: int = 0
    agrees: int = 0
    documents_failed: int = 0
    stopped: str | None = None
    disagreements: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def summary(self) -> dict[str, Any]:
        """Return the counts as plain JSON."""
        return {
            "week_of": self.week.isoformat(),
            "drawn": self.drawn,
            "already_checked": self.already_checked,
            "checked": self.checked,
            "agrees": self.agrees,
            "disagrees": len(self.disagreements),
            "documents_failed": self.documents_failed,
            "stopped_early": self.stopped,
            "left_this_week": self.drawn - self.already_checked - self.checked,
            "disagreements": self.disagreements,
        }


class _Failed(Exception):
    """A source could not be read or checked; the record waits for the next run."""


def _done(session: Session, week: date, model: str) -> set[UUID]:
    rows = TABLES[SampleCheck]
    query = select(rows.c.catalyst_id).where(
        rows.c.week_of == week,
        rows.c.prompt_id == PROMPT[0],
        rows.c.prompt_version == PROMPT[1],
        rows.c.model == model,
    )
    return set(session.execute(query).scalars())


def _documents(session: Session, claims: list[UUID]) -> dict[UUID, Any]:
    claim, document = TABLES[Claim], TABLES[SourceDocument]
    query = (
        select(claim.c.id, document.c.snapshot_key, document.c.content_type, document.c.published_at)
        .join(document, document.c.id == claim.c.source_document_id)
        .where(claim.c.id.in_(claims))
    )
    return {row.id: row for row in session.execute(query)}


def _check_record(
    detail: CatalystDetail, documents: Mapping[UUID, Any], store: SnapshotStore, client: ModelClient
) -> list[dict[str, JsonValue]]:
    prompt = load_prompt(*PROMPT)
    verdicts: list[dict[str, JsonValue]] = []
    for source in detail.sources:
        points = points_for(detail, source)
        if not points:
            continue
        row = documents[source.claim_id]
        try:
            text = visible_text(store.get(row.snapshot_key), row.content_type)
            day = row.published_at.astimezone(NEW_YORK).date()
            answer = client.run(ROLE, prompt, checker_input(points, day, text))
        except (ModelError, SnapshotStoreError, httpx.HTTPError) as error:
            log_event(_log, "sample_check_failed", catalyst_id=str(detail.id), error=type(error).__name__)
            raise _Failed from error
        found = judge(points, answer.data["points"], text)
        verdicts += [v | {"claim_id": str(source.claim_id), "url": source.url} for v in found]
    return verdicts


def _store(
    session: Session, detail: CatalystDetail, week: date, model: str, points: list[Any], at: datetime
) -> None:
    verdict = SampleVerdict.AGREES if all(p["agrees"] for p in points) else SampleVerdict.DISAGREES
    row = SampleCheck(
        catalyst_id=detail.id,
        week_of=week,
        verdict=verdict,
        points=points,
        prompt_id=PROMPT[0],
        prompt_version=PROMPT[1],
        model=model,
        checked_at=at,
    )
    session.execute(insert(TABLES[SampleCheck]).values(**row.model_dump()).on_conflict_do_nothing())


def run_sample_check(
    session_factory: sessionmaker[Session],
    store: SnapshotStore,
    client: ModelClient,
    *,
    week: date,
    limit: int,
    now: Callable[[], datetime],
    size: int = SAMPLE_SIZE,
) -> SampleReport:
    """Check up to `limit` records of the week's draw that this prompt and model have not checked yet."""
    model = client.model_for(ROLE)
    with read_session(session_factory) as session:
        sample = draw(published(session), week, size)
        done = _done(session, week, model)
        documents = _documents(session, [s.claim_id for d in sample for s in d.sources])
    report = SampleReport(week, drawn=len(sample), already_checked=sum(d.id in done for d in sample))
    for detail in [d for d in sample if d.id not in done][:limit]:
        try:
            points = _check_record(detail, documents, store, client)
        except SpendCapReachedError as error:
            report.stopped = str(error)
            break
        except _Failed:
            report.documents_failed += 1
            continue
        with session_factory.begin() as session:
            _store(session, detail, week, model, points, now())
        report.checked += 1
        if all(p["agrees"] for p in points):
            report.agrees += 1
        else:
            report.disagreements.append(
                {
                    "catalyst_id": str(detail.id),
                    "title": detail.title,
                    "points": sorted({str(p["point"]) for p in points if not p["agrees"]}),
                    "links": sorted({str(p["url"]) for p in points if not p["agrees"]}),
                }
            )
    log_event(_log, "sample_check_done", **report.summary())
    return report
