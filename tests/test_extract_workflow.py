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
    for name in ("export.json", "catalysts.json", "estimates-sample.json"):
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
    seal = 'uv run catalyst seal --to "$PUBLIC_KEY" --in "$RUNNER_TEMP/estimates-sample.json"'
    assert body.count(seal) == 1
    # The key reaches the script through the environment, never pasted into it.
    assert body.count("PUBLIC_KEY: ${{ inputs.public_key }}") == 3
    assert body.count("inputs.public_key") == 3


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


SAMPLE = "Preview estimated dates, writing nothing"


def test_the_estimates_sample_is_a_mode_of_its_own() -> None:
    body = text()
    assert "options: [extract, evaluate, export, estimates-sample]" in body
    assert "if: ${{ inputs.mode == 'estimates-sample' }}" in step(SAMPLE)
    # It writes nothing, so it does not bring the live database up to date either.
    migrate = step("Bring the live database up to date")
    assert "estimates-sample" not in migrate


def test_the_estimates_sample_is_a_dry_run_that_needs_a_key_first() -> None:
    sample = step(SAMPLE)
    assert "set -euo pipefail" in sample
    check = sample.index('if [ -z "$PUBLIC_KEY" ]')
    run = sample.index("uv run catalyst publish-estimates")
    assert check < run
    assert "exit 1" in sample[check:run]
    command = sample[run : sample.index("\n", sample.index("--sample-out", run))]
    assert "--dry-run" in command
    assert "--snapshot-store supabase" in command
    assert '--sample-out "$RUNNER_TEMP/estimates-sample.json"' in command
    # A repeatable sample of 400 trials, stopped cleanly after 20 minutes, inside the step's 30.
    assert "--sample 400 --max-minutes 20" in command
    assert "timeout-minutes: 30" in sample


def test_the_estimates_sample_prints_counts_and_seals_the_rows_like_the_export() -> None:
    sample = step(SAMPLE)
    # Only the counts reach the summary; the rows file is sealed first and only the sealed text printed.
    assert '> "$RUNNER_TEMP/estimates-counts.json"' in sample
    assert 'cat "$RUNNER_TEMP/estimates-counts.json"' in sample
    seal = 'uv run catalyst seal --to "$PUBLIC_KEY" --in "$RUNNER_TEMP/estimates-sample.json"'
    assert f'{seal} > "$RUNNER_TEMP/estimates-sample.sealed"' in sample
    assert sample.index(seal) < sample.index('cat "$RUNNER_TEMP/estimates-sample.sealed"')
    assert sample.count("estimates-sample.json") == 2  # written by the dry run, read only by seal
    for name in ("SUPABASE_DB_URL", "SUPABASE_DB_PASSWORD", "SUPABASE_URL", "SUPABASE_SECRET_KEY"):
        assert f"{name}: ${{{{ secrets.{name} }}}}" in sample
    assert "CATALYST_ANTHROPIC_API_KEY" not in sample  # no model call
