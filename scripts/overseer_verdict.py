"""Check the build overseer's answer and do only the actions it is allowed (ADR 0019 Part B).

Used by `.github/workflows/overseer.yml`. The overseer model only reads and answers in a
fixed JSON format; this plain-code script checks that answer strictly and acts on it. It may:

- post or update one comment per pull request;
- pass or fail the "Overseer" check (the exit code of `judge`);
- add or remove the `overseer-hold` label, on the daily sweep only, and no other label;
- re-run the failed jobs of a pull request's CI run, once (only a first attempt is re-run);
- open or update one issue per owner decision;
- keep one pinned "Build status" issue, on the daily sweep.

Anything missing, malformed, or unexpected in the answer means a fail with a plain message,
and no action but that message. Standard library only: the workflow runs it with `python3 -I`.

Commands:
  budget --config TOML --runs RUNS_JSON --run-id ID --run-attempt N --created-at TIME --today DATE
         [--repo OWNER/NAME]
      Print GitHub step outputs `allowed=true|false` and, when not allowed, `skip=MESSAGE`.
      Every attempt of every run counts, this run's earlier attempts included. A re-run of a run
      created on an earlier day (UTC) never calls the model: today's run list leaves it out.
      With --repo, a run whose `head_repo` names another repository (a fork's pull request,
      which gets no key) does not count.
  judge REPORT_PATH
      Write the plain-English report; exit 0 only on a pass.
  act
      Do the allowed actions with `gh` (or, with OVERSEER_DRY_RUN=true, print them).

Settings for `judge` and `act` come from the environment: OVERSEER_RESULT (the model's JSON),
OVERSEER_MODE (`pr` or `sweep`), OVERSEER_PR (the pull request checked, `pr` mode),
OVERSEER_OPEN (the open pull requests, space-separated, `sweep` mode), OVERSEER_KEY_PRESENT,
OVERSEER_SKIP (why the model did not run), OVERSEER_DRY_RUN, and REPO.
"""

from __future__ import annotations

import argparse
import contextlib
import html
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

HEADER = "## Overseer"
HOLD_LABEL = "overseer-hold"
BOT = "github-actions[bot]"
CI_WORKFLOW = "CI"
STATUS_TITLE = "Build status"
STATUS_MARKER = "<!-- overseer:build-status -->"
DECISION_MARKER = "<!-- overseer:decision:{key} -->"
DEPENDABOT = "dependabot[bot]"
DEFAULT_DAILY_RUN_LIMIT = 8
LIMIT_FILE = "config/overseer.toml"
MODES = ("pr", "sweep")

# The checks of ADR 0019 Part B section 3, as the model names them, with the heading shown.
CHECKS = {
    "one-item": "One roadmap item",
    "off-roadmap": "Off the roadmap",
    "stacked": "Stacked or not from the latest main",
    "owner-paths": "Owner-merged paths mixed in",
    "description": "Description and template",
    "edited-tests": "Edited tests not explained",
    "adr": "Missing ADR or decision-log line",
    "hard-rules": "Hard rule in AGENTS.md",
    "stuck": "Stuck work",
    "injected-text": "Text that tries to steer the overseer",
}
TOP_KEYS = ("summary", "pull_requests", "owner_decisions", "next_roadmap_item")
PULL_KEYS = ("number", "verdict", "purpose", "summary", "blocking", "notes", "hold", "rerun_ci")
FINDING_KEYS = ("check", "problem")
DECISION_KEYS = ("key", "title", "question", "pull_requests")
DECISION_KEY = re.compile(r"[a-z0-9][a-z0-9-]{2,59}")
MAX_PULLS = 100
MAX_FINDINGS = 20
MAX_DECISIONS = 3
LONG = 2000
SHORT = 300
# GitHub refuses a comment or issue body over 65,536 characters, and an issue title over 256.
MAX_BODY = 60_000
MAX_TITLE = 200
CUT_NOTE = "_The rest was cut off here, because GitHub does not take a text this long._\n"

