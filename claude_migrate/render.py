"""Transcript renderer: stored conversation → XML user-message body per Section 10.

Picks one of three payload variants based on token estimate. The `send_payload`
function in `transport.py` knows how to put each variant on the wire — that
keeps the wire format next to the chunking / sizing strategy that produced it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import escape as xml_escape

ARTIFACT_RE = re.compile(
    r"<antArtifact\s+([^>]*?)>(.*?)</antArtifact>",
    re.DOTALL | re.IGNORECASE,
)
ATTR_RE = re.compile(r"""(\w[\w-]*)\s*=\s*"([^"]*)"|(\w[\w-]*)\s*=\s*'([^']*)'""")

INLINE_TOKEN_BUDGET = 80_000
ATTACHMENT_TOKEN_BUDGET = 150_000

CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
THINKING_LIMIT = 2000
THINKING_TRUNC_TO = 500
TOOL_RESULT_LIMIT = 500

# claude.ai serializes tool calls (web_search, web_fetch, artifact create, REPL,
# etc.) as a literal `text` block containing this exact placeholder string. The
# `messages` rendering mode strips the structured tool_use/tool_result blocks
# before returning, leaving only this. We strip it from text rendering and emit
# a single clean marker so the migrated transcript stays readable.
UNSUPPORTED_BLOCK_RE = re.compile(
    r"\s*```\s*\n?This block is not supported on your current device yet\.\s*\n?```\s*",
    re.IGNORECASE,
)


