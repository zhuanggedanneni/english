"""One WebSocket carries a whole spoken lesson.

Why a single socket: a turn needs the student's audio up, the transcript down,
the reply down and the reply audio down, all interleaved. Doing that over HTTP
means three round trips per turn and a visible stall between them.

The channel is **half-duplex on purpose**: from the moment the tutor starts
thinking until the last synthesised word has been played, the microphone is
muted. The student cannot accidentally cut the tutor off, and the tutor never
answers over its own voice. The turn boundary itself is still decided on the
server from raw PCM energy, not by a button.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import struct
import time
import uuid
from array import array

from fastapi import WebSocket

from .config import settings
from . import llm_provider
from .content import TOPICS
from .deepseek_client import DeepSeekError, stream_chat
from .doubao_tts import DoubaoTTS, DoubaoTtsError
from .grades import DEFAULT_GRADE
from .grammar_coach import analyze as analyze_grammar
from .translate import to_chinese
from .providers import speech_evaluator
from .scene_gen import generate as generate_scene
from .store import store, utc_now
from .tutor import clean as clean_reply
from .tutor import system_prompt

# 发音带读：什么时候该停下来纠一个词（分数来自本地逐词评测）
COACH_MIN_TURN = 3       # 前三轮只聊不纠
COACH_WEAK = 75          # 低于这个分才值得停下来（本地模型对母语口音普遍给 70-80）
COACH_SEVERE = 65        # 明显读错：可以马上再纠
COACH_COOLDOWN = 2       # 轻微问题：两次带读至少隔这么多轮
COACH_IMPROVED = 8       # 同一个词进步不到 8 分就不重复纠
GRAMMAR_FIX_BELOW = 72    # 语法分低于这个才值得专门说一句
COACH_FLOOR = 80         # 明显偏弱；连着几轮没纠过时，用它补一次轻提示
COACH_LONG_GAP = 3       # 多久没纠过就允许补一次
PRON_STREAK_SEVERE = 65  # 连续两轮低于这个分，外教主动放慢带读
PRON_STREAK_GOOD = 82    # 连续两轮高于这个分，外教具体表扬并加难度
GRAMMAR_STREAK_GOOD = 85 # 连续两轮语法干净，算“说得很标准”
# 虚词在连读里本来就弱读，模型给它们的分天然偏低（I 常常 20 分）。
# 纠这些词既没用又打击人，带读只挑实词。
DRILL_LEAD = {"a", "an", "the", "my", "your", "his", "her", "this", "that", "some"}
DRILL_TAIL_STOP = {
    "a", "an", "the", "to", "of", "and", "but", "so", "for", "in", "on", "at",
    "with", "is", "are", "was", "were", "do", "does", "did", "can", "could", "would",
}


def drill_phrase(sentence: str, word: str) -> str:
    """Cut a 2-4 word chunk around *word* that a student can actually repeat.

    The model used to invent phrases ("why order") whenever it was asked to
    shorten the sentence itself, so the chunk is computed here and handed over
    verbatim: article/adjective + the word + its object, never ending on a
    preposition or an article.
    """
    tokens = re.findall(r"[A-Za-z']+", sentence or "")
    lowered = [token.lower() for token in tokens]
    target = (word or "").lower()
    if target not in lowered:
        return word or ""
    index = lowered.index(target)

    start = index - 1 if index > 0 and lowered[index - 1] in DRILL_LEAD else index
    end = index + 1
    if end < len(tokens) and lowered[end] not in DRILL_TAIL_STOP:
        end += 1
    chunk = tokens[start:end]
    while len(chunk) > 1 and chunk[-1].lower() in DRILL_TAIL_STOP:
        chunk.pop()
    return " ".join(chunk)


def lowest_content_word(word_results: list[dict]) -> dict | None:
    """The weakest content word of a turn, even when nothing dipped below the
    "weak" threshold. Used so a good speaker still gets an occasional nudge."""
    candidates = [
        {
            "word": item.get("word"),
            "score": item.get("score"),
            "phonemes": item.get("phonemes") or "",
            "symbols": [symbol for symbol in (item.get("weak_phonemes") or [])],
        }
        for item in word_results
        if isinstance(item.get("word"), str)
        and len(item["word"]) >= 3
        and item["word"].lower() not in COACH_SKIP
        and isinstance(item.get("score"), (int, float))
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda item: item["score"])


COACH_SKIP = {
    "i", "a", "an", "the", "of", "to", "it", "is", "am", "are", "was", "were", "be",
    "do", "does", "did", "can", "could", "would", "will", "shall", "should", "may",
    "and", "or", "but", "for", "in", "on", "at", "with", "by", "from", "as", "so",
    "my", "your", "me", "you", "we", "they", "he", "she", "his", "her", "its",
    "our", "their", "this", "that", "these", "those", "there", "here",
    "please", "thanks", "thank", "yes", "no", "oh", "just", "very", "also", "too",
}

ASR_RATE = 16000
TTS_RATE = 24000

VOICE_RMS = 380          # above this counts as speech
# 说完之后要安静这么久才认为"说完了"。1 秒会把句中换气切成一整轮，
# 学生刚说到一半就被打断、DeepSeek 只拿到半句。
SILENCE_SECONDS = float(os.getenv("TURN_SILENCE_SECONDS", "2.0"))
MAX_TURN_SECONDS = 45.0
WORDS_PER_SECOND = 2.2   # fallback timing when the browser voice reads the line

TOPIC_PROMPTS = {topic["id"]: list(topic.get("prompts") or []) for topic in TOPICS}

FALLBACK_PROMPTS = [
    "That's interesting — can you tell me a bit more about it?",
    "Why does that matter to you?",
    "What would you do next if you had the chance?",
]


CJK = re.compile(r"[\u3400-\u9fff]")
HELP_HINTS = re.compile(
    r"(i\s*(don'?t|do not|didn'?t)\s*(understand|get it|catch)|"
    r"what does .{0,30}mean|what do you mean|i'?m lost|"
    r"pardon|come again|say that again|can you repeat|repeat that|slow down|too fast|"
    r"再说(一遍)?|没听懂|听不懂|什么意思|说的什么|不懂|不会|用中文|中文)",
    re.IGNORECASE,
)


def cjk_ratio(text: str) -> float:
    text = (text or "").strip()
    if not text:
        return 0.0
    return len(CJK.findall(text)) / len(text)


CJK_PUNCT = "，。！？；：、（）「」『』…—·《》"


def language_segments(text: str) -> list[tuple[str, bool]]:
    """Split one sentence into Chinese / English runs for per-voice TTS.

    The tutor often writes "do you want to eat，这句就是你想吃吗". Spoken as a
    whole it would go to the Chinese voice and the English phrase would come out
    accented; split at the language boundary and each half gets a natural voice.
    """
    segments: list[tuple[str, bool]] = []
    current: list[str] = []
    kind: bool | None = None  # True = Chinese
    for char in text or "":
        if char.isspace():
            current.append(char)
            continue
        char_zh = bool(CJK.match(char)) or char in CJK_PUNCT
        if kind is None or char_zh == kind:
            current.append(char)
            kind = char_zh
            continue
        joined = "".join(current).strip()
        if joined:
            segments.append((joined, kind))
        current = [char]
        kind = char_zh
    joined = "".join(current).strip()
    if joined and kind is not None:
        segments.append((joined, kind))
    return segments


def is_chinese(text: str) -> bool:
    """True when a line is mostly Chinese — used to pick a Chinese TTS voice."""
    return cjk_ratio(text) >= 0.3


def asks_for_help(text: str) -> bool:
    """The student is lost, or simply wrote in Chinese."""
    return bool(HELP_HINTS.search(text or "")) or cjk_ratio(text) >= 0.3


def pcm_rms(chunk: bytes) -> int:
    if len(chunk) < 2:
        return 0
    samples = array("h")
    samples.frombytes(chunk[: len(chunk) // 2 * 2])
    if not samples:
        return 0
    total = 0
    for value in samples:
        total += value * value
    return int((total / len(samples)) ** 0.5)


def wav_bytes(pcm: bytes, rate: int = ASR_RATE) -> bytes:
    data_size = len(pcm)
    header = b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", data_size)
    return header + pcm


def split_sentences(text: str, *, max_len: int = 90) -> list[str]:
    """Split streamed text into speakable chunks without breaking mid-word."""
    import re

    parts = re.split(r"(?<=[.!?])\s+|(?<=[。！？])", text)
    chunks: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        while len(part) > max_len:
            cut = part.rfind(",", 0, max_len)
            if cut < 20:
                cut = part.rfind(" ", 0, max_len)
            if cut < 20:
                cut = max_len
            chunks.append(part[:cut + 1].strip())
            part = part[cut + 1:].strip()
        if part:
            chunks.append(part)
    return chunks


class _SentenceBuffer:
    """Accumulates streamed deltas and releases complete sentences early."""

    def __init__(self) -> None:
        self.buffer = ""

    def feed(self, delta: str) -> list[str]:
        self.buffer += delta
        ready: list[str] = []
        while True:
            index = -1
            for mark in ".!?。！？":
                position = self.buffer.find(mark)
                if position >= 0 and (index < 0 or position < index):
                    index = position
            if index < 0:
                # No sentence end yet. If enough words are already queued, ship a
                # clause so the voice starts before the sentence finishes.
                if len(self.buffer) < 58:
                    break
                comma = self.buffer.rfind(",", 30, 70)
                if comma < 0:
                    break
                clause = self.buffer[: comma + 1].strip()
                self.buffer = self.buffer[comma + 1:].lstrip()
                if clause:
                    ready.append(clause)
                continue
            if index + 1 >= len(self.buffer):
                break
            sentence = self.buffer[: index + 1].strip()
            self.buffer = self.buffer[index + 1:].lstrip()
            if sentence:
                ready.append(sentence)
        return ready

    def flush(self) -> list[str]:
        rest, self.buffer = self.buffer.strip(), ""
        return [rest] if rest else []


class ConversationSocket:
    def __init__(self, websocket: WebSocket) -> None:
        self.ws = websocket
        self.session: dict | None = None
        self.tts: DoubaoTTS | None = None
        self.tts_zh: DoubaoTTS | None = None
        self._tts_zh_task: asyncio.Task | None = None
        self.tts_ok = False
        self._send_lock = asyncio.Lock()

        self.asr_queue: asyncio.Queue[bytes | None] | None = None
        self.asr_task: asyncio.Task | None = None
        self.asr_active = False
        self.asr_text = ""
        self.asr_epoch = 0

        self.turn_pcm = bytearray()
        self.pre_roll: list[bytes] = []
        self.speech_seen = False
        self.last_voice_at = 0.0
        self.turn_started_at = 0.0

        self.ai_speaking = False
        self.playback_phase = False
        self.committing = False
        self.realtime_score = False
        # Realtime scoring is only meaningful with a real evaluator behind it;
        # the demo provider would otherwise print invented numbers into the UI.
        self.score_ready = settings.speech_mode in ("local", "xfyun")
        self.closed = False
        self.total_audio_seconds = 0.0
        self.audio_sent_bytes = 0
        self.fallback_words = 0
        self._playback_event: asyncio.Event | None = None
        self._playback_task: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None
        self._tts_task: asyncio.Task | None = None
        self.translate = False       # 学生开启后才翻译外教的话
        self.grammar_on = True       # 语法点评默认开，可在设置页关掉
        self._score_tasks: set[asyncio.Task] = set()
        self._feedback_tasks: set[asyncio.Task] = set()
        self._grammar_tasks: set[asyncio.Task] = set()
        self._translate_tasks: set[asyncio.Task] = set()

    # ── transport ──────────────────────────────────────────────
    async def send_json(self, payload: dict) -> None:
        if self.closed:
            return
        async with self._send_lock:
            try:
                await self.ws.send_json(payload)
            except Exception:  # noqa: BLE001 - client may vanish mid-sentence
                self.closed = True

    async def send_bytes(self, payload: bytes) -> None:
        if self.closed:
            return
        async with self._send_lock:
            try:
                await self.ws.send_bytes(payload)
            except Exception:  # noqa: BLE001
                self.closed = True

    async def error(self, code: str, text: str) -> None:
        await self.send_json({"type": "error", "code": code, "text": text})

    # ── lifecycle ──────────────────────────────────────────────
    async def run(self) -> None:
        await self.ws.accept()
        # Warm the TTS handshake while the browser is still asking for the mic:
        # it costs ~500ms and would otherwise sit on the critical path to the
        # very first sound.
        self._tts_task = asyncio.create_task(self._connect_tts()) if settings.doubao_configured else None
        await self.send_json({
            "type": "ready",
            "asr": "doubao" if settings.doubao_configured else "browser",
            "tts": "doubao" if settings.doubao_configured else "browser",
            "voice": settings.doubao_tts_voice,
            "sample_rate": TTS_RATE,
        })
        try:
            while not self.closed:
                message = await self.ws.receive()
                if message.get("bytes") is not None:
                    await self.on_audio(message["bytes"])
                    continue
                text = message.get("text")
                if text is None:
                    break
                try:
                    await self.on_message(json.loads(text))
                except json.JSONDecodeError:
                    continue
        except Exception as error:  # noqa: BLE001 - disconnect or protocol error
            if type(error).__name__ not in {"WebSocketDisconnect", "RuntimeError"}:
                await self.error("socket", f"对话连接中断：{error}")
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self.closed = True
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        if self._playback_task is not None:
            self._playback_task.cancel()
            self._playback_task = None
        self._playback_event = None
        await self.stop_asr()
        if self._tts_zh_task is not None:
            self._tts_zh_task.cancel()
            self._tts_zh_task = None
        seen: set[int] = set()
        for client in (self.tts, self.tts_zh):
            if client is not None and id(client) not in seen:
                seen.add(id(client))
                await client.close()
        self.tts = None
        self.tts_zh = None

    async def _watch_silence(self) -> None:
        """Commit a turn even if the client stops sending frames.

        Energy-based VAD only runs when audio arrives; a browser that pauses its
        audio worklet (background tab, Bluetooth hiccup) would otherwise leave
        the turn hanging forever.
        """
        while not self.closed:
            await asyncio.sleep(0.2)
            if (
                not self.ai_speaking
                and not self.committing
                and self.speech_seen
                and self.last_voice_at
                and time.monotonic() - self.last_voice_at >= SILENCE_SECONDS
            ):
                await self.commit_turn()

    async def on_message(self, payload: dict) -> None:
        kind = payload.get("type")
        if kind == "start":
            await self.start(payload)
        elif kind == "turn_end":
            # Half-duplex: while the tutor holds the floor the button does
            # nothing. The student's turn starts on its own once the audio ends.
            if self.ai_speaking or self.playback_phase:
                return
            await self.commit_turn(manual=True)
        elif kind == "turn_text":
            # Typed transcript (or the browser-recognition fallback).
            text = str(payload.get("text") or "").strip()
            if text and not self.ai_speaking and not self.committing:
                await self.handle_reply(text)
        elif kind == "playback_done":
            # The client finished playing the last PCM chunk. This is the real
            # end of the tutor's turn; the server-side estimate only backstops.
            if self._playback_event is not None:
                self._playback_event.set()
        elif kind == "score_toggle":
            self.realtime_score = bool(payload.get("enabled"))
            await self.send_json({
                "type": "score_config",
                "enabled": self.realtime_score,
                "ready": self.score_ready,
            })
        elif kind == "translate_toggle":
            self.translate = bool(payload.get("enabled"))
            await self.send_json({"type": "translate_config", "enabled": self.translate})
        elif kind == "grammar_toggle":
            self.grammar_on = bool(payload.get("enabled"))
            await self.send_json({"type": "grammar_config", "enabled": self.grammar_on})
        elif kind == "end":
            await self.finish()

    async def start(self, payload: dict) -> None:
        if self.session is not None:
            return
        scene_request = payload.get("scene") or {"kind": "preset", "id": "campus"}
        grade = str(payload.get("grade") or DEFAULT_GRADE)
        minutes = int(payload.get("minutes") or 5)
        self.realtime_score = bool(payload.get("realtime_score"))
        self.translate = bool(payload.get("translate"))
        self.grammar_on = bool(payload.get("grammar", True))

        await self.send_json({"type": "state", "value": "preparing"})

        scene = await generate_scene(scene_request, grade)
        if self._tts_task is not None:
            await self._tts_task

        started = time.monotonic()
        session = {
            "id": str(uuid.uuid4()),
            "kind": "conversation",
            "topic": scene.get("topic_id") or "campus",
            "topic_title": scene.get("title_zh"),
            "grade": grade,
            "scene": scene,
            "duration_seconds": minutes * 60,
            "status": "active",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "turns": [],
            "opening": scene["opening"],
            "realtime_score": self.realtime_score,
        }
        self.session = store.create_session(session)
        self.turn_started_at = started
        self._watchdog = asyncio.create_task(self._watch_silence())

        await self.send_json({
            "type": "scene",
            "session_id": session["id"],
            "title": scene.get("title_zh"),
            "role": scene.get("role"),
            "role_zh": scene.get("role_zh"),
            "setting": scene.get("setting"),
            "goal": scene.get("goal"),
            "knowledge": scene.get("knowledge") or [],
            "grade": grade,
            "opening": scene["opening"],
            "source": scene.get("source"),
            "topic": session["topic"],
            "duration_seconds": session["duration_seconds"],
            "realtime_score": self.realtime_score,
            "translate": self.translate,
            "grammar": self.grammar_on,
            "score_ready": self.score_ready,
        })
        # The opening line is the tutor's first turn: hold the floor until the
        # client has played it, otherwise the student's first words land on top
        # of the greeting and get thrown away.
        self.ai_speaking = True
        await self.send_json({"type": "state", "value": "speaking"})
        await self.speak_out(scene["opening"])
        await self.send_json({"type": "opening_done"})
        self._begin_playback()

    async def _connect_tts(self) -> None:
        if not settings.doubao_configured:
            return
        client = DoubaoTTS()
        try:
            await client.connect()
            self.tts = client
            self.tts_ok = True
        except DoubaoTtsError as error:
            await self.error("tts_unavailable", f"{error}；本次改用本地朗读。")
            await client.close()
            return
        # 中英文同一个音色：一条连接就够，语感也一致（默认走这条）。
        if settings.doubao_tts_voice_zh in ("", settings.doubao_tts_voice):
            self.tts_zh = self.tts
            return
        # 要原生英文口音时把两个音色设成不同值：英文音色读中文返回 0 字节，
        # 所以中文那句得换条连接。后台连，别拖慢开场。
        self._tts_zh_task = asyncio.create_task(self._connect_tts_zh())

    async def _connect_tts_zh(self) -> None:
        if not settings.doubao_configured:
            return
        client = DoubaoTTS(voice=settings.doubao_tts_voice_zh)
        try:
            await client.connect()
            self.tts_zh = client
        except DoubaoTtsError:
            # 中文音色不可用时，中文只上屏不朗读，不影响英语对话。
            await client.close()

    # ── audio in ───────────────────────────────────────────────
    async def on_audio(self, chunk: bytes) -> None:
        if self.session is None or self.closed:
            return
        # Half-duplex. The client also stops sending while the tutor speaks, so
        # this is the second line of defence: even a late frame cannot start a
        # turn or disturb the reply that is being spoken.
        if self.ai_speaking:
            return

        now = time.monotonic()
        level = pcm_rms(chunk)

        if not self.asr_active:
            self.pre_roll.append(chunk)
            if len(self.pre_roll) > 25:
                self.pre_roll.pop(0)

        if level >= VOICE_RMS:
            self.speech_seen = True
            self.last_voice_at = now
            self.turn_pcm += chunk
            if not self.asr_active:
                seed = b"".join(self.pre_roll) + chunk
                self.pre_roll = []
                await self.start_asr(seed)
            else:
                self._push_audio(chunk)
            if now - self.turn_started_at > MAX_TURN_SECONDS:
                await self.commit_turn()
            return

        if self.asr_active:
            self.turn_pcm += chunk
            self._push_audio(chunk)
            if self.speech_seen and now - self.last_voice_at >= SILENCE_SECONDS:
                await self.commit_turn()

    # ── ASR ────────────────────────────────────────────────────
    def _push_audio(self, chunk: bytes) -> None:
        """Feed the recogniser without ever letting backpressure break a turn."""
        queue = self.asr_queue
        if queue is None:
            return
        try:
            queue.put_nowait(chunk)
        except asyncio.QueueFull:
            # A stalled recogniser must not kill the lesson; dropping the frame
            # only costs a syllable, and VAD still commits the turn.
            pass

    async def start_asr(self, seed: bytes | None = None) -> None:
        from .doubao_asr import stream_pcm

        self.asr_epoch += 1
        epoch = self.asr_epoch
        self.asr_text = ""
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=400)
        self.asr_queue = queue
        self.asr_active = True
        if seed:
            queue.put_nowait(seed)
        self.turn_started_at = self.last_voice_at or time.monotonic()

        async def frames():
            # Bind the queue locally: ``detach_asr`` clears ``self.asr_queue``
            # while this generator may still be parked on ``get()``. Reading the
            # attribute each loop would raise AttributeError on None and surface
            # as a bogus "音频上传失败" banner mid-conversation.
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item

        async def pump():
            try:
                async for event in stream_pcm(frames()):
                    if epoch != self.asr_epoch:
                        break
                    if event.get("type") == "error":
                        # Once we have text, a provider hiccup must not kill the
                        # turn: the transcript is already usable.
                        if not self.asr_text:
                            await self.error("asr", str(event.get("text") or "语音识别中断"))
                        break
                    text = str(event.get("text") or "").strip()
                    if text:
                        self.asr_text = text
                        await self.send_json({"type": "asr", "stage": event.get("type"), "text": text})
            except Exception as error:  # noqa: BLE001
                if not self.asr_text and epoch == self.asr_epoch:
                    await self.error("asr", f"语音识别不可用：{error}")

        if not settings.doubao_configured:
            await self.send_json({"type": "asr_unavailable"})
            self.asr_active = False
            return
        self.asr_task = asyncio.create_task(pump())

    async def stop_asr(self, timeout: float = 2.5) -> None:
        if not self.asr_active:
            return
        self.asr_active = False
        if self.asr_queue is not None:
            try:
                self.asr_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        if self.asr_task is not None:
            try:
                await asyncio.wait_for(self.asr_task, timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.asr_task.cancel()
            self.asr_task = None

    def detach_asr(self) -> None:
        """Close the recogniser in the background.

        Waiting for the provider's final frame costs ~600ms and the partial
        transcript is already good enough to answer, so the turn proceeds now
        and the socket is drained afterwards.
        """
        task, self.asr_task = self.asr_task, None
        queue, self.asr_queue = self.asr_queue, None
        self.asr_active = False
        if queue is not None:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        if task is None:
            return

        async def drain() -> None:
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()

        asyncio.create_task(drain())

    # ── turn ───────────────────────────────────────────────────
    async def commit_turn(self, *, manual: bool = False) -> None:
        if self.session is None or self.ai_speaking or self.committing:
            return
        if not self.turn_pcm and not self.asr_text:
            return
        self.committing = True
        try:
            # 先把状态推到"整理中"，再等识别器收尾：学生说完 2 秒内就能看到反应。
            await self.send_json({"type": "state", "value": "transcribing"})
            # 关键：等最后一帧转完再拿文本。以前这里用 detach_asr() 抢跑，
            # 句尾的单词经常还没出来就发给 DeepSeek 了，外教答的是半句话。
            await self.stop_asr(timeout=2.5)
            transcript = self.asr_text.strip()
            audio = bytes(self.turn_pcm)
            voice_ended_at = self.last_voice_at or time.monotonic()
            self.turn_pcm = bytearray()
            self.pre_roll = []
            self.speech_seen = False
            self.last_voice_at = 0.0
            self.asr_text = ""

            if audio:
                seconds = len(audio) / (ASR_RATE * 2)
                self.total_audio_seconds += seconds
            if not transcript:
                await self.send_json({"type": "state", "value": "listening"})
                if manual:
                    await self.error("no_speech", "这一轮没有识别到英文，请再说一次。")
                return
            await self.handle_reply(transcript, audio, voice_ended_at=voice_ended_at)
        finally:
            self.committing = False

    def support_note(self, turn_number: int, transcript: str) -> dict | None:
        """Whether this turn needs a Chinese hand-holding moment.

        Two cases, checked in this order:
        1. the student is lost ("I don't understand", "什么意思", or wrote Chinese);
        2. the previous scored turn had a clear grammar problem that has not been
           corrected recently.
        """
        if asks_for_help(transcript):
            return {"help": True}
        note = self.grammar_note(turn_number)
        return {"grammar": note} if note else None

    def _streak(self, turn_number: int, judge) -> int:
        """Count consecutive recent turns that satisfy *judge*.

        Turns without a verdict for that dimension are skipped rather than
        breaking the streak: scoring is asynchronous, so one missing turn must
        not hide a real pattern.
        """
        streak = 0
        for turn in reversed((self.session or {}).get("turns", [])):
            if int(turn.get("number") or 0) >= turn_number:
                continue
            value = judge(turn)
            if value is None:
                continue
            if not value:
                break
            streak += 1
        return streak

    @staticmethod
    def _grammar_bad(turn: dict) -> bool | None:
        verdict = turn.get("grammar") or {}
        score = verdict.get("turn_score")
        if not verdict or not isinstance(score, (int, float)):
            return None
        return bool(verdict.get("issues")) and score < GRAMMAR_FIX_BELOW

    @staticmethod
    def _grammar_good(turn: dict) -> bool | None:
        verdict = turn.get("grammar") or {}
        score = verdict.get("turn_score")
        if not verdict or not isinstance(score, (int, float)):
            return None
        return not verdict.get("issues") and score >= GRAMMAR_STREAK_GOOD

    @staticmethod
    def _pron_score(turn: dict) -> float | None:
        score = (turn.get("pronunciation") or {}).get("overall_score")
        return score if isinstance(score, (int, float)) else None

    def grammar_streak(self, turn_number: int) -> int:
        return self._streak(turn_number, self._grammar_bad)

    def pron_streak(self, turn_number: int) -> int:
        return self._streak(
            turn_number,
            lambda turn: (
                None if self._pron_score(turn) is None
                else self._pron_score(turn) < PRON_STREAK_SEVERE
            ),
        )

    def praise_note(self, turn_number: int) -> dict | None:
        """Encourage a student who has been accurate and clear for two turns."""
        if self.session is None or turn_number < 3:
            return None
        good = self._streak(turn_number, self._grammar_good)
        if good < 2:
            return None
        if self._streak(turn_number, lambda turn: (
            None if self._pron_score(turn) is None
            else self._pron_score(turn) < PRON_STREAK_SEVERE
        )):
            return None
        last_pron = None
        for turn in reversed(self.session.get("turns", [])):
            if int(turn.get("number") or 0) >= turn_number:
                continue
            last_pron = self._pron_score(turn)
            if last_pron is not None:
                break
        if last_pron is not None and last_pron < PRON_STREAK_GOOD:
            return None
        return {"count": good}

    def grammar_note(self, turn_number: int) -> dict | None:
        """A grammar slip worth one short correction, at most every other turn.

        Reads the grammar verdict of an earlier turn (never the current one: that
        analysis runs in parallel with this reply).
        """
        session = self.session
        if session is None or turn_number < 2:
            return None
        streak = self.grammar_streak(turn_number)
        last = session.get("last_grammar_fix") or {}
        if last and turn_number - int(last.get("turn") or 0) < 2 and streak < 2:
            return None
        for turn in reversed(session.get("turns", [])):
            verdict = turn.get("grammar") or {}
            if not verdict:
                continue
            if turn_number - int(turn.get("number") or 0) > 2:
                return None
            issues = verdict.get("issues") or []
            score = verdict.get("turn_score")
            if not issues or not isinstance(score, (int, float)) or score >= GRAMMAR_FIX_BELOW:
                continue
            first = issues[0]
            session["last_grammar_fix"] = {"turn": turn_number, "type": first.get("type")}
            note = {
                "sentence": turn.get("transcript") or "",
                "label": first.get("label") or "语法",
                "snippet": first.get("snippet") or "",
                "fix": first.get("fix") or "",
                "note": first.get("note") or "",
                "rewrite": verdict.get("rewrite") or "",
                "score": round(score),
            }
            if streak >= 2:
                note["streak"] = streak
            return note
        return None

    def coach_note(self, turn_number: int) -> dict | None:
        """Decide whether this turn should include a "repeat after me" correction.

        Driven by the actual scores, not by a fixed cadence:

        * before turn 3 nothing is coached — the opening turns are for getting
          the student talking, not for grading them;
        * only the latest scored turn counts, and only if it is at most two
          turns old (coaching a sentence from five minutes ago feels random);
        * a word must score below ``COACH_WEAK`` to be worth stopping on;
        * a clearly wrong word (< ``COACH_SEVERE``) can be drilled again right
          away, a borderline one waits at least one turn;
        * the same word is not drilled twice in a row unless it improved by at
          least 8 points — otherwise the lesson turns into nagging.

        Scores come from turns that already finished evaluating, so this never
        waits on the current turn's model run.
        """
        session = self.session
        if session is None or turn_number < COACH_MIN_TURN:
            return None
        streak = self.pron_streak(turn_number)

        for turn in reversed(session.get("turns", [])):
            pronunciation = turn.get("pronunciation") or {}
            if not pronunciation:
                continue
            if turn_number - int(turn.get("number") or 0) > 2:
                return None
            weak = [
                item for item in (pronunciation.get("weak_words") or [])
                if isinstance(item.get("word"), str)
                and isinstance(item.get("score"), (int, float))
                and len(item["word"]) >= 3
                and item["word"].lower() not in COACH_SKIP
            ]
            if not turn.get("transcript"):
                continue
            if not weak:
                lowest = pronunciation.get("lowest") or {}
                gap = turn_number - int((session.get("last_coach") or {}).get("turn") or 0)
                if (
                    isinstance(lowest.get("score"), (int, float))
                    and (lowest["score"] < COACH_FLOOR or streak >= 2)
                    and (gap >= COACH_LONG_GAP or streak >= 2)
                ):
                    weak = [lowest]
            if not weak:
                continue
            worst = min(weak, key=lambda item: item["score"])
            score = round(worst["score"])
            if score >= COACH_WEAK and score >= COACH_FLOOR and streak < 2:
                return None

            last = session.get("last_coach") or {}
            if streak < 2:
                if last.get("word") == worst["word"] and score - last.get("score", 0) < COACH_IMPROVED:
                    return None
                if (
                    score >= COACH_SEVERE
                    and last
                    and turn_number - int(last.get("turn") or 0) < COACH_COOLDOWN
                ):
                    return None

            session["last_coach"] = {"turn": turn_number, "word": worst["word"], "score": score}
            note = {
                "sentence": turn["transcript"],
                "drill": drill_phrase(turn["transcript"], worst["word"]),
                "word": worst["word"],
                "ipa": worst.get("phonemes") or "",
                "score": score,
                "severity": "clear" if score < COACH_SEVERE or streak >= 2 else "mild",
                # local_pronounce 存的是音素字符串列表，历史数据里可能是字典，两种都收。
                "symbols": [
                    item if isinstance(item, str) else item.get("symbol")
                    for item in (worst.get("symbols") or [])
                    if isinstance(item, str) or isinstance(item, dict)
                ][:2],
            }
            if streak >= 2:
                note["streak"] = streak
            return note
        return None

    async def handle_reply(self, transcript: str, audio: bytes = b"", *, voice_ended_at: float | None = None) -> None:
        session = self.session
        if session is None:
            return
        self.ai_speaking = True
        self.playback_phase = False
        self.audio_sent_bytes = 0
        self.fallback_words = 0
        await self.send_json({"type": "state", "value": "thinking"})
        await self.send_json({"type": "student", "text": transcript})

        turn_number = len(session["turns"]) + 1
        audio_url = None
        audio_bytes = 0
        if audio:
            audio_url, audio_bytes = store.save_audio(
                session["id"], f"turn-{turn_number:02d}.wav", wav_bytes(audio)
            )

        reply_text = ""
        reply_mode = "fallback"
        spoken_index = 0
        buffer = _SentenceBuffer()
        collected: list[str] = []
        started = time.monotonic()
        voice_ended_at = voice_ended_at or self.last_voice_at or started
        first_token_ms = None
        first_audio_ms = None

        async def speak(text: str) -> None:
            nonlocal spoken_index, first_audio_ms
            if not text.strip():
                return
            await self.send_json({"type": "tts_start", "index": spoken_index, "text": text})
            # 同一音色就整句送；两条音色才按语言切段，各念各的。
            same_voice = self.tts_zh is self.tts
            segments = [(text, False)] if same_voice else (language_segments(text) or [(text, False)])
            for segment, chinese in segments:
                client = self.tts_zh if chinese else self.tts
                if client is None:
                    # 该语言的音色没连上：只上屏，不朗读，别让英文嗓音念中文。
                    continue
                try:
                    async for chunk in client.speak(segment):
                        if first_audio_ms is None:
                            first_audio_ms = int((time.monotonic() - voice_ended_at) * 1000)
                        self.audio_sent_bytes += len(chunk)
                        await self.send_bytes(chunk)
                except DoubaoTtsError as error:
                    await self.error("tts", f"{error}；改用本地朗读。")
                    self.fallback_words += len(segment.split())
                    await self.send_json({"type": "speak_fallback", "text": segment})
            await self.send_json({"type": "tts_end", "index": spoken_index})
            spoken_index += 1

        try:
            # 优先级：学生听不懂 > 语法纠错 > 发音带读。一轮只做一件事，
            # 三件事叠在一起会变成说教，学生反而接不上话。
            support = self.support_note(turn_number, transcript)
            coach = None if support else self.coach_note(turn_number)
            progress = None if (support or coach) else self.praise_note(turn_number)
            messages = [
                {"role": "system", "content": system_prompt(session, coach=coach, support=support, progress=progress)},
                *history_messages(session),
            ]
            messages.append({"role": "user", "content": transcript})
            # 带读/讲解那一轮把温度压一点：三句话的结构不能跑偏，也别把跟读句写飞。
            async for delta in stream_chat(
                messages,
                temperature=0.7 if (coach or support) else 0.85,
                max_tokens=240 if support else (190 if coach else (150 if progress else 140)),
                timeout=20,
            ):
                if first_token_ms is None:
                    first_token_ms = int((time.monotonic() - started) * 1000)
                collected.append(delta)
                await self.send_json({"type": "reply_delta", "text": delta})
                for sentence in buffer.feed(delta):
                    await speak(sentence)
            for sentence in buffer.flush():
                await speak(sentence)
            reply_text = clean_reply("".join(collected))
            reply_mode = llm_provider.current()
        except DeepSeekError as error:
            reply_text = local_reply(session, transcript)
            reply_mode = "fallback"
            if first_token_ms is None:
                first_token_ms = int((time.monotonic() - started) * 1000)
            await self.send_json({"type": "reply_delta", "text": reply_text})
            await speak(reply_text)
            await self.error(llm_provider.current(), f"{error}；本轮使用本地备用回复。")

        turn = {
            "number": turn_number,
            "transcript": transcript,
            "tutor_reply": reply_text,
            "tutor_mode": reply_mode,
            "audio_url": audio_url,
            "audio_bytes": audio_bytes,
            "duration_seconds": round(len(audio) / (ASR_RATE * 2), 2),
            "created_at": utc_now(),
        }
        session["turns"].append(turn)
        store.save_session(session)

        await self.send_json({
            "type": "reply_done",
            "text": reply_text,
            "mode": reply_mode,
            "first_token_ms": first_token_ms,
            "latency": {
                "after_speech_ms": int((started - voice_ended_at) * 1000),
                "first_token_ms": first_token_ms,
                "first_audio_ms": first_audio_ms,
            },
        })
        await self.send_json({"type": "turn", "turn": turn, "session_id": session["id"]})

        if self.realtime_score and audio and not is_chinese(transcript):
            task = asyncio.create_task(self._score(turn_number, transcript, audio, turn["duration_seconds"]))
            self._score_tasks.add(task)
            task.add_done_callback(self._score_tasks.discard)

        # 翻译和语法都走独立 task：外教已经开口了，这两件事只补侧栏和字幕，
        # 任何一件慢了都不该让学生等下一句。
        if self.translate and reply_text:
            task = asyncio.create_task(self._translate(turn_number, reply_text))
            self._translate_tasks.add(task)
            task.add_done_callback(self._translate_tasks.discard)

        if self.grammar_on and not is_chinese(transcript):
            task = asyncio.create_task(self._grammar(turn_number, transcript))
            self._grammar_tasks.add(task)
            task.add_done_callback(self._grammar_tasks.discard)

        self._begin_playback()

    def _begin_playback(self) -> None:
        """Hold the floor until the client reports the last chunk has played.

        ``audio_sent_bytes`` gives a duration estimate; that estimate is only a
        safety net for a dropped ``playback_done``, never the normal path.
        """
        played = self.audio_sent_bytes / (TTS_RATE * 2) + self.fallback_words / WORDS_PER_SECOND
        self.audio_sent_bytes = 0
        self.fallback_words = 0
        self.playback_phase = True
        self._playback_event = asyncio.Event()
        if self._playback_task is not None:
            self._playback_task.cancel()
            self._playback_task = None
        self._playback_task = asyncio.create_task(self._finish_playback(played))

    async def _finish_playback(self, seconds: float) -> None:
        event = self._playback_event
        try:
            if event is not None:
                try:
                    await asyncio.wait_for(event.wait(), timeout=max(3.0, seconds) + 4.0)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(max(0.4, seconds))
        except asyncio.CancelledError:
            return
        await self.release_playback()

    async def release_playback(self) -> None:
        """Hand the channel back to the student once the tutor is silent."""
        self.playback_phase = False
        self.ai_speaking = False
        self._playback_event = None
        task, self._playback_task = self._playback_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        self.pre_roll = []
        await self.send_json({"type": "state", "value": "listening"})

    async def _translate(self, turn_number: int, text: str) -> None:
        """Subtitle for the tutor's last line, on demand only."""
        chinese = await to_chinese(text)
        if not chinese or self.closed:
            return
        await self.send_json({
            "type": "reply_translation",
            "turn": turn_number,
            "source": text,
            "zh": chinese,
        })

    async def _grammar(self, turn_number: int, text: str) -> None:
        """Grammar verdict for one student sentence, kept out of the reply path."""
        await self.send_json({"type": "grammar_pending", "turn": turn_number})
        verdict = await analyze_grammar(text, (self.session or {}).get("grade") or DEFAULT_GRADE)
        if self.closed:
            return
        if verdict is None:
            await self.send_json({"type": "grammar", "turn": turn_number, "unavailable": True})
            return
        await self.send_json({"type": "grammar", "turn": turn_number, **verdict})

        session = self.session
        if session is None:
            return
        for turn in session.get("turns", []):
            if turn.get("number") == turn_number:
                turn["grammar"] = {
                    "turn_score": verdict["turn_score"],
                    "types": verdict["types"],
                    "issues": verdict["issues"][:2],
                    "summary": verdict["summary"],
                }
                store.save_session(session)
                break

    async def _score(self, turn_number: int, text: str, audio: bytes, seconds: float) -> None:
        """Per-turn pronunciation check, deliberately off the reply path.

        The tutor starts answering the moment the transcript is ready; the
        acoustic model runs in its own task and only feeds the side panel, so a
        slow evaluation never delays the next thing the student hears.
        """
        await self.send_json({"type": "score_pending", "turn": turn_number})
        if not self.score_ready:
            await self.send_json({
                "type": "score",
                "turn": turn_number,
                "unavailable": "发音评测未配置，本轮只记录录音。",
            })
            return
        try:
            result = await speech_evaluator.assess_pronunciation(text, "sentence", wav_bytes(audio))
        except Exception as error:  # noqa: BLE001 - scoring must never break the lesson
            await self.send_json({"type": "score", "turn": turn_number, "error": str(error)})
            return
        if result.get("demo"):
            await self.send_json({"type": "score", "turn": turn_number, "unavailable": "演示模式不产出真实发音分。"})
            return

        words = len(text.split())
        result["words"] = words
        result["seconds"] = round(seconds, 2)
        # Speaking rate is cheap, real and useful even when accuracy is low: the
        # student can see "too slow / too rushed" without reading a paragraph.
        result["wpm"] = round(words / seconds * 60) if seconds >= 1 and words else None
        coach = settings.pron_feedback_enabled and bool(result.get("word_results"))
        await self.send_json({
            "type": "score",
            "turn": turn_number,
            "text": text,
            "assessment": result,
            "coach": coach,
        })

        session = self.session
        if session is not None:
            for turn in session.get("turns", []):
                if turn.get("number") == turn_number:
                    turn["pronunciation"] = {
                        key: result.get(key)
                        for key in (
                            "overall_score", "accuracy", "fluency", "completeness",
                            "focus_words", "weak_words", "wpm", "words", "provider",
                        )
                    }
                    # 弱词表只收 <72 的词；说得好的一轮也要留一个"最低分实词"，
                    # 否则连着几轮都没得纠，带读永远不会出现。
                    lowest = lowest_content_word(result.get("word_results") or [])
                    if lowest:
                        turn["pronunciation"]["lowest"] = lowest
                    store.save_session(session)
                    break

        # The coach reads the same numbers through a separate model call with its
        # own prompt: the tutor never grades, the coach never role-plays.
        if settings.pron_feedback_enabled and result.get("word_results"):
            task = asyncio.create_task(self._feedback(turn_number, text, result))
            self._feedback_tasks.add(task)
            task.add_done_callback(self._feedback_tasks.discard)

    async def _feedback(self, turn_number: int, text: str, assessment: dict) -> None:
        from .pron_feedback import analyze

        payload = await analyze(text, assessment)
        await self.send_json({"type": "score_feedback", "turn": turn_number, **payload})

    async def speak_out(self, text: str) -> None:
        """Speak a standalone line (the opening) outside the reply pipeline."""
        index = 0
        for sentence in split_sentences(text):
            if not sentence:
                continue
            await self.send_json({"type": "tts_start", "index": index, "text": sentence})
            if self.tts_ok and self.tts is not None:
                try:
                    async for chunk in self.tts.speak(sentence):
                        self.audio_sent_bytes += len(chunk)
                        await self.send_bytes(chunk)
                except DoubaoTtsError as error:
                    await self.error("tts", f"{error}；改用本地朗读。")
                    self.fallback_words += len(sentence.split())
                    await self.send_json({"type": "speak_fallback", "text": sentence})
            else:
                self.fallback_words += len(sentence.split())
                await self.send_json({"type": "speak_fallback", "text": sentence})
            await self.send_json({"type": "tts_end", "index": index})
            index += 1

    # ── finish ─────────────────────────────────────────────────
    async def finish(self) -> None:
        session = self.session
        if session is None:
            return
        await self.stop_asr()
        # Stop waiting on a playback that the student just cancelled.
        self.ai_speaking = False
        self.playback_phase = False
        self._playback_event = None
        if self._playback_task is not None:
            self._playback_task.cancel()
            self._playback_task = None
        await self.send_json({"type": "state", "value": "reporting"})
        # A score for the last turn may still be in flight; give it a short
        # window so the report can quote the real numbers instead of omitting
        # the final turn. Anything slower is not worth delaying the report.
        if self._score_tasks:
            await asyncio.wait(list(self._score_tasks), timeout=4)
        # The coach analysis for the last turn may still be in flight; the panel
        # is closed by the report, so give it a short window to land.
        if self._feedback_tasks:
            await asyncio.wait(list(self._feedback_tasks), timeout=2)
        session["elapsed_seconds"] = min(
            session["duration_seconds"], int(self.total_audio_seconds + 30 * len(session["turns"]))
        )
        session["status"] = "completed"
        from .tutor import make_report

        report = await make_report(session)
        session["report"] = report
        store.save_session(session)
        store.complete_session(session, report)
        await self.send_json({"type": "report", "report": report, "session": session})


def history_messages(session: dict, budget: int = 3600) -> list[dict]:
    """Whole-lesson replay, newest first until the character budget runs out."""
    picked: list[tuple[str, str]] = []
    for turn in reversed(session.get("turns", [])):
        student = (turn.get("transcript") or "").strip()[:240]
        tutor = (turn.get("tutor_reply") or "").strip()[:280]
        cost = len(student) + len(tutor) + 24
        if picked and cost > budget:
            break
        budget -= cost
        if tutor:
            picked.append(("assistant", tutor))
        if student:
            picked.append(("user", student))
    return [{"role": role, "content": content} for role, content in reversed(picked)]


def local_reply(session: dict, transcript: str) -> str:
    """Deterministic follow-up used only when DeepSeek is unreachable.

    Preset scenes borrow their authored prompts so a lesson without API balance
    still moves in a sensible direction instead of repeating one generic line.
    """
    topic_id = (session.get("scene") or {}).get("topic_id") or session.get("topic")
    prompts = (TOPIC_PROMPTS.get(topic_id) or []) + FALLBACK_PROMPTS
    index = min(len(session.get("turns", [])), len(prompts) - 1)
    return prompts[index]
