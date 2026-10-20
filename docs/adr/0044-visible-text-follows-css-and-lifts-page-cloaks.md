# 0044 — Visible text follows CSS inheritance and lifts a page-wide cloak

- **Status:** accepted
- **Date:** 2026-10-20
- **Amends:** ADR 0018, part 5c (what `visible_text` hides)

## Context

The live extraction run on 2026-10-20 (Actions run 39427364641) read 224 press releases and skipped 56 as having "no readable text". Their stored copies have thousands of words: on 2026-10-17, before `visible_text` existed, the extractor read 2,569 to 13,597 tokens of text from the four that were checked. In the run of 2026-10-18 (with `visible_text`), the model call for each of the 56 sent 18 or 28 tokens besides the cached instructions: the "Publication date" line and at most a few words. `visible_text` hid all, or nearly all, of every page, and no page was partly hidden: each release read either in full or as empty.

The 56 come from 19 filers and filing agents (Donnelley, Toppan Merrill, Workiva, Broadridge, EdgarAgents, M2, and others; about one in five of the large agents' releases), and other releases from the same company and template read in full. That points to something that hides a whole page, not to one agent's layout. The stored copies are in Supabase Storage and the SEC pages need the contact e-mail, so neither was reachable from the coding session; the exact markup on those 56 pages is not yet confirmed.

`visible_text` over-hid in three ways that real pages can trigger:

1. **A cloak on the whole page.** `body { display:none }`, `html { visibility:hidden }`, a hidden `<body>`, or one hidden wrapper around everything. Pages use this to stay blank until a script, a viewer, or a check shows them. A person reading the page in a browser sees it all, but `visible_text` dropped every word.
2. **Inheritance that a child undoes.** In CSS, visibility, font size, and colour are inherited, and a child may set them again: `<div style="font-size:0">` with children in 10pt is a common way to remove gaps between inline blocks. `visible_text` hid everything inside the parent.
3. **Print-only rules.** A rule in `@media print` applied on screen too.

## Decision

1. **A page cloak is lifted.** When the visible text has no letters at all and one element (the outermost hiding one) hides every hidden letter, that element is a cloak, not a hidden part. It is read again as if it hid nothing, up to three levels deep (html, body, a wrapper). Every hide inside it still hides, so a planted notice inside a cloaked page stays hidden. The page is read, but it still hides its words from readers, so every claim from it is held for a person (`source_hides_text`, from `read_visible`, in the extractor and the second reading). If any letter is visible outside it, or two elements share the hidden letters, nothing is lifted.
2. **Inherited properties follow CSS.** `display:none`, opacity under 0.1, a place off the page, and a clip still hide everything inside, as no child can undo them. Visibility:hidden, transparent text, and a font of 1px or 1pt and under are inherited until a child sets visibility:visible, its own colour, or a readable size in absolute units (pt, px, in, cm, mm, pc, also in the `font` shorthand). A size in em or % scales the parent's, so it stays hidden. `visibility:collapse` stays a full hide. A show counts only when the browser would keep it: the whole style must read cleanly (no braces, comments, escapes, or open strings or brackets, and every part a property and a value), the value must be whole and valid (a `font` shorthand needs a family, and no CSS-wide keyword such as `inherit`), and it must not use `var()`, `env()`, `attr()`, or `calc()`. Of repeated attributes, only the first may show. Code checks this itself; tinycss2 was considered but not added, as the checks are short and the rule is to show only what reads cleanly.
3. **Only rules that apply on screen.** Rules in `@media` blocks for print or speech alone are skipped. A rule that shows text is used only when it is one plain selector outside any `@media` block, since only the last part of a selector is matched: `.missing * { visibility:visible }` or a small-screen rule never shows text a browser keeps hidden. A rule that hides is still used everywhere, as before. Shows come only from `<style>` elements the HTML parser finds in the head or body, with no `media` attribute and a CSS type; `<style>` text in a comment, a script, or an attribute is not an element, so it may hide but never show. Strings are emptied and one-line at-rules (`@import`, `@charset`, `@layer a;`) dropped before braces are counted. A sheet that still does not read cleanly (an escape, an open string, a nested rule, unbalanced braces) is read rule by rule as before, and only its hides count. If a sheet has a rule the code cannot read (a pseudo-class or attribute test) that hides, none of that sheet's rules show, since that rule may win in a browser.
4. **The log says what hid a page.** When a copy has 50 words or more but fewer than 50 visible, the job logs `document_hides_its_words` with the word count and the element that hides the most letters (tag, id, class, style, cut to 100 characters each). The next run will show what hid any page this does not fix.

`document_text` and `TEXT_VERSION` do not change. No recorded page's visible text changes: the 12 gold-set releases, the 64 injection fixtures, and the 4 other SEC pages give the same visible text as before. Under each of five cloaks (a body rule, an html rule, a hidden body, a hidden wrapper, a print-only rule), all 80 read exactly as they do without it, where the old `visible_text` read none of them; no planted hidden text shows, and every page that was held for hiding words is still held.

## Alternatives

- **Publish from a lifted page without review.** If the cloak is added in delivery, holding sends those releases to a person for nothing. Not chosen (Opus review of PR #91): a page that hides all of itself is still a page that hides its words, and a person should read it before anything from it is published.
- **Lift a cloak that holds nearly every word** (say 90%). Would catch a cloak with a short visible line outside it, but a page could then plant a large hidden part beside a small visible one. Not chosen.
- **Skip the whole hidden-text filter when it leaves under 50 words.** Simple, but it would let a page hide its injected words behind a small visible text. Not chosen.

## Consequences

- Known gaps kept from before this change, for a separate item: `display:/**/none`, escaped identifiers, `font:0/0 a`, `opacity:5%`, and `-webkit-text-fill-color:transparent` still hide nothing.

- The 56 skipped releases are checked again on every run for free (ADR 0038, PR #79). Those blanked by a cloak, an undone inherited style, or a print rule are read on the next run, with no data edit; claims from a cloaked page wait for a person.
- Any that still read as empty are logged with what hides them, so the next fix can start from the real markup.
- Hidden-text tests: every existing one passes unchanged; new tests cover each over-hiding case, the same cases under each cloak, and the rules that must not show text.
