"""Small replaceable adapter for OpenAI-compatible chat-completions APIs."""
from dataclasses import dataclass
import json
import os
import time
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-6-luna"
    api_key_env: str = "LEVEL2_API_KEY"
    timeout: float = 45.0
    input_usd_per_million: Optional[float] = None
    output_usd_per_million: Optional[float] = None
    max_response_bytes: int = 512 * 1024

    def __post_init__(self):
        if not self.base_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError("base_url must use HTTPS (HTTP is allowed only for localhost)")
        if not self.model or not self.api_key_env.isidentifier():
            raise ValueError("model and a valid API-key environment variable name are required")
        if not 1 <= self.timeout <= 180:
            raise ValueError("timeout must be between 1 and 180 seconds")
        if self.max_response_bytes < 1024:
            raise ValueError("max_response_bytes must be at least 1024")
        if (self.input_usd_per_million is None) != (self.output_usd_per_million is None):
            raise ValueError("configure both token prices or neither")
        if any(x is not None and x < 0 for x in
               (self.input_usd_per_million, self.output_usd_per_million)):
            raise ValueError("token prices cannot be negative")


class OpenAICompatibleProvider:
    def __init__(self, config: ProviderConfig, *, opener=urlopen):
        self.config = config
        self._opener = opener

    def complete(self, messages, *, max_tokens=900):
        key = os.environ.get(self.config.api_key_env)
        if not key:
            raise ProviderError(f"API key environment variable {self.config.api_key_env} is not set")
        if not isinstance(messages, list) or not messages or any(
            not isinstance(m, dict) or m.get("role") not in {"system", "user", "assistant"}
            or not isinstance(m.get("content"), str) for m in messages
        ):
            raise ProviderError("messages must be a non-empty list of text chat messages")
        if not 1 <= max_tokens <= 4096:
            raise ProviderError("max_tokens is outside the allowed range")
        payload = json.dumps({
            "model": self.config.model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
            "reasoning_effort": "none",
            "store": False,
        }).encode("utf-8")
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        request = Request(url, data=payload, method="POST", headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        })
        started = time.monotonic()
        try:
            with self._opener(request, timeout=self.config.timeout) as response:
                raw = response.read(self.config.max_response_bytes + 1)
        except HTTPError as exc:
            raise ProviderError(f"provider returned HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError) as exc:
            # Do not include exception text: URL/library errors can contain request details.
            raise ProviderError(f"provider request failed ({type(exc).__name__})") from None
        if len(raw) > self.config.max_response_bytes:
            raise ProviderError("provider response exceeded configured size limit")
        try:
            data = json.loads(raw.decode("utf-8"))
            text = data["choices"][0]["message"]["content"]
        except (UnicodeError, ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("provider returned an invalid chat-completions response") from None
        if not isinstance(text, str):
            raise ProviderError("provider response content was not text")
        usage = data.get("usage")
        input_tokens = output_tokens = None
        cost = None
        if isinstance(usage, dict):
            input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
            output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
            if (isinstance(input_tokens, int) and isinstance(output_tokens, int)
                    and self.config.input_usd_per_million is not None
                    and self.config.output_usd_per_million is not None):
                cost = (input_tokens * self.config.input_usd_per_million
                        + output_tokens * self.config.output_usd_per_million) / 1_000_000
        return {
            "text": text,
            "model": data.get("model", self.config.model),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": cost,
            "elapsed_seconds": time.monotonic() - started,
        }
