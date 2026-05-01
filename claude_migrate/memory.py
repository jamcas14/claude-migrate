"""Prepare paste-ready text for Anthropic's official memory import flow.

Memory cannot be transferred programmatically — Anthropic gates it behind the
official `claude.com/import-memory` flow. What this module does:
  1. Print a canonical extraction prompt the user pastes into the SOURCE
     account to get its memory contents.
  2. Print clear instructions for taking that output to claude.com/import-memory
     in the destination account.

`memory open` is a sanity-check stub that opens the import-memory URL
and reminds the user what to expect.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import webbrowser
from textwrap import dedent

EXTRACTION_PROMPT = dedent(
    """\
    Please dump my long-term memory in a structured form so I can import it
    into a different Claude.ai account. Use this exact format:

      # Identity
      - role / occupation / domains
      - communication preferences

      # Projects
      - one bullet per active project, with status

      # Decisions and constraints
      - durable preferences / things to remember

      # People
      - names, relationships, only if you've stored them

    Only include facts that are actually in your memory of me — don't make
    things up to fill sections, and skip any section that's empty. After the
    dump, end with a single line: "END OF MEMORY EXPORT".
    """
)


IMPORT_INSTRUCTIONS = dedent(
    """\
    ────────────────────────────────────────────────────────────────
    STEP 1 — Run the extraction in your SOURCE account
    ────────────────────────────────────────────────────────────────

    1. Open https://claude.ai signed in to your SOURCE account.
    2. Start a new chat.
    3. Paste the prompt below. Copy Claude's full response.

    --- BEGIN EXTRACTION PROMPT ---
    {prompt}
    --- END EXTRACTION PROMPT ---

    ────────────────────────────────────────────────────────────────
    STEP 2 — Import in your DESTINATION account
    ────────────────────────────────────────────────────────────────

    1. Open https://claude.com/import-memory while signed in to the
       DESTINATION account.
    2. Paste the response you copied in Step 1.
    3. Click "Import memory" and follow the prompts to confirm.

    Note: Anthropic's importer may rephrase or condense your text — that's
    expected, the important content gets stored. Run `claude-migrate memory
    open` afterward to bring up the import page in your browser.
    """
)


def _try_clipboard(text: str) -> bool:
    """Best-effort copy to the OS clipboard. Returns True if any tool succeeded."""
    candidates = [
        ("pbcopy",),                # macOS
        ("xclip", "-selection", "clipboard"),
        ("xsel", "--clipboard", "--input"),
        ("wl-copy",),               # wayland
        ("clip.exe",),              # Windows in WSL
    ]
    for cmd in candidates:
        if shutil.which(cmd[0]) is None:
            continue
        try:
            proc = subprocess.run(
                cmd, input=text, text=True, check=False, timeout=5
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return True
    return False


def prepare(*, copy: bool = True, stream: bool = True) -> None:
    """Print the prompt + instructions; optionally copy to clipboard."""
    if stream:
        print(IMPORT_INSTRUCTIONS.format(prompt=EXTRACTION_PROMPT.rstrip()))
    if copy and _try_clipboard(EXTRACTION_PROMPT):
        print("\n  ✓ Extraction prompt copied to clipboard.\n", file=sys.stderr)
    elif copy:
        print(
            "\n  (Could not access clipboard — copy the prompt above by hand.)\n",
            file=sys.stderr,
        )


def verify_open() -> None:
    """Open the destination account's memory page so the user can eyeball it."""
    url = "https://claude.com/import-memory"
    print(f"Opening {url} in your browser. Confirm the imported text matches "
          "what you pasted, then close the tab.")
    try:
        webbrowser.open(url)
    except webbrowser.Error:
        print("Could not auto-launch a browser. Open the URL above manually.")
