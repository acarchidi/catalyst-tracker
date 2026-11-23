# 0063 — The issue's calendar is split into due, open windows, and later

- **Status:** accepted
- **Date:** 2026-11-23
- **Roadmap item:** Phase 2, Marketing and outreach agent: "Weekly issue hand-off"
- **Amends:** ADR 0053 (the single "Upcoming Catalysts" section)

## Context

The first issue's "Upcoming Catalysts through 23 December 2026" listed entries with wide windows, such as "by the end of 2027" and "in 2026", beside entries that end within 30 days. Readers could take every entry as due within 30 days. On 2026-11-23 the owner chose to split the section, and to list the open windows on the free issue too rather than only count them.

## Decision

1. The issue's calendar entries are split on the horizon, the issue date plus 30 days:
   - **Due by <horizon>:** the window ends on or before the horizon. Always shown; when empty it says "Nothing is due by then."
   - **Open windows:** the window starts on or before the horizon and ends after it. Shown when not empty, on the free and the paid issue, with the line "Windows that start by <horizon> and end later."
   - **Later (paid only):** the window starts after the horizon. The free issue never holds these (ADR 0047, decision 3). Shown when not empty, with the line "Windows that start after <horizon>."
2. The calendar is ordered due, then open windows, then later, each earliest start first, before the list limit (25 free, 200 paid) cuts it. A wide window that started long ago never pushes a due entry off the list. Found when rebuilding the 2026-11-23 issue: cutting first left the free issue with 0 due entries where the paid issue had 3.
3. The digest JSON keeps `upcoming` and adds the counts `due`, `open_windows` and `later`.

## Alternatives

- **Count open windows on the free issue.** Proposed first; the owner chose to list them, since a bare count is the kind of line ADR 0057 removed.
- **Keep one section and add a date label.** ADR 0053 removed date labels from the issue.

## Consequences

- Readers see which entries are due within 30 days and which only have a wide window.
- `test_the_calendar_is_the_next_30_days_in_date_order_and_only_open_catalysts` now expects due entries before the open window; the records it checks are unchanged.
- One existing test now expects "Due by 4 November 2026 (0)" where it expected "Upcoming Catalysts through 4 November 2026 (0)"; the paid-heading test is replaced by a test of the split.
