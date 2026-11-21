"""Thin LLM interface so agents can run against Claude or a scripted fake in tests."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_MODEL = "claude-sonnet-5-5"


@dataclass
class LLMResponse:
    content: list[dict[str, Any]]  # Anthropic-style content blocks (text / tool_use)
    stop_reason: str
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def text(self) -> str:
        return "\n".join(b["text"] for b in self.content if b.get("type") == "text").strip()

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]


class LLM(Protocol):
    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 4096,
    ) -> LLMResponse: ...


class AnthropicLLM:
    """Claude via the Anthropic Messages API (needs ANTHROPIC_API_KEY)."""

    def __init__(self, model: str | None = None, client: Any | None = None):
        from anthropic import AsyncAnthropic

        self.model = model or os.environ.get("DEVOPS_AGENTS_MODEL", DEFAULT_MODEL)
        self.client = client or AsyncAnthropic()

    @staticmethod
    def _block(b: Any) -> dict[str, Any]:
        if b.type == "text":
            return {"type": "text", "text": b.text}
        if b.type == "tool_use":
            return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
        return b.model_dump(exclude_none=True)

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 4096,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            # Cache the (large, stable) system prompt + tool definitions across turns.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        resp = await self.client.messages.create(**kwargs)
        return LLMResponse(
            content=[self._block(b) for b in resp.content],
            stop_reason=resp.stop_reason or "end_turn",
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )
