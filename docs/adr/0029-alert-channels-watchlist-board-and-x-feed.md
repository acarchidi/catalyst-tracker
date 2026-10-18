# 0029 — More alert channels, a watchlist board, and a most-watched feed on X

- **Status:** accepted (planned; nothing is built now)
- **Date:** 2026-10-18
- **Roadmap items:** Phase 9, all four items

## Context

On 2026-10-18 the owner asked for three things at the end of the roadmap:

1. Users choose how alerts reach them: email, phone, or a few other kinds.
2. A watchlist that users pin catalysts to, as a board with dynamic pictures of where the watched catalysts fall in time.
3. Possibly an X (Twitter) bot that posts the most-watched or most-alerted catalysts, perhaps only on a higher paid tier, since the owner sees it as valuable.

**What the spec already has.**
- Watchlists: free gets 1 list of 10 items; paid gets unlimited lists.
- Alerts: free gets a weekly email digest; paid gets email, web push, and Slack after each run.
- Calendar export (.ics), RSS feeds, and webhooks for alerts (paid).
- No social posts for now (owner decision 2026-10-17; ADR 0020).
- Only one paid tier, at $15 a month.

**Costs found on 2026-10-18.** These come from the vendors' pages and third-party summaries. They are checked again when each item is built.
- Text messages through Twilio: $0.0083 per US message segment, plus carrier fees of about $0.003 to $0.005. US business texting also needs A2P 10DLC registration, with a one-time brand fee and a campaign fee that sources put at about $10 a month ([Twilio US pricing](https://www.twilio.com/en-us/sms/pricing/usa); [third-party calculator](https://zernio.com/twilio-pricing-calculator)).
- X API: pay per use. Third-party summaries put a plain post at about $0.015 and a post with a link at about $0.20. The older $200-a-month Basic plan is reported closed to new sign-ups. None of this was confirmed on X's own pages ([PostProxy](https://www.postproxy.dev/blog/x-api-pricing-2026/); [OpenTweet](https://opentweet.io/how-to/x-api-pay-per-use-explained)).

## Decision

1. **More alert channels, each chosen per watchlist or saved search.**
   - **Channels:** email, text message, web push, Slack, Discord, Microsoft Teams, Telegram, and a user's own webhook.
   - **Who gets what:** free stays the weekly email digest. Paid users pick any channels, instant or a daily digest, and quiet hours.
   - **Text messages cost money per message.** So they carry a monthly cap per user, set in config (starting at 100). Past the cap, alerts fall back to the user's other channels until the month ends. At the prices above, 100 texts cost about $1.30, out of a $15 subscription.
   - **Text-message rules:** a user's number is confirmed by a code before any alert is sent; "STOP" ends all texts at once; a text says who sent it and links to the record.
   - **Owner setup, gathered into one sitting when this item starts:** a Twilio account with its 10DLC registration, and a Telegram bot token. Discord, Teams, and Slack use webhook addresses the user pastes in. Web push needs no account: its keys are made by code and kept as GitHub secrets.
   - **Every alert, on any channel,** carries the source link and the "not financial advice" notice. Alerts are sent by plain code from published data only.
2. **A watchlist board.**
   - Users pin catalysts, companies, drugs, indications, and saved searches to a board. Each pinned company, drug, or search brings in its catalysts as they publish.
   - **Timeline view:** each catalyst is drawn at its date window. A single day is a dot; a quarter or a half is a bar of that length. Colour shows the catalyst type, and the date label (company-confirmed, regulator-scheduled, registry estimate, inferred) is shown on each one.
   - **Controls:** zoom from weeks to years; group rows by company, drug, indication, or type; filter.
   - **Overlap view:** shows how many watched catalysts fall in each week or month, so clusters stand out.
   - **Changes show on the board:** a slipped window shows its old position faintly, with an arrow to the new one and the date of the change. Free users see the latest change only, as elsewhere.
   - **Updates:** the board refreshes after each run, with no page reload.
   - **Accessibility:** a table view that holds the same data, with keyboard control (WCAG 2.2 AA).
   - **Export:** the board as an image and as .ics. A board can be shared as a read-only link; sharing is off until the user turns it on.
   - **Never on the board:** price or return series (data license), and buy or sell language.
   - **Limits:** free boards follow the free watchlist limit (1 board, 10 items). Paid boards are unlimited.
   - The charting library is chosen when this item is built, with its own ADR.
3. **A most-watched and most-alerted ranking, in the product first.**
   - Plain code counts, after each run, how many users watch each catalyst and how many alerts each one sent, over the last 7 days.
   - **Privacy:**
     - Only totals are ever shown. A catalyst is ranked only when at least 10 different users watch it (set in config), so no one person's interest can be worked out.
     - Users are never named.
     - The privacy policy says how watch counts are used.
   - The ranking is a page and an RSS feed. It is a paid feature until the owner sets a tier (point 4).
4. **The X feed: a decision for the owner, with a recommendation.**
   - **A public X account cannot be limited to paying users.** Every post on it is free for anyone to read. Three ways to sell it remain:
     - **(a) Recommended:** keep the full, current ranking in the product, on the paid tier or a higher one. The X account posts a delayed, shorter version, for example the top 5 of last week, as marketing with a link to the page.
     - **(b)** Post the current ranking only to paying followers through X's own subscriber feature. X takes a share and sets rules on who can use it, both checked at build time.
     - **(c)** A protected X account that paying users are let into. Keeping its followers in line with the subscriber list takes manual work.
   - **A higher tier does not exist yet.** Whether to add one (for example "Pro"), its price, and what goes in it are the owner's decisions. They are asked when this phase starts, not now.
   - **How posts are made:**
     - Plain code fills a fixed template from the ranking, with no model. The owner approves the template once, and changes to it go back to the owner.
     - Posts go out on a fixed schedule, never in reaction to a price move. That keeps the account a regular, impersonal publication (spec: Compliance).
     - Only original posts: no replies, mentions, or messages (spec: Marketing automation).
     - Each post carries the notice and a link to our page, which holds the sources.
   - **Cost:** about $0.20 a post with a link, if the prices above hold. One post a day is about $6 a month. The X API spend gets its own daily cap in config.
   - **Owner setup:** an X account for the product and an X developer account, gathered into the same sitting as point 1 where possible.
   - **The first post** needs the owner's go-ahead (checkpoint CP12), because it publishes outside the project.
5. **Every rule stays the same.** Only published data is used, every value keeps its source, and nothing tells a user what to buy or sell.

## Alternatives

- **Phone calls instead of text messages.** Rejected. A call costs more, and an alert carries a link, which a call cannot.
- **A native mobile app for push alerts.** Rejected for v1 (spec: Not in v1). Web push works from the installed web app.
- **Posting the full current ranking on X for free.** Rejected as the default, because it gives away the part the owner wants to sell. It stays option (a)'s upgrade path if the free version grows the audience more than it costs in sales.
- **A model writing the X posts.** Rejected. A template from plain code cannot invent a claim, and it needs only one approval instead of one per post.

## Consequences

- Phase 9 needs the Phase 2 web app, the Phase 3 accounts and billing, and the Phase 3 watchlist alerts first (roadmap: Phase 3, "Watchlists, alerts").
- The owner's setup for this phase is one sitting: Twilio with its registration, a Telegram bot token, an X account, and an X developer account.
- Running costs grow with use: text messages per paid user (capped), and X posts per day (capped).
- Whether attention data is worth paying for is an assumption, not a finding. It can be tested later, once outcomes exist, by comparing the most-watched catalysts with the rest.
- The spec's "no social posts for now" gains one exception: this feed, from Phase 9.
