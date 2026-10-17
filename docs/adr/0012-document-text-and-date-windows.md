# 0012 — Plain text for quotes, and how date phrases become windows

- **Status:** accepted
- **Date:** 2026-10-17
- **Roadmap item:** Phase 1, "Extractor agents and prompt evaluation sets" (first part)

## Context

A claim stores a verbatim quote and its character offsets "in the stored copy's text" (spec: Provenance; `Claim`). The stored copies are mostly HTML (SEC exhibits wrapped in SGML headers) and JSON, so the text the offsets count in must be defined, and must not drift. The spec also stores each catalyst date as a window with a precision of day, month, quarter, half, or year, plus the raw words ("2H26"). It does not say what window vague phrases such as "mid-2027" or "by year-end" name, and says date parsing is written test first, with property-based tests.

## Decision

1. **Document text.** `catalyst.extract.text.document_text` turns a stored copy into plain text: only the SEC `<TEXT>` part of a filing, no head, style, or script; one line per block element (paragraph, div, list item, table row, line break); table cells separated by a space; character references decoded once; runs of spaces made one space; blank lines dropped. The rules carry `TEXT_VERSION = "1"`. Changing them means a new version, and claims made against the old one are re-checked.
2. **Quotes.** `find_quote` accepts a quote only if its words appear in the text in the same order, with the same case and punctuation; only the spacing between words may differ (a model may write a line break as a space). The stored quote is always the text's own characters at the found offsets, so it is always the source's words.
   Copies that are not HTML (the JSON records from ClinicalTrials.gov, openFDA, and the Federal Register) keep their characters and get only the spacing tidied; the extractor reads press releases, so this matters only if a later agent quotes a JSON record.
3. **Date windows,** read by code from the source's own phrase, as of the document's publication date:

| Phrase | Window | Precision |
| --- | --- | --- |
| October 30, 2026 | that day | day |
| November 12 (no year) | that day in the document's year if not before the document's date; otherwise no window | day |
| April 2027 | the month | month |
| Q1 2027, 1Q27, first quarter of 2027 | the quarter | quarter |
| 2H26, H2 2026, second half of 2026 | July to December | half |
| early 2027 / late 2027 | January to June / July to December | half |
| mid-2027 | April to September | half |
| 2027, during 2027 | the year | year |
| year-end 2026, the end of 2027 | October to December | quarter |
| by (the end of) a period | the document's date to the end of that period | that period's |
| by year-end, later this year | the document's date to December 31 | year |
| no year given (the fourth quarter, this quarter, next year) | the document's year, or the next year for "next" | as above |

   A two-digit year needs an apostrophe ("April '27", "'27"), except in compact forms (2H26, 1Q27, Q1 27, H2 26). Alone, "April 27" is a day and "26" is a number, never a year. This rule came from the review of the first version, which read "November 12" as November 2012.

   A "by" deadline already past on the document's date gives no window, and a "by" window that is one day long has day precision (the `catalyst` table requires that). Vague phrases ("soon", "in the coming months", "upon approval") and impossible dates (February 30) give no window: the claim goes to review rather than get an invented date.

## Alternatives

- **Let the model return start and end dates.** Faster to build, but a model's arithmetic is not checkable and the spec puts dates under rule checks written in code. The model returns the phrase; code makes the window.
- **"Mid-2027" as the middle quarter (May to July) or as June.** Narrower windows look precise but a company saying "mid-year" usually means somewhere around the turn of the half; April to September covers the usual readings. The raw phrase is kept, so the convention can change later without losing anything.
- **Offsets in the raw HTML.** Exact, but quotes would include markup, and the model reads text, not tags.

## Consequences

- Every quote can be checked by code: find it in the text, compare the offsets. A quote the model changed even slightly fails, and the claim does not publish.
- On the 96 press releases from the Gate 1 rehearsal, the text is 2,798 to 69,569 characters (median 12,977), a simple search found 1,728 date phrases; 1,392 got a window (858 days, 408 quarters, 120 halves, 6 years). The rest are days without a year that fall before the document's date, which get no window by design.
- New phrase shapes need a new pattern and tests; until then they give no window and go to review.
