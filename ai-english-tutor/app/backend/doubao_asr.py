"""Doubao (Volcengine) streaming ASR v3 binary protocol.

The browser streams 16 kHz mono PCM16 to our backend; this module wraps the same
audio into Volcengine's framed/gzip protocol and turns server frames back into
plain ``{"type": "partial"|"final", "text": ...}`` events.

Reference: POST wss://your-speech-endpoint.example.com/asr
Headers: X-Api-Key, X-Api-Resource-Id, X-Api-Request-Id.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import struct
import uuid
from typing import AsyncIterator

import websockets

from .config import settings

PROTOCOL_VERSION = 0b0001
HEADER_SIZE = 0b0001  # 4 bytes

FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_REQUEST = 0b0010
FULL_SERVER_RESPONSE = 0b1001
SERVER_ACK = 0b1011
SERVER_ERROR_RESPONSE = 0b1111

NO_SEQUENCE = 0b0000
POS_SEQUENCE = 0b0001
LAST_NO_SEQUENCE = 0b0010
LAST_WITH_SEQUENCE = 0b0011

JSON = 0b0001
GZIP = 0b0001


class DoubaoAsrError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


def _header(message_type: int, flags: int, serialization: int = JSON, compression: int = GZIP) -> bytes:
    return bytes([
        (PROTOCOL_VERSION << 4) | HEADER_SIZE,
        (message_type << 4) | flags,
        (serialization << 4) | compression,
        0x00,
    ])


def _gzip(payload: bytes) -> bytes:
    return gzip.compress(payload, mtime=0)


def build_full_request(payload: dict) -> bytes:
    body = _gzip(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return _header(FULL_CLIENT_REQUEST, NO_SEQUENCE) + struct.pack(">I", len(body)) + body


def build_audio_frame(chunk: bytes, *, last: bool = False) -> bytes:
    body = _gzip(chunk)
    flags = LAST_NO_SEQUENCE if last else NO_SEQUENCE
    return _header(AUDIO_ONLY_REQUEST, flags) + struct.pack(">I", len(body)) + body


def parse_frame(data: bytes) -> dict:
    """Decode one server frame into a dict with type/seq/payload."""
    if len(data) < 4:
        raise DoubaoAsrError("收到了不完整的语音识别数据包")
    first, second, third, _ = data[:4]
    header_bytes = (first & 0x0F) * 4
    message_type = second >> 4
    flags = second & 0x0F
    compression = third & 0x0F
    offset = header_bytes

    if message_type == SERVER_ERROR_RESPONSE:
        code = struct.unpack(">I", data[offset:offset + 4])[0]
        size = struct.unpack(">I", data[offset + 4:offset + 8])[0]
        offset += 8
        payload = data[offset:offset + size]
        if compression == GZIP and payload:
            payload = gzip.decompress(payload)
        message = payload.decode("utf-8", "replace") if payload else ""
        try:
            message = json.dumps(json.loads(message), ensure_ascii=False)
        except ValueError:
            pass
        raise DoubaoAsrError(f"语音识别服务返回错误 {code}：{message[:200]}", code=code)

    sequence = None
    if flags & 0b0001:
        sequence = struct.unpack(">i", data[offset:offset + 4])[0]
        offset += 4

    size = struct.unpack(">I", data[offset:offset + 4])[0]
    offset += 4
    payload = data[offset:offset + size]
    if compression == GZIP and payload:
        payload = gzip.decompress(payload)

    body: dict = {}
    if payload:
        try:
            body = json.loads(payload.decode("utf-8"))
        except ValueError:
            body = {"raw": payload.decode("utf-8", "replace")}
    return {"type": message_type, "flags": flags, "sequence": sequence, "body": body}


def _result_text(body: dict) -> str:
    payload = body.get("payload_msg") if isinstance(body.get("payload_msg"), dict) else body
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, list):
        result = result[0] if result else {}
    if isinstance(result, dict):
        return str(result.get("text") or "")
    return str(payload.get("text") or "") if isinstance(payload, dict) else ""


def request_payload() -> dict:
    return {
        "user": {"uid": "echo-learner"},
        "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1},
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "enable_nonstream": True,
            "show_utterances": True,
            "result_type": "full",
            "end_window_size": 600,
            "force_to_speech_time": 800,
        },
    }


async def stream_pcm(audio: AsyncIterator[bytes]) -> AsyncIterator[dict]:
    """Yield partial/final transcript events while audio is being uploaded."""
    if not settings.doubao_configured:
        raise DoubaoAsrError("尚未配置豆包 API Key")

    headers = {
        "X-Api-Key": settings.doubao_api_key,
        "X-Api-Resource-Id": settings.doubao_asr_resource_id,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }
    try:
        socket = await websockets.connect(
            settings.doubao_asr_url,
            additional_headers=headers,
            max_size=None,
            open_timeout=12,
            close_timeout=5,
            ping_interval=20,
        )
    except Exception as error:  # noqa: BLE001 - surfaced as a readable message
        raise DoubaoAsrError(f"无法连接豆包语音识别：{error}") from error

    async with socket:
        await socket.send(build_full_request(request_payload()))
        events: asyncio.Queue[dict | None] = asyncio.Queue()

        async def reader():
            try:
                async for raw in socket:
                    if isinstance(raw, str):
                        continue
                    frame = parse_frame(raw)
                    text = _result_text(frame["body"])
                    finished = bool(frame["body"].get("is_last_package"))
                    if text:
                        events.put_nowait({"type": "final" if finished else "partial", "text": text})
                    if finished:
                        break
            except DoubaoAsrError as error:
                events.put_nowait({"type": "error", "text": str(error)})
            except Exception as error:  # noqa: BLE001
                events.put_nowait({"type": "error", "text": f"语音识别中断：{error}"})
            finally:
                events.put_nowait(None)

        async def sender():
            try:
                async for chunk in audio:
                    if chunk:
                        await socket.send(build_audio_frame(chunk))
                await socket.send(build_audio_frame(b"", last=True))
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                # A turn ends in three normal ways: the audio generator is
                # drained and closed by the caller, the provider already sent
                # its last frame, or the socket is gone. None of them is worth
                # showing to the student as an upload failure.
                if socket.close_code is not None or isinstance(error, (AttributeError, StopAsyncIteration)):
                    return
                events.put_nowait({"type": "error", "text": f"音频上传失败：{error}"})
                events.put_nowait(None)

        reader_task = asyncio.create_task(reader())
        sender_task = asyncio.create_task(sender())
        try:
            while True:
                event = await events.get()
                if event is None:
                    break
                yield event
                if event.get("type") == "error":
                    break
        finally:
            sender_task.cancel()
            reader_task.cancel()
            await asyncio.gather(sender_task, reader_task, return_exceptions=True)