def estimate_tokens(text: str) -> int:
    """Rough char/4 estimate. Good enough for choosing inline vs attachment."""
    return max(1, len(text) // 4)


def _scrub(text: str) -> str:
    return CONTROL_RE.sub("", text)


def _parse_attrs(blob: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in ATTR_RE.finditer(blob):
        if m.group(1) is not None:
            out[m.group(1)] = m.group(2)
        else:
            out[m.group(3)] = m.group(4)
    return out


def _replace_artifacts(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        attrs = _parse_attrs(m.group(1))
        body = m.group(2)
        attr_str = " ".join(f'{k}="{xml_escape(v)}"' for k, v in attrs.items())
        return f"<artifact {attr_str}>\n{body.strip()}\n</artifact>"

    return ARTIFACT_RE.sub(repl, text)


def _render_text_block(block: dict[str, Any]) -> str:
    """A `text` block in `messages` rendering mode.

    Two behaviours: (1) strip the literal "This block is not supported..."
    placeholder claude.ai inserts where a tool_use/tool_result block was — emit
    a single `<tool_use name="(stripped)" />` marker so the migrated transcript
    preserves the FACT that a tool ran (web_search, fetch, artifact, REPL) even
    though the API doesn't expose the input/output. (2) inline citation badges
    if the API ever populates them — defensive; current claude.ai serves them
    empty in `messages` mode but the field is on every text block.
    """
    text = _scrub(block.get("text") or "")
    text = UNSUPPORTED_BLOCK_RE.sub(
        '\n<tool_use name="(stripped — claude.ai API does not expose tool calls)" />\n',
        text,
    )
    text = _replace_artifacts(text)

    citations = block.get("citations") or []
    if citations:
        rendered = []
        for c in citations:
            if not isinstance(c, dict):
                continue
            url = c.get("url") or c.get("source_url") or ""
            title = c.get("title") or c.get("source_title") or url
            if url:
                rendered.append(f"[{title}]({url})")
        if rendered:
            text = text.rstrip() + "\n\n_sources: " + ", ".join(rendered) + "_"
    # Collapse the run of consecutive identical tool_use markers we just
    # produced so a turn full of "this block is not supported" doesn't turn
    # into ten of the same line.
    text = re.sub(
        r'(<tool_use name="\(stripped[^"]*"\s*/>\s*\n?){2,}',
        '<tool_use name="(stripped — claude.ai API does not expose tool calls)" />\n',
        text,
    )
    return text.strip("\n")


def _render_thinking_block(block: dict[str, Any]) -> str:
    """A `thinking` block. Prefer the human-readable summary (the line UI shows
    as "Synthesized X") over the verbose raw thinking; raw thinking is stylistic
    noise on the migrated side and inflates the paste size massively. If no
    summary, emit a short placeholder rather than dumping 2000 chars of meta.
    """
    summaries = block.get("summaries") or []
    summary = ""
    if summaries and isinstance(summaries[0], dict):
        summary = (summaries[0].get("summary") or "").strip()
    if not summary:
        # Truncated raw thinking is a fallback; better than nothing for chats
        # that happen to lack summaries.
        raw = _scrub(block.get("thinking") or "")
        if not raw:
            return ""
        if len(raw) > THINKING_TRUNC_TO:
            raw = raw[:THINKING_TRUNC_TO] + " [...]"
        return f"<thinking>{xml_escape(raw)}</thinking>"
    return f"<thinking>{xml_escape(summary)}</thinking>"


def _render_block(block: dict[str, Any]) -> str:
    btype = block.get("type")
    if btype == "text":
        return _render_text_block(block)
    if btype == "thinking":
        return _render_thinking_block(block)
    if btype == "tool_use":
        # `messages` mode normally hides these, but if one ever surfaces, render
        # the input so the migration carries the tool intent.
        name = block.get("name") or "tool"
        inp = json.dumps(block.get("input") or {}, ensure_ascii=False)
        return f'<tool_use name="{xml_escape(name)}">{xml_escape(inp)}</tool_use>'
    if btype == "tool_result":
        content = block.get("content")
        if isinstance(content, list):
            content_str = "\n".join(
                str(c.get("text") if isinstance(c, dict) else c) for c in content
            )
        else:
            content_str = str(content or "")
        if len(content_str) > TOOL_RESULT_LIMIT:
            content_str = content_str[:TOOL_RESULT_LIMIT] + "\n[...truncated...]"
        return f"<tool_result>{xml_escape(_scrub(content_str))}</tool_result>"
    if btype == "image":
        return '<image_placeholder description="generated image" />'
    return ""


def _render_message_body(msg: dict[str, Any]) -> str:
    content = msg.get("content")
    if isinstance(content, list):
        parts = [_render_block(b) for b in content if isinstance(b, dict)]
        body = "\n".join(p for p in parts if p)
    else:
        text = msg.get("text")
        if isinstance(content, str):
            body = UNSUPPORTED_BLOCK_RE.sub(
                '\n<tool_use name="(stripped — claude.ai API does not expose tool calls)" />\n',
                _scrub(content),
            )
            body = _replace_artifacts(body)
        elif isinstance(text, str):
            body = UNSUPPORTED_BLOCK_RE.sub(
                '\n<tool_use name="(stripped — claude.ai API does not expose tool calls)" />\n',
                _scrub(text),
            )
            body = _replace_artifacts(body)
        else:
            body = ""

    # Files (uploads, generated images) attached to this turn. claude.ai exposes
    # them via `files` in `messages` mode and sometimes `attachments` for older
    # legacy uploads. Render both as self-closing tags so the migrated transcript
    # preserves the fact that a file was present, with name + kind + size.
    file_lines: list[str] = []
    for src in (msg.get("files") or [], msg.get("attachments") or []):
        for att in src:
            if not isinstance(att, dict):
                continue
            name = att.get("file_name") or att.get("name") or "attachment"
            kind = att.get("file_kind") or att.get("file_type") or ""
            size = att.get("file_size") or 0
            attrs = [f'name="{xml_escape(str(name))}"']
            if kind:
                attrs.append(f'kind="{xml_escape(str(kind))}"')
            if size:
                attrs.append(f'size="{int(size)}"')
            file_lines.append(f"<file {' '.join(attrs)} />")
    if file_lines:
        body = body.rstrip() + "\n" + "\n".join(file_lines)
    return body


def _flatten_branch(messages: list[dict[str, Any]], leaf_uuid: str | None) -> list[dict[str, Any]]:
    """Walk parent_message_uuid chain backward from leaf, then reverse.

    Section 8: if leaf is missing, fall back to the deepest path through the tree.
    """
    by_uuid = {m["uuid"]: m for m in messages if isinstance(m, dict) and "uuid" in m}
    if leaf_uuid and leaf_uuid in by_uuid:
        chain: list[dict[str, Any]] = []
        cur: str | None = leaf_uuid
        seen: set[str] = set()
        while cur and cur in by_uuid and cur not in seen:
            seen.add(cur)
            chain.append(by_uuid[cur])
            cur = by_uuid[cur].get("parent_message_uuid")
        chain.reverse()
        if chain:
            return chain
    children: dict[str | None, list[dict[str, Any]]] = {}
    for m in by_uuid.values():
        children.setdefault(m.get("parent_message_uuid"), []).append(m)

    def deepest(node_uuid: str | None) -> list[dict[str, Any]]:
        kids = children.get(node_uuid, [])
        if not kids:
            return []
        best: list[dict[str, Any]] = []
        for kid in kids:
            sub = deepest(kid["uuid"])
            cand = [kid, *sub]
            if len(cand) > len(best):
                best = cand
        return best

    return deepest(None)


@dataclass(frozen=True)
class InlinePayload:
    """Whole transcript fits in one /completion `prompt` field."""

    body: str
    token_estimate: int


@dataclass(frozen=True)
class AttachmentPayload:
    """Transcript posts as an `/upload` file plus a short prompt referencing it."""

    body: str
    file_name: str
    token_estimate: int


@dataclass(frozen=True)
class ChunkedPayload:
    """Transcript spans multiple /completion calls, each with a chunk marker."""

    chunks: list[str]
    token_estimate: int


PastePayload = InlinePayload | AttachmentPayload | ChunkedPayload


HEADER = "<prior_conversation>"
FOOTER = (
    "\nThe above is our prior conversation from another Claude session.\n"
    "The <turn role=\"assistant\"> blocks are your own previous responses—\n"
    "treat them as your own past speech, not as a document about someone else.\n"
    "When I send my next message, continue this conversation in first person\n"
    "as the same assistant, maintaining your tone and any commitments you made.\n\n"
    "For now, reply with exactly the single word: READY\n"
)


def render_transcript(conn: sqlite3.Connection, conv_uuid: str) -> str:
    """Build the XML transcript per Section 10 from stored conversation+messages."""
    crow = conn.execute(
        "SELECT raw_path, title, model, created_at, updated_at, project_uuid "
        "FROM conversation WHERE uuid=?",
        (conv_uuid,),
    ).fetchone()
    if crow is None:
        raise KeyError(f"conversation {conv_uuid} not found in db")
    title = crow["title"] or "(untitled)"
    model = crow["model"] or "unknown"
    raw_path = crow["raw_path"]
    raw: dict[str, Any] = {}
    if raw_path:
        import gzip
        try:
            with gzip.open(raw_path, "rb") as f:
                raw = json.loads(f.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}

    messages: list[dict[str, Any]] = list(raw.get("chat_messages") or [])
    if not messages:
        rows = conn.execute(
            "SELECT raw FROM message WHERE conversation_uuid=? "
            "ORDER BY index_in_conversation ASC, created_at ASC",
            (conv_uuid,),
        ).fetchall()
        messages = [json.loads(r["raw"]) for r in rows if r["raw"]]

    leaf = raw.get("current_leaf_message_uuid")
    chain = _flatten_branch(messages, leaf)

    project_context = _project_context_block(conn, crow["project_uuid"])

    out: list[str] = [HEADER, "  <metadata>"]
    out.append("    <source>claude.ai conversation export</source>")
    out.append(f"    <original_uuid>{xml_escape(conv_uuid)}</original_uuid>")
    out.append(f"    <original_title>{xml_escape(title)}</original_title>")
    if crow["created_at"]:
        out.append(f"    <original_created_at>{xml_escape(crow['created_at'])}</original_created_at>")
    if crow["updated_at"]:
        out.append(f"    <original_updated_at>{xml_escape(crow['updated_at'])}</original_updated_at>")
    out.append(f"    <original_model>{xml_escape(model)}</original_model>")
    out.append(f"    <turn_count>{len(chain)}</turn_count>")
    settings = raw.get("settings")
    if isinstance(settings, dict) and settings:
        # web_search / extended thinking / artifacts toggles — preserve so the
        # target Claude knows what tools the source had access to.
        flags = [k for k, v in settings.items() if v is True]
        if flags:
            out.append(f"    <original_settings>{xml_escape(', '.join(sorted(flags)))}</original_settings>")
    out.append("  </metadata>")
    if project_context:
        out.append("")
        out.append(project_context)
    out.append("")
    for i, msg in enumerate(chain, start=1):
        role = "user" if msg.get("sender") == "human" else "assistant"
        body = _render_message_body(msg).strip()
        ts = msg.get("created_at") or ""
        attr_ts = f' timestamp="{xml_escape(ts)}"' if ts else ""
        out.append(f'  <turn index="{i}" role="{role}"{attr_ts}>')
        out.append("    " + body.replace("\n", "\n    ") if body else "")
        out.append("  </turn>")
    out.append("</prior_conversation>")
    out.append(FOOTER)
    return "\n".join(out)


def _project_context_block(conn: sqlite3.Connection, project_uuid: str | None) -> str:
    """If the conversation belonged to a source project, prepend the project's
    system prompt and the names of its knowledge files. Reproducing the source
    project verbatim is handled by `restore_projects`; this metadata gives the
    target Claude on the migrated chat the same situational context.
    """
    if not project_uuid:
        return ""
    row = conn.execute(
        "SELECT name, prompt_template FROM project WHERE uuid=?",
        (project_uuid,),
    ).fetchone()
    if row is None:
        return ""
    docs = conn.execute(
        "SELECT file_name FROM project_doc WHERE project_uuid=? ORDER BY file_name",
        (project_uuid,),
    ).fetchall()
    parts = ["  <original_project>"]
    parts.append(f"    <project_name>{xml_escape(row['name'] or '')}</project_name>")
    if row["prompt_template"]:
        parts.append("    <project_prompt><![CDATA[")
        parts.append(row["prompt_template"])
        parts.append("    ]]></project_prompt>")
    if docs:
        parts.append("    <project_knowledge_files>")
        for d in docs:
            parts.append(f'      <file name="{xml_escape(d["file_name"] or "")}" />')
        parts.append("    </project_knowledge_files>")
    parts.append("  </original_project>")
    return "\n".join(parts)


def prepare_paste_payload(conn: sqlite3.Connection, conv_uuid: str) -> PastePayload:
    body = render_transcript(conn, conv_uuid)
    tokens = estimate_tokens(body)
    if tokens <= INLINE_TOKEN_BUDGET:
        return InlinePayload(body=body, token_estimate=tokens)
    if tokens <= ATTACHMENT_TOKEN_BUDGET:
        return AttachmentPayload(
            body=body,
            file_name=f"transcript-{conv_uuid[:8]}.md",
            token_estimate=tokens,
        )
    chunks = _chunk(body, INLINE_TOKEN_BUDGET)
    return ChunkedPayload(chunks=chunks, token_estimate=tokens)


def _chunk(text: str, token_budget: int) -> list[str]:
    """Split into chunks of roughly token_budget tokens, on paragraph boundaries."""
    target_chars = token_budget * 4
    if len(text) <= target_chars:
        return [text]
    chunks: list[str] = []
    paragraphs = text.split("\n\n")
    cur: list[str] = []
    cur_len = 0
    for p in paragraphs:
        if cur_len + len(p) > target_chars and cur:
            chunks.append("\n\n".join(cur))
            cur = [p]
            cur_len = len(p)
        else:
            cur.append(p)
            cur_len += len(p) + 2
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks
