"""Wire format for posting a rendered transcript to a target conversation.

Each `PastePayload` variant has a different posting recipe:
  - Inline: one /completion call with the full body in `prompt`.
  - Attachment: multipart upload first, then /completion with the file_uuid.
  - Chunked: multiple /completion calls with chunk-of-N markers; only the last
    chunk omits the "do not respond yet" tail so Claude waits for everything.

Keeping the dispatch here means the rendering / sizing decision in `render.py`
and the wire format that consumes it sit one import away — the conversation
restore loop just calls `send_payload(...)` and the strategy is invisible.
"""

from __future__ import annotations

import json

from curl_cffi import CurlMime

from .client import ClaudeClient
from .errors import NetworkError, SchemaDrift
from .render import (
    AttachmentPayload,
    ChunkedPayload,
    InlinePayload,
    PastePayload,
)

SSE_TIMEOUT = 60.0
UPLOAD_TIMEOUT = 120.0
ATTACHMENT_PROMPT = (
    "Load the attached transcript of our prior conversation. "
    "Reply with exactly the single word: READY"
)
CHUNK_TAIL = "\nDo not respond yet — more is coming."


async def send_payload(
    client: ClaudeClient,
    target_org: str,
    conv_uuid: str,
    payload: PastePayload,
    *,
    early_cancel: bool = True,
) -> None:
    """Post `payload` to `conv_uuid` on target. Raises typed errors on failure.

    `early_cancel` (default True): drop the SSE stream after the FIRST model
    event arrives. By then claude.ai has already committed the user message
    server-side, and the migration only needs the user's transcript-paste to
    land — the assistant "READY" reply is decorative. For chunked payloads
    every chunk except the last waits for stop normally, since later chunks
    depend on Claude having processed the earlier ones.
    """
    match payload:
        case InlinePayload(body=body):
            await _await_completion(
                client,
                target_org,
                conv_uuid,
                {"prompt": body, "attachments": [], "files": []},
                early_cancel=early_cancel,
            )
        case AttachmentPayload(body=body, file_name=file_name):
            file_uuid = await _upload_attachment(
                client, target_org, file_name, body
            )
            # claude.ai's web UI sends uploaded files via the `files` field on
            # /completion, NOT `attachments` (the latter is only for legacy
            # rich-email-style attachments and 400s on text uploads).
            await _await_completion(
                client,
                target_org,
                conv_uuid,
                {
                    "prompt": ATTACHMENT_PROMPT,
                    "attachments": [],
                    "files": [file_uuid],
                },
                early_cancel=early_cancel,
            )
        case ChunkedPayload(chunks=chunks):
            for idx, chunk in enumerate(chunks):
                marker = f"\n\n[chunk {idx + 1} of {len(chunks)}]"
                final = idx == len(chunks) - 1
                tail = "" if final else CHUNK_TAIL
                # All chunks except the last wait for full stop (the next chunk's
                # "do not respond yet" depends on Claude having seen this one).
                await _await_completion(
                    client,
                    target_org,
                    conv_uuid,
                    {"prompt": chunk + marker + tail, "attachments": [], "files": []},
                    early_cancel=early_cancel and final,
                )
        case _:
            raise SchemaDrift(
                f"transport.send_payload received unsupported payload type "
                f"{type(payload).__name__}; update the match arms in transport.py"
            )


async def _await_completion(
    client: ClaudeClient,
    target_org: str,
    conv_uuid: str,
    body: dict[str, object],
    *,
    early_cancel: bool = True,
) -> None:
    """POST /completion as SSE; read until a stop signal (or early-cancel).

    Modern claude.ai API: each event is `event: completion` + `data: {...}`.
    A terminating event has `stop_reason` set or `type: "message_stop"`.

    `early_cancel`: break on the first non-empty model event instead of
    waiting for the full assistant turn. The user message is committed
    server-side BEFORE the SSE stream emits anything, so receiving any
    model-side event proves the migration paste landed. The assistant's
    "READY" reply being truncated is fine for migration purposes.
    """
    saw_stop = False
    async for line in client.stream(
        "POST",
        f"/api/organizations/{target_org}/chat_conversations/{conv_uuid}/completion",
        json_body=body,
        timeout=SSE_TIMEOUT,
    ):
        if not line.startswith("data:"):
            continue
        payload_text = line[5:].strip()
        if not payload_text or payload_text == "[DONE]":
            saw_stop = True
            continue
        try:
            evt = json.loads(payload_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(evt, dict):
            continue
        # Terminal events end the wait under both modes.
        if evt.get("stop_reason"):
            saw_stop = True
            break
        if evt.get("type") in ("message_stop", "message_complete"):
            saw_stop = True
            break
        # Early-cancel: any non-terminal model event proves the user
        # message was committed; drop the connection to free wall clock.
        if early_cancel and (
            evt.get("type")
            in ("message_start", "content_block_start", "content_block_delta")
            or "completion" in evt
        ):
            saw_stop = True
            break
    if not saw_stop:
        raise NetworkError("completion stream ended without a stop signal")


async def _upload_attachment(
    client: ClaudeClient,
    target_org: str,
    file_name: str,
    content: str,
) -> str:
    """Multipart upload for >80K-token transcripts. Returns the file_uuid.

    `curl_cffi >= 0.7` removed the `files=` shortcut; we now build a `CurlMime`
    explicitly. Closed in a `finally` to release the C-side buffer.
    """
    mime = CurlMime()
    mime.addpart(
        name="file",
        filename=file_name,
        data=content.encode("utf-8"),
        content_type="text/markdown",
    )
    try:
        async with client.session() as sess:
            resp = await sess.post(
                f"{client.settings.base_url}/api/{target_org}/upload",
                multipart=mime,
                headers=client._headers({"Accept": "application/json"}),
                timeout=UPLOAD_TIMEOUT,
            )
    finally:
        mime.close()
    if resp.status_code != 200:
        raise NetworkError(f"upload returned {resp.status_code}")
    try:
        j = json.loads(resp.content)
    except json.JSONDecodeError as e:
        # /upload occasionally serves a Cloudflare interstitial page with a
        # 200 status code; surface as SchemaDrift rather than crashing on
        # an uncaught JSONDecodeError.
        raise SchemaDrift(
            f"upload returned 200 but body is not JSON: "
            f"{resp.content[:200]!r}"
        ) from e
    if not isinstance(j, dict):
        raise SchemaDrift(f"upload returned 200 with non-dict body: {type(j).__name__}")
    fu = j.get("file_uuid") or j.get("uuid")
    if not isinstance(fu, str):
        raise SchemaDrift("upload response missing file_uuid")
    return fu
