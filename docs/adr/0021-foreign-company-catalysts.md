# 0021 — Foreign company catalysts, in two stages

- **Status:** accepted
- **Date:** 2026-10-17
- **Roadmap items:** Phase 2, "Foreign companies listed in the US"; Phase 5, "Companies listed only outside the US"

## Context

On 2026-10-17 the owner asked for catalysts of public foreign companies, with every filing source that needs.

Foreign companies fall into two groups, and they need very different work.

1. **Foreign companies listed in the US.** These are foreign private issuers, many with American depositary shares. They file with the SEC on EDGAR, but on their own forms:
   - Form 20-F is the annual report (Canadian issuers may use Form 40-F instead);
   - Form 6-K carries what they publish at home, including press releases.

   They have no 8-K ([SEC overview for foreign private issuers](https://www.sec.gov/divisions/corpfin/internatl/foreign-private-issuers-overview.shtml)). The spec's coverage is "US-listed" companies, so these companies are already in scope. But the collector reads only 8-K press-release exhibits, so their catalysts are missed today.
2. **Companies listed only outside the US**, for example in London, Euronext, Switzerland, the Nordics, Germany, Australia, Hong Kong, Japan, Korea, mainland China, Canada, and Israel. Each market has its own announcement system, its own access terms, and sometimes its own language. Their trials are often filed in other registries, and their approvals come from other regulators.

## Decision

1. **Stage 1, Phase 2: foreign companies listed in the US.** No new account or key is needed, because everything comes from EDGAR.
   - The collector also reads Form 6-K press-release exhibits, and Forms 20-F and 40-F.
   - The coverage list adds foreign issuers under the same industry codes.
   - The extractor and verifier get recorded 6-K fixtures and gold-set entries. This is a source change, so both prompts are scored again.
   - The entity resolver handles depositary-share tickers and home-market names.
   - Company dates follow the company's own fiscal year. "FY2026" can mean April 2026 to March 2027 for a company whose year ends in March, which is common in Japan. The window parser takes the fiscal year end from the 20-F.
2. **Stage 2, Phase 5: companies listed only outside the US, market by market.**
   - Markets are added in order of how many listed drug developers each has.
   - For each market, before any code:
     - check its announcement source's robots.txt and terms;
     - set a rate limit;
     - find out whether it needs an account or key. Any keys are gathered for the owner in one sitting.

     A market whose terms forbid automated collection is left out, or reached through a licensed feed only with the owner's approval of the cost.
   - **Other languages.**
     - The quote stays in the original language, character for character, so the quote check still works.
     - A translation is shown beside it and labeled as a translation.
     - The gold set gains entries in each language before that market publishes.
   - **More registries and regulators:**
     - trial registries: the EU's CTIS, ISRCTN, Japan's jRCT, China's ChiCTR, Australia and New Zealand's ANZCTR, and Korea's CRIS, with the WHO's ICTRP as an index;
     - regulators: EMA, MHRA, PMDA, NMPA, Health Canada, and TGA.

     Each is checked the same way before it is built.
   - **Identifiers.** Companies also carry ISIN and LEI codes, and names in other scripts, so the entity resolver can match them across sources.
   - **Prices.** The planned price data covers US listings. Until a license covers other exchanges and currencies, companies listed only abroad show no market value, price statistics, or scenario price ranges.
3. **Every rule stays the same.**
   - Every value has a source document and a verbatim quote.
   - Agents that read web text have no tools.
   - Only the publish gate writes live data.
   - No advice.

## Alternatives

- **All foreign markets at once.** That would mean many new sources, terms checks, and languages in one step, with the gold set unable to keep up. Errors would rise above the Gate 2 limit.
- **Only foreign companies listed in the US.** This is cheaper, but it leaves out companies that many investors follow on their home exchange. The owner asked for public foreign companies in general.
- **A paid global news feed instead of each market's own source.** It would be quicker to build, but it costs money every month, may limit how data is shown, and is a secondary source rather than the company's own filing. It stays an option, chosen per market, with the owner's approval.

## Consequences

- Stage 1 widens coverage at no extra cost and mostly reuses the existing collectors.
- Stage 2 is the larger build. Each market adds a terms check, possibly an owner setup step, and gold-set work before it publishes.
- The country and regulator filters already listed in the spec start to matter.
