"""Tests for transport.send_payload — the wire-format dispatch per payload variant."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from claude_migrate.render import (
    AttachmentPayload,
    ChunkedPayload,
    InlinePayload,
)
from claude_migrate.transport import CHUNK_TAIL, send_payload


class _FakeClient:
    """Records every stream/upload call so tests can assert on the wire format.

    Implements just enough of `ClaudeClient` for transport.send_payload to run:
      - stream(method, path, json_body, timeout) → SSE async iterator
      - session() → async ctx manager whose .post(...) returns a fake response
      - _headers(extra) → dict
      - settings.base_url
    """

    class _Settings:
        base_url = "https://claude.ai"

    settings = _Settings()

    def __init__(self, *, upload_file_uuid: str = "file-abc") -> None:
        self.stream_calls: list[dict[str, Any]] = []
        self.upload_calls: list[dict[str, Any]] = []
        self._upload_file_uuid = upload_file_uuid

    def _headers(self, extra: Any = None) -> dict[str, str]:
        h = {"Cookie": "fake"}
        if extra:
            h.update(extra)
        return h

    def stream(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        timeout: float = 60.0,
    ) -> AsyncIterator[str]:
        self.stream_calls.append(
            {"method": method, "path": path, "body": json_body, "timeout": timeout}
        )

        async def gen() -> AsyncIterator[str]:
            # Single SSE event with a stop_reason ends the stream cleanly.
            yield 'data: {"stop_reason": "end_turn"}'

        return gen()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Any]:
        outer = self

        class _Resp:
            status_code = 200
            content = (
                f'{{"file_uuid": "{outer._upload_file_uuid}"}}'.encode()
            )

        class _Sess:
            async def post(
                self_inner, url: str, *, multipart: Any, headers: Any, timeout: float
            ) -> Any:
                outer.upload_calls.append(
                    {"url": url, "multipart": multipart, "timeout": timeout}
                )
                return _Resp()

        yield _Sess()


@pytest.mark.asyncio
async def test_inline_posts_one_completion_with_prompt_body() -> None:
    client = _FakeClient()
    payload = InlinePayload(body="hello body", token_estimate=10)
    await send_payload(client, "org-1", "conv-1", payload)  # type: ignore[arg-type]
    assert len(client.stream_calls) == 1
    call = client.stream_calls[0]
    assert call["path"].endswith("/conv-1/completion")
    assert call["body"] == {"prompt": "hello body", "attachments": [], "files": []}


@pytest.mark.asyncio
async def test_attachment_uploads_then_completes_with_short_prompt() -> None:
    client = _FakeClient(upload_file_uuid="upload-xyz")
    payload = AttachmentPayload(
        body="a long transcript",
        file_name="transcript-conv-1.md",
        token_estimate=100_000,
    )
    await send_payload(client, "org-1", "conv-1", payload)  # type: ignore[arg-type]
    # one upload + one completion call
    assert len(client.upload_calls) == 1
    assert client.upload_calls[0]["url"].endswith("/api/org-1/upload")
    assert len(client.stream_calls) == 1
    body = client.stream_calls[0]["body"]
    # claude.ai's web UI sends uploads as `files: [<uuid>]`, not `attachments`.
    assert body["files"] == ["upload-xyz"]
    assert body["attachments"] == []
    # prompt is the short directive, not the transcript itself.
    assert "transcript" in body["prompt"].lower()
    assert "a long transcript" not in body["prompt"]


@pytest.mark.asyncio
async def test_chunked_emits_one_completion_per_chunk_with_markers() -> None:
    client = _FakeClient()
    chunks = ["chunk-A", "chunk-B", "chunk-C"]
    payload = ChunkedPayload(chunks=chunks, token_estimate=200_000)
    await send_payload(client, "org-1", "conv-1", payload)  # type: ignore[arg-type]
    assert len(client.stream_calls) == 3
    for idx, call in enumerate(client.stream_calls):
        prompt = call["body"]["prompt"]
        assert chunks[idx] in prompt
        assert f"[chunk {idx + 1} of 3]" in prompt


@pytest.mark.asyncio
async def test_chunked_only_last_chunk_omits_do_not_respond_tail() -> None:
    """Final chunk must NOT carry the 'do not respond yet' instruction —
    otherwise Claude waits forever for a chunk that never comes."""
    client = _FakeClient()
    chunks = ["A", "B", "C"]
    payload = ChunkedPayload(chunks=chunks, token_estimate=200_000)
    await send_payload(client, "org-1", "conv-1", payload)  # type: ignore[arg-type]
    assert CHUNK_TAIL.strip() in client.stream_calls[0]["body"]["prompt"]
    assert CHUNK_TAIL.strip() in client.stream_calls[1]["body"]["prompt"]
    assert CHUNK_TAIL.strip() not in client.stream_calls[2]["body"]["prompt"]


@pytest.mark.asyncio
async def test_chunked_single_chunk_omits_tail() -> None:
    """Edge case: a 'chunked' payload with one chunk shouldn't say 'more is coming'."""
    client = _FakeClient()
    payload = ChunkedPayload(chunks=["only"], token_estimate=200_000)
    await send_payload(client, "org-1", "conv-1", payload)  # type: ignore[arg-type]
    assert CHUNK_TAIL.strip() not in client.stream_calls[0]["body"]["prompt"]
