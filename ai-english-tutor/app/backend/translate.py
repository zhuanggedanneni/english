"""把外教刚说的话译成中文——只在学生打开"翻译"开关时才调用。

刻意不做进外教的回复里：同一次生成同时吐英文和中文，会让 TTS 把中文也念
出来，也会让模型把两种语言混着说。这里用独立一次调用，回复说完再补上译文，
对话首字延迟一点不受影响。
"""

from __future__ import annotations

import hashlib

from .config import settings
from . import llm_provider
from .deepseek_client import DeepSeekError, chat

_MAX_CACHE = 200
_cache: dict[str, str] = {}


def _key(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()


async def to_chinese(text: str) -> str | None:
    """Return a short spoken-Chinese translation, or ``None`` if unavailable."""
    text = (text or "").strip()
    if not text:
        return None
    cached = _cache.get(_key(text))
    if cached:
        return cached
    if not llm_provider.available():
        return None

    messages = [
        {
            "role": "system",
            "content": (
                "Translate the English sentence into natural spoken Chinese for a "
                "Chinese senior-high student. Return only the translation: no quotes, "
                "no pinyin, no explanation, no English left in it. Keep it short."
            ),
        },
        {"role": "user", "content": text},
    ]
    try:
        result = await chat(
            messages,
            json_mode=False,
            temperature=0.0,
            max_tokens=120,
            timeout=12,
            thinking=False,
        )
    except DeepSeekError:
        return None

    chinese = (result.text or "").strip().strip('"“”「」')
    if not chinese:
        return None
    if len(_cache) >= _MAX_CACHE:
        _cache.clear()
    _cache[_key(text)] = chinese
    return chinese
