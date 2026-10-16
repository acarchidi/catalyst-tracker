"""Formats of the external identifiers we store (spec: Data model, cross-references)."""

from typing import Annotated

from pydantic import Field

Cik = Annotated[int, Field(ge=1, le=9_999_999_999, description="SEC Central Index Key.")]
Ticker = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,9}$", max_length=10)]
NctId = Annotated[str, Field(pattern=r"^NCT\d{8}$", max_length=11)]
EuCtNumber = Annotated[str, Field(pattern=r"^\d{4}-\d{6}-\d{2}(-\d{2})?$", max_length=17)]
MeshId = Annotated[str, Field(pattern=r"^[DC]\d{6,9}$", max_length=10)]
EfoId = Annotated[str, Field(pattern=r"^[A-Za-z]+_\d+$", max_length=40)]
Icd10Code = Annotated[str, Field(pattern=r"^[A-Z]\d[0-9A-Z](\.[0-9A-Z]{1,4})?$", max_length=8)]
Unii = Annotated[str, Field(pattern=r"^[A-Z0-9]{10}$", max_length=10)]
ChemblId = Annotated[str, Field(pattern=r"^CHEMBL\d+$", max_length=20)]
HgncSymbol = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9\-]{0,24}$", max_length=25)]
SicCode = Annotated[str, Field(pattern=r"^\d{4}$", max_length=4)]
