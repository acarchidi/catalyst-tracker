"""Plan the drug list: brand, generic, and code names from Drugs@FDA and ClinicalTrials.gov (ADR 0035).

Plain code, no model, no network, no database: this module reads records already fetched and
decides which names become which company's drug. `catalyst.drug_list_store` writes the plan.

- **Approved products** come from Drugs@FDA: each new-drug or biologics application (NDA, BLA)
  with an approval gives its brand names and, for a product of one active ingredient, the
  ingredient's generic name. The sponsor must name exactly one covered company.
- **Investigational drugs** come from ClinicalTrials.gov: the drug, biological, and genetic
  interventions of trials whose lead sponsor is exactly one covered company's registered name.

ADR 0015 kept registry interventions out because they list comparators ("azithromycin"), placebos,
and short forms ("ETH"). Here an intervention needs a main name that names a drug; its other names
count too (owner decision 2026-10-19), held to more rules, and never shown. A registry name is
dropped when:
- it is used only in comparator arms (placebo, active comparator, sham, no intervention);
- it names a placebo, vehicle, standard of care, or other stand-in (`STAND_IN_WORDS`);
- it joins several drugs ("X + Y", "X/Y", "X and Y", "X (+) Y");
- it is a description, a trial, or holds an ordinary word (`drug_list_words`);
- it is a short form: a name without digits and with fewer than five letters;
- it is a target, antibody class, isotope, or the like written as a code ("CTLA-4", "IgG1", "64Cu"),
  or, as an other name, it is a code not shaped like a drug code;
- another intervention of the same trial lists it too, or records with different main names do;
- it is another drug's main name and the two are not joined (`_join_aliases`);
- as a main name without a digit, it does not look like a drug's name (`looks_like_a_drug`);
- it is another company's licensed vaccine, cell therapy, or plasma product (`drug_list_cber`);
- as an other name or in brackets, it has no digit, no ® or ™, and no record gives it as a main name;
- Drugs@FDA lists it for another sponsor, as it does for every approved comparator. When that is
  the intervention's main name, or one of its names is another sponsor's brand, or one of its
  codes is another large sponsor's (`PARTNER_PREFIXES`), the whole intervention is that sponsor's
  drug, and none of its names are recorded.

Any name that two owners' records give, after these rules, is recorded for no one and kept for
review as shared: a generic or brand name matches across companies, so it would link one
company's claim to another company's drug.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, cast
from uuid import UUID

from catalyst.collectors.coverage import normalize_name
from catalyst.drug_list_cber import CBER_PRODUCTS
from catalyst.drug_list_words import (
    DESCRIPTION_WORDS,
    FORM_WORDS,
    INN_ENDINGS,
    METALS,
    NON_DRUG_WORDS,
    NOT_INN_WORDS,
    ORDINARY_WORDS,
    PARTNER_PREFIXES,
    SALT_WORDS,
    STAND_IN_WORDS,
    TRIAL_WORDS,
)
from catalyst.resolve import DrugRef, name_key
from catalyst.schemas import DrugNameKind, DrugNameSource

# Drugs@FDA
_APPLICATION_TYPES = ("NDA", "BLA")
_APPROVED = "AP"
# FDA's four-letter suffix for a biologic ("DOSTARLIMAB-GXLY"); the generic name is the part before it.
_BIOLOGIC_SUFFIX = re.compile(r"-[A-Za-z]{4}$")
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
    # FDA's own misspelling, checked by hand on 2026-10-19: BLA761400 (LYNOZYFIC) is filed under
    # "RENGENERON PHARMACEUTICALS, INC." and labelled by Regeneron (second review of PR #73).
    "rengeneron": "regeneron",
}

# ClinicalTrials.gov
REGISTRY_TYPES = frozenset({"DRUG", "BIOLOGICAL", "GENETIC"})
_COMPARATOR_ARMS = frozenset(
    {"PLACEBO_COMPARATOR", "ACTIVE_COMPARATOR", "SHAM_COMPARATOR", "NO_INTERVENTION"}
)
_JOINS_DRUGS = re.compile(r"[+/,;&]|\b(?:and|plus|with|or|in combination)\b", re.IGNORECASE)
_DOSE = re.compile(r"^[\d.,]+(?:mg|mcg|µg|ug|ml|g|kg|mg/kg|mg/m2|%|iu|x)?$", re.IGNORECASE)
_TRADEMARK = re.compile(r"[®™©℠]")
# Things shaped like code names that are not drugs (as in `catalyst.resolve_claims`).
_NOT_DRUG_PREFIXES = frozenset(
    {"COVID", "SARS", "IL", "CD", "FY", "CY", "Q", "H", "NCT", "ICD", "GLP", "HER", "PD"}
)
_SHORTEST_CODE = 4  # letters and digits in a code name: "AP01" and "4D-150" are drugs, "14C" is not
_SHORTEST_GENERIC = 5  # letters in a name without digits; "ETH" and "ATEV" are short forms
_SHORTEST_RUN = 4  # letters in a run of words the comparator rule looks up ("Dexamethasone Ophthalmic")
_MOST_WORDS = 6  # a longer main name is a description ("Active BMN 111: Subcutaneous injection ...")
_MOST_OTHER_WORDS = 4  # the same for an other name, which is free text
# A drug code, as an other name must be shaped (second review of PR #73, B2'): a letter prefix
# (one dash-joined part allowed: "Cu-AKY", "GR-MD"), then two to seven digits, then up to two short
# parts ("ENTR-601-45", "MK-3475-D79"). "REGN5458", "GS-5423", "SCH 58235", "X4P-001".
_DASH = "\\-\u2010-\u2013"
_CODE_SHAPE = re.compile(
    rf"^(?P<prefix>[A-Za-z][A-Za-z0-9]{{0,5}}?(?:[{_DASH}][A-Za-z]{{1,5}})?)[\s{_DASH}]?(?P<number>\d{{2,7}})"
    rf"(?:[{_DASH}]?[A-Za-z0-9]{{1,4}}){{0,2}}$"
)
# Targets, receptors, antibody classes, and viruses written like codes: "CTLA-4", "PI3K", "IgG1",
# "B7-H4", "LPA1", "ILT4", "5-HT3 RA", "CD19", "IL-17A", "HER2", "HPV16", "BCMAxCD3".
_TARGETS = (
    "ctla|pi3k|kras|nras|braf|egfr|fgfr|tigit|lag|tim|trop|b7|cldn|dll|tl1a|stat|btk|jak|mek|erk|akt|"
    "mtor|parp|cdk|vegfr?|ret|alk|ros|axl|flt|pdgfr|ig[gmae]|lpa|ilt|apoc|pcsk|angptl|tnf|tcr|aav|"
    "gprc|5-?ht|gaba|nmda|ampa|mglur|hla|fxia?|hbv|hiv|hcv|rsv|cmv|ebv|hpv|h[1-9]n|bcma|gd|sstr|fap|"
    "mage|wt|glp|gip|gcgr|tlr|ido|cxcr|ccr|cd|il|her|pd|nk|car|fgf|muc|ox|gpr|hsd17b"
)
_TARGET = re.compile(
    rf"^(?:anti[\s{_DASH}]?|a)?(?:{_TARGETS})[\s{_DASH}./]?(?:\d{{0,2}}[A-Za-z]{{0,2}}|[A-Za-z]\d{{1,2}}[A-Za-z]?)"
    rf"(?:x[A-Za-z]{{2,4}}\d{{0,2}})?(?:\s?[A-Za-z]{{1,2}})?$",
    re.IGNORECASE,
)
# Isotopes ("131I", "64Cu", "Lu-177", "Tc-99m"), insulin strengths ("U100"), variants ("B.1.1.7"),
# excipients and adjuvants ("PS80", "QS-21", "AS01B", "MF59", "PEG 400").
_ELEMENTS = "Lu|Ga|Cu|Zr|In|Y|I|F|Tc|Ac|Pb|Ra|Th|Sm|Sr|C|N|O|Bi|At|Re|Ho|Tb|Er"
_NOT_A_DRUG_CODE = re.compile(
    rf"^\d{{1,3}}m?(?:{_ELEMENTS})$|^(?:{_ELEMENTS})[{_DASH}]?\d{{1,3}}m?$|^U[{_DASH}]?\d{{2,3}}$|\d\.\d|"
    rf"[A-Za-z]\.\d|^(?:PS|Tween)[\s{_DASH}]?(?:20|80)$|^QS[\s{_DASH}]?21$|^AS0\d[A-Z]?$|^MF[\s{_DASH}]?59$|"
    rf"^PEG[\s{_DASH}]?\d+$|^[A-Z]\d{{2,4}}[A-Z]$|"  # a mutation, "G12C", "V600E", "T790M"
    # Third review of PR #73: "AC-225" (actinium), "non-PS80", and protocol numbers ("MK-3475-001").
    rf"^AC[{_DASH}]?\d{{1,3}}$|^(?i:non)[\s{_DASH}]|^[A-Z]{{2,5}}[{_DASH}]?\d{{3,7}}[{_DASH}]\d{{3}}$"
)
# A large sponsor's code prefix, as `PARTNER_PREFIXES` lists them: "RO7187797", "MK-8962".
_PARTNER_CODE = re.compile(rf"^(?P<prefix>[A-Z]{{2,4}})[\s{_DASH}]?(?P<number>\d{{3,9}})")
_CODE_PREFIX = re.compile(r"^[A-Z]{2,6}$")  # the letters of a code written apart from its number
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
    marked: bool = False  # the registry marks it ® or ™


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
    while len(words) > 1 and words[-1].casefold() in SALT_WORDS and not _is_ester(words):
        if len(words) == 2 and words[0].casefold() in METALS:
            break  # "FERRIC CITRATE" and "POTASSIUM CHLORIDE" stay whole: the metal alone names nothing
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
    while words and (words[0].casefold() in FORM_WORDS or _DOSE.match(words[0])):
        words.pop(0)
    while words and (words[-1].casefold() in FORM_WORDS or _DOSE.match(words[-1])):
        if len(words) > 1 and words[-1].isdigit() and _CODE_PREFIX.match(words[-2]):
            break  # "SCH 58235" and "BMN 333" are codes, not a name and a dose
        words.pop()
    return " ".join(words)


def _code_name(name: str, *, main: bool) -> bool:
    """Say whether a name with a digit is a drug code.

    Every code must not be a target, receptor, antibody class, virus, isotope, insulin strength,
    variant, excipient, or adjuvant ("CTLA-4", "IgG1", "64Cu", "U100", "B.1.1.7", "PS80"), nor a
    disease, congress, or fiscal-year prefix (as in `catalyst.resolve_claims`). An other name, which
    is free text, must also be shaped like a drug code (`_CODE_SHAPE`); a main name, which names
    the intervention, may be shaped as its sponsor writes it ("Descartes-08", "OPGx-BEST1").
    """
    if sum(ch.isalnum() for ch in name) < _SHORTEST_CODE or not any(ch.isalpha() for ch in name):
        return False  # "14C" labels a radioactive tracer
    prefix = re.match(r"[^\W\d_]+", name)
    if prefix and prefix.group(0).upper() in _NOT_DRUG_PREFIXES:
        return False
    if _TARGET.match(name) or _NOT_A_DRUG_CODE.search(name):
        return False
    if main:
        return True
    shape = _CODE_SHAPE.match(name)
    if shape is None or not any(ch.isupper() for ch in shape.group("prefix")):
        return False
    one_letter = sum(ch.isalpha() for ch in shape.group("prefix")) == 1
    return not one_letter or len(shape.group("number")) >= 4  # "R115777", not "U100" or "C225"


def _registry_part(text: str, trademark: bool, *, main: bool = True) -> ListedName | None:
    name = _EDGE_PUNCTUATION.sub("", _trimmed(text))
    words = [w.casefold() for w in re.findall(r"[^\W_]+", name)]
    most = _MOST_WORDS if main else _MOST_OTHER_WORDS
    if not words or len(words) > most or ":" in name or _JOINS_DRUGS.search(name):
        return None
    # Ordinary words count as whole words between spaces, so "AKCEA-APO(a)-LRx" keeps its "a".
    if (
        set(words) & (DESCRIPTION_WORDS | TRIAL_WORDS | NON_DRUG_WORDS)
        or {w.casefold() for w in name.split()} & ORDINARY_WORDS
    ):
        return None
    if set(words) <= SALT_WORDS | METALS:
        return None  # "fumarate" or "sodium" alone names no drug
    if any(ch.isdigit() for ch in name):
        return ListedName(name, DrugNameKind.CODE, main, trademark) if _code_name(name, main=main) else None
    if sum(ch.isalpha() for ch in name) < _SHORTEST_GENERIC:
        return None
    # Generic names are written in small letters in the registry; a name in capitals is a brand ("ELEVIDYS").
    brand = trademark or name.isupper()
    return ListedName(name, DrugNameKind.BRAND if brand else DrugNameKind.INN, main, trademark)


def _joins_drugs(text: str) -> bool:
    words = {w.casefold() for w in re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text))}
    return bool(words & STAND_IN_WORDS) or bool(_JOINS_DRUGS.search(text))


def registry_names(text: str) -> list[ListedName] | None:
    """Return the drug names in a registry intervention's name, or None for a stand-in or a combination.

    The first name returned is the main name, unless the text names no drug. Dose and form words
    at either end are dropped. A name with a digit is a code name, which matches only its own
    company's claims. A name without one must have five letters or more, and is a brand when the
    registry marks it ® or ™ or writes it in capitals. Text in a bracket is an other name
    ("Selinexor (KPT-330)"). A join anywhere, brackets included ("simvastatin (+) ezetimibe"),
    makes a combination. Descriptions, ordinary words, trial names, targets, isotopes, names of more
    than six words, and short forms give none.
    """
    if _joins_drugs(text):
        return None
    outside = _PARENTHESES.sub(" ", text)
    found = [_registry_part(outside, bool(_TRADEMARK.search(outside)))]
    found += [
        _registry_part(part, bool(_TRADEMARK.search(part)), main=False) for part in _PARENTHESES.findall(text)
    ]
    return list(dict.fromkeys(filter(None, found)))


def registry_other_names(text: str) -> list[ListedName]:
    """Return the names in one of an intervention's listed other names.

    Other names are free text ("Brilinta", "Commercial tablet", "Recombinant humanized
    immunoglobulin G1 (IgG1) Fab"), so they are held to more rules than a main name: at most four
    words, and a code must be shaped like a drug code. They are never the name shown (owner
    decision 2026-10-19: other names stay, filtered).
    """
    if _joins_drugs(text):
        return []
    parts = [_PARENTHESES.sub(" ", text), *_PARENTHESES.findall(text)]
    found = (_registry_part(part, bool(_TRADEMARK.search(part)), main=False) for part in parts)
    return list(dict.fromkeys(filter(None, found)))


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
    mains: int = 0  # how many records give it as a main name


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

    @property
    def unconfirmed(self) -> bool:
        """Say whether only registry words name the drug: no code, no Drugs@FDA name (fifth review)."""
        return all(
            n.source is DrugNameSource.CTGOV and n.kind is not DrugNameKind.CODE for n in self.names.values()
        )


_SOURCE_RANK = {DrugNameSource.OPENFDA: 0, DrugNameSource.CTGOV: 1, DrugNameSource.CLAIM: 2}
_KIND_RANK = {DrugNameKind.INN: 0, DrugNameKind.BRAND: 1, DrugNameKind.CODE: 2}


def _shown_rank(name: PlannedName) -> tuple[bool, int, int, int, int, str]:
    """Rank names to show: FDA's first, generic before brand before code.

    Then, of FDA's, the shortest ("trametinib", not "trametinib dimethyl sulfoxide"); of the
    registry's, the main name most records give.
    """
    common = -name.mains if name.source is DrugNameSource.CTGOV else 0
    fda_short = len(name.name) if name.source is DrugNameSource.OPENFDA else 0
    return (not name.main, _SOURCE_RANK[name.source], _KIND_RANK[name.kind], common, fda_short, name.name)


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
    foreign_records: int = 0  # registry interventions that are another sponsor's approved drug
    plain_words: int = 0  # registry interventions whose main name has no digit and no drug-name shape
    bridges: int = 0  # other names that would join two records with different main names, dropped


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


def _another_s_drug(
    record: SourceRecord,
    owner: Owner,
    fda_owners: Mapping[str, set[Owner]],
    brand_owners: Mapping[str, set[Owner]],
) -> bool:
    """Say whether a registry intervention is another sponsor's approved drug, so none of its names are ours.

    That is when Drugs@FDA lists its main name, or a run of its words, for another sponsor
    ("Nivolumab" with ONO-4538 and BMS-936658), or lists any of its names exactly as another
    sponsor's brand ("Gazyvaro" with "Gazyva", Genentech's) (second review of PR #73, B3). A
    molecule another sponsor sells, listed beside the sponsor's own code ("AP01" with
    "Pirfenidone"), takes only itself out: the code may be a new form of it.
    """
    main = record.names[0]
    if main.main and _approved_for_another(main.name, owner, fda_owners):
        return True
    return any(brand_owners.get(name_key(n.name), {owner}) - {owner} for n in record.names)


def _fda_owners(kept: Iterable[tuple[SourceRecord, Owner]], *, brands: bool = False) -> dict[str, set[Owner]]:
    """Index every name Drugs@FDA gives (only its brands, with `brands`), with each owner that holds it."""
    found: defaultdict[str, set[Owner]] = defaultdict(set)
    for record, owner in kept:
        if record.source is not DrugNameSource.OPENFDA:
            continue
        names = [n.name for n in record.names if n.kind is DrugNameKind.BRAND or not brands]
        if not brands:
            names += record.other_names
        for name in names:
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
    fda_owners, brand_owners = _fda_owners(kept), _fda_owners(kept, brands=True)
    partners = _partners_drugs(kept)
    owners: defaultdict[str, set[Owner]] = defaultdict(set)
    listed: list[tuple[SourceRecord, UUID, list[ListedName]]] = []
    for record, owner in kept:
        names = list(record.names)
        if record.source is DrugNameSource.CTGOV and (
            _another_s_drug(record, owner, fda_owners, brand_owners) or id(record) in partners
        ):
            plan.comparators += len(names)
            plan.foreign_records += 1
            continue
        if record.source is DrugNameSource.CTGOV:
            names = [n for n in names if not _approved_for_another(n.name, owner, fda_owners)]
            plan.comparators += len(record.names) - len(names)
        for name in names:
            owners[name_key(name.name)].add(owner)
        if isinstance(owner, UUID):
            listed.append((record, owner, names))
    return owners, listed


def _vouched(records: list[SourceRecord]) -> list[SourceRecord]:
    """Drop a registry other name without a digit that nothing vouches for (third review of PR #73).

    "cemiplimab (maintenance)", "LX9211 (blinded)", "Anti-LAG 3": such a name counts only when
    the registry marks it ® or ™, or a record (Drugs@FDA's, or an intervention's) gives it as a
    main name. Codes have their own rules (`_code_name`).
    """
    mains = {name_key(n.name) for record in records for n in record.names if n.main}

    def vouched(name: ListedName) -> bool:
        return (
            name.main or name.marked or any(ch.isdigit() for ch in name.name) or name_key(name.name) in mains
        )

    return [
        replace(record, names=tuple(filter(vouched, record.names)))
        if record.source is DrugNameSource.CTGOV
        else record
        for record in records
    ]


def _partner_s_code(record: SourceRecord) -> bool:
    """Say whether a registry intervention gives a code of a large sponsor other than the trial's.

    Regeneron's NCT05137054 gives Roche's cevostamab as "RO7187797": a partner's drug, unapproved,
    so Drugs@FDA cannot show it is another's (third review of PR #73). A two-letter prefix needs
    four digits or more ("GS-5423", not GenSight's "GS010").
    """
    sponsor = record.sponsor.casefold()
    for name in record.names:
        shape = _PARTNER_CODE.match(name.name) if name.kind is DrugNameKind.CODE else None
        if shape is None or (len(shape.group("prefix")) == 2 and len(shape.group("number")) < 4):
            continue
        words = PARTNER_PREFIXES.get(shape.group("prefix"))
        if words and not any(word in sponsor for word in words):
            return True
    return False


def _partners_drugs(kept: list[tuple[SourceRecord, Owner]]) -> set[int]:
    """Return the registry interventions (by `id`) that are a partner's drug, by a partner's code.

    Unless the main name is the owner's own approved drug in Drugs@FDA: Insmed's brensocatib,
    licensed from AstraZeneca, keeps its code AZD7986. A trial of the sponsor that names a
    partner's unapproved drug without the code does not count (fourth review of PR #73).
    """
    flagged = {id(r) for r, _ in kept if r.source is DrugNameSource.CTGOV and _partner_s_code(r)}
    own = {
        (owner, name_key(n.name)) for r, owner in kept if r.source is DrugNameSource.OPENFDA for n in r.names
    }
    return {id(r) for r, owner in kept if id(r) in flagged and (owner, name_key(r.names[0].name)) not in own}


_SHORTEST_STEM_WORD = 6  # letters in a word whose ending counts as an INN stem ("skin" is not "-kin")
_LETTERS_BEFORE_STEM = 3  # "lasofoxifene", not a stem with one letter before it


def _inn_shaped(name: str) -> bool:
    """Say whether a word of the name ends like a WHO generic name ("navitoclax", "tanruprubart").

    The word needs three letters or more before the stem, and must not be a class or ordinary word
    that ends like one ("interleukin", "nucleotide": `NOT_INN_WORDS`) (sixth review of PR #73).
    """
    words = [w.casefold() for w in re.findall(r"[^\W\d_]+", name)]
    return any(
        len(w) >= _SHORTEST_STEM_WORD
        and w not in NOT_INN_WORDS
        and any(w.endswith(stem) and len(w) - len(stem) >= _LETTERS_BEFORE_STEM for stem in INN_ENDINGS)
        for w in words
    )


def looks_like_a_drug(name: str) -> bool:
    """Say whether a registry main name without a digit looks like a drug's name (fifth review of PR #73).

    That is a generic name by its INN stem, or a brand the registry writes in capitals ("ELEVIDYS").
    A class, target, or disease word ("Taxane", "FGF Receptor", "Clostridium difficile") does not,
    nor does a coined name in mixed case ("Shingrix"): without a dictionary, plain code cannot tell
    a brand from an ordinary or medical word. A name the registry marks ® or ™, or that Drugs@FDA
    or the CBER list gives, counts too (`_drug_shaped`).
    """
    return _inn_shaped(name) or (name.isupper() and sum(ch.isalpha() for ch in name) >= _SHORTEST_GENERIC)


def cber_records() -> list[SourceRecord]:
    """Return the CBER list's products as Drugs@FDA-like records with no names of their own.

    They feed only the comparator rule, so a rival's vaccine, cell therapy, or plasma product in a
    covered company's trial is known to be another's. A brand's first word counts too ("FLUZONE"
    for "Fluzone High-Dose", "PREVNAR" for "PREVNAR 20").
    """
    records: list[SourceRecord] = []
    for licence, owner, brands in CBER_PRODUCTS:
        first = [
            b.split()[0] for b in brands if len(b.split()) > 1 and len(b.split()[0]) >= _SHORTEST_GENERIC
        ]
        records.append(
            SourceRecord(
                DrugNameSource.OPENFDA,
                licence,
                owner,
                (),
                other_names=tuple(dict.fromkeys([*brands, *first])),
            )
        )
    return records


def _drug_shaped(
    plan: DrugPlan,
    given: list[SourceRecord],
    cber: list[SourceRecord],
    sponsor_of: Mapping[DrugNameSource, Mapping[str, UUID]],
) -> list[SourceRecord]:
    """Set aside a registry main name that has no digit and does not look like a drug's name.

    Unless the registry marks it ® or ™ or a Drugs@FDA or CBER record gives it: the comparator
    rule then decides whose it is. So "Oral Taxane", "Clostridium difficile", and an unmarked
    "Shingrix" name no drug. When the intervention gives a code, the code alone stands as its main
    name ("FGF Receptor" with LY2874455 is LY2874455; "treprostinil palmitil" with INS1009 is
    INS1009) (fifth review of PR #73). Records of no covered sponsor are left for `plan_drugs`.
    """
    fda = {
        name_key(name)
        for r in [*given, *cber]
        if r.source is DrugNameSource.OPENFDA
        for name in (*(n.name for n in r.names), *r.other_names)
    }
    kept: list[SourceRecord] = []
    for record in given:
        main = record.names[0] if record.names else None
        if main is None or record.source is not DrugNameSource.CTGOV or _owner(record, sponsor_of) is None:
            kept.append(record)
            continue
        plain = main.main and not main.marked and not any(c.isdigit() for c in main.name)
        if not plain or name_key(main.name) in fda or looks_like_a_drug(main.name):
            kept.append(record)
            continue
        plan.plain_words += 1
        if code := next((n for n in record.names if n.kind is DrugNameKind.CODE), None):
            kept.append(replace(record, names=(replace(code, main=True),)))
    return kept


def plan_drugs(
    records: Iterable[SourceRecord], sponsor_of: Mapping[DrugNameSource, Mapping[str, UUID]]
) -> DrugPlan:
    """Decide each covered company's drugs from the records, and which names are shared.

    `sponsor_of` maps, per source, a sponsor (Drugs@FDA: by `fda_sponsor_key`; the registry: as
    registered) to the one covered company it names; a sponsor naming several is left out of it.
    """
    plan = DrugPlan()
    kept: list[tuple[SourceRecord, Owner]] = []
    given, cber = list(records), cber_records()
    plan.records = len(given)
    for record in _vouched(_drug_shaped(plan, given, cber, sponsor_of)):
        if (owner := _owner(record, sponsor_of)) is None:
            plan.unlinked_records += 1
        else:
            kept.append((record, owner))
    # The CBER list gives no names, only owners for the comparator rule; every one has an owner.
    kept += [(record, _owner(record, sponsor_of) or f"other:{record.sponsor}") for record in cber]
    owners, listed = _owned_names(plan, kept)
    shared = {key for key, held in owners.items() if len(held) > 1}
    for record, _, names in listed:
        for name in (n for n in names if name_key(n.name) in shared):
            key = name_key(name.name)
            _withhold(plan, key, name.name, (o for o in owners[key] if isinstance(o, UUID)), [record.ref])
    by_company: defaultdict[UUID, list[tuple[SourceRecord, list[ListedName]]]] = defaultdict(list)
    for record, owner, names in listed:
        if names and not names[0].main:
            continue  # the main name went (another sponsor's): its other names name no drug of ours
        if record.source is DrugNameSource.CTGOV and names and name_key(names[0].name) in shared:
            continue  # the main name is shared: its other names would make a drug named by them alone
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


def _without_bridges(
    plan: DrugPlan, entries: list[tuple[SourceRecord, list[ListedName]]]
) -> list[tuple[SourceRecord, list[ListedName]]]:
    """Drop an other name that records with different main names list (second review of PR #73, B1').

    Aktis's two imaging agents both list "64Cu"; Regeneron's linvoseltamab and vonsetamig both
    list "BCMAxCD3". Such a name says what the drugs share, not which drug it is. Main names are
    first joined only by the records that give them together (an FDA product's brand and generic
    name). An other name that is itself another record's main name ("INS1007" under brensocatib,
    and a trial of "INS1007") still joins the two, if only one main name lists it and
    `_join_aliases` allows it; when it does not, the other name is dropped (third review).
    """
    entries = _words_stand_alone(entries)
    mains = _Groups()
    main_keys: set[str] = set()
    for _, names in entries:
        keys = [name_key(n.name) for n in names if n.main]
        main_keys.update(keys)
        mains.join(keys)
    _join_aliases(entries, mains, main_keys)
    listing = _listing(entries, mains)
    bridges = {key for key, roots in listing.items() if len(roots) > 1}
    # An other name that is another drug's main name, and was not joined to this one, names that drug.
    bridges |= {key for key, roots in listing.items() if key in main_keys and roots != {mains.find(key)}}
    plan.bridges += len(bridges)
    kept = [(r, [n for n in ns if n.main or name_key(n.name) not in bridges]) for r, ns in entries]
    return [(r, ns) for r, ns in kept if ns]


def _main_root(names: list[ListedName], mains: _Groups) -> str | None:
    key = next((name_key(n.name) for n in names if n.main), None)
    return None if key is None else mains.find(key)


def _fda_keys(entries: Iterable[tuple[SourceRecord, list[ListedName]]]) -> set[str]:
    return {name_key(n.name) for r, ns in entries if r.source is DrugNameSource.OPENFDA for n in ns}


def _is_words(record: SourceRecord, names: list[ListedName], fda: set[str]) -> bool:
    """Say whether a registry record's main name has no digit and is no Drugs@FDA name of the company.

    A generic name by its INN stem is not such a name ("navitoclax" with ABT-263 in one
    intervention): the fifth review asked that it join the code its own intervention gives.
    """
    main = names[0]
    plain = not any(ch.isdigit() for ch in main.name) and name_key(main.name) not in fda
    return record.source is DrugNameSource.CTGOV and main.main and plain and not _inn_shaped(main.name)


def _words_stand_alone(
    entries: list[tuple[SourceRecord, list[ListedName]]],
) -> list[tuple[SourceRecord, list[ListedName]]]:
    """Keep a registry main name without a digit from joining codes, FDA names, or marked brands.

    "FGF Receptor" (LY2874455), "Avidin" (SSR29261), "Yellow Fever" (YF VAX®), and "Anti-Beta
    Interferon (PF-06823859)" are a target, a protein, a disease, and a class, written where the
    drug's name goes. Such a name may only stand alone as its own drug, or with other plain words
    ("izalontamab brengitecan", "Iza-bren"); a word name that is the company's Drugs@FDA name
    ("Brensocatib") joins as before (fourth review of PR #73).
    """
    fda = _fda_keys(entries)
    kept: list[tuple[SourceRecord, list[ListedName]]] = []
    for record, names in entries:
        if _is_words(record, names, fda):
            names = [
                names[0],
                *(n for n in names[1:] if not n.marked and not any(c.isdigit() for c in n.name)),
            ]
        kept.append((record, names))
    return kept


@dataclass
class _GroupFacts:
    """What one group of main names holds, for the alias rule."""

    codes: set[str] = field(default_factory=set[str])
    code_mains: set[str] = field(default_factory=set[str])
    others: set[str] = field(default_factory=set[str])
    words: bool = False  # a registry word main name (`_is_words`)
    firm: bool = False  # a code, a Drugs@FDA name, or a marked brand


def _group_facts(
    entries: list[tuple[SourceRecord, list[ListedName]]], mains: _Groups
) -> defaultdict[str, _GroupFacts]:
    fda = _fda_keys(entries)
    facts: defaultdict[str, _GroupFacts] = defaultdict(_GroupFacts)
    for record, names in entries:
        own = _main_root(names, mains)
        if own is None:
            continue
        found = facts[own]
        found.codes |= {name_key(n.name) for n in names if n.kind is DrugNameKind.CODE}
        found.code_mains |= {name_key(n.name) for n in names if n.kind is DrugNameKind.CODE and n.main}
        found.others |= {name_key(n.name) for n in names if not n.main}
        found.words = found.words or _is_words(record, names, fda)
        firm = record.source is DrugNameSource.OPENFDA or any(
            n.kind is DrugNameKind.CODE or n.marked for n in names
        )
        found.firm = found.firm or firm
    return facts


def _may_join(key: str, alias: _GroupFacts, listed: _GroupFacts, parts: int) -> bool:
    """Say whether an alias may join two groups of main names (third and fourth reviews of PR #73)."""
    if parts > 1:
        return False  # the listing drug's records name two or more other drugs: a combination
    if (alias.words and listed.firm) or (alias.firm and listed.words):
        return False  # a word main name never joins a code, an FDA name, or a marked brand
    if alias.code_mains and listed.code_mains:
        return False  # two drugs with their own code main names: "INO-3112" and its part "VGX-3100"
    mine, theirs = alias.codes - {key}, listed.codes - {key}
    return not mine or not theirs or bool(mine & theirs)


def _join_aliases(
    entries: list[tuple[SourceRecord, list[ListedName]]], mains: _Groups, main_keys: set[str]
) -> None:
    """Join an other name that is another record's main name, listed under one main name only.

    Never when the listing drug's records, together, list the main names of two or more other
    drugs: INO-3112's trials list its parts VGX-3100 and INO-9012, in one trial or one in each.
    Never between two drugs that each have a code main name, or whose other codes differ ("panHer"
    is BMS-599626; BMS-690514 trials list "panHER"). Never a word main name with a code (see
    `_words_stand_alone`). And at most one hop: when A's trial lists B and B's trial lists C,
    none of the three joins (third and fourth reviews of PR #73).
    """
    facts = _group_facts(entries, mains)
    edges: list[tuple[str, str]] = []
    for key, roots in _listing(entries, mains).items():
        if key not in main_keys or len(roots) != 1:
            continue
        alias, listed = mains.find(key), next(iter(roots))
        parts = len({mains.find(k) for k in facts[listed].others & main_keys} - {listed})
        if alias != listed and _may_join(key, facts[alias], facts[listed], parts):
            edges.append((alias, listed))
    hops = _Groups()
    for edge in edges:
        hops.join(edge)
    size = Counter(hops.find(group) for group in {group for edge in edges for group in edge})
    for alias, listed in edges:
        if size[hops.find(alias)] == 2:
            mains.join([alias, listed])


def _listing(entries: list[tuple[SourceRecord, list[ListedName]]], mains: _Groups) -> dict[str, set[str]]:
    """Map each other name to the joined main names of the records that list it."""
    found: defaultdict[str, set[str]] = defaultdict(set)
    for _, names in entries:
        main = next((name_key(n.name) for n in names if n.main), None)
        for name in names:
            if not name.main and main is not None:
                found[name_key(name.name)].add(mains.find(main))
    return found


def _product(record: SourceRecord) -> str:
    """Name an FDA product: its one ingredient, or, for a combination, its brand (VYTORIN is not ZETIA)."""
    return record.ingredient or "+" + name_key(record.names[0].name)


def _company_drugs(
    plan: DrugPlan, company: UUID, entries: list[tuple[SourceRecord, list[ListedName]]]
) -> list[PlannedDrug]:
    """Join one company's names into drugs; a registry record that joins two approved products is left out."""
    entries = _without_bridges(plan, entries)
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
            planned.mains += name.main
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
            if others:  # a code name too: one code under two companies is one partnered drug
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
