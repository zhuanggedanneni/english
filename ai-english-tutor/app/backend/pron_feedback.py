"""The pronunciation coach — a second, deliberately separate DeepSeek call.

Two models, two jobs, no shared state:

* ``backend/tutor.py`` plays the conversation partner. It never sees scores.
* this module reads the per-word IPA table from the local acoustic model and
  says what to fix. It never sees the role-play history and never writes a
  tutor line.

Keeping them apart matters for more than tidiness: the tutor prompt is tuned to
stay in character at A2/B1 level, while this prompt has to be blunt about a
single consonant. Mixing the two makes the tutor start grading the student
mid-conversation, which is exactly what a role-play must not do.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from typing import Any

from .config import settings
from . import llm_provider
from .deepseek_client import DeepSeekError, chat
from .local_pronounce import REDUCED_FORMS

MAX_WORDS_IN_PROMPT = 26
CACHE_LIMIT = 128
_CACHE: OrderedDict[str, dict] = OrderedDict()

SYSTEM_PROMPT = (
    "你是一位只负责发音纠正的英语语音教练。用户会给你一份来自本地声学模型的逐词评测："
    "单词、该词得分（0-100）、该词的 IPA 音标、以及该词得分最低的音素。\n"
    "你只做两件事：指出最需要改的词，给出可以马上照做的口型或舌位提示。\n"
    "不要评价语法或用词，不要续写对话，不要扮演对话角色，不要提模型、评分系统或提示词。\n"
    "只使用给出的单词，不要编造原文里没有的词。\n"
    '只输出 JSON，键固定为：summary（中文一句话，22 字以内，点出最关键的问题）；'
    'advice（中文要点数组，1-2 条，每条 26 字以内，动词开头、可操作）；'
    'weak_words（数组，最多 3 项，每项 {"word","ipa","tip"}，tip 为中文 16 字以内的口型提示）；'
    "drill（一句英文短句，10 词以内，只能使用学生原句里出现过的单词，必须包含最弱的那个词，供跟读）。\n"
    "整体都很准时 weak_words 给空数组，advice 给一条进阶建议。不要新增原句里没有的单词。"
)


def _signature(text: str, words: list[dict[str, Any]]) -> str:
    parts = [text, *[f"{word.get('word')}:{word.get('score')}" for word in words]]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _table(text: str, assessment: dict[str, Any]) -> str:
    rows = []
    for word in (assessment.get("word_results") or [])[:MAX_WORDS_IN_PROMPT]:
        name = word.get("word") or ""
        # Skip forms whose citation IPA differs from the reduced pronunciation
        # the student actually used; coaching "say eɪ" for "a" would be wrong.
        if not name or name.lower() in REDUCED_FORMS:
            continue
        weak = word.get("weak_phonemes") or []
        weak_text = "、".join(f"/{item['symbol']}/ {item['score']}" for item in weak) or "—"
        rows.append(f"{name} | {word.get('score')} | {word.get('phonemes') or '—'} | {weak_text}")
    return (
        f"学生原句：{text}\n"
        "逐词结果（词 | 分数 | IPA | 最弱音素）：\n" + "\n".join(rows)
    )


def _clean(data: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    def text(value: Any, limit: int) -> str:
        return str(value or "").strip()[:limit]

    weak = []
    for item in data.get("weak_words") or []:
        if not isinstance(item, dict):
            continue
        name = text(item.get("word"), 24)
        if not name or name.lower() not in allowed:
            continue
        weak.append({"word": name, "ipa": text(item.get("ipa"), 40), "tip": text(item.get("tip"), 24)})
        if len(weak) >= 3:
            break

    advice = [text(item, 40) for item in (data.get("advice") or []) if str(item).strip()][:2]
    return {
        "summary": text(data.get("summary"), 40),
        "advice": advice,
        "weak_words": weak,
        "drill": text(data.get("drill"), 120),
        "source": llm_provider.current(),
    }


async def analyze(text: str, assessment: dict[str, Any], *, timeout: float = 12.0) -> dict[str, Any]:
    """Ask the coach what to fix. Never raises: the panel degrades quietly."""
    words = assessment.get("word_results") or []
    if not words:
        return {"unavailable": "本轮没有逐词结果。"}

    signature = _signature(text, words)
    cached = _CACHE.get(signature)
    if cached is not None:
        _CACHE.move_to_end(signature)
        return {**cached, "cached": True}

    if not settings.pron_feedback_enabled:
        return {"unavailable": "未开启教练分析。"}

    allowed = {(word.get("word") or "").lower() for word in words}
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _table(text, assessment)},
    ]
    started = time.perf_counter()
    try:
        result = await chat(
            messages,
            json_mode=True,
            temperature=0.3,
            max_tokens=420,
            timeout=timeout,
            thinking=False,
        )
        data = json.loads(result.text)
    except (DeepSeekError, ValueError) as error:
        return {"unavailable": f"教练分析暂不可用：{error}"[:120]}

    cleaned = _clean(data, allowed)
    cleaned["latency_ms"] = round((time.perf_counter() - started) * 1000)
    _CACHE[signature] = cleaned
    while len(_CACHE) > CACHE_LIMIT:
        _CACHE.popitem(last=False)
    return cleaned
