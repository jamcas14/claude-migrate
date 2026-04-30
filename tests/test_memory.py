"""Memory module: prompt structure + import instructions."""

from __future__ import annotations

import pytest

from claude_migrate.memory import (
    EXTRACTION_PROMPT,
    IMPORT_INSTRUCTIONS,
    prepare,
)


def test_extraction_prompt_has_required_sections() -> None:
    for section in ("# Identity", "# Projects", "# Decisions", "# People"):
        assert section in EXTRACTION_PROMPT


def test_extraction_prompt_ends_with_marker() -> None:
    assert "END OF MEMORY EXPORT" in EXTRACTION_PROMPT


def test_import_instructions_link_to_official() -> None:
    assert "claude.com/import-memory" in IMPORT_INSTRUCTIONS
    assert "{prompt}" in IMPORT_INSTRUCTIONS


def test_prepare_streams_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    prepare(copy=False, stream=True)
    captured = capsys.readouterr()
    assert "STEP 1" in captured.out
    assert "STEP 2" in captured.out
    assert "claude.com/import-memory" in captured.out
