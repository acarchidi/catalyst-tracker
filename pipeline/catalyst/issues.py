"""Post alert and weekly-summary plans as GitHub issues (ADR 0023). Plain code; no model posts.

Each issue opened here ends with a hidden marker that names its key and how many runs in a row
raised it. A repeat comments on the open issue and counts up; a solved alert is closed with a
comment; a weekly summary edits its own week's issue and closes older ones. Issues without a
marker are never touched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

API = "https://api.github.com"
_MARKER = re.compile(r"<!-- catalyst-issue key=(\S+) runs=(\d+) -->")
_PAGE = 100
_LABEL_EXISTS = 422  # GitHub answers 422 when asked to create a label that exists
_LABELS = {
    "alert": ("d73a4a", "Something in the pipeline needs the owner (ADR 0023)"),
    "weekly-summary": ("0e8a16", "The weekly summary for the owner (ADR 0023)"),
}


def marker(key: str, runs: int) -> str:
    """Return the hidden line that ties an issue to its alert key."""
    return f"<!-- catalyst-issue key={key} runs={runs} -->"


@dataclass(frozen=True)
class OpenIssue:
    """An open issue as GitHub lists it."""

    number: int
    body: str


@dataclass(frozen=True)
class Open:
    """Open a new issue."""

    title: str
    body: str
    label: str


@dataclass(frozen=True)
class Comment:
    """Comment on an open issue."""

    number: int
    body: str


@dataclass(frozen=True)
class Edit:
    """Replace an open issue's body."""

    number: int
    body: str


@dataclass(frozen=True)
class Close:
    """Close an issue, with a comment first when there is one."""

    number: int
    comment: str | None


Action = Open | Comment | Edit | Close


def _keyed(issues: list[OpenIssue]) -> dict[str, tuple[OpenIssue, int]]:
    found: dict[str, tuple[OpenIssue, int]] = {}
    for issue in issues:
        if match := _MARKER.search(issue.body):
            found.setdefault(match[1], (issue, int(match[2])))
    return found


def _repeat(issue: dict[str, Any], open_issue: OpenIssue, runs: int) -> list[Action]:
    runs += 1
    escalate_at = issue.get("escalate_at")
    if isinstance(escalate_at, int) and runs >= escalate_at:
        head = (
            f"**This job has now failed {runs} runs in a row.** "
            "A problem that comes back usually needs a fix, not another run."
        )
    else:
        head = f"This happened again ({runs} runs in a row)."
    body = _MARKER.sub(marker(issue["key"], runs), open_issue.body)
    return [Comment(open_issue.number, f"{head}\n\n{issue['body']}"), Edit(open_issue.number, body)]


def _weekly(plan: dict[str, Any], existing: dict[str, tuple[OpenIssue, int]]) -> list[Action]:
    actions: list[Action] = []
    keys = {issue["key"] for issue in plan["issues"]}
    for issue in plan["issues"]:
        body = f"{issue['body']}\n\n{marker(issue['key'], 1)}"
        found = existing.get(issue["key"])
        actions.append(Edit(found[0].number, body) if found else Open(issue["title"], body, plan["label"]))
    actions += [Close(issue.number, None) for key, (issue, _) in existing.items() if key not in keys]
    return actions


def plan_actions(plan: dict[str, Any], open_issues: list[OpenIssue]) -> list[Action]:
    """Return the changes that bring the open issues with the plan's label in line with the plan."""
    existing = _keyed(open_issues)
    if plan["mode"] == "weekly":
        return _weekly(plan, existing)
    actions: list[Action] = []
    for issue in plan["issues"]:
        found = existing.get(issue["key"])
        if found is None:
            body = f"{issue['body']}\n\n{marker(issue['key'], 1)}"
            actions.append(Open(issue["title"], body, plan["label"]))
        else:
            actions += _repeat(issue, *found)
    for solved in plan["solved"]:
        if found := existing.get(solved["key"]):
            actions.append(Close(found[0].number, solved["comment"]))
    return actions


def _open_issues(client: httpx.Client, repo: str, label: str, headers: dict[str, str]) -> list[OpenIssue]:
    issues: list[OpenIssue] = []
    page = 1
    while True:
        params = {"labels": label, "state": "open", "per_page": _PAGE, "page": page}
        response = client.get(f"/repos/{repo}/issues", params=params, headers=headers)
        response.raise_for_status()
        listed: list[dict[str, Any]] = response.json()
        # The issues list includes pull requests; those are never ours.
        issues += [OpenIssue(i["number"], i.get("body") or "") for i in listed if "pull_request" not in i]
        if len(listed) < _PAGE:
            return issues
        page += 1


def _ensure_label(client: httpx.Client, repo: str, label: str, headers: dict[str, str]) -> None:
    color, description = _LABELS.get(label, ("ededed", ""))
    payload = {"name": label, "color": color, "description": description}
    response = client.post(f"/repos/{repo}/labels", json=payload, headers=headers)
    if response.status_code != _LABEL_EXISTS:
        response.raise_for_status()


def _apply(client: httpx.Client, repo: str, action: Action, headers: dict[str, str]) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []
    match action:
        case Open(title, body, label):
            calls.append(("POST", "issues", {"title": title, "body": body, "labels": [label]}))
        case Comment(number, body):
            calls.append(("POST", f"issues/{number}/comments", {"body": body}))
        case Edit(number, body):
            calls.append(("PATCH", f"issues/{number}", {"body": body}))
        case Close(number, comment):
            if comment:
                calls.append(("POST", f"issues/{number}/comments", {"body": comment}))
            calls.append(("PATCH", f"issues/{number}", {"state": "closed", "state_reason": "completed"}))
    for method, path, payload in calls:
        client.request(method, f"/repos/{repo}/{path}", json=payload, headers=headers).raise_for_status()


def post(plan: dict[str, Any], client: httpx.Client, repo: str, token: str) -> list[Action]:
    """Read the open issues with the plan's label, then make each change; return what was done."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    actions = plan_actions(plan, _open_issues(client, repo, plan["label"], headers))
    if any(isinstance(action, Open) for action in actions):
        _ensure_label(client, repo, plan["label"], headers)
    for action in actions:
        _apply(client, repo, action, headers)
    return actions


def plan_markdown(plan: dict[str, Any]) -> str:
    """Return the plan's issue bodies for the run's job summary."""
    if not plan["issues"]:
        return "No alerts: nothing needs the owner.\n"
    return "".join(f"## {issue['title']}\n\n{issue['body']}\n\n" for issue in plan["issues"])
