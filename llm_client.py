"""Small OpenAI-compatible client shared by bootstrap/research workflows."""

from __future__ import annotations

import json
import os
import re
import urllib.request


class LLMConfigurationError(RuntimeError):
    pass


class LLMResponseError(RuntimeError):
    pass


def parse_json_object(raw: str) -> dict:
    """Parse a JSON object, tolerating a surrounding markdown fence."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            preview = raw.strip().replace("\n", " ")[:240]
            raise LLMResponseError(
                f"LLM response contains no JSON object; response preview={preview!r}"
            )
        try:
            value = json.loads(match.group())
        except json.JSONDecodeError as exc:
            raise LLMResponseError(f"invalid JSON from LLM: {exc}") from exc
    if not isinstance(value, dict):
        raise LLMResponseError("LLM response must be a JSON object")
    return value


class ConfiguredLLM:
    """OpenAI-compatible chat client configured entirely through environment."""

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, timeout: int = 120):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or
                         "https://api.openai.com/v1").rstrip("/")
        self.model = model or os.environ.get("LLM_MODEL", "step-3.7-flash")
        self.timeout = timeout
        if not self.api_key:
            raise LLMConfigurationError("OPENAI_API_KEY is not configured")

    def chat(self, *, system_prompt: str, user_prompt: str,
             temperature: float = 0.1, max_tokens: int = 4096) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
        try:
            return result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError("chat completion response has no message content") from exc

    def json(self, *, system_prompt: str, user_prompt: str,
             temperature: float = 0.1, max_tokens: int = 4096) -> dict:
        return parse_json_object(self.chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        ))
