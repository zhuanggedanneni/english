"""DeepSeek HTTP client with tolerant endpoint handling and a quota-aware probe.

Only two things are assumed: an OpenAI-style chat completion payload and a
Bearer token. Everything else (base path, model name, whether ``response_format``
is accepted) is discovered or configured, because the exact DeepSeek Flash
vision contract can differ between deployments.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from .config import settings

LOGGER = logging.getLogger("uvicorn.error")

# Thinking mode is ON by default with effort=high (official docs, 2026-11).
# A spoken turn needs a first token in hundreds of milliseconds, not a chain of
# thought, so every latency-sensitive call disables it explicitly.
THINKING_OFF = {"type": "disabled"}

# Endpoint candidates in the order we try them. Official DeepSeek exposes both
# /chat/completions and /v1/chat/completions; other gateways usually keep one.
CHAT_PATHS = ("/chat/completions", "/v1/chat/completions")
MODEL_PATHS = ("/models", "/v1/models")


class DeepSeekError(RuntimeError):
    """Raised when the remote service rejects or cannot answer a request."""

    def __init__(self, message: str, *, status: int | None = None, code: str = "unknown", hint: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.hint = hint

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "status": self.status, "message": str(self), "hint": self.hint}


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _has_image(messages: list[dict[str, Any]]) -> bool:
    """Local Qwen is text-only: photo grading must keep using DeepSeek."""
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "image_url" for part in content
        ):
            return True
    return False


def _classify_status(status: int) -> tuple[str, str, str]:
    """Map an HTTP status to (code, user message, actionable hint)."""
    if status in (401, 403):
        return (
            "auth_failed",
            "DeepSeek 拒绝了这组凭证。",
            "确认 API Key 没有多余空格，且与当前 Base URL 属于同一个平台。",
        )
    if status == 402:
        return (
            "insufficient_balance",
            "DeepSeek 账户余额不足，无法完成本次识别。",
            "充值后即可恢复；之前保存的照片和听写记录不会丢失。",
        )
    if status == 404:
        return (
            "endpoint_not_found",
            "这个 Base URL 下找不到对应的接口。",
            "核对接口文档里的 Base URL 是否应带 /v1，或改用自己的网关地址。",
        )
    if status == 429:
        return (
            "rate_limited",
            "请求过于频繁，已被限流。",
            "等几秒再试；批量批改时建议一次不超过 8 个词。",
        )
    if status == 400:
        return (
            "bad_request",
            "服务不接受这次请求的格式。",
            "常见原因是模型不支持图片输入，或接口不接受 response_format 参数。",
        )
    if status >= 500:
        return (
            "server_error",
            "DeepSeek 服务端暂时异常。",
            "稍后重试；若持续出现，检查平台状态页。",
        )
    return ("http_error", f"DeepSeek 返回了 {status}。", "")


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return (response.text or "").strip()[:300]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or "")[:300]
        if isinstance(error, str):
            return error[:300]
        return str(data.get("message") or "")[:300]
    return ""


@dataclass
class ChatResult:
    text: str
    model: str
    endpoint: str
    usage: dict[str, Any] = field(default_factory=dict)


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
    """Send a chat completion, retrying the payload shape if the server objects."""
    from . import llm_provider

    has_image = _has_image(messages)
    use_qwen = llm_provider.current() == llm_provider.QWEN and not has_image
    LOGGER.info(
        "[llm %s] chat provider=%s model=%s image=%s json=%s",
        time.strftime("%H:%M:%S"),
        "qwen" if use_qwen else "deepseek",
        model or (settings.qwen_model if use_qwen else settings.deepseek_vision_model),
        has_image,
        json_mode,
    )

    if use_qwen:
        from .qwen_client import chat as qwen_chat

        return await qwen_chat(
            messages,
            model=model,
            json_mode=json_mode,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            thinking=thinking,
        )

    if not settings.deepseek_api_key:
        raise DeepSeekError(
            "没有配置 DeepSeek API Key。",
            code="unconfigured",
            hint="把 Key 写进 .env 的 DEEPSEEK_API_KEY，重启服务后生效。",
        )
    model = model or settings.deepseek_vision_model
    base = settings.deepseek_base_url.rstrip("/")
    last_error: DeepSeekError | None = None

    async with httpx.AsyncClient(timeout=timeout) as client:
        for path in CHAT_PATHS:
            payload: dict[str, Any] = {
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            if not thinking:
                payload["thinking"] = THINKING_OFF
            try:
                response = await client.post(f"{base}{path}", headers=_headers(settings.deepseek_api_key), json=payload)
            except httpx.HTTPError as error:
                last_error = DeepSeekError(
                    f"无法连接 DeepSeek：{type(error).__name__}。",
                    code="network_error",
                    hint=f"核对 Base URL 和本机网络；当前地址 {base}{path}。",
                )
                continue

            # Some gateways reject response_format or the thinking field; retry
            # once without whichever one the error text blames.
            if response.status_code == 400:
                body = response.text.lower()
                dropped = False
                if json_mode and "response_format" in body:
                    payload.pop("response_format", None)
                    dropped = True
                if not thinking and "thinking" in body and "thinking" in payload:
                    payload.pop("thinking", None)
                    dropped = True
                if dropped:
                    try:
                        response = await client.post(
                            f"{base}{path}", headers=_headers(settings.deepseek_api_key), json=payload
                        )
                    except httpx.HTTPError as error:
                        last_error = DeepSeekError(f"无法连接 DeepSeek：{type(error).__name__}。", code="network_error")
                        continue

            if response.status_code == 404:
                last_error = DeepSeekError(
                    "接口路径不存在。", status=404, code="endpoint_not_found",
                    hint=f"已尝试 {base}{path}，请核对文档中的 Base URL。",
                )
                continue

            if response.status_code >= 400:
                code, message, hint = _classify_status(response.status_code)
                detail = _extract_error_detail(response)
                raise DeepSeekError(
                    f"{message}{('（' + detail + '）') if detail else ''}",
                    status=response.status_code,
                    code=code,
                    hint=hint,
                )

            try:
                data = response.json()
                content = data["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError) as error:
                raise DeepSeekError(
                    "服务返回的内容不是预期的对话结构。",
                    status=response.status_code,
                    code="bad_response",
                    hint="如果这是自建网关，请确认它兼容 OpenAI 的 choices[0].message.content。",
                ) from error

            if isinstance(content, list):  # some gateways return content parts
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            return ChatResult(
                text=str(content or ""),
                model=str(data.get("model") or model),
                endpoint=f"{base}{path}",
                usage=data.get("usage") or {},
            )

    raise last_error or DeepSeekError("DeepSeek 请求失败。", code="unknown")


async def stream_chat(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float = 0.9,
    max_tokens: int = 180,
    timeout: float = 30.0,
) -> AsyncIterator[str]:
    """Yield answer deltas as SSE arrives. Thinking mode is always disabled.

    ``reasoning_content`` is ignored on purpose: with thinking off the model
    sends none, and a spoken reply must never wait for a chain of thought.
    """
    from . import llm_provider

    use_qwen = llm_provider.current() == llm_provider.QWEN
    LOGGER.info(
        "[llm %s] stream provider=%s model=%s",
        time.strftime("%H:%M:%S"),
        "qwen" if use_qwen else "deepseek",
        model or (settings.qwen_model if use_qwen else settings.deepseek_vision_model),
    )

    if use_qwen:
        from .qwen_client import stream_chat as qwen_stream

        async for delta in qwen_stream(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        ):
            yield delta
        return

    if not settings.deepseek_api_key:
        raise DeepSeekError(
            "没有配置 DeepSeek API Key。",
            code="unconfigured",
            hint="把 Key 写进 .env 的 DEEPSEEK_API_KEY，重启服务后生效。",
        )
    model = model or settings.deepseek_vision_model
    base = settings.deepseek_base_url.rstrip("/")
    last_error: DeepSeekError | None = None

    limits = httpx.Timeout(timeout, connect=10.0, write=15.0, pool=10.0)
    async with httpx.AsyncClient(timeout=limits) as client:
        for path in CHAT_PATHS:
            payload: dict[str, Any] = {
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "messages": messages,
                "stream": True,
                "thinking": THINKING_OFF,
            }
            url = f"{base}{path}"
            try:
                async with client.stream("POST", url, headers=_headers(settings.deepseek_api_key), json=payload) as response:
                    if response.status_code == 404:
                        last_error = DeepSeekError(
                            "接口路径不存在。", status=404, code="endpoint_not_found",
                            hint=f"已尝试 {url}，请核对文档中的 Base URL。",
                        )
                        continue
                    if response.status_code >= 400:
                        raw = (await response.aread()).decode("utf-8", "replace")
                        code, message, hint = _classify_status(response.status_code)
                        # A gateway may reject the thinking field itself: retry without it.
                        if response.status_code == 400 and "thinking" in raw.lower():
                            payload.pop("thinking", None)
                            last_error = DeepSeekError(
                                f"{message}（当前网关不接受 thinking 字段）", status=400, code=code, hint=hint
                            )
                            continue
                        raise DeepSeekError(
                            f"{message}{('（' + raw[:200] + '）') if raw else ''}",
                            status=response.status_code, code=code, hint=hint,
                        )

                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if not chunk or chunk == "[DONE]":
                            if chunk == "[DONE]":
                                break
                            continue
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
                    return
            except httpx.HTTPError as error:
                last_error = DeepSeekError(
                    f"无法连接 DeepSeek：{type(error).__name__}。",
                    code="network_error",
                    hint=f"核对 Base URL 和本机网络；当前地址 {url}。",
                )
                continue

    raise last_error or DeepSeekError("DeepSeek 流式请求失败。", code="unknown")



def image_part(image_bytes: bytes, mime: str) -> dict[str, Any]:
    mime = mime if mime.startswith("image/") else "image/jpeg"
    encoded = base64.b64encode(image_bytes).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


@dataclass
class ProbeResult:
    configured: bool
    base_url: str
    model: str
    reachable: bool
    http_status: int | None
    latency_ms: int | None
    verdict: str
    message: str
    hint: str = ""
    endpoint: str = ""
    models: list[str] = field(default_factory=list)
    model_available: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "base_url": self.base_url,
            "model": self.model,
            "reachable": self.reachable,
            "http_status": self.http_status,
            "latency_ms": self.latency_ms,
            "verdict": self.verdict,
            "message": self.message,
            "hint": self.hint,
            "endpoint": self.endpoint,
            "models": self.models,
            "model_available": self.model_available,
        }


async def probe(timeout: float = 12.0) -> ProbeResult:
    """Check reachability, credentials and quota without spending completion tokens.

    `/models` is free to call, and a 402 there still proves the service is
    reachable and the key is recognised — which is exactly what "connectivity"
    means for an account that is currently in arrears.
    """
    base = settings.deepseek_base_url.rstrip("/")
    model = settings.deepseek_vision_model
    if not settings.deepseek_api_key:
        return ProbeResult(
            configured=False,
            base_url=base,
            model=model,
            reachable=False,
            http_status=None,
            latency_ms=None,
            verdict="unconfigured",
            message="尚未填写 DeepSeek API Key。",
            hint="把 Key 写入 .env 的 DEEPSEEK_API_KEY 后重启服务。",
        )

    last_status: int | None = None
    last_latency: int | None = None
    last_endpoint = ""
    network_error = ""

    async with httpx.AsyncClient(timeout=timeout) as client:
        for path in MODEL_PATHS:
            started = time.perf_counter()
            try:
                response = await client.get(f"{base}{path}", headers=_headers(settings.deepseek_api_key))
            except httpx.HTTPError as error:
                network_error = f"{type(error).__name__}: {error}"
                continue
            latency = int((time.perf_counter() - started) * 1000)
            last_status, last_latency, last_endpoint = response.status_code, latency, f"{base}{path}"

            if response.status_code == 200:
                models: list[str] = []
                try:
                    data = response.json()
                    rows = data.get("data") if isinstance(data, dict) else None
                    if isinstance(rows, list):
                        models = [str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")]
                except ValueError:
                    pass
                available = (model in models) if models else None
                message = "连接正常，凭证有效。"
                hint = ""
                if available is False:
                    message = "连接与凭证正常，但模型列表里没有配置的模型名。"
                    hint = f"可用模型：{', '.join(models[:8])}；请核对 DEEPSEEK_VISION_MODEL。"
                return ProbeResult(
                    configured=True, base_url=base, model=model, reachable=True,
                    http_status=200, latency_ms=latency, verdict="ok",
                    message=message, hint=hint, endpoint=last_endpoint,
                    models=models, model_available=available,
                )

            if response.status_code == 404:
                continue  # try /v1/models
            break

    if last_status is None:
        return ProbeResult(
            configured=True, base_url=base, model=model, reachable=False,
            http_status=None, latency_ms=last_latency, verdict="network_error",
            message="无法建立到 DeepSeek 的网络连接。",
            hint=f"{network_error[:160]}；核对 Base URL 与代理设置。",
            endpoint=last_endpoint,
        )

    code, message, hint = _classify_status(last_status)
    # 402 still means: reachable + key recognised, quota exhausted.
    verdict = "insufficient_balance" if last_status == 402 else code
    return ProbeResult(
        configured=True, base_url=base, model=model,
        # Any HTTP answer — including 401/402 — proves the host is reachable.
        reachable=True,
        http_status=last_status, latency_ms=last_latency,
        verdict=verdict, message=message, hint=hint, endpoint=last_endpoint,
    )
