"""OpenAI chat-completions client (non-streaming core, streaming optional)."""

from __future__ import annotations

import json
from typing import Any, Iterator, Sequence

from .base import ProviderError, post_json, post_stream

Message = dict[str, Any]


class OpenAIClient:
    name = "openai"
    supports_images = True

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 10.0,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Provider-specific switches that must not be sent to a vanilla endpoint,
        # e.g. {"enable_thinking": False} for a Qwen-compatible gateway.
        self.extra_body = dict(extra_body or {})

    # ------------------------------------------------------------ payload

    def _payload(self, messages: Sequence[Message], image: str | None, stream: bool) -> dict[str, Any]:
        rendered: list[dict[str, Any]] = []
        for message in messages:
            if image and message.get("role") == "user":
                rendered.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": str(message.get("content") or "")},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
                        ],
                    }
                )
            else:
                rendered.append({"role": message.get("role", "user"), "content": str(message.get("content") or "")})
        payload: dict[str, Any] = {"model": self.model, "messages": rendered, "temperature": 0}
        if stream:
            payload["stream"] = True
        else:
            payload["response_format"] = {"type": "json_object"}
        payload.update(self.extra_body)
        return payload

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    # ------------------------------------------------------------ public API

    def complete(self, messages: Sequence[Message], image: str | None = None, stream: bool = False):
        if not self.api_key:
            raise ProviderError("openai: no api key configured", kind="auth")
        if stream:
            return self.stream(messages, image=image)
        data = post_json(
            f"{self.base_url}/chat/completions",
            self._headers(),
            self._payload(messages, image, stream=False),
            self.timeout,
        )
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"openai: unexpected response shape: {json.dumps(data)[:200]}", kind="http") from exc

    def stream(self, messages: Sequence[Message], image: str | None = None) -> Iterator[str]:
        for chunk in post_stream(
            f"{self.base_url}/chat/completions",
            self._headers(),
            self._payload(messages, image, stream=True),
            self.timeout,
        ):
            try:
                delta = json.loads(chunk)["choices"][0].get("delta", {}).get("content")
            except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                continue
            if delta:
                yield delta
