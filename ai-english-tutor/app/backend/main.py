from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import settings
from . import llm_provider
from .coach import build_plan
from .conversation_ws import ConversationSocket
from .content import (
    GRADES, PRACTICE_COUNT_BY_GRADE, SENTENCES, TOPICS, VOCAB_META, WORDS,
    public_word, scenes_for_grade, sentence_by_id, topic_by_id, vocabulary_for_grade, word_by_id,
)
from .deepseek_client import ProbeResult, probe as probe_deepseek
from .deepseek_grader import PhotoGraderError, grade_handwriting
from .providers import ProviderNotReady, provider, speech_evaluator, speech_status
from .scene_gen import generate as generate_scene
from .store import AUDIO_DIR, store, utc_now
from .tutor import make_report, reply as generate_tutor_reply, status as tutor_status
from .xfyun_ise import IseError

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024

app = FastAPI(title="Echo 高中英语外教", version="0.1.0")


@app.on_event("startup")
async def warm_local_pronouncer() -> None:
    """Load the acoustic model before the first student speaks.

    Two wav2vec2 checkpoints take ~25 s to load cold and ~2 s per turn warm.
    Warming in the background means the first real turn behaves like the fifth.
    """
    if settings.speech_mode != "local":
        return

    from .local_pronounce import local_pronouncer

    async def run() -> None:
        ok = await local_pronouncer.warmup()
        print(f"[pronounce] local model warmup {'ok' if ok else 'failed'} ({settings.local_pronounce_url})")

    asyncio.create_task(run())


class SessionRequest(BaseModel):
    topic: str = "campus"
    duration_minutes: int = Field(default=5, ge=1, le=30)


class FinishRequest(BaseModel):
    elapsed_seconds: int = Field(default=0, ge=0, le=3600)


class DictationRequest(BaseModel):
    word_id: str
    answer: str = Field(min_length=1, max_length=80)


class ReviewRequest(BaseModel):
    known: bool


class QuizAnswer(BaseModel):
    correct: bool


class LLMRequest(BaseModel):
    provider: str


async def read_audio(audio: UploadFile) -> bytes:
    content = await audio.read(MAX_AUDIO_BYTES + 1)
    if len(content) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="单段音频不能超过 25 MB")
    if not content:
        raise HTTPException(status_code=422, detail="没有收到音频，请重新录音")
    return content


async def read_photo(image: UploadFile) -> bytes:
    content = await image.read(MAX_IMAGE_BYTES + 1)
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="照片不能超过 12 MB")
    if not content:
        raise HTTPException(status_code=422, detail="没有收到照片")
    if image.content_type and not image.content_type.startswith("image/"):
        raise HTTPException(status_code=422, detail="请上传 JPG、PNG 或 WebP 照片")
    return content


def safe_audio_name(prefix: str, original: str | None) -> str:
    extension = Path(original or "speech.webm").suffix.lower()
    if extension not in {".webm", ".wav", ".ogg", ".mp4", ".m4a", ".mp3"}:
        extension = ".webm"
    return f"{prefix}-{uuid.uuid4().hex[:8]}{extension}"


def require_session(session_id: str) -> dict:
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="没有找到这次练习")
    return session


@app.exception_handler(ProviderNotReady)
async def provider_not_ready_handler(_, error: ProviderNotReady):
    return JSONResponse(status_code=503, detail={"message": str(error), "code": "PROVIDER_NOT_READY"})


_probe_cache: dict[str, object] = {"at": 0.0, "result": None}


async def probe_grader(force: bool = False) -> ProbeResult:
    """Cache the probe briefly so opening a page cannot hammer the provider."""
    cached = _probe_cache.get("result")
    if (
        not force
        and isinstance(cached, ProbeResult)
        and time.monotonic() - float(_probe_cache.get("at") or 0) < 30
    ):
        return cached
    result = await probe_deepseek()
    _probe_cache["at"] = time.monotonic()
    _probe_cache["result"] = result
    return result


