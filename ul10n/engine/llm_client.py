"""Unified OpenAI-compatible LLM client for local llama-server and cloud APIs."""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

_RETRY_CODES = frozenset({408, 429, 500, 502, 503, 504})
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_VERSIONED_API_SUFFIX_RE = re.compile(r"/(?:v\d+(?:beta|alpha)?(?:/openai)?|openai)$", re.IGNORECASE)


def normalize_chat_url(base_url: str) -> str:
    """Normalize OpenAI-compatible base URLs across OpenAI, DeepSeek, Gemini, Volcengine, Zhipu, etc."""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/chat/completions"):
        return url
    path = urlparse(url).path.rstrip("/")
    if _VERSIONED_API_SUFFIX_RE.search(path):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


def _is_loopback(url: str) -> bool:
    low = (url or "").lower()
    return any(f"://{h}" in low for h in _LOOPBACK_HOSTS)


def is_hy_mt_model(model_name: str) -> bool:
    low = (model_name or "").casefold()
    return "hy-mt" in low or "hymt" in low


def strip_thinking_blocks(text: str) -> str:
    """Remove <think>...</think> blocks emitted by reasoning models before JSON extraction."""
    s = _THINK_BLOCK_RE.sub("", text or "")
    # Also handle truncated unclosed <think> block when a reasoning model hits max_tokens
    if "<think>" in s.lower() and "</think>" not in s.lower():
        idx = s.lower().find("<think>")
        prefix = s[:idx].strip()
        if prefix:
            return prefix
    return s.strip()


def repair_curly_json_quotes(text: str) -> str:
    """Repair Chinese curly quotes mistakenly used as JSON structural delimiters."""
    repaired = re.sub(r'([{\[:,]\s*)“', r'\1"', text)
    repaired = re.sub(r'”(\s*[,}\]:])', r'"\1', repaired)
    return repaired


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Extract first valid JSON object from model output, tolerating think blocks, fences & curly quotes."""
    s = strip_thinking_blocks(text)
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s).strip()
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end <= start:
        return None
    candidate = s[start : end + 1]
    for variant in (candidate, repair_curly_json_quotes(candidate)):
        try:
            obj = json.loads(variant)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    return None


def extract_json_array(text: str) -> list[dict[str, Any]] | None:
    """Extract first valid JSON array of objects from model output, tolerating think blocks, fences & curly quotes."""
    s = strip_thinking_blocks(text)
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s).strip()
    start = s.find("[")
    end = s.rfind("]")
    if start < 0 or end <= start:
        return None
    candidate = s[start : end + 1]
    for variant in (candidate, repair_curly_json_quotes(candidate)):
        try:
            obj = json.loads(variant)
            if isinstance(obj, list):
                return [item for item in obj if isinstance(item, dict)]
        except ValueError:
            continue
    return None


def _extract_message_content(msg: dict[str, Any]) -> str:
    raw = msg.get("content")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def _friendly_http_error(status_code: int, body_snippet: str) -> str:
    hints = {
        401: "API Key 无效或未授权，请检查设置中的 API Key",
        402: "云端 API 账户余额不足或配额已用尽",
        403: "无权访问该模型或接口，请检查权限与地区限制",
        404: "接口地址或模型名称不存在，请检查 Base URL 与模型名",
        429: "触发云端 API 速率限制，请稍后再试",
    }
    hint = hints.get(status_code)
    snippet = (body_snippet or "").strip().replace("\n", " ")[:180]
    if hint:
        return f"{hint} (HTTP {status_code})" + (f"：{snippet}" if snippet else "")
    return f"模型请求失败 (HTTP {status_code}): {snippet}"


@dataclass(frozen=True)
class ChatResult:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMClient:
    """Thread-safe OpenAI-compatible HTTP client with keep-alive pooling & token accounting."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "local",
        *,
        timeout: float = 90.0,
        max_retries: int | None = None,
    ):
        self.base_url = base_url
        self.url = normalize_chat_url(base_url)
        self.api_key = api_key
        self.model = model or "local"
        self.is_loopback = _is_loopback(self.url)
        self.is_cloud = not self.is_loopback
        self.max_retries = (3 if self.is_cloud else 2) if max_retries is None else max_retries
        self._usage_lock = threading.Lock()
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_requests = 0
        self._client = httpx.Client(
            timeout=timeout,
            trust_env=not self.is_loopback,
            verify=not self.is_loopback,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )

    @property
    def total_tokens(self) -> int:
        with self._usage_lock:
            return self.total_prompt_tokens + self.total_completion_tokens

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        extra: dict[str, Any] | None = None,
    ) -> ChatResult:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if extra:
            payload.update(extra)

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.post(self.url, headers=headers, json=payload)
                if resp.status_code in _RETRY_CODES:
                    retry_after = 0.0
                    raw_ra = resp.headers.get("Retry-After")
                    if raw_ra:
                        try:
                            retry_after = min(15.0, max(0.5, float(raw_ra)))
                        except ValueError:
                            retry_after = 0.0
                    err_msg = _friendly_http_error(resp.status_code, resp.text[:200])
                    if attempt < self.max_retries:
                        delay = retry_after if retry_after > 0 else min(10.0, 1.0 * (2**attempt))
                        time.sleep(delay)
                        continue
                    raise RuntimeError(err_msg)
                if resp.status_code == 400 and "max_tokens" in (resp.text or "") and "max_completion_tokens" in (resp.text or ""):
                    # OpenAI o1/o3/gpt-5 compatibility fallback
                    alt_payload = dict(payload)
                    alt_payload["max_completion_tokens"] = alt_payload.pop("max_tokens", max_tokens)
                    alt_payload.pop("temperature", None)
                    resp = self._client.post(self.url, headers=headers, json=alt_payload)
                if resp.status_code >= 400:
                    raise ValueError(_friendly_http_error(resp.status_code, resp.text[:240]))
                data = resp.json()
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                content = _extract_message_content(msg)
                usage = data.get("usage") or {}
                p_tok = int(usage.get("prompt_tokens") or 0)
                c_tok = int(usage.get("completion_tokens") or 0)
                with self._usage_lock:
                    self.total_prompt_tokens += p_tok
                    self.total_completion_tokens += c_tok
                    self.total_requests += 1
                return ChatResult(
                    content=content,
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                )
            except ValueError:
                raise
            except Exception as exc:
                last_err = exc
                if attempt < self.max_retries:
                    time.sleep(min(8.0, 0.8 * (2**attempt)))
        raise RuntimeError(f"无法连接模型服务：{last_err}")

    def test_connection(self) -> str:
        """Quick minimal-token smoke call returning model reply snippet."""
        res = self.chat(
            [{"role": "user", "content": "OK"}],
            temperature=0.0,
            max_tokens=16,
        )
        return res.content.strip() or "OK"
