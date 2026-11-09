# 0047 — Distribution loops, and a weekly digest built by plain code

- **Status:** accepted (decisions 1 to 5); decision 6 lists what waits for the owner
- **Date:** 2026-11-09
- **Roadmap item:** Phase 2, "Marketing and outreach agent" (owner request 2026-11-09)

## Context

On 2026-11-09 the owner asked how the product can reach paying subscribers with less owner work, and asked for the open loops to be found and closed.

The spec defers the distribution plan (Open questions). ADR 0020 gives a marketing agent that drafts, with the owner approving every item. ADR 0029 allows one exception: the X feed, which plain code fills from a template the owner approves once.

A loop here means a chain that feeds itself: each run of the data produces something a reader sees, the reader's action is counted, and the count changes what is built next. On 2026-11-09 no loop is closed:

| Loop | What exists | What is missing |
| --- | --- | --- |
| 1. Data to content | Published catalysts, the read API, the export | Nothing turns a week of changes into something to read |
| 2. Content to readers | A local API and a local MCP server | A public site, a sign-up form, a sender, a feed, a hosted MCP server |
| 3. Readers to subscribers | The free and paid shapes in the API | Accounts, checkout, the business setup (Gate 3) |
| 4. Use to learning | Model-cost logs | Any count of visits, sign-ups, opens, or paid conversions |
| 5. Outreach | 209 contacts in `outreach/prospects.csv`, all `new` | Drafts, the approval record, the sender, reply tracking |
| 6. Accuracy to trust | The weekly sample checker, the link check | A public page showing the error rate and corrections |

Two more facts shape the decision:

- Catalyst data is already offered to AI assistants by others. A web search on 2026-11-09 found a Dropwatch MCP connector, a BPIQ MCP endpoint marked "Premium", and at least two more catalyst servers in the Glama directory. A directory listing alone will not set this product apart; a free tier with a source on every record can.
- A model that writes recurring public text can write a sentence no record supports. The product's whole claim is that every value has a source.

## Decision

1. **Recurring public text is computed, not drafted.** Anything that goes out on a schedule (the weekly digest, feed entries, the X feed of ADR 0029) is built by plain code from published records. A model drafts only one-off items, such as a note to a named person. Those stay under ADR 0020: the owner approves each one.
2. **The weekly digest** (`catalyst.outreach.digest`, run as `catalyst digest`) is the first piece, and closes loop 1.
   - **Input:** every published catalyst, read through `catalyst.api.read.published`. Nothing from the review queue can enter.
   - **Schedule:** an issue is dated on a Monday and reports the seven days before it, in New York time. Any other date is refused. An issue is never built early because of a market event (ADR 0020, decision 2).
   - **Three lists.** A catalyst is listed once, under the first that fits:
     - *Dates that moved:* the latest date move was learned in the week. Each shows the window now, the window before, and how it changed.
     - *New on the calendar:* the catalyst's first value was learned in the week. A record that waited in review across a week's end is therefore listed on the calendar, not as new.
     - *Also on the calendar:* everything else in the next 30 days, earliest first, up to a limit (25 by default); the rest are counted.
   - **How a window changed** is one of five words, decided by the days alone: *later* (both ends moved later), *earlier* (both ends moved earlier), *narrowed* (the new window lies inside the old), *widened* (the old lies inside the new), or *restated* (the same days, written another way). A window that grew at one end only is *widened*, never *later*.
   - **Every entry carries** the company, the title, the date as the source wrote it, the date label, the verbatim quote, and the deep link.
   - **Same records, same issue.** Building an issue again from the same data changes nothing. An issue is built from the data as it stands, so building an old week later can differ: a date that moved again since then shows only its latest move.
   - **No issue ahead of time.** The command refuses a Monday later than the latest one. A later issue would list days past today's free calendar.
3. **The digest holds only what a free caller may see** (spec: Offering; ADR 0025, decision 6). It lists only open catalysts whose window overlaps the next 30 days, in the free shape (`CatalystSummary`), with only the latest date change. A date move or a new catalyst outside the next 30 days is counted, and the issue states the two counts. It never names those records. Only a record whose window starts after the 30 days is counted; an open record whose window has already passed is neither listed nor counted.
4. **Output is JSON and an HTML fragment.** The website, the newsletter sender, and any feed read the JSON. The HTML has no styles, scripts, or attributes except link addresses.
   - Every word that comes from a record is escaped. Quotes are verbatim source text, so they are untrusted.
   - A source address becomes a link only when it is a plain https address with a host, no sign-in part, and no characters that do not print. Any other address is shown as text.
   - Markdown was rejected as the output: many Markdown readers turn a bare address inside a quote into a link, and escaping cannot stop that reliably.
5. **The digest sends nothing.** `catalyst digest` writes files and prints counts. The sender (ADR 0020), the sign-up form, and the schedule are separate roadmap items. The sender stays off until business setup gives a postal address, a sending service, and a sending domain.
6. **Waiting on the owner.** Nothing below is built or changed until the owner decides:
   - **(a) One approval for the digest's template, or an approval for each issue?** ADR 0020 declined "templates approved once" for the newsletter. That was decided when a model was to write each issue. The digest has no model text, goes only to people who signed up, and keeps a fixed day. The question is whether it may go out on a template approved once, as the X feed does. Until the owner answers, ADR 0020 stands and each issue needs approval.
   - **(b) Whether date moves outside the next 30 days may be named in the free digest.** They are the most newsworthy records and also what the paid tier sells. Today they are counted only.
   - **(c) New features proposed, not on the roadmap:** a free calendar that newsletter writers can embed with a link back; a form for a company to request a correction with a source; a higher-priced tier for professional users; a referral credit.

## Alternatives

- **A model writes each issue (ADR 0020 as first written).** Better prose, but each issue then needs a full read by the owner, and one invented sentence would damage the product's central claim. Kept for one-off notes only.
- **List every date move, including those outside the next 30 days.** A stronger free issue, but it gives away the paid date history. Left as owner decision 6(b).
- **Markdown output.** Easier to read in a pull request, but unsafe for untrusted quotes (decision 4).
- **Build the sender first.** The sender needs accounts and a postal address that do not exist yet. The digest needs none.

## Consequences

- Loop 1 is closed. Loops 2 to 6 have roadmap items; each needs the public site or the business setup first.
- The first issue can be previewed as soon as a live export or a database is at hand. Its preview goes to the owner before any sending is built (CP7 still gates the first send).
- Adding a section to the digest means deciding whether a free caller may see it. The test that checks the free shape makes that a reviewed choice.
- A corrections section waits for the first correction writer: no code sets `is_correction` yet.
- Revisit when the owner answers decision 6, and when accounts make a paid digest possible.