@app.get("/api/health")
def health():
    provider_status = asdict(provider.status)
    # The label describes what the conversation page can actually do; the old
    # demo/default wording predates the streaming pipeline.
    if llm_provider.available():
        provider_status["tutor"] = True
        spoken = "豆包流式识别" if settings.doubao_configured else "浏览器识别"
        voice = "豆包合成语音" if settings.doubao_configured else "浏览器朗读"
        provider_status["label"] = f"{spoken} · {llm_provider.label()}外教 · {voice}"
    return {
        "ok": True,
        "provider": provider_status,
        "llm": llm_provider.status(),
        "speech_eval": speech_status(),
        "photo_grading": {
            "ready": settings.deepseek_configured,
            "provider": "DeepSeek" if settings.deepseek_configured else "待配置",
            "model": settings.deepseek_vision_model,
            "base_url": settings.deepseek_base_url,
        },
        "tutor": tutor_status(),
        "conversation": {
            "streaming": True,
            "endpoint": "/api/ws/conversation",
            "asr": "豆包流式识别" if settings.doubao_configured else "浏览器识别",
            "tts": "豆包流式合成" if settings.doubao_configured else "浏览器朗读",
            "tts_voice": settings.doubao_tts_voice if settings.doubao_configured else "",
            "tts_resource": settings.doubao_tts_resource_id if settings.doubao_configured else "",
        },
        "realtime_asr": {
            "configured": settings.doubao_configured,
            "provider": "豆包流式语音识别 2.0" if settings.doubao_configured else "浏览器语音识别",
            "resource_id": settings.doubao_asr_resource_id,
            "endpoint": settings.doubao_asr_url,
        },
    }


@app.get("/api/llm")
def llm_status():
    return llm_provider.status()


@app.post("/api/llm")
def llm_switch(payload: LLMRequest):
    try:
        llm_provider.set_current(payload.provider)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return llm_provider.status()


@app.get("/api/grader/probe")
async def grader_probe(force: bool = False):
    """Connectivity self-check that never spends completion tokens.

    A 402 response is reported as ``insufficient_balance`` — that still proves
    the host, TLS and key are fine, which is what a connectivity check needs.
    """
    result = await probe_grader(force=force)
    return result.as_dict()


@app.get("/api/content")
def content(topic: str = "campus", grade: str = "高一", content_type: str = "all"):
    if grade not in GRADES:
        raise HTTPException(status_code=422, detail="年级只能是高一、高二或高三")
    selected_words = [word for word in WORDS if topic == "all" or word["topic"] == topic]
    selected_sentences = [s for s in SENTENCES if topic == "all" or s["topic"] == topic]
    if content_type == "word":
        selected_sentences = []
    elif content_type == "sentence":
        selected_words = []
    return {
        "topics": TOPICS,
        "words": selected_words,
        "sentences": selected_sentences,
        "selected_topic": topic,
        "grades": GRADES,
        "grade": grade,
        "vocabulary": [public_word(item) for item in vocabulary_for_grade(grade)],
        "scenes": scenes_for_grade(grade),
        "vocabulary_meta": {
            **VOCAB_META,
            "grade_total": PRACTICE_COUNT_BY_GRADE[grade],
            "grade_totals": PRACTICE_COUNT_BY_GRADE,
        },
    }


@app.get("/api/progress")
def progress():
    data = store.progress()["learner"]
    word_states = data.get("words", {})
    known = sum(1 for state in word_states.values() if state.get("known"))
    review_ids = [
        word_id for word_id, state in word_states.items()
        if state.get("attempts", 0) > 0 and not state.get("known")
    ]
    sessions = data.get("sessions", [])
    seconds = sum(item.get("duration_seconds", 0) for item in sessions)
    latest = sessions[-1] if sessions else None
    return {
        "known_words": known,
        "total_words": sum(PRACTICE_COUNT_BY_GRADE.values()),
        "grade_totals": PRACTICE_COUNT_BY_GRADE,
        "review_ids": review_ids,
        "speaking_seconds": seconds,
        "session_count": len(sessions),
        "latest_session": latest,
        "word_states": word_states,
    }


@app.get("/api/plan")
def learning_plan(grade: str = "高一"):
    if grade not in GRADES:
        raise HTTPException(status_code=422, detail="年级只能是高一、高二或高三")
    return build_plan(grade, store.progress()["learner"])


@app.post("/api/conversation/session", status_code=201)
def create_conversation(payload: SessionRequest):
    topic = topic_by_id(payload.topic)
    if not topic:
        raise HTTPException(status_code=422, detail="请选择一个有效对话场景")
    now = utc_now()
    session = {
        "id": str(uuid.uuid4()),
        "kind": "conversation",
        "topic": payload.topic,
        "duration_seconds": payload.duration_minutes * 60,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "turns": [],
        "opening": topic["opening"],
    }
    return store.create_session(session)


