"""AI-generated conversation scenes.

Every lesson starts from either a preset topic or the student's own description,
but the opening line itself is always written by DeepSeek at runtime. A rotating
set of angles plus a recent-opening blacklist keeps two lessons from sounding the
same, which a static ``opening`` string in ``content.py`` could never do.

The scene also carries ``knowledge``: the grammar and functional language the
student should practise. Presets declare it; for a student-written scene the
model derives it from the description, capped by the grade profile.
"""
from __future__ import annotations

import difflib
import json
import random

from . import llm_provider
from .content import topic_by_id
from .deepseek_client import DeepSeekError, chat
from .grades import DEFAULT_GRADE, profile
from .store import DATA_DIR

HISTORY_FILE = DATA_DIR / "scene_history.json"
HISTORY_LIMIT = 24

ANGLES = [
    "从一件刚发生的小麻烦切入，而不是寒暄",
    "从对方正在忙、只能给你半分钟切入",
    "从天气或时间带来的具体不便切入",
    "从一个需要你马上做决定的选择切入",
    "从你带来的一个物品或一张纸切入",
    "从一次误会或听错的话切入",
    "从一个临时改变的计划切入",
    "从别人委托你转达一件事切入",
    "从对方手里只剩最后一个选择切入",
    "从一句被打断的话继续往下说",
]

CUSTOM_ROLES = ["店员", "邻居", "面试官", "新认识的同学", "活动组织者", "房东", "导游", "医生"]

CUSTOM_OPENINGS = [
    "Hi there — sorry, we are a bit busy today. What can I do for you?",
    "You are early. While we wait, tell me what you are hoping to get done.",
    "Before we start, can you tell me what happened?",
]


def _history() -> list[str]:
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return [str(item) for item in data.get("openings", [])][-HISTORY_LIMIT:]
    except (FileNotFoundError, ValueError):
        return []


def _remember(opening: str) -> None:
    openings = _history()
    openings.append(opening)
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(
        json.dumps({"openings": openings[-HISTORY_LIMIT:]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _too_similar(opening: str, recent: list[str]) -> bool:
    return any(difflib.SequenceMatcher(None, opening.lower(), old.lower()).ratio() > 0.8 for old in recent)


def describe_scene(scene: dict) -> tuple[str, str, str]:
    """Return (kind, title_zh, description_for_model)."""
    kind = (scene or {}).get("kind", "preset")
    if kind == "custom":
        text = str((scene or {}).get("text", "")).strip()[:200]
        return "custom", "自定义情景", f"学生自己描述的场景：{text}"
    topic = topic_by_id((scene or {}).get("id", "campus")) or {}
    title = topic.get("title") or "校园生活"
    english = topic.get("english_title") or "campus life"
    description = topic.get("description") or ""
    return "preset", title, f"预设主题：{english}（{title}）。{description}"


def _system_prompt(grade: str) -> str:
    item = profile(grade)
    return (
        "You design one scene for a Chinese senior-high student practising spoken English. "
        f"The student is {grade} (CEFR {item['cefr']}, {item['vocabulary']}).\n"
        "Return JSON only, with these keys: "
        '"title_zh" (Chinese scene name, <=8 chars), "role" (the character you play, English, <=4 words), '
        '"role_zh" (that character in Chinese), "setting" (one short English sentence describing where we are), '
        '"opening" (your first spoken line), "goal" (what the student should accomplish, Chinese, <=20 chars), '
        '"knowledge" (array of 1-3 Chinese strings naming the grammar or functional language this scene '
        f"should practise, all within {grade} level).\n"
        "Rules for opening:\n"
        "1. English only, 12 to 28 words, exactly one question at the end.\n"
        f"2. Keep every sentence under {item['sentence_words']} words; vocabulary must fit {item['cefr']}.\n"
        "3. Sound like a real person mid-day, not a textbook. Contractions welcome.\n"
        "4. Never start with 'Hello, how are you' or introduce yourself as an AI.\n"
        f"5. No structures the student has not learnt yet: {'、'.join(item['avoid'])}.\n"
        "6. No stage directions, no emoji, no quotation marks around the line."
    )


def _user_prompt(kind: str, title: str, description: str, grade: str, angle: str, avoid: list[str]) -> str:
    avoid_text = "\n".join(f"- {line}" for line in avoid[-6:]) or "- (none yet)"
    item = profile(grade)
    return (
        f"Scene: {description}\n"
        f"Student grade: {grade} (CEFR {item['cefr']}). "
        f"The student can handle: {'；'.join(item['allowed'])}.\n"
        f"Use this angle: {angle}\n"
        f"Recently used openings you must not repeat or paraphrase:\n{avoid_text}\n"
        "Write a fresh opening that fits the angle, and name the language points it can draw out."
    )


def _fallback(kind: str, topic_id: str, grade: str, knowledge: list[str]) -> dict:
    topic = topic_by_id(topic_id) or {}
    pool = topic.get("openings") or CUSTOM_OPENINGS
    roles = topic.get("roles") or CUSTOM_ROLES
    recent = _history()
    fresh = [line for line in pool if line not in recent] or pool
    opening = random.choice(fresh)
    return {
        "title_zh": "自定义情景" if kind == "custom" else (topic.get("title") or "校园生活"),
        "role": "Ms. Hale",
        "role_zh": random.choice(roles),
        "setting": "You are talking with someone in the middle of a normal day.",
        "opening": opening,
        "goal": f"用英语完成一次{grade}水平的真实对话",
        "knowledge": knowledge,
        "source": "fallback",
    }


async def generate(scene: dict, grade: str = DEFAULT_GRADE) -> dict:
    """Generate the scene card. Never raises: falls back to local templates."""
    grade = grade or DEFAULT_GRADE
    kind, title, description = describe_scene(scene)
    topic_id = (scene or {}).get("id", "campus") if kind == "preset" else "custom"
    topic = topic_by_id(topic_id) or {}
    knowledge = [str(item) for item in (topic.get("knowledge") or [])][:3]
    recent = _history()
    angle = random.choice(ANGLES)
    payload = _fallback(kind, topic_id, grade, knowledge)

    if llm_provider.available():
        for attempt in range(2):
            messages = [
                {"role": "system", "content": _system_prompt(grade)},
                {"role": "user", "content": _user_prompt(kind, title, description, grade, angle, recent + [payload["opening"]])},
            ]
            try:
                result = await chat(messages, json_mode=True, temperature=1.0, max_tokens=380, timeout=45, thinking=False)
                data = json.loads(result.text)
            except (DeepSeekError, ValueError):
                break
            opening = str(data.get("opening") or "").strip().strip('"')
            if not opening or _too_similar(opening, recent):
                angle = random.choice(ANGLES)
                continue
            generated = [str(item)[:40] for item in (data.get("knowledge") or []) if str(item).strip()]
            payload = {
                "title_zh": str(data.get("title_zh") or title)[:16],
                "role": str(data.get("role") or "Ms. Hale")[:40],
                "role_zh": str(data.get("role_zh") or "外教")[:20],
                "setting": str(data.get("setting") or "")[:160],
                "opening": opening,
                "goal": str(data.get("goal") or "")[:40],
                "knowledge": (knowledge or generated)[:3],
                "source": llm_provider.current(),
            }
            break

    _remember(payload["opening"])
    payload["kind"] = kind
    payload["topic_id"] = topic_id
    payload["grade"] = grade
    payload["angle_used"] = angle
    return payload
