"""Alerts and weekly summaries reach the owner as GitHub issues; plain code decides each change."""

import json
from typing import Any

import httpx
import pytest

from catalyst.issues import Close, Comment, Edit, Open, OpenIssue, marker, plan_actions, post

ALERT = {"key": "job-failed:collect", "title": "Collection needs a look", "body": "Body.", "escalate_at": 2}


def _plan(issues: list[dict[str, Any]], solved: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"mode": "alert", "label": "alert", "issues": issues, "solved": solved or []}


def test_a_new_alert_opens_an_issue_with_its_key() -> None:
    actions = plan_actions(_plan([ALERT]), [])
    assert actions == [
        Open("Collection needs a look", "Body.\n\n" + marker("job-failed:collect", 1), "alert")
    ]


def test_a_repeat_comments_on_the_open_issue_and_counts_the_runs() -> None:
    open_issue = OpenIssue(7, "Old body.\n\n" + marker("job-failed:collect", 1))
    actions = plan_actions(_plan([{**ALERT, "escalate_at": 3}]), [open_issue])
    assert actions == [
        Comment(7, "This happened again (2 runs in a row).\n\nBody."),
        Edit(7, "Old body.\n\n" + marker("job-failed:collect", 2)),
    ]


def test_the_second_failure_in_a_row_says_so_plainly() -> None:
    open_issue = OpenIssue(7, "Old body.\n\n" + marker("job-failed:collect", 1))
    comment = plan_actions(_plan([ALERT]), [open_issue])[0]
    assert isinstance(comment, Comment)
    assert comment.body.startswith("**This job has now failed 2 runs in a row.**")


def test_a_solved_alert_is_closed_with_a_comment_and_unknown_ones_are_ignored() -> None:
    open_issue = OpenIssue(7, "Old body.\n\n" + marker("job-failed:collect", 3))
    solved = [
        {"key": "job-failed:collect", "comment": "Solved."},
        {"key": "job-failed:other", "comment": "x"},
    ]
    assert plan_actions(_plan([], solved), [open_issue]) == [Close(7, "Solved.")]


def test_issues_without_our_marker_are_left_alone() -> None:
    assert (
        plan_actions(_plan([], [{"key": "job-failed:collect", "comment": "x"}]), [OpenIssue(3, "hi")]) == []
    )


def test_a_weekly_summary_replaces_its_own_week_and_closes_older_ones() -> None:
    week = {"key": "weekly-summary:2026-10-24", "title": "Weekly summary", "body": "New."}
    plan = {"mode": "weekly", "label": "weekly-summary", "issues": [week], "solved": []}
    same = OpenIssue(9, "Old.\n\n" + marker("weekly-summary:2026-10-24", 1))
    older = OpenIssue(4, "Older.\n\n" + marker("weekly-summary:2026-10-17", 1))
    assert plan_actions(plan, [same, older]) == [
        Edit(9, "New.\n\n" + marker("weekly-summary:2026-10-24", 1)),
        Close(4, None),
    ]
    first = plan_actions(plan, [older])
    assert first[0] == Open(
        "Weekly summary", "New.\n\n" + marker("weekly-summary:2026-10-24", 1), "weekly-summary"
    )


class FakeGitHub:
    """Answers the few GitHub REST calls the poster makes, and records them."""

    def __init__(self, open_issues: list[dict[str, Any]]) -> None:
        self.open_issues = open_issues
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        assert request.headers["authorization"] == "Bearer token-for-tests"
        if request.method == "GET":
            assert request.url.params["labels"] == "alert"
            assert request.url.params["state"] == "open"
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=self.open_issues if page == 1 else [])
        if request.url.path.endswith("/labels"):
            return httpx.Response(422, json={"message": "Validation Failed"})  # the label exists already
        return httpx.Response(201, json={})


def test_post_reads_open_issues_then_makes_each_change() -> None:
    server = FakeGitHub(
        [
            {"number": 7, "body": "B\n\n" + marker("job-failed:collect", 1)},
            {"number": 8, "body": None, "pull_request": {}},
        ]
    )
    client = httpx.Client(transport=httpx.MockTransport(server), base_url="https://api.github.com")
    solved = [{"key": "job-failed:collect", "comment": "Solved."}]
    post(
        _plan([{**ALERT, "key": "spend-cap:extractor", "escalate_at": None}], solved),
        client,
        "o/r",
        "token-for-tests",
    )
    writes = [(method, path) for method, path, _ in server.calls if method != "GET"]
    assert writes == [
        ("POST", "/repos/o/r/labels"),
        ("POST", "/repos/o/r/issues"),
        ("POST", "/repos/o/r/issues/7/comments"),
        ("PATCH", "/repos/o/r/issues/7"),
    ]
    assert server.calls[-1][2] == {"state": "closed", "state_reason": "completed"}
    assert server.calls[2][2]["labels"] == ["alert"]


def test_post_stops_on_a_github_error() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(403)), base_url="https://x")
    with pytest.raises(httpx.HTTPStatusError):
        post(_plan([ALERT]), client, "o/r", "token-for-tests")
