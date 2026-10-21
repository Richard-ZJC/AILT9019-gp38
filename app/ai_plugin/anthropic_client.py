"""Anthropic messages API client (non-streaming core, streaming optional)."""

from __future__ import annotations

import json
from typing import Any, Iterator, Sequence

from .base import ProviderError, post_json, post_stream

Message = dict[str, Any]


class AnthropicClient:
    name = "anthropic"
    supports_images = True

    def __init__(
        self,
        api_key: str,
        model: str = "claude-3-5-haiku-latest",
        base_url: str = "https://api.anthropic.com",
        timeout: float = 10.0,
        max_tokens: int = 1024,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens
        # e.g. {"thinking": {"type": "disabled"}} on a Qwen-compatible gateway.
        self.extra_body = dict(extra_body or {})

    # ------------------------------------------------------------ payload

    def _payload(self, messages: Sequence[Message], image: str | None, stream: bool) -> dict[str, Any]:
        system_parts: list[str] = []
        rendered: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role", "user")
            content = str(message.get("content") or "")
            if role == "system":
                system_parts.append(content)
                continue
            if image and role == "user":
                rendered.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/jpeg", "data": image},
                            },
                            {"type": "text", "text": content},
                        ],
                    }
                )
                continue
            rendered.append({"role": "assistant" if role == "assistant" else "user", "content": content})

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "messages": rendered,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if stream:
            payload["stream"] = True
        payload.update(self.extra_body)
        return payload

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}

    # ------------------------------------------------------------ public API

    def complete(self, messages: Sequence[Message], image: str | None = None, stream: bool = False):
        if not self.api_key:
            raise ProviderError("anthropic: no api key configured", kind="auth")
        if stream:
            return self.stream(messages, image=image)
        data = post_json(
            f"{self.base_url}/v1/messages",
            self._headers(),
            self._payload(messages, image, stream=False),
            self.timeout,
        )
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ProviderError(f"anthropic: unexpected response shape: {json.dumps(data)[:200]}", kind="http")
        text = "".join(block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text")
        return text

    def stream(self, messages: Sequence[Message], image: str | None = None) -> Iterator[str]:
        for chunk in post_stream(
            f"{self.base_url}/v1/messages",
            self._headers(),
            self._payload(messages, image, stream=True),
            self.timeout,
        ):
            try:
                event = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "content_block_delta":
                delta = event.get("delta", {}).get("text")
                if delta:
                    yield delta