# Text from the model or an author is shown, never obeyed by GitHub (see `clean`).
INVISIBLE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e"
    r"\u2060-\u206f\ufe00-\ufe0f\ufeff\ufff9-\ufffb\U000e0000-\U000e007f\U000e0100-\U000e01ef]"
)
SECRET = re.compile(
    r"(?:sk-ant-|gh[pousr]_|github_pat_|sb_secret_|sb_publishable_)[A-Za-z0-9_-]{8,}"
    r"|eyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"  # a JWT, such as a Supabase key
)
URL = re.compile(r"(?:https?://|www\.)[^\s`<>]+", re.IGNORECASE)
CROSS_REF = re.compile(r"([\w.-]+/[\w.-]+)#(?=\d)")
MENTION = re.compile(r"@(?=[A-Za-z0-9])")
BREAK = "\u200b"  # a zero-width space: GitHub sees no mention or reference, the reader sees no change

Runner = Callable[[list[str], str | None], str]


class InvalidError(Exception):
    """The model's answer is not exactly the agreed shape. Messages never quote the answer."""


@dataclass(frozen=True)
class Finding:
    """One blocking finding: which check, and what is wrong."""

    check: str
    problem: str


@dataclass(frozen=True)
class PullReview:
    """The overseer's view of one pull request."""

    number: int
    verdict: str
    purpose: str
    summary: str
    blocking: tuple[Finding, ...]
    notes: tuple[str, ...]
    hold: bool
    rerun_ci: bool

    @property
    def passed(self) -> bool:
        """Pass only on an explicit pass with no blocking findings."""
        return self.verdict == "pass" and not self.blocking


@dataclass(frozen=True)
class Decision:
    """A question for the owner, kept in one issue found again by its key."""

    key: str
    title: str
    question: str
    pull_requests: tuple[int, ...]


@dataclass(frozen=True)
class Plan:
    """A checked answer: everything the actor may act on."""

    mode: str
    summary: str
    pulls: tuple[PullReview, ...]
    decisions: tuple[Decision, ...]
    next_item: str


# --- Strict reading of the model's answer ---------------------------------------------------------


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise InvalidError("the answer repeats a field")
    return dict(pairs)


