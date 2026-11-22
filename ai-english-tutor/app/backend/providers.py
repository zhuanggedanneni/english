"""Provider boundary for Doubao ASR, tutor generation and pronunciation scoring.

The demo provider deliberately marks every generated result as demo output. The
Doubao adapter is activated by environment variables but its wire protocol is
implemented only after the exact product/API documentation is supplied. This
avoids silently using a different ASR model or inventing an API payload.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

from .config import settings
from .content import topic_by_id


@dataclass(frozen=True)
class ProviderStatus:
    mode: str
    realtime_asr: bool
    pronunciation: bool
    tutor: bool
    label: str


class DemoProvider:
    status = ProviderStatus(
        mode="demo",
        realtime_asr=False,
        pronunciation=False,
        tutor=False,
        label="演示对话 · 浏览器示范朗读",
    )

    async def stream_asr(self, audio: AsyncIterator[bytes]):
        """Consume streamed PCM in demo mode; browser SpeechRecognition is the UI fallback."""
        async for _ in audio:
            pass

    def tutor_reply(self, session: dict, transcript: str) -> str:
        topic = topic_by_id(session["topic"])
        turn_count = len(session.get("turns", [])) + 1
        prompt = topic["prompts"][min(turn_count - 1, len(topic["prompts"]) - 1)]
        words = len(transcript.split())
        if words < 4:
            return f"Let's add one more detail. {prompt}"
        if turn_count == 1:
            return f"That gives me a clear starting point. {prompt}"
        return f"I see what you mean. {prompt}"

    async def assess_pronunciation(self, target_text: str, content_type: str, audio_bytes: bytes) -> dict:
        # UI contract preview only, explicitly identified as non-linguistic demo data.
        duration_hint = min(12, max(0, len(audio_bytes) // 32000))
        score = min(92, 74 + duration_hint)
        return {
            "demo": True,
            "provider": "demo",
            "overall_score": score,
            "accuracy": score,
            "completeness": 100,
            "fluency": max(70, score - 3),
            "word_results": [],
            "focus_words": [],
            "audio_seconds": duration_hint,
            "summary": "音频与标准文本已保存。当前分数仅用于预览报告布局，不代表真实发音水平。",
            "suggestion": "接入发音评测后，这里会标出具体错音和需要重读的单词。",
        }

    def assess_conversation(self, session: dict) -> dict:
        turns = session.get("turns", [])
        spoken = [turn for turn in turns if turn.get("transcript", "").strip()]
        word_count = sum(len(turn["transcript"].split()) for turn in spoken)
        audio_turns = sum(1 for turn in turns if turn.get("audio_url"))
        score = min(92, 68 + len(spoken) * 4) if spoken else None
        return {
            "demo": True,
            "overall_score": score,
            "pronunciation": None,
            "fluency": score,
            "expression": min(94, 64 + word_count // 3) if spoken else None,
            "turn_count": len(turns),
            "audio_turn_count": audio_turns,
            "word_count": word_count,
            "summary": f"本次保存了 {audio_turns} 段学生音频和 {len(turns)} 轮文字记录。演示报告只检查学习流程，不评价真实发音。",
            "pronunciation_summary": "发音准确度、完整度和具体错音将在接入语音评测 API 后生成。",
            "next_step": "从本轮记录中选一句，进入跟读训练。",
        }


class DoubaoProvider(DemoProvider):
    """Doubao streaming ASR through the v3 binary protocol.

    The tutor reply is handled by ``backend/tutor.py`` (DeepSeek text model), so
    this provider only owns speech recognition. Credentials never enter
    frontend JS.
    """

    status = ProviderStatus(
        mode="doubao",
        realtime_asr=True,
        pronunciation=False,
        tutor=True,
        label="豆包实时转写 · DeepSeek 外教回复",
    )

    async def stream_asr(self, audio: AsyncIterator[bytes]):
        from .doubao_asr import stream_pcm

        async for event in stream_pcm(audio):
            yield event

    def tutor_reply(self, session: dict, transcript: str) -> str:
        # Deterministic fallback used by tutor.py when DeepSeek is unavailable.
        return super().tutor_reply(session, transcript)

    def assess_pronunciation(self, target_text: str, content_type: str, audio_bytes: bytes) -> dict:
        raise ProviderNotReady("需要按发音评测接口上传学生音频和标准英文")

    def assess_conversation(self, session: dict) -> dict:
        raise ProviderNotReady("需要按评估接口上传每轮音频、转写文本和会话内容")


class XfyunSpeechEvaluator:
    """Normalizes iFlytek ISE output for the existing assessment UI contract."""

    def __init__(self) -> None:
        from .xfyun_ise import evaluate_pcm, wav_to_pcm16k

        self._evaluate_pcm = evaluate_pcm
        self._wav_to_pcm = wav_to_pcm16k

    async def assess_pronunciation(self, target_text: str, content_type: str, audio_bytes: bytes) -> dict:
        info = self._wav_to_pcm(audio_bytes)
        result = await self._evaluate_pcm(info.pcm, target_text)
        result["audio_seconds"] = info.duration_seconds
        result["content_type"] = content_type
        return result


class ProviderNotReady(RuntimeError):
    pass


def get_provider():
    if settings.ai_provider == "doubao":
        return DoubaoProvider()
    return DemoProvider()


def get_speech_evaluator():
    mode = settings.speech_mode
    if mode == "local":
        from .local_pronounce import local_pronouncer

        return local_pronouncer
    if mode == "xfyun":
        return XfyunSpeechEvaluator()
    return DemoProvider()


def speech_status() -> dict:
    mode = settings.speech_mode
    if mode == "local":
        return {
            "mode": "local",
            "provider": "本地逐词评测",
            "ready": True,
            "word_level": True,
            "label": "本地逐词评测已就绪（IPA 音标级）",
            "endpoint": settings.local_pronounce_url,
        }
    if mode == "xfyun":
        return {
            "mode": "xfyun",
            "provider": "iFlytek ISE",
            "ready": True,
            "word_level": True,
            "label": "讯飞发音评测已就绪",
        }
    return {
        "mode": "demo",
        "provider": "演示评分",
        "ready": False,
        "word_level": False,
        "label": "发音评分待配置",
    }


provider = get_provider()
speech_evaluator = get_speech_evaluator()
