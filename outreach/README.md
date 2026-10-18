# Outreach prospect list

`prospects.csv` lists people and organizations the marketing agent may later draft notes for (ADR 0020). It is a research list, not a mailing list. **Nothing is sent to anyone from this file.** A note goes out only after the owner approves its exact text, its exact recipients, and its channel.

The first 209 rows were checked on 2026-10-17. 115 of them have a published email; the other 94 have only a contact page.

## The rules (ADR 0020)

- **Public professional contacts only.** Every row names the public page where it was found (`source_url`). An email must appear on that page exactly as written.
- **No guessed emails.** Never build an address from a name pattern, such as first initial plus last name. If the page shows no address, leave `email` blank and record the contact page.
- **No bought lists, and nothing from behind a login.** No LinkedIn, Crunchbase, PitchBook, ZoomInfo, Apollo, RocketReach, Hunter, or any paid source.
- **United States only** for now. Canada's and the EU's email rules are stricter, so contacts there wait.
- **Work addresses only.** No gmail, yahoo, hotmail, outlook, aol, icloud, proton or similar personal mailboxes.
- **Respect each site.** Follow robots.txt, go slowly, and do not work around a site that blocks access.
- **Opted out means opted out.** A person who says no, or asks to be removed, is marked `do_not_contact`. Their row stays, so a later research run cannot add them back. They are never written to again.

## Columns

| Column | What it holds |
| --- | --- |
| `category` | One of `institutional_small`, `pharma_investor_group`, `individual_investor_analyst`, `information_service`, `media`, `investor_community` |
| `name` | The person, or the outlet or firm when there is no named person |
| `organization` | Who they work for or publish under |
| `role` | Their role, or the kind of address (for example "General inquiries" for info@) |
| `email` | The published work address, in lower case, or blank |
| `contact_page_url` | The page to use when there is no email (https) |
| `source_url` | The page where the row's details were found (https) |
| `source_checked_on` | The date that page was last checked (YYYY-MM-DD) |
| `notes` | A short "why it fits" note, and caveats such as "US base unconfirmed" |
| `status` | Where this contact stands; see below |
| `status_changed_on` | The date the status last changed (YYYY-MM-DD); blank only while `new` |

## Statuses

| Status | Meaning |
| --- | --- |
| `new` | Found by research; nothing decided yet |
| `approved` | The owner agreed this contact may be written to |
| `contacted` | A note the owner approved was sent |
| `replied` | They wrote back |
| `declined` | They said no thanks; no more notes |
| `do_not_contact` | Never write to them. Keep the row so they are not added again |

## Adding rows

1. Find the contact on the organization's own public page. Copy the address exactly as shown.
2. Add the row with `status` = `new` and a blank `status_changed_on`. Keep rows sorted by category, then organization, then name.
3. Run `uv run catalyst prospects-check`. It prints the counts per category and status, and fails on any bad row, naming its line.

Later research runs add rows with `merge_prospects` in `pipeline/catalyst/outreach/prospects.py`. It adds only contacts not already listed (same email, or the same name at the same organization when there is no email). It never changes an existing row's status, and never adds back anyone marked `do_not_contact`.

To check that no `do_not_contact` row was deleted, compare against an earlier copy: `uv run catalyst prospects-check --previous <earlier copy>`.

## Things to know before using the list

- **Weak addresses.** Eight rows have a support mailbox as the only published address: BiotechEdge, FDATracker, Tikr, Biotech Primer, StockBrokers.com, Insider Monkey, Raging Bull and Alpha Vantage. Their `role` says "Support (only address published)".
- **Parent-company addresses.** BioPharma Dive uses info@industrydive.com and Pharmaceutical Executive uses pharmexec@mjhlifesciences.com. The other MJH Life Sciences brands are contact-page only, so one parent address is not repeated.
- **US base unconfirmed.** 29 rows say "US base unconfirmed" in `notes`, mostly Substack writers and small data sites whose location is not stated. Mark any that turn out to be outside the US as `do_not_contact`, or remove them before they are ever contacted.
- **Competitors.** BioPharmCatalyst and FDATracker are direct competitors. Dan Sfera, Merlin Trader, PDUFA Pulse and BiopharmaWatch publish similar catalyst calendars. They may fit better as partnership or listing talks than as customers.
- **Notes are not verified.** The "why it fits" notes come from general knowledge, not from the page. Fund sizes and strategies were not checked.
- **Contact-page-only writers.** 16 individual and newsletter rows have only a Substack or site "about" page. Some may be reachable only through Substack itself.
- **Small fixes on import.** Three emails were lower-cased (Logos Capital, Defiance ETFs, Ausangate Capital) and Gabelli's page address was changed from http to https.
