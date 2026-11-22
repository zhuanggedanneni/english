"""Local Qwen through Ollama's OpenAI-compatible API.

Same contract as ``deepseek_client``: callers pass OpenAI-style messages and
get a ``ChatResult`` or an async stream of text deltas. The local endpoint has
no auth and is text-only, so image requests stay on DeepSeek (routing lives in
``deepseek_client``).
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from .config import settings
from .deepseek_client import ChatResult, DeepSeekError


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if settings.qwen_api_key:
        headers["Authorization"] = f"Bearer {settings.qwen_api_key}"
    return headers


def _clean_json(text: str) -> str:
    """Qwen有时会把 JSON 包在 ```json 里，解析前先剥掉。"""
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if text and text[0] not in "{[":
        starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
        if starts:
            start = min(starts)
            end = max(text.rfind("}"), text.rfind("]"))
            if end > start:
                text = text[start:end + 1]
    return text


def _error(response: httpx.Response) -> DeepSeekError:
    try:
        data = response.json()
        detail = str((data or {}).get("error") or (data or {}).get("message") or "")[:300]
    except ValueError:
        detail = (response.text or "").strip()[:300]
    if response.status_code == 404:
        return DeepSeekError(
            f"本地 Qwen 没有找到模型 {settings.qwen_model}。",
            status=404,
            code="model_not_found",
            hint="运行 ollama list 确认模型名，或改 .env 里的 QWEN_MODEL。",
        )
    return DeepSeekError(
        f"本地 Qwen 返回 {response.status_code}：{detail or '没有更多信息'}",
        status=response.status_code,
        code="qwen_error",
        hint=f"确认 Ollama 在 {settings.qwen_base_url} 正常运行。",
    )


async def _post(payload: dict[str, Any], timeout: float) -> httpx.Response:
    url = f"{settings.qwen_base_url}/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(url, headers=_headers(), json=payload)
    except httpx.HTTPError as error:
        raise DeepSeekError(
            f"无法连接本地 Qwen（{url}）：{type(error).__name__}。",
            code="network_error",
            hint="先启动 Ollama / 启动服务.bat，确认 127.0.0.1:11434 可访问。",
        ) from error


async def chat(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    json_mode: bool = True,
    temperature: float = 0.0,
    max_tokens: int = 1200,
    timeout: float = 90.0,
    thinking: bool = False,
) -> ChatResult:
    if not settings.qwen_base_url:
        raise DeepSeekError(
            "没有配置本地 Qwen 地址。",
            code="unconfigured",
            hint="在 .env 里设置 QWEN_BASE_URL（默认 http://127.0.0.1:11434/v1）。",
        )
    model = model or settings.qwen_model
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if not thinking:
        payload["reasoning_effort"] = "none"

    response = await _post(payload, timeout)
    if response.status_code >= 400:
        raise _error(response)
    try:
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise DeepSeekError(
            "本地 Qwen 返回的内容不是预期的对话结构。",
            code="bad_response",
            hint="确认 Ollama 的 /v1/chat/completions 返回 OpenAI 格式。",
        ) from error

    text = str(content or "")
    if json_mode:
        text = _clean_json(text)
    return ChatResult(
        text=text,
        model=str(data.get("model") or model),
        endpoint=f"{settings.qwen_base_url}/chat/completions",
        usage=data.get("usage") or {},
    )


async def stream_chat(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float = 0.9,
    max_tokens: int = 180,
    timeout: float = 30.0,
) -> AsyncIterator[str]:
    if not settings.qwen_base_url:
        raise DeepSeekError(
            "没有配置本地 Qwen 地址。",
            code="unconfigured",
            hint="在 .env 里设置 QWEN_BASE_URL（默认 http://127.0.0.1:11434/v1）。",
        )
    model = model or settings.qwen_model
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        "reasoning_effort": "none",
    }
    url = f"{settings.qwen_base_url}/chat/completions"
    limits = httpx.Timeout(timeout, connect=10.0, write=15.0, pool=10.0)
    try:
        async with httpx.AsyncClient(timeout=limits) as client:
            async with client.stream("POST", url, headers=_headers(), json=payload) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise _error(response)
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if not chunk:
                        continue
                    if chunk == "[DONE]":
                        break
                    try:
                        body = json.loads(chunk)
                    except ValueError:
                        continue
                    choices = body.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield str(piece)
    except httpx.HTTPError as error:
        raise DeepSeekError(
            f"本地 Qwen 流式请求失败：{type(error).__name__}。",
            code="network_error",
            hint="确认 Ollama 在 127.0.0.1:11434 正常运行。",
        ) from error
