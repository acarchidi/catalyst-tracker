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
6. Copy the key now; it is shown once. Keep it in a private note until Part 5.

## Part 3 — Supabase database (5 minutes)

1. Go to **supabase.com**, click **Start your project**, then **Continue with GitHub**.
2. If asked to create an organization: name `catalyst-tracker`, plan **Free**, click **Create**.
3. Click **New project**:
   - **Name**: `catalyst-tracker`
   - **Database password**: click **Generate a password**, then copy it into your note
   - **Region**: **East US (North Virginia)**
   - Click **Create new project** and wait about 2 minutes
4. Copy into your note:
   - **Project URL**: shown on the project's home page, like `https://xxxx.supabase.co`
   - **Secret key**: **API Keys** at the bottom of the home page (or gear icon, then **API Keys**); copy the key that starts with `sb_secret_`. If none is listed, click the button to create one
   - **Session pooler string**: green **Connect** button, then **Session pooler**; copy it exactly, with `[YOUR-PASSWORD]` left in. The password itself goes in its own secret, without square brackets

## Part 4 — Not needed

Live jobs run on GitHub's machines (ADR 0011), so the keys go only into GitHub's secrets (Part 5). A coding session's network cannot reach the live database anyway.

## Part 5 — Keys in GitHub's secrets (done 2026-10-17 for Supabase and SEC)

1. Go to **github.com/acarchidi/catalyst-tracker/settings/secrets/actions**.
2. For each row, click **New repository secret**, type the name exactly, paste the value, click **Add secret**.

| Name | Value |
| --- | --- |
| `SUPABASE_URL` | Project URL (Part 3) |
| `SUPABASE_SECRET_KEY` | `sb_secret_` key (Part 3) |
| `SUPABASE_DB_PASSWORD` | Database password (Part 3) |
| `SUPABASE_DB_URL` | Session pooler string, with `[YOUR-PASSWORD]` left in (Part 3) |
| `SEC_CONTACT_EMAIL` | The project's SEC contact address: a new address made for this project, not a personal one; the SEC requires one on every automated request |
| `CATALYST_ANTHROPIC_API_KEY` | Anthropic key (Part 2), when Phase 1 needs it |

3. Delete your note with the keys.

## Part 6 — Reviewer key, so pull requests merge themselves (5 minutes, once)

The merge robot merges Claude Code's pull requests once the automatic checks and the Claude review pass (ADR 0007). The review needs its own key; until it exists, nothing merges by itself.

1. Go to **platform.claude.com/settings/workspaces**. Click **Add Workspace**. Name it `catalyst-review`. Click **Create**.
2. Click the new workspace. Open the **Limits** tab, click **Change Limit**, and enter **30** ($30 a month).
3. Open the **API Keys** tab and click **Create Key**. Name it `catalyst-review`, then click **Create Key**. Copy the key.
4. Go to **github.com/acarchidi/catalyst-tracker/settings/secrets/actions**. Click **New repository secret**.
5. Name: `CATALYST_REVIEW_API_KEY`. Paste the key into **Secret**. Click **Add secret**. Do not keep the key anywhere else.

**To stop the robot entirely** (the dependable brake): go to **github.com/acarchidi/catalyst-tracker/actions**, click **Merge robot** on the left, click the **...** button at the top right, and click **Disable workflow**. **Enable workflow** in the same place turns it back on.

**To stop it merging one pull request:** open the pull request, click **Labels** in the right-hand column, and choose `hold`. The robot merges within seconds of the last check finishing, so this works only on a pull request whose checks are still running. The robot creates the `hold` label the first time it runs. Removing the label later does not restart the robot; ask Claude Code to re-run the checks.

**Pull requests the robot leaves for you.** The robot does not merge changes to itself, the review, the project rules, the check settings, or the spending caps. Claude Code tells you when one is waiting, what it does, and what a separate reviewer found. Merge it only when Claude Code says it is ready and the checks named **Lint, types, tests, migrations**, **Dependency audit**, and **Secret scan** show green ticks. To merge: open it, scroll down, click **Merge pull request**, then **Confirm merge**.

**Later, with a paid GitHub plan (optional).** GitHub Pro or higher can also protect main against everyone, not only the robot. Then: open **github.com/acarchidi/catalyst-tracker/blob/main/.github/rulesets/main.json** and click the download arrow; go to **github.com/acarchidi/catalyst-tracker/settings/rules**; click the arrow next to **New ruleset**, then **Import a ruleset**; choose the file; click **Create**. Tell Claude Code first, because the ruleset also changes which pull requests wait for you.

## Later phases

Claude Code will tell you when each of these is needed: Vercel project (Phase 2), Resend email (Phase 2), Sentry (Phase 2), business entity and Stripe (Gate 3), Tiingo ($50/month, Phase 3), domain name (Phase 2).
