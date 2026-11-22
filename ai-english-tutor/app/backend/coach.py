"""Rule-based learning coach.

It produces a transparent next action from real practice evidence: due reviews,
accuracy, untouched words and speaking time. No external model is needed for the
schedule, so the recommendation keeps working offline and stays explainable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .content import scenes_for_grade, vocabulary_for_grade


def _parse(value: str | None):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def speaking_today(data: dict) -> int:
    today = datetime.now().astimezone().date()
    total = 0
    for session in data.get("sessions", []):
        completed = _parse(session.get("completed_at"))
        if completed and completed.astimezone().date() == today:
            total += int(session.get("duration_seconds") or 0)
    return total


def build_plan(grade: str, data: dict) -> dict:
    words = vocabulary_for_grade(grade)
    states = data.get("words", {})
    now = datetime.now(timezone.utc)
    due, weak, untouched, known = [], [], [], []
    for word in words:
        state = states.get(word["id"], {})
        attempts = int(state.get("attempts") or 0)
        if state.get("known"):
            known.append(word)
        if attempts == 0:
            untouched.append(word)
            continue
        due_at = _parse(state.get("next_review"))
        accuracy = (state.get("correct") or 0) / max(1, attempts)
        if not state.get("known") and accuracy < 0.6:
            weak.append((word, accuracy, attempts))
        elif due_at and due_at <= now:
            due.append(word)
    weak.sort(key=lambda item: (item[1], -item[2]))
    review_ids = list(dict.fromkeys(
        [item[0]["id"] for item in weak] + [item["id"] for item in due]
    ))
    spoken = speaking_today(data)
    total = len(words)
    mastery = round(len(known) / total * 100) if total else 0

    step_review = None
    if review_ids:
        step_review = {
            "id": "review",
            "type": "quiz",
            "title": "先清掉快要忘的词",
            "detail": f"{len(review_ids)} 个词已到期或正确率偏低；系统会按掌握度混合选择题和默写。",
            "action": "quiz",
            "scope": "review",
            "mode": "adaptive",
            "count": min(12, max(5, len(review_ids))),
            "minutes": 4,
        }
    step_speak = None
    if spoken < 180:
        step_speak = {
            "id": "speak",
            "type": "conversation",
            "title": "把词放进 5 分钟对话",
            "detail": "今天开口还不到 3 分钟。先说完一段，再回来看词。",
            "action": "conversation",
            "minutes": 5,
        }
    step_scene = None
    scenes = scenes_for_grade(grade)
    if scenes:
        scene = scenes[0]
        step_scene = {
            "id": "scene",
            "type": "vocabulary",
            "title": f"进入新情景：{scene['title']}",
            "detail": "六个关键词放进同一段场景，先理解，再听音和复述。",
            "action": "vocabulary",
            "minutes": 4,
        }
    step_paper = {
        "id": "paper",
        "type": "dictation",
        "title": "纸笔听写 5 个词",
        "detail": "摄像头拍下整页答案，AI 逐词核对拼写。",
        "action": "dictation",
        "mode": "paper",
        "minutes": 3,
    }
    steps = [step for step in (step_review, step_speak, step_scene, step_paper) if step][:3]
    focus = steps[0] if steps else {
        "id": "scene", "type": "vocabulary", "title": "自由复习", "detail": "选择任意情景继续。",
        "action": "vocabulary", "minutes": 4,
    }
    if review_ids:
        message = f"今天最该先处理的是 {len(review_ids)} 个到期或薄弱词，而不是继续堆新词。"
    elif spoken < 180:
        message = "你的词汇在增加，但开口时间还不够。先把刚认识的词用进对话。"
    elif untouched:
        message = "复习节奏已经跟上。现在进入下一个情景，把新词放进真实句子里。"
    else:
        message = "今天的任务基本完成。可以再刷一轮，或者用纸笔听写检验回忆。"
    return {
        "grade": grade,
        "coach": {
            "message": message,
            "headline": "今天先做这一件事",
        },
        "stats": {
            "grade_total": total,
            "known": len(known),
            "mastery": mastery,
            "due": len(due),
            "weak": len(weak),
            "untouched": len(untouched),
            "speaking_seconds": spoken,
        },
        "steps": steps,
        "focus": focus,
        "focus_word_ids": review_ids[:12],
        "focus_words": [word["word"] for word in [item[0] for item in weak[:3]] + due[:3]],
        "generated_at": now.isoformat(),
    }
