"""Renderer tests: branch flattening, artifact extraction, payload sizing."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from claude_migrate.render import (
    AttachmentPayload,
    InlinePayload,
    _flatten_branch,
    _render_message_body,
    _render_text_block,
    _render_thinking_block,
    _replace_artifacts,
    estimate_tokens,
    prepare_paste_payload,
    render_transcript,
)
from claude_migrate.store import (
    upsert_conversation,
    upsert_message,
    upsert_project,
    upsert_project_doc,
    write_raw,
)

# ---------------------------------------------------------------------------
# Branch flattening
# ---------------------------------------------------------------------------


def _msg(uuid: str, parent: str | None, sender: str = "human", text: str = "") -> dict[str, Any]:
    return {"uuid": uuid, "parent_message_uuid": parent, "sender": sender, "text": text}


def test_flatten_main_branch() -> None:
    msgs = [
        _msg("a", None, "human", "1"),
        _msg("b", "a", "assistant", "2"),
        _msg("c", "b", "human", "3"),
        _msg("d", "c", "assistant", "4"),
    ]
    chain = _flatten_branch(msgs, leaf_uuid="d")
    assert [m["uuid"] for m in chain] == ["a", "b", "c", "d"]


def test_flatten_with_branch_picks_leaf() -> None:
    msgs = [
        _msg("a", None),
        _msg("b", "a", "assistant"),
        _msg("c1", "b", "human"),  # alternate branch
        _msg("c2", "b", "human"),  # main
        _msg("d", "c2", "assistant"),
    ]
    chain = _flatten_branch(msgs, leaf_uuid="d")
    assert [m["uuid"] for m in chain] == ["a", "b", "c2", "d"]


def test_flatten_missing_leaf_falls_back_to_deepest() -> None:
    msgs = [
        _msg("a", None),
        _msg("b", "a", "assistant"),
        _msg("c", "b", "human"),
    ]
    chain = _flatten_branch(msgs, leaf_uuid=None)
    assert [m["uuid"] for m in chain] == ["a", "b", "c"]


def test_flatten_self_cycle_does_not_loop() -> None:
    msgs = [_msg("a", "a")]
    chain = _flatten_branch(msgs, leaf_uuid="a")
    assert [m["uuid"] for m in chain] == ["a"]


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def test_artifact_extraction() -> None:
    raw = (
        "Here is a plan.\n"
        '<antArtifact identifier="x" type="text/markdown" title="Plan">\n'
        "# Plan\n1. Step\n"
        "</antArtifact>\n"
        "Done."
    )
    out = _replace_artifacts(raw)
    assert "<artifact " in out
    assert 'identifier="x"' in out
    assert 'type="text/markdown"' in out
    assert "# Plan" in out
    assert "<antArtifact" not in out


# ---------------------------------------------------------------------------
# Token estimator
# ---------------------------------------------------------------------------


def test_estimate_tokens_zero_floor() -> None:
    assert estimate_tokens("") == 1


def test_estimate_tokens_proportional() -> None:
    assert estimate_tokens("a" * 4000) >= 1000


# ---------------------------------------------------------------------------
# End-to-end render via SQLite
# ---------------------------------------------------------------------------


def test_render_transcript_minimal(db_conn: sqlite3.Connection, tmp_data_dir: Path) -> None:
    conv = {
        "uuid": "conv-1",
        "name": "Test conversation",
        "created_at": "2024-01-02T03:04:05Z",
        "updated_at": "2024-01-02T03:04:05Z",
        "model": "claude-sonnet-4-5",
        "current_leaf_message_uuid": "msg-2",
        "chat_messages": [
            {
                "uuid": "msg-1",
                "parent_message_uuid": None,
                "sender": "human",
                "content": [{"type": "text", "text": "hello"}],
            },
            {
                "uuid": "msg-2",
                "parent_message_uuid": "msg-1",
                "sender": "assistant",
                "content": [{"type": "text", "text": "hi back"}],
            },
        ],
    }
    raw_path = write_raw("conv-1", conv)
    upsert_conversation(db_conn, "org-1", conv, raw_path=str(raw_path))
    for i, m in enumerate(conv["chat_messages"]):
        m["index"] = i
        upsert_message(db_conn, "conv-1", m)

    out = render_transcript(db_conn, "conv-1")
    assert "<prior_conversation>" in out
    assert "<original_title>Test conversation</original_title>" in out
    assert "hello" in out
    assert "hi back" in out
    assert "READY" in out


def test_render_with_artifact(db_conn: sqlite3.Connection, tmp_data_dir: Path) -> None:
    text = (
        "<antArtifact identifier=\"plan\" type=\"text/markdown\" title=\"P\">\n"
        "# Plan\n</antArtifact>"
    )
    conv = {
        "uuid": "conv-2",
        "name": "Artifact",
        "current_leaf_message_uuid": "m1",
        "chat_messages": [
            {
                "uuid": "m1",
                "parent_message_uuid": None,
                "sender": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        ],
    }
    raw_path = write_raw("conv-2", conv)
    upsert_conversation(db_conn, "org-1", conv, raw_path=str(raw_path))
    upsert_message(db_conn, "conv-2", conv["chat_messages"][0] | {"index": 0})

    out = render_transcript(db_conn, "conv-2")
    assert "<artifact " in out
    assert "<antArtifact" not in out
    assert "# Plan" in out


def test_payload_choice_inline(db_conn: sqlite3.Connection, tmp_data_dir: Path) -> None:
    conv = {
        "uuid": "conv-3",
        "name": "Short",
        "current_leaf_message_uuid": "m1",
        "chat_messages": [
            {
                "uuid": "m1",
                "parent_message_uuid": None,
                "sender": "human",
                "content": [{"type": "text", "text": "x" * 100}],
            },
        ],
    }
    raw_path = write_raw("conv-3", conv)
    upsert_conversation(db_conn, "org-1", conv, raw_path=str(raw_path))
    upsert_message(db_conn, "conv-3", conv["chat_messages"][0] | {"index": 0})
    payload = prepare_paste_payload(db_conn, "conv-3")
    assert isinstance(payload, InlinePayload)


def test_payload_choice_attachment(db_conn: sqlite3.Connection, tmp_data_dir: Path) -> None:
    long_text = "word " * 80_000  # ~400K chars → ~100K tokens
    conv = {
        "uuid": "conv-4",
        "name": "Long",
        "current_leaf_message_uuid": "m1",
        "chat_messages": [
            {
                "uuid": "m1",
                "parent_message_uuid": None,
                "sender": "human",
                "content": [{"type": "text", "text": long_text}],
            },
        ],
    }
    raw_path = write_raw("conv-4", conv)
    upsert_conversation(db_conn, "org-1", conv, raw_path=str(raw_path))
    upsert_message(db_conn, "conv-4", conv["chat_messages"][0] | {"index": 0})
    payload = prepare_paste_payload(db_conn, "conv-4")
    assert isinstance(payload, AttachmentPayload)
    assert payload.file_name.endswith(".md")


# ---------------------------------------------------------------------------
# rendering_mode=messages — structured blocks
# ---------------------------------------------------------------------------


def test_thinking_block_uses_summary_not_raw() -> None:
    """When summaries[0].summary is present, render that — NOT the raw thinking.
    The UI shows the summary line ('Synthesized X'); raw thinking is verbose
    noise that inflates the migrated paste size and reads as out-of-character
    on the new account."""
    block = {
        "type": "thinking",
        "thinking": "OK so the user is asking about Postgres — let me think... lots of stuff here, this would be 500+ chars normally",
        "summaries": [{"summary": "Investigated Postgres migration approaches."}],
    }
    out = _render_thinking_block(block)
    assert "Investigated Postgres migration approaches." in out
    assert "OK so the user is asking" not in out, "raw thinking must NOT leak through"


def test_thinking_block_falls_back_to_truncated_raw_when_no_summary() -> None:
    block = {"type": "thinking", "thinking": "raw thought" * 200, "summaries": []}
    out = _render_thinking_block(block)
    assert "<thinking>" in out
    assert len(out) < 1500, "must be truncated, not the full 2000+ char body"


def test_thinking_block_with_no_thinking_returns_empty() -> None:
    assert _render_thinking_block({"type": "thinking", "thinking": "", "summaries": []}) == ""


def test_unsupported_block_placeholder_collapsed_to_marker() -> None:
    """claude.ai's `messages` mode flattens tool calls into text blocks
    containing only this placeholder string. Strip it from the rendered
    transcript and emit one clean tool_use marker so downstream Claude sees
    the FACT that a tool ran without the noise."""
    block = {
        "type": "text",
        "text": "\n```\nThis block is not supported on your current device yet.\n```\n",
    }
    out = _render_text_block(block)
    assert "This block is not supported" not in out
    assert "tool_use" in out


def test_consecutive_unsupported_placeholders_collapse_to_one() -> None:
    """A turn full of tool calls becomes one marker, not ten."""
    block = {
        "type": "text",
        "text": (
            "\n```\nThis block is not supported on your current device yet.\n```\n"
            "\n```\nThis block is not supported on your current device yet.\n```\n"
            "\n```\nThis block is not supported on your current device yet.\n```\n"
        ),
    }
    out = _render_text_block(block)
    assert out.count("tool_use") == 1


def test_text_block_passes_normal_content_through() -> None:
    block = {"type": "text", "text": "Short answer: yes — and here's why."}
    out = _render_text_block(block)
    assert "Short answer: yes — and here's why." in out


def test_text_block_renders_citations_when_populated() -> None:
    block = {
        "type": "text",
        "text": "Postmark has 93.8% delivery rate.",
        "citations": [
            {"url": "https://example.com/test", "title": "EmailToolTester"},
            {"url": "https://example.com/post", "title": "Postmark"},
        ],
    }
    out = _render_text_block(block)
    assert "Postmark has 93.8%" in out
    assert "[EmailToolTester](https://example.com/test)" in out
    assert "_sources:" in out


def test_files_rendered_as_self_closing_tags() -> None:
    msg = {
        "content": [{"type": "text", "text": "Here is a screenshot."}],
        "files": [
            {"file_name": "screenshot.png", "file_kind": "image", "file_size": 12345}
        ],
    }
    body = _render_message_body(msg)
    assert "<file " in body
    assert 'name="screenshot.png"' in body
    assert 'kind="image"' in body
    assert 'size="12345"' in body


def test_per_turn_timestamps_in_render(db_conn: sqlite3.Connection, tmp_data_dir: Path) -> None:
    conv = {
        "uuid": "conv-ts",
        "name": "Timestamp test",
        "current_leaf_message_uuid": "m1",
        "chat_messages": [
            {
                "uuid": "m1",
                "parent_message_uuid": None,
                "sender": "human",
                "created_at": "2026-04-30T17:56:23.114947Z",
                "content": [{"type": "text", "text": "hello"}],
            },
        ],
    }
    raw_path = write_raw("conv-ts", conv)
    upsert_conversation(db_conn, "org-1", conv, raw_path=str(raw_path))
    upsert_message(db_conn, "conv-ts", conv["chat_messages"][0] | {"index": 0})
    out = render_transcript(db_conn, "conv-ts")
    assert 'timestamp="2026-04-30T17:56:23.114947Z"' in out


def test_project_context_inlined_when_conversation_in_project(
    db_conn: sqlite3.Connection, tmp_data_dir: Path
) -> None:
    """Conversations from a project must carry the project's system prompt and
    knowledge file names into the migrated transcript so the target Claude has
    the same situational context as the source had."""
    upsert_project(db_conn, "org-1", {
        "uuid": "p-1",
        "name": "Postgres migration project",
        "prompt_template": "You are a database migration expert. Only respond in concise bullet lists.",
    })
    upsert_project_doc(db_conn, "p-1", {
        "uuid": "doc-1",
        "file_name": "schema.sql",
        "content": "CREATE TABLE users (id INT);",
    })
    conv = {
        "uuid": "conv-proj",
        "name": "Working chat",
        "project_uuid": "p-1",
        "current_leaf_message_uuid": "m1",
        "chat_messages": [
            {
                "uuid": "m1",
                "parent_message_uuid": None,
                "sender": "human",
                "content": [{"type": "text", "text": "What schema changes do I need?"}],
            },
        ],
    }
    raw_path = write_raw("conv-proj", conv)
    upsert_conversation(db_conn, "org-1", conv, raw_path=str(raw_path))
    upsert_message(db_conn, "conv-proj", conv["chat_messages"][0] | {"index": 0})
    out = render_transcript(db_conn, "conv-proj")
    assert "<original_project>" in out
    assert "Postgres migration project" in out
    assert "database migration expert" in out
    assert "schema.sql" in out


def test_settings_flags_in_metadata(db_conn: sqlite3.Connection, tmp_data_dir: Path) -> None:
    """If the source conversation had web_search / extended thinking / artifacts
    enabled, that's worth recording in the metadata so the target Claude knows."""
    conv = {
        "uuid": "conv-set",
        "name": "Settings test",
        "current_leaf_message_uuid": "m1",
        "settings": {
            "enabled_web_search": True,
            "preview_feature_uses_artifacts": True,
            "paprika_mode": "extended",
        },
        "chat_messages": [
            {
                "uuid": "m1",
                "parent_message_uuid": None,
                "sender": "human",
                "content": [{"type": "text", "text": "x"}],
            },
        ],
    }
    raw_path = write_raw("conv-set", conv)
    upsert_conversation(db_conn, "org-1", conv, raw_path=str(raw_path))
    upsert_message(db_conn, "conv-set", conv["chat_messages"][0] | {"index": 0})
    out = render_transcript(db_conn, "conv-set")
    assert "<original_settings>" in out
    assert "enabled_web_search" in out
    assert "preview_feature_uses_artifacts" in out
