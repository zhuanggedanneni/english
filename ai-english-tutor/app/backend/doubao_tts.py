"""Doubao (Volcengine) bidirectional streaming TTS, v3 event protocol.

One WebSocket connection is reused for a whole conversation; each sentence is a
separate session inside it, so the handshake cost is paid once per lesson rather
than once per sentence.

    wss://your-speech-endpoint.example.com/tts
    headers: X-Api-Key, X-Api-Resource-Id, X-Api-Connect-Id

    StartConnection(1)  -> ConnectionStarted(50)
    StartSession(100)   -> SessionStarted(150)
    TaskRequest(200)    -> TTSResponse(352) audio, TTSEnded(359)
    FinishSession(102)  -> SessionFinished(152)

Reference: https://www.volcengine.com/docs/6561/1329505
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
AUDIO_ONLY_SERVER = 0b1011
FRONT_END_RESULT = 0b1100
SERVER_ERROR_RESPONSE = 0b1111

FLAG_WITH_EVENT = 0b0100
JSON = 0b0001
GZIP = 0b0001
NO_COMPRESSION = 0b0000

EVENT_START_CONNECTION = 1
EVENT_FINISH_CONNECTION = 2
EVENT_CONNECTION_STARTED = 50
EVENT_CONNECTION_FAILED = 51
EVENT_CONNECTION_FINISHED = 52
EVENT_START_SESSION = 100
EVENT_CANCEL_SESSION = 101
EVENT_FINISH_SESSION = 102
EVENT_SESSION_STARTED = 150
EVENT_SESSION_CANCELED = 151
EVENT_SESSION_FINISHED = 152
EVENT_SESSION_FAILED = 153
EVENT_USAGE_RESPONSE = 154
EVENT_TASK_REQUEST = 200
EVENT_SENTENCE_START = 350
EVENT_SENTENCE_END = 351
EVENT_TTS_RESPONSE = 352
EVENT_TTS_ENDED = 359

CONNECTION_EVENTS = {EVENT_CONNECTION_STARTED, EVENT_CONNECTION_FAILED, EVENT_CONNECTION_FINISHED}


class DoubaoTtsError(RuntimeError):
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


def _compress(payload: bytes) -> bytes:
    return gzip.compress(payload, mtime=0) if payload else payload


def build_event(event: int, payload: dict | bytes | None = None, *, session_id: str | None = None) -> bytes:
    """Serialise one client event frame: header + event + sid + payload."""
    if isinstance(payload, dict):
        body = _compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        compression = GZIP
    else:
        body = _compress(payload or b"")
        compression = GZIP if body else NO_COMPRESSION
    frame = bytearray(_header(FULL_CLIENT_REQUEST, FLAG_WITH_EVENT, compression=compression))
    frame += struct.pack(">I", event)
    if session_id is not None:
        encoded = session_id.encode("utf-8")
        frame += struct.pack(">I", len(encoded)) + encoded
    frame += struct.pack(">I", len(body)) + body
    return bytes(frame)


def parse_event(data: bytes) -> dict:
    """Decode a server frame.

    Layouts differ by event family (connection events carry no session id), so
    both are attempted and the one whose length fields add up wins.
    """
    if len(data) < 8:
        raise DoubaoTtsError("收到了不完整的语音合成数据包")
    first, second, third, _ = data[:4]
    header_bytes = (first & 0x0F) * 4
    message_type = second >> 4
    flags = second & 0x0F
    serialization = third >> 4
    compression = third & 0x0F
    offset = max(header_bytes, 4)

    if message_type == SERVER_ERROR_RESPONSE:
        code = struct.unpack(">I", data[offset:offset + 4])[0]
        offset += 4
        size = struct.unpack(">I", data[offset:offset + 4])[0]
        offset += 4
        body = data[offset:offset + size]
        if compression == GZIP and body:
            body = gzip.decompress(body)
        raise DoubaoTtsError(f"语音合成服务返回错误 {code}：{body.decode('utf-8', 'replace')[:200]}", code=code)

    event = struct.unpack(">I", data[offset:offset + 4])[0] if flags & FLAG_WITH_EVENT else None
    offset += 4 if flags & FLAG_WITH_EVENT else 0

    session_id = None
    payload = b""
    candidates = [True, False] if event not in CONNECTION_EVENTS else [False]
    for with_session in candidates:
        cursor = offset
        try:
            if with_session:
                if cursor + 4 > len(data):
                    continue
                size = struct.unpack(">I", data[cursor:cursor + 4])[0]
                cursor += 4
                if size > 128 or cursor + size + 4 > len(data):
                    continue
                session_id = data[cursor:cursor + size].decode("utf-8")
                cursor += size
            if cursor + 4 > len(data):
                continue
            size = struct.unpack(">I", data[cursor:cursor + 4])[0]
            cursor += 4
            if cursor + size != len(data):
                continue
            payload = data[cursor:cursor + size]
            break
        except (struct.error, UnicodeDecodeError):
            continue
    else:
        session_id = None
        payload = data[offset + 4:] if len(data) >= offset + 4 else b""

    if compression == GZIP and payload:
        try:
            payload = gzip.decompress(payload)
        except OSError:
            pass

    body: dict = {}
    if payload and message_type != AUDIO_ONLY_SERVER and serialization == JSON:
        try:
            body = json.loads(payload.decode("utf-8"))
        except ValueError:
            body = {}
    return {
        "event": event,
        "message_type": message_type,
        "session_id": session_id,
        "audio": payload if message_type in (AUDIO_ONLY_SERVER, FRONT_END_RESULT) else b"",
        "body": body,
    }


def start_session_payload(text: str = "", voice: str | None = None) -> dict:
    return {
        "user": {"uid": "echo-learner"},
        "namespace": "BidirectionalTTS",
        "req_params": {
            "speaker": voice or settings.doubao_tts_voice,
            "audio_params": {
                "format": "pcm",
                "sample_rate": 24000,
                "channel": 1,
                "speech_rate": 0,
            },
            "text": text,
        },
    }


def task_request_payload(text: str) -> dict:
    return {
        "user": {"uid": "echo-learner"},
        "namespace": "BidirectionalTTS",
        "req_params": {"text": text},
    }


class DoubaoTTS:
    """Reusable bidirectional TTS connection for one conversation."""

    def __init__(self, voice: str | None = None) -> None:
        self.voice = voice or settings.doubao_tts_voice
        self._socket = None
        self._lock = asyncio.Lock()
        self._pending: list[dict] = []
        self.usage_characters = 0

    @property
    def connected(self) -> bool:
        return self._socket is not None

    async def connect(self, timeout: float = 10.0) -> None:
        if not settings.doubao_configured:
            raise DoubaoTtsError("尚未配置豆包 API Key")
        headers = {
            "X-Api-Key": settings.doubao_api_key,
            "X-Api-Resource-Id": settings.doubao_tts_resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }
        try:
            self._socket = await websockets.connect(
                settings.doubao_tts_url,
                additional_headers=headers,
                max_size=None,
                open_timeout=timeout,
                close_timeout=5,
                ping_interval=20,
            )
        except Exception as error:  # noqa: BLE001 - surfaced as a readable message
            raise DoubaoTtsError(f"无法连接豆包语音合成：{error}") from error
        await self._socket.send(build_event(EVENT_START_CONNECTION, {}))
        frame = await self._recv(timeout)
        if frame["event"] == EVENT_CONNECTION_FAILED:
            raise DoubaoTtsError(f"语音合成连接被拒绝：{json.dumps(frame['body'], ensure_ascii=False)[:200]}")
        if frame["event"] != EVENT_CONNECTION_STARTED:
            raise DoubaoTtsError(f"语音合成握手异常（事件 {frame['event']}）")

    async def _recv(self, timeout: float = 20.0) -> dict:
        assert self._socket is not None
        try:
            raw = await asyncio.wait_for(self._socket.recv(), timeout=timeout)
        except asyncio.TimeoutError as error:
            raise DoubaoTtsError("语音合成响应超时") from error
        if isinstance(raw, str):
            raise DoubaoTtsError(f"语音合成返回了非二进制数据：{raw[:120]}")
        return parse_event(raw)

    async def speak(self, text: str, *, timeout: float = 20.0) -> AsyncIterator[bytes]:
        """Synthesise one sentence, yielding PCM16/24k mono chunks as they arrive.

        Flow: StartSession -> SessionStarted -> TaskRequest(text) ->
        FinishSession -> TTSResponse* -> SessionFinished.
        """
        text = (text or "").strip()
        if not text:
            return
        async with self._lock:
            if self._socket is None:
                await self.connect()
            socket = self._socket
            assert socket is not None
            session_id = str(uuid.uuid4())
            await socket.send(build_event(EVENT_START_SESSION, start_session_payload(voice=self.voice), session_id=session_id))
            deadline = asyncio.get_running_loop().time() + timeout

            while True:
                frame = await self._recv(max(0.5, deadline - asyncio.get_running_loop().time()))
                if frame["session_id"] not in (None, session_id):
                    continue
                if frame["event"] == EVENT_SESSION_STARTED:
                    break
                if frame["event"] == EVENT_SESSION_FAILED:
                    raise DoubaoTtsError(
                        f"语音合成会话失败：{json.dumps(frame['body'], ensure_ascii=False)[:200]}"
                    )

            await socket.send(build_event(EVENT_TASK_REQUEST, task_request_payload(text), session_id=session_id))
            await socket.send(build_event(EVENT_FINISH_SESSION, {}, session_id=session_id))

            try:
                while True:
                    frame = await self._recv(max(0.5, deadline - asyncio.get_running_loop().time()))
                    event = frame["event"]
                    if frame["session_id"] not in (None, session_id):
                        continue
                    if event in (EVENT_TTS_RESPONSE, FRONT_END_RESULT):
                        if frame["audio"]:
                            yield frame["audio"]
                        continue
                    if event == EVENT_SESSION_FAILED:
                        raise DoubaoTtsError(
                            f"语音合成失败：{json.dumps(frame['body'], ensure_ascii=False)[:200]}"
                        )
                    if event == EVENT_USAGE_RESPONSE:
                        usage = (frame["body"] or {}).get("usage") or {}
                        self.usage_characters += int(usage.get("text_words") or 0)
                        continue
                    if event in (EVENT_SESSION_FINISHED, EVENT_TTS_ENDED):
                        break
            except DoubaoTtsError as error:
                if "超时" not in str(error):
                    raise

    async def close(self) -> None:
        socket, self._socket = self._socket, None
        if socket is None:
            return
        try:
            await socket.send(build_event(EVENT_FINISH_CONNECTION, {}))
        except Exception:  # noqa: BLE001
            pass
        try:
            await socket.close()
        except Exception:  # noqa: BLE001
            pass
