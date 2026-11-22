"""学生句子的语法点评：时态、搭配、主谓宾是否成立。

和发音教练一样是独立的一次调用：
- 外教继续扮演角色，不纠错；
- 这里只看学生这一句写得对不对，返回分数、问题点和改写。

同一句不重复分析（缓存），短句不分析（没有信息量）。
"""

from __future__ import annotations

import hashlib
import json

from .config import settings
from . import llm_provider
from .deepseek_client import DeepSeekError, chat
from .grades import DEFAULT_GRADE, level_block

_MAX_CACHE = 200
_cache: dict[str, dict] = {}

# 模型返回英文标签，界面要中文；同时给前端一个稳定的色块分类。
TYPE_LABELS = {
    "tense": ("时态", "tense"),
    "agreement": ("主谓一致", "agreement"),
    "collocation": ("搭配", "collocation"),
    "preposition": ("介词", "preposition"),
    "article": ("冠词", "article"),
    "wordform": ("词性", "wordform"),
    "order": ("语序", "order"),
    "other": ("其他", "other"),
}


def _key(text: str) -> str:
    return hashlib.sha1(text.strip().lower().encode("utf-8")).hexdigest()


def _clean_issues(raw) -> list[dict]:
    issues: list[dict] = []
    for item in (raw or [])[:3]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "other").lower()
        label, tag = TYPE_LABELS.get(kind, TYPE_LABELS["other"])
        snippet = str(item.get("snippet") or "").strip()[:80]
        fix = str(item.get("fix") or "").strip()[:80]
        note = str(item.get("note") or "").strip()[:60]
        if not (snippet or note):
            continue
        issues.append({"type": tag, "label": label, "snippet": snippet, "fix": fix, "note": note})
    return issues


async def analyze(text: str, grade: str = DEFAULT_GRADE) -> dict | None:
    """Grade one student sentence. ``None`` means "no verdict available"."""
    text = " ".join((text or "").split())
    if len(text.split()) < 3:
        return None  # "Yes." 这种句子没有语法可讲
    cached = _cache.get(_key(text))
    if cached:
        return cached
    if not llm_provider.available():
        return None

    messages = [
        {
            "role": "system",
            "content": (
                "You are an English grammar coach for a Chinese senior-high student. "
                "Judge ONE sentence the student said out loud, focusing on tense, "
                "subject-verb agreement, collocation (which verb/preposition goes with "
                "which word), articles and word order. "
                f"{level_block(grade)} "
                "Return JSON only: {\"score\": 0-100, "
                "\"issues\": [{\"type\": \"tense|agreement|collocation|preposition|"
                "article|wordform|order|other\", \"snippet\": \"the wrong fragment, "
                "copied verbatim\", \"fix\": \"the corrected fragment\", "
                "\"note\": \"Chinese, ≤20 chars, why it is wrong\"}], "
                "\"rewrite\": \"the whole sentence rewritten correctly, or the original "
                "if it is already fine\", \"summary\": \"Chinese, ≤24 chars\"}. "
                "Rules: report only real errors — spoken English that is merely informal "
                "is not an error. At most 2 issues, most important first. If the sentence "
                "is correct, return score ≥ 90 and an empty issues array. "
                "The snippet MUST be a contiguous substring of the student's sentence, "
                "character for character; never paraphrase and never invent a fragment."
            ),
        },
        {"role": "user", "content": text},
    ]
    try:
        result = await chat(
            messages,
            json_mode=True,
            temperature=0.1,
            max_tokens=400,
            timeout=20,
            thinking=False,
        )
    except DeepSeekError:
        return None

    try:
        payload = json.loads(result.text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    try:
        score = int(round(float(payload.get("score"))))
    except (TypeError, ValueError):
        return None
    score = max(0, min(100, score))
    issues = _clean_issues(payload.get("issues"))
    if score >= 90:
        issues = []  # 分数高就别硬凑问题

    verdict = {
        "turn_score": score,
        "issues": issues,
        "rewrite": str(payload.get("rewrite") or "").strip()[:160],
        "summary": str(payload.get("summary") or "").strip()[:40],
        "types": [item["type"] for item in issues],
    }
    if len(_cache) >= _MAX_CACHE:
        _cache.clear()
    _cache[_key(text)] = verdict
    return verdict
