"""A published catalyst's title, composed in code from checked fields (CP4 review, B1; ADR 0018, 5d).

The extractor proposes a title, but a model's words are not checked against the quote, so a
hostile or invented title ("FDA approves pegozafermin; shares set to double") must never be
published. The title is composed instead from:
- the type's fixed label (`catalyst.verify.event_type.LABELS`), a type the quote must state
  (`type_named_near_quote`);
- the preferred names, from the reference tables, of the drugs the claim was matched to, each
  named in or just before the quote (`drug_named_near_quote`);
- the study names of the trials the claim was matched to, or their registry numbers when they
  have none, each named in the quote (`trial_named_in_quote`).

"Topline data: pegozafermin, RAINIER trial". The indication is left out until indications are
matched, since nothing checks the extractor's indication against the quote. The date words are
left out too: the title is written once, while the window and its date words move with each
date change and are shown beside it. The model's title stays in the claim, which is not published.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from catalyst.db.tables import TABLES
from catalyst.schemas import CatalystType, Drug, Trial
from catalyst.verify.event_type import LABELS


def _listed(names: Iterable[str]) -> str:
    """Join names as a sentence does: "A", "A and B", "A, B and C"."""
    ordered = sorted(set(names), key=lambda name: (name.casefold(), name))
    return ordered[0] if len(ordered) == 1 else f"{', '.join(ordered[:-1])} and {ordered[-1]}"


def compose_title(catalyst_type: CatalystType, drugs: Iterable[str], trials: Iterable[str]) -> str:
    """Return the title: the type's label, then the drugs, then the trials, each from checked fields."""
    drugs, trials = list(drugs), list(trials)
    parts = [_listed(drugs)] if drugs else []
    if trials:
        parts.append(f"{_listed(trials)} {'trial' if len(set(trials)) == 1 else 'trials'}")
    label = LABELS[catalyst_type]
    return f"{label}: {', '.join(parts)}" if parts else label


@dataclass(frozen=True)
class TitleNames:
    """The reference tables' names a title may use: each drug's preferred name, each trial's name."""

    drugs: Mapping[UUID, str]
    trials: Mapping[UUID, str]

    def title(self, catalyst_type: str, drugs: Iterable[UUID], trials: Iterable[UUID]) -> str:
        """Compose the title for a claim's type and its matched drugs and trials."""
        return compose_title(
            CatalystType(catalyst_type), [self.drugs[d] for d in drugs], [self.trials[t] for t in trials]
        )


def load_title_names(session: Session) -> TitleNames:
    """Read every drug's preferred name, and every trial's study name or, without one, its number."""
    drug, trial = TABLES[Drug], TABLES[Trial]
    drugs = {row.id: row.preferred_name for row in session.execute(select(drug.c.id, drug.c.preferred_name))}
    trials = {
        row.id: row.study_name or row.nct_id
        for row in session.execute(select(trial.c.id, trial.c.study_name, trial.c.nct_id))
    }
    return TitleNames(drugs, trials)
