"""Pydantic models for claude.ai API entities. All allow extras (forward-compat)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ALLOW = ConfigDict(extra="allow", populate_by_name=True)


class Org(BaseModel):
    model_config = ALLOW

    uuid: str
    name: str | None = None
    capabilities: list[str] | dict[str, Any] | None = None


class Account(BaseModel):
    model_config = ALLOW

    uuid: str | None = None
    email_address: str | None = Field(default=None, alias="email_address")
    full_name: str | None = None
    settings: dict[str, Any] | None = None


class CustomStyle(BaseModel):
    model_config = ALLOW

    uuid: str
    name: str
    prompt: str | None = None
    summary: str | None = None
    examples: list[Any] | None = None


class ProjectDoc(BaseModel):
    model_config = ALLOW

    uuid: str
    file_name: str
    content: str
    created_at: datetime | None = None


class Project(BaseModel):
    model_config = ALLOW

    uuid: str
    name: str
    description: str | None = None
    prompt_template: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    is_starred: bool | None = None


class Attachment(BaseModel):
    model_config = ALLOW

    uuid: str | None = None
    file_name: str | None = None
    file_kind: str | None = None
    file_size: int | None = None
    file_uuid: str | None = None
    extracted_content: str | None = None


class ContentBlock(BaseModel):
    """Generic content block for messages — any of text/tool_use/tool_result/thinking."""

    model_config = ALLOW

    type: str
    text: str | None = None
    thinking: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    id: str | None = None
    tool_use_id: str | None = None
    content: list[dict[str, Any]] | str | None = None


class Message(BaseModel):
    model_config = ALLOW

    uuid: str
    sender: str
    text: str | None = None
    content: list[ContentBlock] | None = None
    parent_message_uuid: str | None = None
    created_at: datetime | None = None
    attachments: list[Attachment] | None = None
    files_v2: list[dict[str, Any]] | None = None
    index: int | None = None


class Conversation(BaseModel):
    model_config = ALLOW

    uuid: str
    name: str | None = None
    summary: str | None = None
    model: str | None = None
    is_starred: bool | None = None
    project_uuid: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    current_leaf_message_uuid: str | None = None
    chat_messages: list[Message] | None = None