@app.get("/api/conversation/{session_id}")
def get_conversation(session_id: str):
    return require_session(session_id)


@app.post("/api/conversation/{session_id}/turn", status_code=201)
async def save_conversation_turn(
    session_id: str,
    transcript: str = Form(default=""),
    duration_seconds: float = Form(default=0),
    audio: UploadFile | None = File(default=None),
):
    session = require_session(session_id)
    if session["status"] != "active":
        raise HTTPException(status_code=409, detail="这次对话已经结束")
    transcript = transcript.strip()[:2000]
    if not transcript and not audio:
        raise HTTPException(status_code=422, detail="请先录音或输入转写文字")

    turn_number = len(session["turns"]) + 1
    audio_url = None
    audio_size = 0
    if audio:
        content = await read_audio(audio)
        filename = safe_audio_name(f"turn-{turn_number:02d}", audio.filename)
        audio_url, audio_size = store.save_audio(session_id, filename, content)

    if transcript:
        tutor_reply, tutor_mode = await generate_tutor_reply(session, transcript)
    else:
        tutor_reply, tutor_mode = "I saved your recording. Add its transcript before we continue.", "system"
    turn = {
        "number": turn_number,
        "transcript": transcript,
        "tutor_reply": tutor_reply,
        "tutor_mode": tutor_mode,
        "audio_url": audio_url,
        "audio_bytes": audio_size,
        "duration_seconds": max(0, round(duration_seconds, 2)),
        "created_at": utc_now(),
    }
    session["turns"].append(turn)
    store.save_session(session)
    return {"turn": turn, "session_id": session_id}


@app.post("/api/conversation/{session_id}/finish")
async def finish_conversation(session_id: str, payload: FinishRequest):
    session = require_session(session_id)
    if session["status"] == "completed" and session.get("report"):
        return {"session": session, "report": session["report"]}
    session["elapsed_seconds"] = min(payload.elapsed_seconds, session["duration_seconds"])
    session["status"] = "completed"
    # Written by DeepSeek from the transcripts; degrades to local statistics when
    # the provider is unavailable, so ending a lesson never fails with a 503.
    report = await make_report(session)
    session["report"] = report
    store.save_session(session)
    store.complete_session(session, report)
    return {"session": session, "report": report}


@app.post("/api/pronunciation/evaluate", status_code=201)
async def pronunciation_evaluate(
    target_text: str = Form(min_length=1, max_length=500),
    content_type: str = Form(default="sentence"),
    content_id: str = Form(default=""),
    audio: UploadFile = File(...),
):
    target_text = target_text.strip()
    if content_type not in {"word", "sentence"}:
        raise HTTPException(status_code=422, detail="跟读内容只能是单词或句子")
    if content_type == "word" and content_id and not word_by_id(content_id):
        raise HTTPException(status_code=422, detail="单词不存在")
    if content_type == "sentence" and content_id and not sentence_by_id(content_id):
        raise HTTPException(status_code=422, detail="句子不存在")
    content = await read_audio(audio)
    evaluation_id = str(uuid.uuid4())
    filename = safe_audio_name("shadowing", audio.filename)
    audio_url, audio_size = store.save_audio(evaluation_id, filename, content)
    try:
        assessment = await speech_evaluator.assess_pronunciation(target_text, content_type, content)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except IseError as error:
        raise HTTPException(status_code=502, detail=f"讯飞评测失败：{error}") from error
    return {
        "id": evaluation_id,
        "target_text": target_text,
        "content_type": content_type,
        "content_id": content_id,
        "audio_url": audio_url,
        "assessment": assessment,
    }


@app.post("/api/dictation/attempt")
def dictation_attempt(payload: DictationRequest):
    word = word_by_id(payload.word_id)
    if not word:
        raise HTTPException(status_code=404, detail="单词不存在")
    normalized = re.sub(r"[^a-z0-9]", "", payload.answer).lower()
    correct = normalized == re.sub(r"[^a-z0-9]", "", word["word"]).lower()
    state = store.record_word(word["id"], correct, "dictation")
    return {
        "correct": correct,
        "answer": word["word"],
        "meaning": word["meaning"],
        "phonetic": word["phonetic"],
        "example": word.get("example", ""),
        "book": word.get("book", ""),
        "unit": word.get("unit", ""),
        "state": state,
        "next_action": "shadow" if not correct else "next",
    }


