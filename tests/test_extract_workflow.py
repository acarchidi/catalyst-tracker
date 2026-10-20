"""The extract workflow never prints the export or unchecked results in plain text (ADR 0034, decision 9).

The repository is public since 2026-10-20, so its job logs are public. These checks read the
workflow file, the way the overseer tests read theirs.
"""

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "extract-live.yml"


def text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def step(name: str) -> str:
    """Return one step's lines, from its name to the next step."""
    body = text()
    start = body.index(f"      - name: {name}\n")
    following = body.find("\n      - name: ", start + 1)
    return body[start : following if following != -1 else len(body)]


def test_no_plain_copy_of_a_data_file_reaches_the_log() -> None:
    body = text()
    for name in ("export.json", "catalysts.json"):
        assert not re.search(rf"\bcat\b[^|\n]*{re.escape(name)}", body)
        assert not re.search(rf"\btee\b[^|\n]*{re.escape(name)}", body)
    assert "upload-artifact" not in body


def test_privacy_is_no_longer_a_switch() -> None:
    # Printing used to depend on the repository being private; now nothing plain is printed at all.
    assert "REPOSITORY_PRIVATE" not in text()
    assert "repository.private" not in text()


def test_files_leave_only_sealed_to_the_input_key() -> None:
    body = text()
    assert body.count('uv run catalyst seal --to "$PUBLIC_KEY" --in "$RUNNER_TEMP/export.json"') == 1
    assert body.count('uv run catalyst seal --to "$PUBLIC_KEY" --in "$RUNNER_TEMP/catalysts.json"') == 1
    # The key reaches the script through the environment, never pasted into it.
    assert body.count("PUBLIC_KEY: ${{ inputs.public_key }}") == 2
    assert body.count("inputs.public_key") == 2


def test_the_export_needs_a_key_before_it_reads_anything() -> None:
    export = step("Export published and held catalysts")
    assert "set -euo pipefail" in export
    check = export.index('if [ -z "$PUBLIC_KEY" ]')
    assert check < export.index("export-catalysts")
    assert "exit 1" in export[check : export.index("export-catalysts")]


def test_the_export_reads_paragraphs_from_the_live_store() -> None:
    export = step("Export published and held catalysts")
    assert "--snapshot-store supabase" in export
    assert "SUPABASE_URL: ${{ secrets.SUPABASE_URL }}" in export
    assert "SUPABASE_SECRET_KEY: ${{ secrets.SUPABASE_SECRET_KEY }}" in export
