# Issue review: checks before anything reaches the owner (ADR 0055, decision 7)

Owner order, 2026-11-24: "please incorporate a full polishing run into the pre-publication process, make a set of best review practices and have them automatically implemented before handing anything off to me."

Every Monday the session runs this review between building the pages (weekly-issue.md, step 5) and handing them over (step 6). Nothing reaches the owner until every check passes. A failure is fixed in code or in the data, then the issue is built again and the whole review runs again. Live data is never edited by hand. A data fault goes to Master control with its claim ids.

`catalyst review-issue` runs the checks marked **(code)**. It prints counts only, writes each finding to a private file, and exits 1 on any failure.

## A. Data, per listed entry

1. **(code)** Each entry's date words appear in one of its quotes.
2. Each entry's quote names the drug or the company for the event. Until Master control settles the rule, a gap is reported and does not hold the issue.
3. **(code)** The ticker is the common stock: no five-letter symbol ending in W, U, R or Z (a warrant, unit, right or CVR), unless that is the company's only listing. SEC's `company_tickers_exchange.json` shows the exchange.
4. **(code)** The company name is a display name: no EDGAR state suffix ("\DE") and no name in capitals.
5. **(code)** No listed window has ended. The entry is open.
6. No entry is an event that a newer filing says already happened, or replaced: a closed deal, a completed submission, a meeting already held. Each "Due" entry is checked against the company's newer filings and news.
7. **(code)** No event appears twice. The free issue holds nothing past its 30 days.

## B. Copy

1. The intro names exactly the sections the issue shows, with the same names.
2. The title and the heading give the same week.
3. **(code)** The "When" lines read as English: "on December 8–11, 2026", never "in December 8–11, 2026"; every year is named, never "this year"; no doubled preposition; "in the first half", not "in first half".
4. The counts in the page's top line equal the section counts.

## C. Rendering

1. **(code)** Every link is an https address on sec.gov or clinicaltrials.gov with a quote fragment.
2. The page opens in Chromium with outside requests blocked and shows the title, the colour key, every section, and the notice.
3. The copy buttons copy the issue as formatted HTML, so headings and links survive the paste.

## D. Compliance

1. **(code)** No advice wording outside the notice: buy, sell, price target, should invest, upside, bullish, bearish, outperform.
2. **(code)** The notice appears once, in small print, as the last element. No other disclaimer wording is added anywhere.
3. No continuous price or return series.

## E. Promotional note (owner's rules, 2026-11-24)

1. **(code)** It starts exactly "In this week's Catalyst Tracker newsletter: ".
2. The first statement gives the most important dated item. Every fact is true from the export. It may leave out detail.
3. Each highlighted entry names its indication, using the common abbreviation where one exists (AML, NSCLC, MASH). The indication comes from the entry's own claim. If the entry has none, either choose another highlight, or use a claim on the same drug from the same company whose quote names the indication, and record that in the review log.
4. **(code)** Statements are separated by one blank line.
5. **(code)** The last statement gives the number of programs added to the tracker from the reporting week's filings: distinct drugs in the New section whose source is dated in that week, corporate events left out.
6. **(code)** It ends with "[paste free post link]", which the owner replaces after publishing. It is at most 280 characters, with the link counted as 23.
7. **(code)** None of these:
   - en or em dashes
   - hashtags
   - emojis
   - exclamation marks
   - questions
   - "not X, it's Y" or "not just X"
   - "biotech catalysts"
   - the owner's banned words (`NOTE_BANNED` in `catalyst/outreach/review.py`)
   - advice words
8. No list of three adjectives. Check against Wikipedia's "Signs of AI writing" guide (WikiProject AI Cleanup).

## F. Image (with the note)

1. It is a 1600×900 PNG titled "Catalyst Tracker: next 30 days", with the issue date.
2. It shows the 30 days the free issue's "Due" section covers.
3. The note's highlights are drawn large, each with company, drug, indication and date, as a bar across its window.
4. The other free entries inside those 30 days are small muted markers labelled with their ticker.
5. Its highlights and dates match the note and the issue.
6. No label overlaps another. The text is readable on a phone.
7. It holds no advice wording.

## G. Fresh-eyes reviewer

An Opus subagent with a fresh context reads both pages, the note and the image. It reads as a skeptical biotech investor and as a copy editor, and lists every factual, naming, grammar or credibility problem. Each finding is checked against the full stored quote before anyone acts on it: link fragments show only the first and last words of a quote, and readers misjudge those. Real findings are fixed in code or sent to Master control. The build, checks A–F and the reviewer then run again. At most two rounds. If the issue still fails, the page shows a one-line hold reason in place of the copy buttons.

## H. The top line

The page's top line says either "Reviewed: all checks passed" with the review time, or "On hold:" with the reason. A review log on the page, below the previews, lists each finding and what was done about it.
