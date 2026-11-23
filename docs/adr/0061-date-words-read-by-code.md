# 0061 — Date words that named no window are read by code, with no person in the loop

- **Status:** accepted
- **Date:** 2026-11-23
- **Roadmap item:** Phase 1, "Verification checks and publish gate" (leads to CP4); a fix from the first export of held claims
- **Amends:** ADR 0012, decision 3 (more phrases are read; a day or month without a year); ADR 0018, part 5e (the owner's rule of 2026-10-19 that a year must be written, now with two exceptions that code settles) and part 5c (date words cut from a longer date are now made whole where code can tell)

## Context

The export of 2026-11-16 holds 646 claims back from publishing. The owner asked on 2026-11-23: "why are all those catalysts held back, fix it and figure it out", "maximize automation at all steps", and, about dates: **"if there's no named window, figure it the fuck out please"**. The owner wants no person in the loop.

Counted on that export, the date reasons were:

| Reason on a held claim | Claims |
| --- | --- |
| `has_window`: the date words name no window | 117 |
| `extractor_checks`: the extractor flagged `no_window` | 112 |
| `window_matches_words`: the words name no window | 107 |
| `date_words_in_quote`: the words do not write their year; a person must check it | 22 |
| `date_words_in_quote`: the words are cut from a longer date in the quote | 11 |

Three causes, all in plain code, none in a prompt:

1. **The reader of date phrases knew too few shapes.** "October 9th, 2026", "early Q1 2027", "calendar H1 2027", "Q3 ‘28", "the Fall of 2026", "in December" all gave no window. The `no_window` flag is set by the same code (`check_proposal`), not by the model.
2. **A claim keeps the window it was stored with.** Five held claims said "reads as … not the stored None": the reader had learned the phrase since, but nothing read the stored claim again.
3. **Two rules sent every doubtful date to a person**: a year not written in digits (owner decision 2026-10-19, after three review rounds found wrong years published), and date words cut from a longer date.

## Decision

1. **More phrases are read** (`catalyst.extract.dates`). The window must hold every usual reading of the words, as ADR 0012 chose for "mid-2027".

   | Phrase | Window | Precision |
   | --- | --- | --- |
   | October 9th, 2026; Saturday, December 12, 2026; on or about September 11, 2026 | that day; a written weekday must be that day's, or there is no window | day |
   | … at 8:15 AM CEST; … 10:25 – 10:35 CEST (after a day) | that day; a time of day needs a day | day |
   | November 19th-20th, 2026 | those days | month |
   | early / mid / late December 2026; End Nov. 2026 | days 1 to 15 / 8 to 22 / 16 to the last; "end" as late | month |
   | early / late Q1 2027; early in the first quarter of 2027 | the quarter's first two months / last two | quarter |
   | mid-Q2 2027; at the end of the third quarter of 2026 | the quarter's middle month / last month | month |
   | spring, summer, fall (autumn) of 2026 | four months from March, June, September, so both ways of counting seasons fit | half |
   | early / late summer 2026 | the season's first two months / last two (mid: the middle two) | quarter |
   | first quarter of calendar year 2027; calendar H1 2027 | as without "calendar" (a fiscal year is still held) | as without |
   | Q4-2026; Q3 ‘28; mid‑2027; mid-year 2027 | as Q4 2026, Q3 '28, mid-2027 | as those |
   | 4Q 2026-Q1 2027 | a range; an unspaced hyphen joins a year to a quarter or half, never "mid-2027" or "2027-2028" | coarsest part |
   | near the end of 2026 or early 2027; throughout 2027; … timeframe | "near", "throughout", "on", "at" and a trailing "timeframe" change nothing | as without |
   | before the end of 2026 | as "by the end of 2026". "Before December 2026" ends before the month and is not read | as "by" |

   The precision is the smallest of month, quarter, half, year that can hold the window.

   One fix found on the way: a deadline on a day ("by December 15, 2027") runs from the release to that day, but was stored with day precision, which the catalyst table refuses for a window longer than one day. It now has month precision, as a range of days does. One held claim on the export has this shape; its window has passed.

2. **A day or a month with no year** ("in December", "November", "Friday, October 23", "August 28-31", "early September", "by early January") is the first one on or after the release's date, or, for a month, the first not yet over. Two limits:
   - **183 days.** If that first one starts more than 183 days after the release, the words name no window and the claim stays held. Why 183: on the export, all 31 such claims fall within five months of their release. A date already past when the copy was filed (a release printed on November 20 and filed on November 23 that says "November 21"), and a past event named by its month, both land eleven to twelve months ahead, so the limit holds them. ADR 0012 gave such a day no window when it was before the release; it now does when the next one is within 183 days ("March 26" in a release of October 14).
   - **A written weekday must match.** "Thursday, October 23" in a release of October 2026 names no window, because October 23, 2026 is a Friday. The same holds when the year is written.

3. **A year the words do not write is settled by code in two cases** (`unsettled_year`); the check `date_words_in_quote` fails otherwise, and its reason now says why instead of "a person must check it".
   - **"This year", "this quarter", "this fall"** ("later this year", "the fourth quarter of this year"): the release's own year or quarter. Held when the release came out in the first 14 days of that year or quarter, because a filing can follow the release by some days (the known limit in ADR 0018, part 5e) and the words may mean the one before.
   - **A month or a day** (decision 2). Held when the quote writes another year anywhere ("For calendar 2027 … in December"); when the quote goes on right after the words with what may be a year ("December 15, 2O27", "December 15 of next year", a digit of another script, a Roman numeral, a spelled-out number); when the quote speaks of a fiscal year; or when a weekday beside the words is not that day's.
   - **Still held, as before:** words with no year and no month ("second half", "fourth quarter", "by year-end", "by the end of the year"), because a heading or another sentence may give the year (ADR 0018, B1′); and words that count from the release ("next year", "the next year"), which may mean twelve months (B1″).
   - The catalyst still shows the company's own words: the stored date words are never changed by this, only read.

4. **Date words cut from a longer date are made whole** (`whole_date`), at extraction and for stored claims. A model sometimes copies "mid-2027" of "by mid-2027", or "2027" of "early 2027". Code takes the whole date, in the quote's own words, only when all of these hold: the words appear once in the quote; one longer stretch around them (up to six words either side) holds every other stretch that reads as another window; a stretch with that window writes its year; and the word before it is not "from" ("moved from Q4 2026 to 2027" may be a change, not a range). Every date rule then runs on the whole words. Anything else is held as before.

5. **Stored claims are read again on every check run** (`verify.run.reread_dates`, first step of `run_checks`, no model call). Only two kinds of claim are touched: one stored with no window and the `no_window` flag, whose words now give one; and one whose words are cut from a longer date (decision 4). The flag is cleared, or becomes `date_passed` when the window ended before the release. Both readers' claims are read again, so the second reading is not out of step. Never touched: a stored window whose words are unchanged (so `window_matches_words` still catches a changed window), and any claim a live catalyst's change history cites. The run summary counts them as `dates_reread`.

6. **Left held, for later work.** Counts are from the same export, after this change:
   - a named conference with no date ("ESMO 2026", "2026 ESMO Congress"): 8 claims, plus 2 that name no meeting ("an upcoming medical congress"). The roadmap's "Conferences" item gives these their days;
   - timing tied to an earnings report ("with our third quarter 2026 results"): 2 claims;
   - no date words at all: 1 claim (a placeholder);
   - also held: a length of time from another event ("within 30 days", six months after another event), 8; a hedge ("potentially in 2026", "as early as this year"), 3; two events in one phrase ("Q3 and Q4 2026, respectively"), 3; date words that are not in the quote, 5; and 7 single shapes not worth a rule yet ("mid-2027 (Q2/Q3)", "Oct. 19-21, Boston", "tomorrow, September 10", "the second week of September 2026", "later in Q3 2026", a month inside other words).
   - `window_not_past` is unchanged and correct: an event whose window has ended is not upcoming.

## Alternatives

- **Change the extractor's prompt to copy whole dates and add years.** A prompt change needs a paid gold-set run, and a model's year is a guess; code that reads the quote is free and checkable.
- **Publish every year-less date in the release's year.** That is what published wrong years before 2026-10-19. Only the two cases code can argue for are settled.
- **Settle "by year-end" and "second half" too** (7 claims on the export). A list of a later year's milestones is written with exactly these words, under a heading the quote does not hold. They stay held until code reads the text around the quote.
- **A limit of eleven months instead of 183 days.** It would also hold a date that had just passed, but it would read a month named seven months ago, and wrongly listed as upcoming, as next year's.
- **Narrower parts** ("early Q1" as January). They look precise; the wider window always holds the real date, and the words are shown beside it.
- **Read stored claims again in a migration, once.** The next new phrase shape would need another; the check run does it every time for free.
- **Rewrite a stored window whenever the reader disagrees with it.** That would quietly repair a changed value that `window_matches_words` exists to catch.

## Consequences

- On the 646 held claims of the 2026-11-16 export, with that day as today: 88 stored claims are read again. `has_window` failures fall from 117 to 39, the `no_window` flag from 112 to 34, "do not write their year" from 22 to 7, and "cut from a longer date" from 11 to 1 (its words appear twice). 73 date phrases that gave no window now give one.
- 67 claims lose every date reason; 5 of them have no other reason left and would publish. The rest wait on other checks (drug names, the second reading), which other work is fixing. 31 of the newly read windows had already ended by the export's date, so `window_not_past` holds them, correctly (69 claims before, 100 after).
- On the 170 published catalysts (175 quoted sources with their date words): every window reads exactly as before, and none is touched, because a claim a live catalyst cites is never read again. One of them shows "early 2027" where its quote says "late 2026 or early 2027"; it was published before the cut-date rule and stays as it is, still failing `date_words_in_quote` as it does today.
- **Known limits, accepted with the owner's decision of 2026-11-23.** A month or a day with no year is read as the next one. It is wrong when the year stands only outside the quote (a heading "2027 milestones" over "data in December", in a release of late 2026), or in the quote in a form the guards do not know. And a past event named by its month alone, more than six months before the release, that the extractor wrongly lists as upcoming, is read as next year's. On the export all 31 such claims read correctly. The second reading and the weekly sample check (ADR 0041) still stand behind each one.
- Six hostile test pages from the CP4 review, whose date words were cut from a longer date, now end with the whole date and its right window instead of a hold; their tests say so. Every page whose year code cannot read is still held.
- The planted-error page and counts are unchanged.
- New phrase shapes still need a pattern and tests; until then they name no window and are held, with the words listed in the export.
