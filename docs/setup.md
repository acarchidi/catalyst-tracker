# Setup steps for the owner

Only the owner can do these steps: they need a login, a signup, or billing. Claude Code does everything else. Run `python3 scripts/check_setup.py` to see which steps are still missing.

Never paste a key into a chat or a commit. Keys go only into the two settings pages named below.

## Part 1 — Repository (done 2026-10-16)

The repository `catalyst-tracker` exists. If the Claude GitHub App is set to "Only select repositories", it must include `catalyst-tracker`: github.com/settings/installations, then **Configure** next to **Claude**.

## Part 2 — Anthropic API key (5 minutes)

1. Go to **platform.claude.com/settings/workspaces** and sign in.
2. Click **Add Workspace**. Name it `catalyst-tracker`. Click **Create**.
3. Click the new workspace.
4. Open the **Limits** tab, click **Change Limit**, and enter **50** ($50 a month).
5. Open the **API Keys** tab and click **Create Key**. Name it `catalyst-tracker`, then click **Create Key**.
6. Copy the key now; it is shown once. Keep it in a private note until Part 4.

## Part 3 — Supabase database (5 minutes)

1. Go to **supabase.com**, click **Start your project**, then **Continue with GitHub**.
2. If asked to create an organization: name `catalyst-tracker`, plan **Free**, click **Create**.
3. Click **New project**:
   - **Name**: `catalyst-tracker`
   - **Database password**: click **Generate a password**, then copy it into your note
   - **Region**: **East US (North Virginia)**
   - Click **Create new project** and wait about 2 minutes
4. Copy into your note:
   - **Project URL**: click **Connect** at the top; it looks like `https://xxxx.supabase.co`
   - **Secret key**: gear icon (**Project Settings**), then **API Keys**; copy the key that starts with `sb_secret_`. If none is listed, click the button to create one

## Part 4 — Give Claude Code the keys (5 minutes)

This creates a separate environment so other projects' sessions never see these keys.

1. Go to **claude.ai/code**.
2. Under the message box, click the first control (the environment selector), then **New environment**.
3. **Name**: `catalyst-tracker`.
4. **Network access**: choose **Custom**. Paste this list into **Allowed domains**, then tick **Also include default list of common package managers**:

```
api.anthropic.com
*.supabase.co
*.supabase.com
sec.gov
*.sec.gov
clinicaltrials.gov
*.clinicaltrials.gov
*.fda.gov
www.federalregister.gov
*.ncbi.nlm.nih.gov
*.globenewswire.com
*.businesswire.com
*.prnewswire.com
```

5. **Environment variables**: paste these lines, replacing each `...` with the value from your note:

```
CATALYST_ANTHROPIC_API_KEY=...
SUPABASE_URL=...
SUPABASE_SECRET_KEY=...
SUPABASE_DB_PASSWORD=...
SEC_CONTACT_EMAIL=...
```

6. Click **Add environment**. Start every catalyst-tracker session in this environment.

The key is deliberately not named `ANTHROPIC_API_KEY`, so Claude Code never bills its own coding work to it.

**SEC contact email:** use a new free address made for this project rather than your personal one. The SEC requires every automated request to carry a contact email.

## Part 5 — The same keys in GitHub, for scheduled jobs

Claude Code tries this step itself with `gh secret set` (after asking you). If GitHub refuses, do it by hand:

1. Go to the repository, then **Settings**, then **Secrets and variables**, then **Actions**.
2. Click **New repository secret**. Name `CATALYST_ANTHROPIC_API_KEY`, paste the value, click **Add secret**.
3. Repeat for the other four names in Part 4.
4. Delete your note with the keys.

## Part 6 — Automatic review and merging (10 minutes, once)

Do this after the open pull requests are merged, in the order below. ADR 0006 explains the design.

**Before you start: the plan.** The repository is private. GitHub enforces main-branch protection and offers auto-merge on private repositories only with a paid plan (GitHub Pro or higher). On the free plan, steps B and C do nothing, and the automatic merging stays off. Check at **github.com/settings/billing**.

**A. Protect main (first, so nothing can merge unchecked)**

1. Open **github.com/acarchidi/catalyst-tracker/blob/main/.github/rulesets/main.json**. Click the **Download raw file** button (a downward arrow, top right of the file).
2. Go to **github.com/acarchidi/catalyst-tracker/settings/rules**.
3. Click the arrow next to **New ruleset**, then **Import a ruleset**. Choose the downloaded `main.json`.
4. Check that the name says **Protect main** and the status says **Active**. Scroll down and click **Create**.
5. If GitHub says the ruleset "contains an invalid actor": delete the entry under **Bypass list**, click **Add bypass**, tick **Repository admin**, click **Add selected**, set it to **For pull requests only**, then click **Create**.
6. If GitHub says the rules will not be enforced on this repository, stop and tell Claude Code; it is the plan limit above.

**B. Let pull requests merge themselves**

1. Go to **github.com/acarchidi/catalyst-tracker/settings**. Stay on **General**.
2. Scroll to **Pull Requests**. Tick **Allow auto-merge**. It saves by itself.

**C. A separate key for the reviewer (last)**

1. Go to **platform.claude.com/settings/workspaces**. Click **Add Workspace**. Name it `catalyst-review`. Click **Create**.
2. Click the new workspace. Open the **Limits** tab, click **Change Limit**, and enter **30** ($30 a month).
3. Open the **API Keys** tab and click **Create Key**. Name it `catalyst-review`, then click **Create Key**. Copy the key.
4. Go to **github.com/acarchidi/catalyst-tracker/settings/secrets/actions**. Click **New repository secret**.
5. Name: `CATALYST_REVIEW_API_KEY`. Paste the key into **Secret**. Click **Add secret**. Do not keep the key anywhere else.

**After this.** Pull requests that touch only ordinary code merge on their own once all checks pass. Pull requests that touch the rules, the database, dependencies, or model settings wait for you. Claude Code tells you when one is waiting and what it does.

Merge a waiting pull request only when all three of these hold:

1. Claude Code has told you it is ready.
2. **Lint, types, tests, migrations**, **Dependency audit**, and **Secret scan** each show a green tick.
3. The "Claude review" comment says **passed**. Two exceptions: dependency updates from Dependabot are never reviewed, and a pull request that changes the review itself (anything under `.github/`, `scripts/review_verdict.py`, `CLAUDE.md`, or `AGENTS.md`) cannot be trusted to report on itself. For both, Claude Code runs a separate review and tells you the result in plain words; go by that.

To merge: open the pull request, tick **Merge without waiting for requirements to be met (bypass rules)**, and click **Merge pull request** (sometimes labelled **Bypass rules and merge**). The bypass skips every check, so never use it when any of the three conditions fails.

## Later phases

Claude Code will tell you when each of these is needed: Vercel project (Phase 2), Resend email (Phase 2), Sentry (Phase 2), business entity and Stripe (Gate 3), Tiingo ($50/month, Phase 3), domain name (Phase 2).
