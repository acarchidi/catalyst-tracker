#!/usr/bin/env python3
"""Report which owner-only setup steps are still missing.

Run at the start of every Claude Code session (see CLAUDE.md):

    python3 scripts/check_setup.py            # checks that settings exist
    python3 scripts/check_setup.py --online   # also checks that each key works

Uses only the Python standard library, so it runs before any dependency is
installed. It never prints secret values. Exit code 1 means a required setting
is missing; the output names the step in docs/setup.md that fixes it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class Requirement:
    """One environment variable the project needs, and the setup step that provides it."""

    name: str
    step: str
    purpose: str


REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement("CATALYST_ANTHROPIC_API_KEY", "Part 2, then Part 4", "model calls by the product's agents"),
    Requirement("SUPABASE_URL", "Part 3, then Part 4", "database and storage address"),
    Requirement("SUPABASE_SECRET_KEY", "Part 3, then Part 4", "server-side database access"),
    Requirement("SUPABASE_DB_PASSWORD", "Part 3, then Part 4", "direct Postgres connection for migrations"),
    Requirement("SEC_CONTACT_EMAIL", "Part 4", "contact address the SEC requires on every request"),
)

TIMEOUT_SECONDS = 15


def _get(url: str, headers: dict[str, str]) -> int:
    """Return the HTTP status of a GET request, or 0 if the host is unreachable."""
    # Only fixed https endpoints and the owner's SUPABASE_URL reach this function.
    request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0


def _describe(status: int) -> str:
    """Turn an HTTP status into a short verdict."""
    if status == 0:
        return "UNREACHABLE (check the environment's allowed domains, docs/setup.md Part 4)"
    if 200 <= status < 300:
        return "works"
    if status in (401, 403):
        return f"REJECTED (HTTP {status}): the key is wrong or revoked"
    return f"unexpected HTTP {status}"


def online_checks(env: dict[str, str]) -> list[tuple[str, str]]:
    """Call each service once with the configured key and report the result."""
    results: list[tuple[str, str]] = []
    if env.get("CATALYST_ANTHROPIC_API_KEY"):
        status = _get(
            "https://api.anthropic.com/v1/models",
            {"x-api-key": env["CATALYST_ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
        )
        results.append(("Anthropic API key", _describe(status)))
    if env.get("SUPABASE_URL") and env.get("SUPABASE_SECRET_KEY"):
        status = _get(
            env["SUPABASE_URL"].rstrip("/") + "/rest/v1/",
            {"apikey": env["SUPABASE_SECRET_KEY"]},
        )
        results.append(("Supabase URL and key", _describe(status)))
    if env.get("SEC_CONTACT_EMAIL"):
        status = _get(
            "https://www.sec.gov/files/company_tickers.json",
            {"User-Agent": f"catalyst-tracker {env['SEC_CONTACT_EMAIL']}"},
        )
        results.append(("SEC EDGAR access", _describe(status)))
    return results


def github_secret_names() -> set[str] | None:
    """Return the repository's GitHub Actions secret names, or None if they cannot be listed."""
    if shutil.which("gh") is None:
        return None
    try:
        completed = subprocess.run(
            ["gh", "secret", "list", "--json", "name"],  # noqa: S607 - gh is found on PATH by design
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        return {item["name"] for item in json.loads(completed.stdout)}
    except (ValueError, KeyError, TypeError):
        return None


def main() -> int:
    """Print the setup report and return 1 if a required setting is missing."""
    parser = argparse.ArgumentParser(description=(__doc__ or "Setup check").splitlines()[0])
    parser.add_argument("--online", action="store_true", help="also check that each key works")
    args = parser.parse_args()

    env = {req.name: os.environ.get(req.name, "") for req in REQUIREMENTS}
    missing = [req for req in REQUIREMENTS if not env[req.name]]

    print("Setup check (docs/setup.md has the steps)\n")
    print("Session environment variables:")
    for req in REQUIREMENTS:
        mark = "ok     " if env[req.name] else "MISSING"
        print(f"  [{mark}] {req.name:<28} {req.purpose}  ->  {req.step}")

    secrets = github_secret_names()
    print("\nGitHub Actions secrets (needed for scheduled jobs, Part 5):")
    if secrets is None:
        print(
            "  [unknown] could not list them from here; Claude Code will try `gh secret set`, or see Part 5"
        )
    else:
        for req in REQUIREMENTS:
            mark = "ok     " if req.name in secrets else "MISSING"
            print(f"  [{mark}] {req.name}")

    if args.online:
        print("\nLive checks:")
        for label, verdict in online_checks(env):
            print(f"  {label:<24} {verdict}")

    if missing:
        first = missing[0]
        print(f"\nNEXT STEP FOR THE OWNER: {first.step} in docs/setup.md (provides {first.name}).")
        return 1
    print("\nAll session settings are present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
