"""Optional OpenAI-compatible HTTP adapter for the PTR review queue.

The adapter is intentionally separate from the comparison engine.  It only
turns one bounded ``build_review_task`` payload into a JSON response; callers
must still pass the response through ``run_llm_reviews`` and its evidence and
hard-field validation.  No endpoint, model, or key is configured by default.
"""

from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mvp.ptr_llm import PTR_LLM_PROMPT_VERSION


class PtrLlmHttpError(RuntimeError):
    """Raised when an optional LLM HTTP call cannot produce JSON."""


class OpenAICompatiblePtrLlmProvider:
    """Minimal JSON-only adapter for ``/chat/completions``-style APIs."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        model_id: str,
        provider_id: str = "openai-compatible",
        timeout: float = 30.0,
    ) -> None:
        if not endpoint.strip():
            raise ValueError("endpoint 不能为空")
        if not api_key.strip():
            raise ValueError("api_key 不能为空")
        if not model_id.strip():
            raise ValueError("model_id 不能为空")
        if timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.model_id = model_id
        self.provider_id = provider_id
        self.timeout = float(timeout)

    def review(self, task: Mapping[str, Any]) -> Mapping[str, Any]:
        """Call the provider once and return the model's JSON object."""

        instructions = "\n".join(str(item) for item in task.get("instructions", []))
        user_payload = json.dumps(dict(task), ensure_ascii=False, sort_keys=True)
        body = {
            "model": self.model_id,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"你是 PTR 与 Report 的受约束复核器。提示词版本：{PTR_LLM_PROMPT_VERSION}。"
                        "只返回 JSON 对象，不要输出 Markdown。\n" + instructions
                    ),
                },
                {"role": "user", "content": user_payload},
            ],
        }
        request = Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise PtrLlmHttpError(f"LLM HTTP 请求失败：{error}") from error
        try:
            envelope = json.loads(raw.decode("utf-8"))
            content = envelope["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(
                    str(item.get("text", "")) if isinstance(item, Mapping) else str(item)
                    for item in content
                )
            payload = json.loads(str(content))
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
            raise PtrLlmHttpError("LLM 返回值不是可解析的 JSON 对象") from error
        if not isinstance(payload, Mapping):
            raise PtrLlmHttpError("LLM 返回值必须是 JSON 对象")
        return payload


__all__ = ["OpenAICompatiblePtrLlmProvider", "PtrLlmHttpError"]