@app.post("/api/vocabulary/{word_id}/quiz")
def quiz_answer(word_id: str, payload: QuizAnswer):
    if not word_by_id(word_id):
        raise HTTPException(status_code=404, detail="单词不存在")
    state = store.record_word(word_id, payload.correct, "quiz")
    return {"word_id": word_id, "state": state}


@app.post("/api/dictation/photo")
async def dictation_photo(
    word_ids: str = Form(min_length=1),
    image: UploadFile = File(...),
):
    try:
        parsed_ids = json.loads(word_ids)
    except json.JSONDecodeError:
        parsed_ids = [item for item in word_ids.split(",") if item]
    if not isinstance(parsed_ids, list) or not 1 <= len(parsed_ids) <= 8:
        raise HTTPException(status_code=422, detail="每次纸笔听写需要 1 到 8 个单词")
    if len(set(parsed_ids)) != len(parsed_ids):
        raise HTTPException(status_code=422, detail="听写单词不能重复")
    expectations = []
    for word_id in parsed_ids:
        word = word_by_id(str(word_id))
        if not word:
            raise HTTPException(status_code=422, detail=f"单词不存在：{word_id}")
        expectations.append(word)
    content = await read_photo(image)
    upload_id = str(uuid.uuid4())
    extension = Path(image.filename or "paper.jpg").suffix.lower()
    if extension not in {".jpg", ".jpeg", ".png", ".webp", ".heic"}:
        extension = ".jpg"
    image_url, image_size = store.save_audio(upload_id, f"paper-{uuid.uuid4().hex[:8]}{extension}", content)
    try:
        result = await grade_handwriting(content, image.content_type or "image/jpeg", expectations)
    except PhotoGraderError as error:
        raise HTTPException(
            status_code=502,
            detail={"message": str(error), "code": error.code, "hint": error.hint},
        ) from error
    if result.status == "graded":
        for item in result.items:
            if item["correct"] is not None:
                store.record_word(item["word_id"], item["correct"], "paper")
    return {
        "id": upload_id,
        "status": result.status,
        "provider": result.provider,
        "message": result.message,
        "hint": result.hint,
        "image_url": image_url,
        "image_bytes": image_size,
        "items": result.items,
        "correct_count": sum(1 for item in result.items if item["correct"]) if result.status == "graded" else None,
        "total": len(result.items),
    }


@app.post("/api/vocabulary/{word_id}/review")
def review_word(word_id: str, payload: ReviewRequest):
    if not word_by_id(word_id):
        raise HTTPException(status_code=404, detail="单词不存在")
    state = store.record_word(word_id, payload.known, "vocabulary")
    return {"word_id": word_id, "state": state}


@app.websocket("/api/ws/conversation")
async def conversation_stream(websocket: WebSocket):
    """Single socket for the whole spoken lesson: audio up, transcript, reply and
    reply audio down. See ``backend/conversation_ws.py`` for the event contract."""
    await ConversationSocket(websocket).run()


@app.websocket("/api/ws/asr")
async def realtime_asr(websocket: WebSocket):
    """Browser sends 16 kHz mono signed PCM16 binary frames.

    The backend is the only place that sees Doubao credentials. Provider events
    use {type: 'partial'|'final'|'error', text: string}; frontend rendering stays
    unchanged when the real adapter is added.
    """
    await websocket.accept()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=80)

    async def audio_frames():
        while True:
            frame = await queue.get()
            if frame is None:
                break
            yield frame

    async def forward_results():
        try:
            async for result in provider.stream_asr(audio_frames()):
                await websocket.send_json(result)
        except (ProviderNotReady, RuntimeError) as error:
            await websocket.send_json({"type": "error", "text": str(error)})

    await websocket.send_json({"type": "ready", "provider": provider.status.mode})
    task = asyncio.create_task(forward_results())
    try:
        while True:
            message = await websocket.receive()
            if message.get("bytes") is not None:
                if queue.full():
                    await websocket.send_json({"type": "error", "text": "语音发送过快，请保持网络稳定"})
                    continue
                await queue.put(message["bytes"])
            elif message.get("text") == "stop":
                break
    except WebSocketDisconnect:
        pass
    finally:
        await queue.put(None)
        await asyncio.wait_for(task, timeout=5)
        if websocket.client_state.name == "CONNECTED":
            await websocket.close()


app.mount("/media", StaticFiles(directory=AUDIO_DIR), name="media")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
