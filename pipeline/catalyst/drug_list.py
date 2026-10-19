"""Plan the drug list: brand, generic, and code names from Drugs@FDA and ClinicalTrials.gov (ADR 0035).

Plain code, no model, no network, no database: this module reads records already fetched and
decides which names become which company's drug. `catalyst.drug_list_store` writes the plan.

- **Approved products** come from Drugs@FDA: each new-drug or biologics application (NDA, BLA)
  with an approval gives its brand names and, for a product of one active ingredient, the
  ingredient's generic name. The sponsor must name exactly one covered company.
- **Investigational drugs** come from ClinicalTrials.gov: the drug, biological, and genetic
  interventions of trials whose lead sponsor is exactly one covered company's registered name.

ADR 0015 kept registry interventions out because they list comparators ("azithromycin"), placebos,
and short forms ("ETH"). Here only an intervention's main name gives a generic or brand name (its
free-text other names give code names only), and a registry name is dropped when:
- it is used only in comparator arms (placebo, active comparator, sham, no intervention);
- it names a placebo, vehicle, standard of care, or other stand-in (`_STAND_IN_WORDS`);
- it joins several drugs ("X + Y", "X/Y", "X and Y", "X (+) Y");
- it is a description or holds an ordinary word (`_DESCRIPTION_WORDS`, `_ORDINARY_WORDS`);
- it is a short form: a name without digits and with fewer than five letters;
- another intervention of the same trial lists it too;
- Drugs@FDA lists it for another sponsor, as it does for every approved comparator.

Any name that two owners' records give, after these rules, is recorded for no one and kept for
review as shared: a generic or brand name matches across companies, so it would link one
company's claim to another company's drug.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID

from catalyst.collectors.coverage import normalize_name
from catalyst.resolve import DrugRef, name_key
from catalyst.schemas import DrugNameKind, DrugNameSource

# Drugs@FDA
_APPLICATION_TYPES = ("NDA", "BLA")
_APPROVED = "AP"
# FDA's four-letter suffix for a biologic ("DOSTARLIMAB-GXLY"); the generic name is the part before it.
_BIOLOGIC_SUFFIX = re.compile(r"-[A-Za-z]{4}$")
# Salt and water words after an active moiety ("RUXOLITINIB PHOSPHATE", "OLEZARSEN SODIUM").
# Esters ("propionate", "furoate") stay: they name different products.
_SALT_WORDS = frozenset(
    [
        "sodium",
        "disodium",
        "potassium",
        "dipotassium",
        "calcium",
        "magnesium",
        "hydrochloride",
        "dihydrochloride",
        "hcl",
        "hydrobromide",
        "mesylate",
        "dimesylate",
        "besylate",
        "tosylate",
        "tartrate",
        "bitartrate",
        "maleate",
        "fumarate",
        "hemifumarate",
        "succinate",
        "citrate",
        "phosphate",
        "diphosphate",
        "sulfate",
        "bisulfate",
        "chloride",
        "bromide",
        "malate",
        "lactate",
        "gluconate",
        "meglumine",
        "tromethamine",
        "hyclate",
        "acetate",
        "monohydrate",
        "dihydrate",
        "trihydrate",
        "hemihydrate",
        "sesquihydrate",
        "anhydrous",
    ]
)
# FDA abbreviates sponsor names ("VERTEX PHARMS INC"); spelled out, they compare as the
# resolver compares company names (ADR 0015).
_SPONSOR_WORDS = {
    "pharm": "pharmaceuticals",
    "pharms": "pharmaceuticals",
    "pharmaceutical": "pharmaceuticals",
    "therap": "therapeutics",
    "theraps": "therapeutics",
    "therapeutic": "therapeutics",
    "lab": "laboratories",
    "labs": "laboratories",
    "laboratory": "laboratories",
}

# ClinicalTrials.gov
REGISTRY_TYPES = frozenset({"DRUG", "BIOLOGICAL", "GENETIC"})
_COMPARATOR_ARMS = frozenset(
    {"PLACEBO_COMPARATOR", "ACTIVE_COMPARATOR", "SHAM_COMPARATOR", "NO_INTERVENTION"}
)
_STAND_IN_WORDS = frozenset(
    [
        "placebo",
        "placebos",
        "vehicle",
        "sham",
        "saline",
        "dummy",
        "matching",
        "matched",
        "comparator",
        "control",
        "standard",
        "soc",
        "chemotherapy",
        "chemo",
        "supportive",
        "investigator",
        "investigators",
        "physician",
        "physicians",
        "choice",
        "background",
        "usual",
        "care",
        "rescue",
        "medication",
        "medications",
        "regimen",
    ]
)
_JOINS_DRUGS = re.compile(r"[+/,;&]|\b(?:and|plus|with|or|in combination)\b", re.IGNORECASE)
# Dose, route, and form words at either end of a registry name ("Brensocatib 25 mg", "Oral X").
_FORM_WORDS = frozenset(
    [
        "tablet",
        "tablets",
        "capsule",
        "capsules",
        "injection",
        "injectable",
        "infusion",
        "oral",
        "iv",
        "sc",
        "im",
        "intravenous",
        "subcutaneous",
        "intramuscular",
        "intravitreal",
        "topical",
        "solution",
        "suspension",
        "cream",
        "ointment",
        "gel",
        "inhalation",
        "inhaled",
        "powder",
        "for",
        "dose",
        "doses",
        "dosing",
        "low",
        "high",
        "mg",
        "mcg",
        "µg",
        "ug",
        "ml",
        "g",
        "kg",
        "mg/kg",
        "mg/m2",
        "%",
        "iu",
        "units",
        "kit",
        "qd",
        "bid",
        "tid",
        "qw",
        "q2w",
        "q3w",
        "q4w",
        "daily",
        "weekly",
        "once",
        "twice",
        "formerly",
        "previously",
    ]
)
_DOSE = re.compile(r"^[\d.,]+(?:mg|mcg|µg|ug|ml|g|kg|mg/kg|mg/m2|%|iu|x)?$", re.IGNORECASE)
_TRADEMARK = re.compile(r"[®™©℠]")
# Things shaped like code names that are not drugs (as in `catalyst.resolve_claims`).
_NOT_DRUG_PREFIXES = frozenset(
    {"COVID", "SARS", "IL", "CD", "FY", "CY", "Q", "H", "NCT", "ICD", "GLP", "HER", "PD"}
)
_SHORTEST_CODE = 4  # letters and digits in a code name: "AP01" and "4D-150" are drugs, "14C" is not
_SHORTEST_GENERIC = 5  # letters in a name without digits; "ETH" and "ATEV" are short forms
_SHORTEST_RUN = 4  # letters in a run of words the comparator rule looks up ("Dexamethasone Ophthalmic")
_MOST_WORDS = 6  # a longer "other name" is a description ("Active BMN 111: Subcutaneous injection ...")
# Words of a description, not a name: "CAR-T cells", "Anti C1q Antibody", "Autologous Cell Therapy",
# "Extension", "Safety observation", "Typical Antipsychotics".
# Ordinary words never in a drug's name ("if necessary", "until May", "in FOLFOX", "brand name").
_ORDINARY_WORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "in",
        "of",
        "to",
        "as",
        "at",
        "by",
        "for",
        "from",
        "if",
        "on",
        "per",
        "than",
        "then",
        "until",
        "via",
        "without",
        "into",
        "during",
        "versus",
        "vs",
        "when",
        "while",
        "brand",
        "name",
        "names",
        "commercial",
        "concentration",
        "administration",
        "administered",
        "necessary",
        "interventional",
        "non",
        "only",
        "alone",
        "monotherapy",
        "radiosensitizer",
        "approved",
        "marketed",
        "available",
    ]
)
_DESCRIPTION_WORDS = frozenset(
    [
        "cell",
        "cells",
        "antibody",
        "antibodies",
        "vaccine",
        "vaccines",
        "peptide",
        "protein",
        "therapy",
        "therapies",
        "autologous",
        "allogeneic",
        "recombinant",
        "inhibitor",
        "inhibitors",
        "agonist",
        "antagonist",
        "active",
        "drug",
        "drugs",
        "study",
        "investigational",
        "product",
        "agent",
        "agents",
        "enhancer",
        "formulation",
        "formulations",
        "biosimilar",
        "generic",
        "reference",
        "arm",
        "cohort",
        "group",
        "part",
        "period",
        "sequence",
        "intervention",
        "treatment",
        "treatments",
        "other",
        "dual",
        "conventional",
        "reduced",
        "prophylaxis",
        "manufactured",
        "lot",
        "protocol",
        "amendment",
        "before",
        "after",
        "post",
        "pre",
        "medium",
        "release",
        "extended",
        "immediate",
        "delayed",
        "single",
        "multiple",
        "concomitant",
        "blood",
        "healthy",
        "volunteer",
        "volunteers",
        "participant",
        "participants",
        "patient",
        "patients",
        "outpatient",
        "inpatient",
        "fasted",
        "fed",
        "food",
        "meal",
        "risk",
        "day",
        "days",
        "week",
        "weeks",
        "month",
        "months",
        "extension",
        "observation",
        "safety",
        "approved",
        "patch",
        "application",
        "phase",
        "challenge",
        "generation",
        "tracer",
        "test",
        "tests",
        "diagnostic",
        "mitigation",
        "strategy",
        "strategies",
        "profile",
        "explore",
        "infiltration",
        "site",
        "typical",
        "atypical",
        "local",
        "plain",
        "tradename",
        "new",
        "seed",
        "process",
        "antipsychotic",
        "antipsychotics",
        "antacid",
        "steroid",
        "steroids",
        "triptan",
        "pharmacokinetics",
        "doublet",
        "triplet",
        "label",
        "eye",
        "drops",
        "moisturizing",
        "moisturizer",
        "moisturizers",
        "stabilizer",
        "antiemetic",
        "antihyperglycemics",
        "biguanides",
        "plasma",
        "frozen",
        "adequate",
        "developmental",
        "prandial",
        "analog",
        "mixture",
        "donor",
        "tki",
        "level",
    ]
)
# A bracket set off by a space holds a second name ("Selinexor (KPT-330)"); one inside a word is
# part of the name ("AKCEA-APO(a)-LRx").
_PARENTHESES = re.compile(r"(?:^|(?<=\s))[(\[]([^()\[\]]*)[)\]]")
_EDGE_PUNCTUATION = re.compile(r"^[\s\-\u2010-\u2015.,;:]+|[\s\-\u2010-\u2015.,;:]+$")  # "CK-3773274 -"

Owner = UUID | str  # a covered company, or "other:<sponsor>" for a sponsor we do not cover


@dataclass(frozen=True)
class ListedName:
    """One name a record gives a product, and its kind."""

    name: str
    kind: DrugNameKind
    main: bool = True  # False for a registry intervention's other names, never the name shown


@dataclass(frozen=True)
class SourceRecord:
    """One record's names for one product, with the sponsor as the record writes it.

    `ingredient` is the generic-name key of a Drugs@FDA product of one active ingredient; two
    such keys in one drug mean a registry record joined two products. `other_names` are names the
    record gives only for the comparator rule (the ingredients of a combination product).
    """

    source: DrugNameSource
    ref: str
    sponsor: str
    names: tuple[ListedName, ...]
    ingredient: str | None = None
    other_names: tuple[str, ...] = ()
    labelers: tuple[str, ...] = ()  # Drugs@FDA: the companies its labels name ("Kura Oncology, Inc.")


def _is_ester(words: list[str]) -> bool:
    """Say whether the words name a fumarate ester ("DIMETHYL FUMARATE", "DIROXIMEL FUMARATE").

    Such a name is the drug's whole generic name; the alcohol part alone names nothing.
    """
    alcohol = words[0].casefold()
    return len(words) == 2 and words[1].casefold() == "fumarate" and alcohol.endswith(("yl", "el"))


def generic_name(ingredient: str) -> str:
    """Return an FDA active ingredient without its biologic suffix and its salt words."""
    words = _BIOLOGIC_SUFFIX.sub("", ingredient.strip()).split()
    while len(words) > 1 and words[-1].casefold() in _SALT_WORDS and not _is_ester(words):
        words.pop()
    return " ".join(words)


def _approved(application: Mapping[str, Any]) -> bool:
    submissions = cast(list[dict[str, Any]], application.get("submissions") or [])
    return any(s.get("submission_status") == _APPROVED for s in submissions)


def fda_records(application: Mapping[str, Any]) -> list[SourceRecord]:
    """Read one Drugs@FDA application's products: brand names and single-ingredient generic names.

    Only approved new-drug and biologics applications count; a generic copy (ANDA) or a tentative
    approval names no drug of its sponsor. A combination product's ingredient names are kept only
    for the comparator rule, never as names of the combination.
    """
    number = str(application.get("application_number") or "")
    if not number.startswith(_APPLICATION_TYPES) or not _approved(application):
        return []
    sponsor = str(application.get("sponsor_name") or "")
    labels = cast(dict[str, Any], application.get("openfda") or {})
    labelers = tuple(str(n) for n in cast(list[str], labels.get("manufacturer_name") or []))
    records: list[SourceRecord] = []
    for product in cast(list[dict[str, Any]], application.get("products") or []):
        ingredients = [
            generic_name(str(i.get("name") or ""))
            for i in cast(list[dict[str, Any]], product.get("active_ingredients") or [])
        ]
        ingredients = [i for i in ingredients if name_key(i)]
        written = [
            str(i.get("name") or "")
            for i in cast(list[dict[str, Any]], product.get("active_ingredients") or [])
        ]
        # "TRYNGOLZA (AUTOINJECTOR)" is the brand TRYNGOLZA in one device.
        brand = _trimmed(_PARENTHESES.sub(" ", str(product.get("brand_name") or "")))  # "ARIKAYCE KIT"
        names: list[ListedName] = []
        single = ingredients[0] if len(ingredients) == 1 else None
        if single:
            names.append(ListedName(single, DrugNameKind.INN))
        if name_key(brand) and name_key(brand) not in {name_key(i) for i in ingredients}:
            names.append(ListedName(brand, DrugNameKind.BRAND))
        if names:
            records.append(
                SourceRecord(
                    DrugNameSource.OPENFDA,
                    number,
                    sponsor,
                    tuple(names),
                    ingredient=name_key(single) if single else None,
                    # Every ingredient as written too ("MAGNESIUM CHLORIDE"), for the comparator rule.
                    other_names=tuple(dict.fromkeys([*([] if single else ingredients), *written])),
                    labelers=labelers,
                )
            )
    return records


def fda_sponsor_key(name: str) -> str:
    """Return a company or FDA sponsor name as the resolver compares it, abbreviations spelled out."""
    return " ".join(_SPONSOR_WORDS.get(word, word) for word in normalize_name(name).split())


def _trimmed(name: str) -> str:
    words = unicodedata.normalize("NFKC", _TRADEMARK.sub(" ", name)).split()
    while words and (words[0].casefold() in _FORM_WORDS or _DOSE.match(words[0])):
        words.pop(0)
    while words and (words[-1].casefold() in _FORM_WORDS or _DOSE.match(words[-1])):
        words.pop()
    return " ".join(words)


def _registry_part(text: str, trademark: bool, *, main: bool = True) -> ListedName | None:
    name = _EDGE_PUNCTUATION.sub("", _trimmed(text))
    words = [w.casefold() for w in re.findall(r"[^\W_]+", name)]
    if not words or len(words) > _MOST_WORDS or ":" in name or _JOINS_DRUGS.search(name):
        return None
    # Ordinary words count as whole words between spaces, so "AKCEA-APO(a)-LRx" keeps its "a".
    if set(words) & _DESCRIPTION_WORDS or {w.casefold() for w in name.split()} & _ORDINARY_WORDS:
        return None
    letters = sum(ch.isalpha() for ch in name)
    if any(ch.isdigit() for ch in name):
        prefix = re.match(r"[^\W\d_]+", name)
        short = sum(ch.isalnum() for ch in name) < _SHORTEST_CODE  # "14C" labels a radioactive tracer
        if letters == 0 or short or (prefix and prefix.group(0).upper() in _NOT_DRUG_PREFIXES):
            return None
        return ListedName(name, DrugNameKind.CODE, main)
    if not main or letters < _SHORTEST_GENERIC:
        return None  # free text beside the main name is no name (Opus review of PR #73, B2)
    # Generic names are written in small letters in the registry; a name in capitals is a brand ("ELEVIDYS").
    brand = trademark or name.isupper()
    return ListedName(name, DrugNameKind.BRAND if brand else DrugNameKind.INN)


def _joins_drugs(text: str) -> bool:
    words = {w.casefold() for w in re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text))}
    return bool(words & _STAND_IN_WORDS) or bool(_JOINS_DRUGS.search(text))


def registry_names(text: str) -> list[ListedName] | None:
    """Return the drug names in a registry intervention's name, or None for a stand-in or a combination.

    Dose and form words at either end are dropped. A name with a digit is a code name, which
    matches only its own company's claims. A name without one must have five letters or more,
    and is a brand when the registry marks it ® or ™. A bracket adds a code name only
    ("Selinexor (KPT-330)"); free text in it ("(Commercial tablet)") names nothing. A join
    anywhere, brackets included ("simvastatin (+) ezetimibe"), makes a combination. Descriptions,
    ordinary words, names of more than six words, and short forms give none.
    """
    if _joins_drugs(text):
        return None
    outside = _PARENTHESES.sub(" ", text)
    found = [_registry_part(outside, bool(_TRADEMARK.search(outside)))]
    found += [_registry_part(part, False, main=False) for part in _PARENTHESES.findall(text)]
    return list(dict.fromkeys(filter(None, found)))


def registry_other_names(text: str) -> list[ListedName]:
    """Return the code names in one of an intervention's listed other names.

    Other names are free text ("Brilinta", "Commercial tablet", a misspelled competitor), so
    only code names are kept from them; brands and generic names come from Drugs@FDA or from a
    main intervention name (Opus review of PR #73, B2).
    """
    if _joins_drugs(text):
        return []
    parts = [_PARENTHESES.sub(" ", text), *_PARENTHESES.findall(text)]
    return list(dict.fromkeys(filter(None, (_registry_part(p, False, main=False) for p in parts))))


def _in_trial_arms(intervention: Mapping[str, Any], arm_types: Mapping[str, str | None]) -> bool:
    """Say whether an intervention is given in an arm other than a comparator (or arms are unknown)."""
    labels = cast(list[str], intervention.get("armGroupLabels") or [])
    if not labels:
        return True
    return any(arm_types.get(label) not in _COMPARATOR_ARMS for label in labels)


def registry_records(study: Mapping[str, Any]) -> list[SourceRecord]:
    """Read one ClinicalTrials.gov record's drug interventions, each with its other names."""
    protocol = cast(dict[str, Any], study.get("protocolSection") or {})
    nct_id = str(protocol.get("identificationModule", {}).get("nctId") or "")
    sponsor = str(protocol.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name") or "")
    module = cast(dict[str, Any], protocol.get("armsInterventionsModule") or {})
    arm_types = {
        str(arm.get("label")): arm.get("type")
        for arm in cast(list[dict[str, Any]], module.get("armGroups") or [])
    }
    kept: list[tuple[list[ListedName], list[ListedName]]] = []
    for intervention in cast(list[dict[str, Any]], module.get("interventions") or []):
        if intervention.get("type") not in REGISTRY_TYPES or not _in_trial_arms(intervention, arm_types):
            continue
        main = registry_names(str(intervention.get("name") or ""))
        if not main or not main[0].main:
            continue  # a stand-in, a combination, or no drug name: its other names name nothing of it
        others = cast(list[str], intervention.get("otherNames") or [])
        kept.append((main, [name for other in others for name in registry_other_names(str(other))]))
    return [
        SourceRecord(DrugNameSource.CTGOV, nct_id, sponsor, tuple(dict.fromkeys([*main, *others])))
        for main, others in _unshared(kept)
    ]


def _unshared(
    interventions: list[tuple[list[ListedName], list[ListedName]]],
) -> list[tuple[list[ListedName], list[ListedName]]]:
    """Drop a listed name that another intervention of the same trial also gives.

    NCT07683000 lists teropavimab's code GS-5423 under "Lenacapavir Tablet" too; such a name
    cannot tell the two drugs apart, so it joins neither (Opus review of PR #73, B1).
    """
    keys = [{name_key(n.name) for n in [*main, *others]} for main, others in interventions]
    mains = [name_key(main[0].name) for main, _ in interventions]
    kept: list[tuple[list[ListedName], list[ListedName]]] = []
    for i, (main, others) in enumerate(interventions):
        apart = [keys[j] for j in range(len(interventions)) if mains[j] != mains[i]]
        elsewhere = set[str]().union(*apart)
        named = [n for n in main if n is main[0] or name_key(n.name) not in elsewhere]
        kept.append((named, [n for n in others if name_key(n.name) not in elsewhere]))
    return kept


@dataclass
class PlannedName:
    """A name to record for a drug, with where it was first found and every record that gives it."""

    name: str
    kind: DrugNameKind
    source: DrugNameSource
    refs: list[str]
    main: bool = True  # some record gives it as a main name: any FDA name, a registry intervention's


@dataclass
class PlannedDrug:
    """One company's drug: every name its records give it, keyed as the resolver compares names."""

    company_id: UUID
    names: dict[str, PlannedName]

    @property
    def preferred_name(self) -> str:
        """The name shown: Drugs@FDA's first, its generic name before its brand; else the registry's.

        A registry intervention's other name is never shown (Opus review of PR #73, B1).
        """
        best = min(self.names.values(), key=_shown_rank)
        fda_generic = best.kind is DrugNameKind.INN and best.source is DrugNameSource.OPENFDA
        return best.name.lower() if fda_generic and best.name.isupper() else best.name  # FDA writes capitals


_SOURCE_RANK = {DrugNameSource.OPENFDA: 0, DrugNameSource.CTGOV: 1, DrugNameSource.CLAIM: 2}
_KIND_RANK = {DrugNameKind.INN: 0, DrugNameKind.BRAND: 1, DrugNameKind.CODE: 2}


def _shown_rank(name: PlannedName) -> tuple[bool, int, int, str]:
    return (not name.main, _SOURCE_RANK[name.source], _KIND_RANK[name.kind], name.name)


@dataclass
class Withheld:
    """A name kept for review: several owners' records give it."""

    name: str
    reason: str
    company_ids: set[UUID]
    refs: set[str]


@dataclass
class DrugPlan:
    """What the records give: each company's drugs, and the names kept for review."""

    drugs: list[PlannedDrug] = field(default_factory=list[PlannedDrug])
    withheld: dict[str, Withheld] = field(default_factory=dict[str, Withheld])
    records: int = 0
    unlinked_records: int = 0  # the sponsor is no covered company, or more than one
    comparators: int = 0  # registry names Drugs@FDA lists for another sponsor
    split_drugs: int = 0  # registry records that joined two approved products, left out


class _Groups:
    """Union-find over name keys."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, key: str) -> str:
        self.parent.setdefault(key, key)
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def join(self, keys: Iterable[str]) -> None:
        first = None
        for key in keys:
            root = self.find(key)
            if first is None:
                first = root
            elif root != first:
                self.parent[root] = first


def _labeled_by(record: SourceRecord, sponsor_of: Mapping[str, UUID]) -> UUID | None:
    """Return the company an FDA sponsor name cut short stands for, when the product's labels agree.

    FDA cuts many sponsor names to their first words ("KURA", "MIRUM"). Such a name stands for a
    covered company only when every label of the application names that one company in full
    ("Kura Oncology, Inc.") and the sponsor's words begin that full name.
    """
    sponsor = fda_sponsor_key(record.sponsor).split()
    labeled = {sponsor_of.get(fda_sponsor_key(name)) for name in record.labelers}
    if not sponsor or len(labeled) != 1 or None in labeled:
        return None
    full = [fda_sponsor_key(name).split() for name in record.labelers]
    return labeled.pop() if all(words[: len(sponsor)] == sponsor for words in full) else None


def _owner(record: SourceRecord, sponsor_of: Mapping[DrugNameSource, Mapping[str, UUID]]) -> Owner | None:
    """Return the record's covered company; another FDA sponsor is an owner too, by its spelled-out name."""
    if record.source is DrugNameSource.OPENFDA:
        fda = sponsor_of[record.source]
        company = fda.get(fda_sponsor_key(record.sponsor)) or _labeled_by(record, fda)
        return company or f"other:{fda_sponsor_key(record.sponsor)}"
    return sponsor_of[record.source].get(record.sponsor)


def _approved_for_another(name: str, owner: Owner, fda_owners: Mapping[str, set[Owner]]) -> bool:
    """Say whether Drugs@FDA lists the name, or a run of its words, for a sponsor other than `owner`.

    So "Pembrolizumab Post-Resection" and "Dexamethasone Ophthalmic" are comparators or partners.
    """
    words = re.findall(r"[^\W_]+", name)
    runs = {name_key(" ".join(words[i:j])) for i in range(len(words)) for j in range(i + 1, len(words) + 1)}
    runs = {key for key in runs if sum(ch.isalpha() for ch in key) >= _SHORTEST_RUN} | {name_key(name)}
    return any(fda_owners.get(key, {owner}) - {owner} for key in runs)


def _fda_owners(kept: Iterable[tuple[SourceRecord, Owner]]) -> dict[str, set[Owner]]:
    """Index every name Drugs@FDA gives, with each owner that holds it."""
    found: defaultdict[str, set[Owner]] = defaultdict(set)
    for record, owner in kept:
        if record.source is DrugNameSource.OPENFDA:
            for name in [*(n.name for n in record.names), *record.other_names]:
                found[name_key(name)].add(owner)
    return found


def _withhold(plan: DrugPlan, key: str, name: str, companies: Iterable[UUID], refs: Iterable[str]) -> None:
    entry = plan.withheld.setdefault(key, Withheld(name, "shared", set(), set()))
    entry.company_ids |= set(companies)
    entry.refs |= set(refs)


def _owned_names(
    plan: DrugPlan, kept: list[tuple[SourceRecord, Owner]]
) -> tuple[dict[str, set[Owner]], list[tuple[SourceRecord, UUID, list[ListedName]]]]:
    """Drop registry comparators; return every owner of each name, and the covered companies' names."""
    fda_owners = _fda_owners(kept)
    owners: defaultdict[str, set[Owner]] = defaultdict(set)
    listed: list[tuple[SourceRecord, UUID, list[ListedName]]] = []
    for record, owner in kept:
        names = list(record.names)
        if record.source is DrugNameSource.CTGOV:
            names = [n for n in names if not _approved_for_another(n.name, owner, fda_owners)]
            plan.comparators += len(record.names) - len(names)
        for name in names:
            owners[name_key(name.name)].add(owner)
        if isinstance(owner, UUID):
            listed.append((record, owner, names))
    return owners, listed


def plan_drugs(
    records: Iterable[SourceRecord], sponsor_of: Mapping[DrugNameSource, Mapping[str, UUID]]
) -> DrugPlan:
    """Decide each covered company's drugs from the records, and which names are shared.

    `sponsor_of` maps, per source, a sponsor (Drugs@FDA: by `fda_sponsor_key`; the registry: as
    registered) to the one covered company it names; a sponsor naming several is left out of it.
    """
    plan = DrugPlan()
    kept: list[tuple[SourceRecord, Owner]] = []
    for record in records:
        plan.records += 1
        if (owner := _owner(record, sponsor_of)) is None:
            plan.unlinked_records += 1
        else:
            kept.append((record, owner))
    owners, listed = _owned_names(plan, kept)
    shared = {key for key, held in owners.items() if len(held) > 1}
    for record, _, names in listed:
        for name in (n for n in names if name_key(n.name) in shared):
            key = name_key(name.name)
            _withhold(plan, key, name.name, (o for o in owners[key] if isinstance(o, UUID)), [record.ref])
    by_company: defaultdict[UUID, list[tuple[SourceRecord, list[ListedName]]]] = defaultdict(list)
    for record, owner, names in listed:
        if names := [n for n in names if name_key(n.name) not in shared]:
            by_company[owner].append((record, names))
    for company, entries in by_company.items():
        plan.drugs += _company_drugs(plan, company, entries)
    return plan


def _joined(entries: Iterable[tuple[SourceRecord, list[ListedName]]]) -> _Groups:
    groups = _Groups()
    for _, names in entries:
        groups.join(name_key(n.name) for n in names)
    return groups


def _product(record: SourceRecord) -> str:
    """Name an FDA product: its one ingredient, or, for a combination, its brand (VYTORIN is not ZETIA)."""
    return record.ingredient or "+" + name_key(record.names[0].name)


def _company_drugs(
    plan: DrugPlan, company: UUID, entries: list[tuple[SourceRecord, list[ListedName]]]
) -> list[PlannedDrug]:
    """Join one company's names into drugs; a registry record that joins two approved products is left out."""
    groups = _joined(entries)
    products: defaultdict[str, set[str]] = defaultdict(set)
    for record, names in entries:
        if record.source is DrugNameSource.OPENFDA:
            products[groups.find(name_key(names[0].name))].add(_product(record))
    joined = {root for root, found in products.items() if len(found) > 1}
    if joined:
        kept = [
            (r, ns)
            for r, ns in entries
            if r.source is DrugNameSource.OPENFDA or groups.find(name_key(ns[0].name)) not in joined
        ]
        plan.split_drugs += len(entries) - len(kept)
        entries, groups = kept, _joined(kept)
    drugs: dict[str, PlannedDrug] = {}
    order = {DrugNameSource.OPENFDA: 0, DrugNameSource.CTGOV: 1, DrugNameSource.CLAIM: 2}
    for record, names in sorted(entries, key=lambda e: (order[e[0].source], e[0].ref)):
        drug = drugs.setdefault(groups.find(name_key(names[0].name)), PlannedDrug(company, {}))
        for name in names:
            planned = drug.names.setdefault(
                name_key(name.name), PlannedName(name.name, name.kind, record.source, [], name.main)
            )
            planned.main = planned.main or name.main
            if record.ref not in planned.refs:
                planned.refs.append(record.ref)
    return list(drugs.values())


@dataclass
class DrugWrite:
    """What to store for one planned drug: names for a drug on file (`drug_id`), or a new drug."""

    company_id: UUID
    drug_id: UUID | None
    preferred_name: str
    names: list[PlannedName]
    known_refs: dict[str, list[str]] = field(default_factory=dict[str, list[str]])  # name on file: refs
    rename: bool = False  # a drug on file is shown by FDA's generic name from now on


@dataclass
class Merged:
    """The plan set against the drugs on file."""

    writes: list[DrugWrite] = field(default_factory=list[DrugWrite])
    withheld: dict[str, Withheld] = field(default_factory=dict[str, Withheld])
    conflicts: int = 0  # planned drugs whose names belong to several of the company's drugs on file


def merge_with_existing(plan: DrugPlan, existing: Iterable[DrugRef]) -> Merged:
    """Place each planned drug: names join the company's one drug on file that shares a name, else a new drug.

    A generic or brand name already given to another company's drug, or to a drug with no
    company, is withheld as shared, as in `plan_drugs`. A planned drug whose names belong to two
    or more of the company's drugs on file is left out: joining them is for a person.
    """
    merged = Merged(withheld=dict(plan.withheld))
    on_file: defaultdict[str, list[DrugRef]] = defaultdict(list)
    for drug in existing:
        for name, _ in drug.names:
            on_file[name_key(name)].append(drug)
    for planned in plan.drugs:
        names = dict(planned.names)
        for key, name in list(names.items()):
            others = [d for d in on_file.get(key, []) if d.company_id != planned.company_id]
            if name.kind is not DrugNameKind.CODE and others:
                companies = {planned.company_id} | {d.company_id for d in others if d.company_id}
                entry = merged.withheld.setdefault(key, Withheld(name.name, "shared", set(), set()))
                entry.company_ids |= companies
                entry.refs |= set(name.refs)
                del names[key]
        if not names:
            continue
        own = {d.id: d for key in names for d in on_file.get(key, []) if d.company_id == planned.company_id}
        if len(own) > 1:
            merged.conflicts += 1
            continue
        target = next(iter(own.values()), None)
        known = {name_key(n): n for n, _ in target.names} if target else {}
        best = min(names.values(), key=_shown_rank)
        fda_generic = (best.source, best.kind) == (DrugNameSource.OPENFDA, DrugNameKind.INN)
        merged.writes.append(
            DrugWrite(
                planned.company_id,
                target.id if target else None,
                PlannedDrug(planned.company_id, names).preferred_name,
                [n for k, n in names.items() if k not in known],
                {known[k]: n.refs for k, n in names.items() if k in known},
                # The spec shows a drug by its generic name; FDA's replaces a code name on file.
                rename=target is not None and fda_generic,
            )
        )
    return merged