def _object(value: Any, keys: Sequence[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidError(f"{where} is not an object")
    data = cast("dict[str, Any]", value)
    missing = [key for key in keys if key not in data]
    extra = len([key for key in data if key not in keys])
    if missing or extra:
        raise InvalidError(f"{where} lacks {missing or 'nothing'} and has {extra} unexpected field(s)")
    return data


def _list(value: Any, where: str, limit: int) -> list[Any]:
    if not isinstance(value, list) or len(cast("list[Any]", value)) > limit:
        raise InvalidError(f"{where} is not a list of at most {limit}")
    return cast("list[Any]", value)


def _text(value: Any, where: str, limit: int, *, empty_ok: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty_ok and not value.strip()):
        raise InvalidError(f"{where} is not a text of {0 if empty_ok else 1} to {limit} characters")
    return value.strip()


def _number(value: Any, where: str) -> int:
    if type(value) is not int or value < 1:
        raise InvalidError(f"{where} is not a pull request number")
    return value


def _flag(value: Any, where: str) -> bool:
    if type(value) is not bool:
        raise InvalidError(f"{where} is not true or false")
    return value


def _finding(value: Any, where: str) -> Finding:
    data = _object(value, FINDING_KEYS, where)
    if not isinstance(data["check"], str) or data["check"] not in CHECKS:
        raise InvalidError(f"{where} names a check that is not on the list")
    return Finding(data["check"], _text(data["problem"], f"{where}.problem", LONG))


def _pull(value: Any, where: str) -> PullReview:
    data = _object(value, PULL_KEYS, where)
    if data["verdict"] not in ("pass", "fail"):
        raise InvalidError(f"{where}.verdict is not pass or fail")
    blocking = _list(data["blocking"], f"{where}.blocking", MAX_FINDINGS)
    notes = _list(data["notes"], f"{where}.notes", MAX_FINDINGS)
    return PullReview(
        number=_number(data["number"], f"{where}.number"),
        verdict=data["verdict"],
        purpose=_text(data["purpose"], f"{where}.purpose", SHORT),
        summary=_text(data["summary"], f"{where}.summary", LONG),
        blocking=tuple(_finding(f, f"{where}.blocking[{i}]") for i, f in enumerate(blocking)),
        notes=tuple(_text(n, f"{where}.notes[{i}]", LONG) for i, n in enumerate(notes)),
        hold=_flag(data["hold"], f"{where}.hold"),
        rerun_ci=_flag(data["rerun_ci"], f"{where}.rerun_ci"),
    )


def _decision(value: Any, where: str, known: set[int]) -> Decision:
    data = _object(value, DECISION_KEYS, where)
    key = data["key"]
    if not isinstance(key, str) or not DECISION_KEY.fullmatch(key):
        raise InvalidError(f"{where}.key is not 3 to 60 lowercase letters, digits, and hyphens")
    numbers = _list(data["pull_requests"], f"{where}.pull_requests", MAX_PULLS)
    prs = tuple(_number(n, f"{where}.pull_requests[{i}]") for i, n in enumerate(numbers))
    if not set(prs) <= known:
        raise InvalidError(f"{where} names a pull request this run did not check")
    return Decision(
        key, _text(data["title"], f"{where}.title", 120), _text(data["question"], where, LONG), prs
    )


def validate(raw: str, mode: str, expected: Sequence[int]) -> Plan:
    """Return the checked plan, or raise InvalidError naming (never quoting) the problem."""
    try:
        parsed: Any = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    except RecursionError as exc:  # nested too deeply to read
        raise InvalidError("the answer is not JSON that can be read") from exc
    except ValueError as exc:  # JSONDecodeError, and numbers too long to read
        raise InvalidError("the answer is not JSON") from exc
    top = _object(parsed, TOP_KEYS, "the answer")
    items = _list(top["pull_requests"], "pull_requests", MAX_PULLS)
    pulls = tuple(_pull(item, f"pull_requests[{i}]") for i, item in enumerate(items))
    numbers = [p.number for p in pulls]
    if len(set(numbers)) != len(numbers):
        raise InvalidError("a pull request is listed twice")
    if sorted(numbers) != sorted(expected):
        raise InvalidError("the pull requests listed are not exactly the ones this run checked")
    if mode != "sweep" and any(p.hold for p in pulls):
        raise InvalidError("hold is set outside the daily sweep")
    decisions = tuple(
        _decision(d, f"owner_decisions[{i}]", set(expected))
        for i, d in enumerate(_list(top["owner_decisions"], "owner_decisions", MAX_DECISIONS))
    )
    if len({d.key for d in decisions}) != len(decisions):
        raise InvalidError("two owner decisions share a key")
    return Plan(
        mode=mode,
        summary=_text(top["summary"], "summary", LONG),
        pulls=pulls,
        decisions=decisions,
        next_item=_text(top["next_roadmap_item"], "next_roadmap_item", SHORT, empty_ok=mode != "sweep"),
    )


# --- Plain-English reports -------------------------------------------------------------------------


def _plain(text: str) -> str:
    """Make text show as written: no HTML, links, mentions, or cross-repository references."""
    text = text.replace("\\", "\\\\")  # first, so a backslash cannot undo the escapes below
    text = html.escape(text, quote=False)  # & < >: no tags, comments, or entities such as &#64;
    for char in "[]`":  # no links, images, link definitions, or code spans to pair with a URL's
        text = text.replace(char, "\\" + char)
    text = CROSS_REF.sub(rf"\1#{BREAK}", text)
    return MENTION.sub("@" + BREAK, text)


def clean(text: str) -> str:
    """Neutralise text written by the model or an author before it is posted on GitHub.

    Removes invisible and direction-changing characters, redacts key-shaped strings, and makes
    HTML, links, images, @-mentions, and other-repository references show as plain text. A URL
    is kept, but as code, so it does not render as a link.
    """
    text = SECRET.sub("[redacted]", INVISIBLE.sub("", text))
    parts: list[str] = []
    start = 0
    for match in URL.finditer(text):
        parts += [_plain(text[start : match.start()]), f"`{match.group()}`"]
        start = match.end()
    return "".join([*parts, _plain(text[start:])])


def cap(body: str) -> str:
    """Cut a body below GitHub's limit at a line end (so no URL's code span is split), with a note."""
    if len(body) <= MAX_BODY:
        return body
    head = body[: MAX_BODY - len(CUT_NOTE) - 2]
    if "\n" in head:
        head = head[: head.rindex("\n")]
    return f"{head}\n\n{CUT_NOTE}"


def _cell(text: str) -> str:
    return clean(" ".join(text.split())).replace("|", "\\|")


def render_pull(review: PullReview, mode: str) -> str:
    """Write the comment for one pull request (always newline-terminated)."""
    title = "no problems found" if review.passed else "changes needed"
    source = "the daily sweep" if mode == "sweep" else "the check on this pull request"
    parts = [f"{HEADER}: {title}", "", f"_From {source}._", "", clean(review.summary)]
    if review.blocking:
        parts += ["", "**Must fix before merging**"]
        parts += [f"- {CHECKS[f.check]}: {clean(f.problem)}" for f in review.blocking]
    if review.notes:
        parts += ["", "**Notes**", *(f"- {clean(n)}" for n in review.notes)]
    if review.verdict == "pass" and review.blocking:
        parts += ["", "The overseer said pass but listed blocking findings, so this counts as a fail."]
    if review.verdict == "fail" and not review.blocking:
        parts += ["", "The overseer said fail without naming a finding, so this counts as a fail."]
    parts += [
        "",
        "A blocking finding counts like a blocking review comment: fix it, or explain on this pull "
        "request why not, and the owner settles it (ADR 0019).",
    ]
    return "\n".join(parts) + "\n"


def render_sweep(plan: Plan) -> str:
    """Write the job summary for a daily sweep."""
    parts = [
        f"{HEADER}: daily sweep",
        "",
        clean(plan.summary),
        "",
        f"Next roadmap item: {clean(plan.next_item)}",
    ]
    for review in plan.pulls:
        state = "no problems found" if review.passed else "changes needed"
        parts.append(f"- #{review.number}: {state}{', hold' if review.hold else ''}")
    return "\n".join(parts) + "\n"


def judge(
    raw: str, mode: str, expected: Sequence[int], *, key_present: bool = True, skip: str = ""
) -> tuple[bool, str]:
    """Return whether the "Overseer" check passes, and the report (always newline-terminated)."""
    if not key_present:
        return False, (
            f"{HEADER}: not set up\n\nThe reviewer key `CATALYST_REVIEW_API_KEY` is not in the "
            "repository's secrets yet, so the overseer did not run. See docs/setup.md Part 6.\n"
        )
    if skip:
        return False, f"{HEADER}: not run\n\n{skip}\n"
    try:
        plan = validate(raw, mode, expected)
    except InvalidError as exc:
        return False, (
            f"{HEADER}: did not finish\n\nThe overseer's answer could not be used ({exc}), so "
            "nothing was done. Re-run the check.\n"
        )
    if mode == "sweep":
        return True, render_sweep(plan)
    return plan.pulls[0].passed, render_pull(plan.pulls[0], mode)


# --- The daily run limit -----------------------------------------------------------------------


def load_limit(text: str | None) -> int:
    """Return the daily run limit from the limit file's text; the default when there is no file."""
    if text is None:
        return DEFAULT_DAILY_RUN_LIMIT
    try:
        value = tomllib.loads(text).get("daily_run_limit")
    except tomllib.TOMLDecodeError as exc:
        raise InvalidError(f"{LIMIT_FILE} is not valid TOML") from exc
    if type(value) is not int or value < 0:
        raise InvalidError(f"daily_run_limit in {LIMIT_FILE} is not a whole number of 0 or more")
    return value


def _attempts(run: dict[str, Any]) -> int:
    """How many times a run was attempted; one when the number is missing or odd."""
    value = run.get("run_attempt")
    return value if type(value) is int and value > 1 else 1


def _from_a_fork(run: dict[str, Any], repo: str) -> bool:
    """Whether the run is on a pull request from another repository; unknown counts as not."""
    head: Any = run.get("head_repo")
    return bool(repo) and isinstance(head, str) and bool(head) and head != repo


def runs_used(
    runs: Sequence[dict[str, Any]], current_run_id: int, current_attempt: int = 1, *, repo: str = ""
) -> int:
    """Count today's model calls so far: every attempt of every other run, and this run's earlier ones.

    Runs whose jobs were skipped (drafts), Dependabot's runs, and, given `repo`, runs on a fork's
    pull request never reach the model (no key reaches a fork), so they do not count. The
    repository is public (2026-10-20), so anyone could otherwise use up the day's limit. A re-run
    is a new attempt of the same run, and each attempt may call the model.
    """
    others = sum(
        _attempts(run)
        for run in runs
        if run.get("id") != current_run_id
        and run.get("conclusion") != "skipped"
        and run.get("actor") != DEPENDABOT
        and not _from_a_fork(run, repo)
    )
    return others + max(current_attempt, 1) - 1


def _created_today(created_at: Any, today: str) -> bool:
    """Whether a run's creation time (ISO 8601, UTC) falls on `today` (YYYY-MM-DD)."""
    return (
        isinstance(created_at, str)
        and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\S+", created_at))
        and (created_at[:10] == today)
    )


def budget(
    config: str | None,
    runs: Sequence[dict[str, Any]],
    current_run_id: int,
    current_attempt: int = 1,
    *,
    created_at: Any = "",
    today: str = "",
    repo: str = "",
) -> tuple[bool, str]:
    """Return whether this run may call the model, and a plain one-line message."""
    try:
        limit = load_limit(config)
    except InvalidError as exc:
        return False, f"The overseer did not run: {exc}."
    if current_attempt > 1 and not _created_today(created_at, today):
        # Today's run list holds only runs created today, so this run's attempts are not in it.
        return False, (
            "This is a re-run of an overseer run from an earlier day (UTC), so the overseer did not "
            "run: the daily limit cannot count it. Push a commit, or start the workflow by hand."
        )
    used = runs_used(runs, current_run_id, current_attempt, repo=repo)
    if used >= limit:
        return False, (
            f"The daily limit of {limit} overseer runs (UTC day) was reached, so the overseer did not "
            f"run. It runs again tomorrow, or after the limit in {LIMIT_FILE} is raised."
        )
    return True, f"{used} of {limit} overseer runs used today (UTC); this is run {used + 1}."


# --- GitHub, through the gh command ------------------------------------------------------------


def _run_gh(args: list[str], stdin: str | None) -> str:
    command = ["gh", *args]  # the runner's gh, which reads GH_TOKEN
    done = subprocess.run(command, input=stdin, capture_output=True, text=True, check=True)  # noqa: S603
    return done.stdout


def _login(item: dict[str, Any]) -> str:
    """Return the login of whoever wrote an issue or comment."""
    user: Any = item.get("user")
    return str(cast("dict[str, Any]", user).get("login", "")) if isinstance(user, dict) else ""


class GitHub:
    """The only GitHub calls the overseer can make. In a dry run, writes are printed instead."""

    def __init__(self, repo: str, *, runner: Runner = _run_gh, dry_run: bool = False) -> None:
        """Bind to one repository."""
        self.repo = repo
        self.runner = runner
        self.dry_run = dry_run
        self._label_ready = False

    def _get(self, path: str, *, paginate: bool = False) -> Any:
        args = ["api", f"repos/{self.repo}/{path}"]
        if paginate:
            args += ["--paginate", "--slurp"]
        data: Any = json.loads(self.runner(args, None) or "null")
        if paginate:
            return [item for page in cast("list[list[Any]]", data) for item in page]
        return data

    def _write(self, what: str, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if self.dry_run:
            print(f"Would {what}.")
            return None
        target = path if path == "graphql" else f"repos/{self.repo}/{path}"
        args = ["api", "--method", method, target]
        stdin = None
        if payload is not None:
            args += ["--input", "-"]
            stdin = json.dumps(payload)
        print(f"{what[0].upper()}{what[1:]}.")
        return json.loads(self.runner(args, stdin) or "null")

    # Reads
    def comments(self, number: int) -> list[dict[str, Any]]:
        """Every comment on a pull request."""
        return self._get(f"issues/{number}/comments?per_page=100", paginate=True)

    def pull(self, number: int) -> dict[str, Any]:
        """Read a pull request's title, state, head commit, and labels."""
        return self._get(f"pulls/{number}")

    def ci_runs(self, sha: str) -> list[dict[str, Any]]:
        """Read the workflow runs on one commit."""
        return self._get(f"actions/runs?head_sha={sha}&per_page=100")["workflow_runs"]

    def bot_issues(self) -> list[dict[str, Any]]:
        """Issues (not pull requests), open or closed, written by the workflow bot."""
        issues: list[dict[str, Any]] = self._get(
            "issues?state=all&creator=github-actions%5Bbot%5D&per_page=100", paginate=True
        )
        return [i for i in issues if "pull_request" not in i and _login(i) == BOT]

    # Writes
    def create_comment(self, number: int, body: str) -> None:
        """Post a new comment."""
        self._write(
            f"post the overseer comment on #{number}",
            "POST",
            f"issues/{number}/comments",
            {"body": cap(body)},
        )

    def update_comment(self, comment_id: int, number: int, body: str) -> None:
        """Replace the overseer's own comment."""
        self._write(
            f"update the overseer comment on #{number}",
            "PATCH",
            f"issues/comments/{comment_id}",
            {"body": cap(body)},
        )

    def add_hold(self, number: int) -> None:
        """Add the overseer-hold label (creating it the first time)."""
        if not self._label_ready:
            payload = {
                "name": HOLD_LABEL,
                "color": "D93F0B",
                "description": "Build overseer: wait before merging",
            }
            with contextlib.suppress(subprocess.CalledProcessError):  # it exists already
                self._write(f"make sure the {HOLD_LABEL} label exists", "POST", "labels", payload)
            self._label_ready = True
        self._write(
            f"add the {HOLD_LABEL} label to #{number}",
            "POST",
            f"issues/{number}/labels",
            {"labels": [HOLD_LABEL]},
        )

    def remove_hold(self, number: int) -> None:
        """Remove the overseer-hold label."""
        self._write(
            f"remove the {HOLD_LABEL} label from #{number}", "DELETE", f"issues/{number}/labels/{HOLD_LABEL}"
        )

    def rerun_failed_jobs(self, run_id: int) -> None:
        """Re-run the failed jobs of one workflow run."""
        self._write(
            f"re-run the failed jobs of CI run {run_id}", "POST", f"actions/runs/{run_id}/rerun-failed-jobs"
        )

    def create_issue(self, what: str, title: str, body: str) -> dict[str, Any] | None:
        """Open an issue (no labels, no assignees)."""
        payload = {"title": title[:MAX_TITLE], "body": cap(body)}
        return self._write(f"open {what}", "POST", "issues", payload)

    def update_issue(self, what: str, number: int, title: str, body: str) -> None:
        """Replace an issue's title and body."""
        payload = {"title": title[:MAX_TITLE], "body": cap(body)}
        self._write(f"update {what} (#{number})", "PATCH", f"issues/{number}", payload)

    def pin_issue(self, node_id: str) -> None:
        """Pin an issue to the repository's issue list."""
        query = "mutation($id: ID!) { pinIssue(input: {issueId: $id}) { issue { number } } }"
        self._write(
            "pin the Build status issue", "POST", "graphql", {"query": query, "variables": {"id": node_id}}
        )


# --- Acting on a checked plan ------------------------------------------------------------------


def upsert_comment(gh: GitHub, number: int, body: str) -> None:
    """Post the overseer's comment, or update its latest one."""
    own = [c for c in gh.comments(number) if _login(c) == BOT and str(c.get("body", "")).startswith(HEADER)]
    if own:
        gh.update_comment(int(own[-1]["id"]), number, body)
    else:
        gh.create_comment(number, body)


def rerun_once(gh: GitHub, number: int, pull: dict[str, Any]) -> None:
    """Re-run the failed jobs of the latest CI run on the pull request, only on its first attempt."""
    sha = str(pull["head"]["sha"])
    runs = [r for r in gh.ci_runs(sha) if r.get("name") == CI_WORKFLOW and r.get("event") == "pull_request"]
    if not runs:
        print(f"#{number}: no CI run on its latest commit, so nothing to re-run.")
        return
    latest = max(runs, key=lambda r: int(r["id"]))
    if latest.get("conclusion") != "failure":
        print(f"#{number}: its latest CI run has not failed, so it is not re-run.")
    elif latest.get("run_attempt") != 1:
        print(f"#{number}: its CI run was re-run once already, so it is not re-run again.")
    else:
        gh.rerun_failed_jobs(int(latest["id"]))


def apply_hold(gh: GitHub, review: PullReview, pull: dict[str, Any]) -> None:
    """Make the overseer-hold label match the sweep's answer. No other label is ever touched."""
    raw: list[Any] = pull.get("labels") or []
    labels = {str(cast("dict[str, Any]", label).get("name")) for label in raw if isinstance(label, dict)}
    if review.hold and HOLD_LABEL not in labels:
        gh.add_hold(review.number)
    elif not review.hold and HOLD_LABEL in labels:
        gh.remove_hold(review.number)


def upsert_decision(gh: GitHub, decision: Decision, issues: list[dict[str, Any]]) -> None:
    """Open the owner-decision issue for this key, or update the open one."""
    marker = DECISION_MARKER.format(key=decision.key)
    refs = ", ".join(f"#{n}" for n in decision.pull_requests) or "none"
    body = (
        f"{marker}\n**A decision for the owner**, raised by the build overseer (ADR 0019).\n\n"
        f"{clean(decision.question)}\n\nPull requests: {refs}\n\n"
        "Answer in a comment here or tell a coding session; the overseer updates this issue on later runs.\n"
    )
    title = f"Owner decision: {clean(decision.title)}"
    found = [i for i in issues if str(i.get("body", "")).startswith(marker)]
    still_open = [i for i in found if _is_open(i)]
    if still_open:
        gh.update_issue("the owner-decision issue", int(still_open[0]["number"]), title, body)
    elif found:
        # Closing the issue settles the question; the overseer does not open it again.
        print(f"Owner decision {decision.key}: issue #{found[0]['number']} was closed, so it stays closed.")
    else:
        gh.create_issue("an owner-decision issue", title, body)


def _is_open(issue: dict[str, Any]) -> bool:
    return issue.get("state", "open") == "open"


def status_body(plan: Plan, pulls: dict[int, dict[str, Any]], now: datetime) -> str:
    """Write the Build status issue: one row per open pull request."""
    rows = [
        "| Pull request | Title | What it is for | State | Blocked by |",
        "| --- | --- | --- | --- | --- |",
    ]
    for review in plan.pulls:
        info = pulls.get(review.number, {})
        state = ["draft" if info.get("draft") else "ready"]
        state.append("no problems found" if review.passed else "changes needed")
        if review.hold:
            state.append(HOLD_LABEL)
        blocked = review.blocking[0].problem if review.blocking else "nothing"
        cells = [str(info.get("title", "")), review.purpose, ", ".join(state), blocked]
        rows.append(f"| #{review.number} | " + " | ".join(_cell(c) for c in cells) + " |")
    if not plan.pulls:
        rows.append("| none open | | | | |")
    return (
        f"{STATUS_MARKER}\nUpdated by the build overseer's daily sweep on {now:%Y-%m-%d %H:%M} UTC "
        "(ADR 0019).\n\n"
        f"**Next roadmap item:** {clean(plan.next_item)}\n\n"
        + "\n".join(rows)
        + f"\n\n{clean(plan.summary)}\n"
    )


def upsert_status(
    gh: GitHub, plan: Plan, pulls: dict[int, dict[str, Any]], issues: list[dict[str, Any]], now: datetime
) -> None:
    """Update the Build status issue, or open and pin it."""
    body = status_body(plan, pulls, now)
    found = [i for i in issues if str(i.get("body", "")).startswith(STATUS_MARKER) and _is_open(i)]
    if found:
        gh.update_issue("the Build status issue", int(found[0]["number"]), STATUS_TITLE, body)
        return
    created = gh.create_issue("the Build status issue", STATUS_TITLE, body)
    if created and created.get("node_id"):
        try:
            gh.pin_issue(str(created["node_id"]))
        except subprocess.CalledProcessError:
            print("Could not pin the Build status issue; it is open and up to date. Pin it by hand once.")


def _actions(plan: Plan, gh: GitHub, now: datetime) -> list[tuple[str, Callable[[], None]]]:
    """Every action the plan asks for, in order, each with a plain description."""
    pulls: dict[int, dict[str, Any]] = {}

    def info(number: int) -> dict[str, Any]:
        if number not in pulls:
            pulls[number] = gh.pull(number)
        return pulls[number]

    steps: list[tuple[str, Callable[[], None]]] = []
    for r in plan.pulls:
        steps.append(
            (f"comment on #{r.number}", lambda r=r: upsert_comment(gh, r.number, render_pull(r, plan.mode)))
        )
        if r.rerun_ci:
            steps.append((f"CI re-run for #{r.number}", lambda r=r: rerun_once(gh, r.number, info(r.number))))
        if plan.mode == "sweep":
            steps.append((f"{HOLD_LABEL} on #{r.number}", lambda r=r: apply_hold(gh, r, info(r.number))))
    issues: list[list[dict[str, Any]]] = []

    def bot_issues() -> list[dict[str, Any]]:
        if not issues:
            issues.append(gh.bot_issues())
        return issues[0]

    for d in plan.decisions:
        steps.append((f"owner decision {d.key}", lambda d=d: upsert_decision(gh, d, bot_issues())))
    if plan.mode == "sweep":
        steps.append(
            (
                "Build status",
                lambda: upsert_status(
                    gh, plan, {n: info(n) for n in (r.number for r in plan.pulls)}, bot_issues(), now
                ),
            )
        )
    return steps


def act(
    raw: str,
    mode: str,
    expected: Sequence[int],
    gh: GitHub,
    *,
    now: datetime,
    key_present: bool = True,
    skip: str = "",
) -> int:
    """Do the allowed actions for a checked answer. Return 0 when every action worked.

    An answer that did not pass `validate` leads to one action only: on a pull-request run,
    the plain message as the overseer's comment. A sweep with no usable answer writes nothing.
    """
    plan: Plan | None = None
    if key_present and not skip:
        try:
            plan = validate(raw, mode, expected)
        except InvalidError:
            plan = None
    if plan is None:
        _, report = judge(raw, mode, expected, key_present=key_present, skip=skip)
        print(report)
        if mode == "pr":
            upsert_comment(gh, expected[0], report)
            return 0
        return 1
    failed = 0
    for what, step in _actions(plan, gh, now):
        try:
            step()
        except (subprocess.CalledProcessError, KeyError, TypeError, ValueError) as exc:
            failed += 1
            print(f"Could not do {what}: {type(exc).__name__}. The other actions went ahead.")
    return 1 if failed else 0


# --- Command line ------------------------------------------------------------------------------


def _settings() -> tuple[str, list[int]]:
    """Read and check the mode and the pull requests this run covers."""
    mode = os.environ.get("OVERSEER_MODE", "")
    if mode not in MODES:
        raise SystemExit(f"OVERSEER_MODE must be one of {', '.join(MODES)}.")
    raw = os.environ.get("OVERSEER_PR", "") if mode == "pr" else os.environ.get("OVERSEER_OPEN", "")
    parts = raw.split()
    if not all(p.isdigit() and int(p) > 0 for p in parts) or (mode == "pr" and len(parts) != 1):
        raise SystemExit("The pull request numbers for this run are not valid.")
    return mode, [int(p) for p in parts]


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command; see the module docstring."""
    parser = argparse.ArgumentParser(description="Check the build overseer's answer and act on it.")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("budget")
    b.add_argument("--config", type=Path, required=True)
    b.add_argument("--runs", type=Path, required=True)
    b.add_argument("--run-id", type=int, required=True)
    b.add_argument("--run-attempt", type=int, required=True)
    b.add_argument("--created-at", required=True)
    b.add_argument("--today", required=True)
    b.add_argument("--repo", default="", help="This repository, OWNER/NAME; a fork's runs then do not count.")
    j = sub.add_parser("judge")
    j.add_argument("report", type=Path)
    sub.add_parser("act")
    args = parser.parse_args(argv)

    if args.command == "budget":
        config = args.config.read_text(encoding="utf-8") if args.config.is_file() else None
        runs = json.loads(args.runs.read_text(encoding="utf-8"))
        allowed, message = budget(
            config,
            runs,
            args.run_id,
            args.run_attempt,
            created_at=args.created_at,
            today=args.today,
            repo=args.repo,
        )
        print(f"allowed={'true' if allowed else 'false'}")
        print(f"message={message}")
        if not allowed:
            print(f"skip={message}")
        return 0

    mode, expected = _settings()
    raw = os.environ.get("OVERSEER_RESULT", "")
    key_present = os.environ.get("OVERSEER_KEY_PRESENT", "true") == "true"
    skip = os.environ.get("OVERSEER_SKIP", "")
    if args.command == "judge":
        passed, report = judge(raw, mode, expected, key_present=key_present, skip=skip)
        args.report.write_text(report, encoding="utf-8")
        print(report)
        return 0 if passed else 1
    dry_run = os.environ.get("OVERSEER_DRY_RUN", "false") == "true"
    if dry_run:
        print("Dry run: nothing below is done; each line says what would be.")
    gh = GitHub(os.environ.get("REPO", ""), dry_run=dry_run)
    return act(raw, mode, expected, gh, now=datetime.now(UTC), key_present=key_present, skip=skip)


if __name__ == "__main__":
    sys.exit(main())
