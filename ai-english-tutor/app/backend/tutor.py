"""AI tutor replies and end-of-lesson reports.

Thinking mode is off for every call here: a spoken turn is worthless if the
first word arrives two seconds late. If DeepSeek fails (no balance, network,
rate limit), the turn degrades to a deterministic prompt so a lesson never
freezes mid-sentence.
"""
from __future__ import annotations

import json
import re
import time

from .config import settings
from . import llm_provider
from .deepseek_client import DeepSeekError, chat
from .grades import DEFAULT_GRADE, level_block, practice_block, report_block, profile
from .providers import provider

# Skip the paid path briefly after a hard failure (balance / auth) so a demo
# cannot stall for a network round-trip on every single turn.
_cooldown_until = 0.0
_cooldown_reason = ""

FALLBACK_PROMPTS = [
    "That's interesting — can you tell me a bit more about it?",
    "Why does that matter to you?",
    "What would you do next if you had the chance?",
]


def _trip(seconds: float, reason: str) -> None:
    global _cooldown_until, _cooldown_reason
    _cooldown_until = time.monotonic() + seconds
    _cooldown_reason = reason


def status() -> dict:
    cooldown = max(0, round(_cooldown_until - time.monotonic()))
    return {
        "provider": llm_provider.current(),
        "model": llm_provider.model_name(),
        "available": llm_provider.available() and cooldown == 0,
        "cooldown_seconds": cooldown,
        "reason": _cooldown_reason if cooldown else "",
    }


def system_prompt(
    session: dict,
    coach: dict | None = None,
    support: dict | None = None,
    progress: dict | None = None,
) -> str:
    """One prompt that adapts to a preset topic or a student-written scene.

    The grade block is not decoration: it caps the AI's own English and tells it
    which target structure to pull out of the student this turn.

    ``coach`` turns one turn into a short pronunciation detour: the tutor says a
    line of praise, names a word the evaluator flagged, and asks the student to
    repeat a phrase from their own sentence. It is the same model, so the detour
    has to be requested explicitly or the tutor would keep role-playing.
    """
    scene = session.get("scene") or {}
    role = scene.get("role") or "Ms. Hale"
    setting = scene.get("setting") or ""
    goal = scene.get("goal") or ""
    title = scene.get("title_zh") or session.get("topic_title") or "free talk"
    grade = session.get("grade") or DEFAULT_GRADE
    knowledge = scene.get("knowledge") or []
    prompt = (
        f"You are {role}, talking with a Chinese senior-high student.\n"
        f"Scene: {title}. {setting}\n"
        f"The student's goal this turn: {goal}\n\n"
        f"{level_block(grade)}\n\n"
        f"{practice_block(grade, knowledge)}\n\n"
        "You are speaking out loud, so your words are read by a text-to-speech engine.\n"
        "Rules:\n"
        "1. Speak English, at most 2 short sentences, within the word limit above — "
        "the only exception is the single Chinese sentence a note below explicitly asks for.\n"
        "2. React to what the student actually said, then ask exactly one question.\n"
        "3. Stay in character: you are the shop assistant / classmate / interviewer, not a teacher.\n"
        "4. Do not correct grammar on your own — only when a grammar note is attached below.\n"
        "5. Sound like a native speaker chatting, not a translator: no 'I'd like to ask', "
        "no repeating the student's words back before answering.\n"
        "6. No emoji, no bullet points, no stage directions, no markdown.\n"
        "7. Remember the whole conversation so far: names, places, plans and what the student "
        "already told you. Never ask something they have answered; refer back to it naturally.\n"
        "8. Be flexible: a short answer gets help to expand, a strong answer gets a harder "
        "why/how question."
    )
    if support and support.get("help"):
        return prompt + (
            "\n\nThe student is lost — this turn overrides rules 1 and 3:\n"
            "They either said they do not understand, or answered you in Chinese.\n"
            "Explain like a patient human teacher, phrase by phrase, not a template:\n"
            "1. Quote the exact English phrase the student is stuck on (usually the question you "
            "just asked, or one key word inside it) and gloss it in Chinese right after, the way "
            'you would speak it: "do you want to eat，这句就是你想吃吗。" or "\'to go\' 就是带走。"\n'
            "   Translate the words that carry meaning — not every single word — and keep it to "
            "1-2 spoken sentences.\n"
            "2. Then say the same thing again in simpler English (<=12 words).\n"
            "3. End with one very easy question they can answer with 2-4 words.\n"
            "Vary your wording: a phrase gloss for a whole sentence, a word gloss when only one "
            "word confused them. Never repeat the same explanation twice in a row."
        )

    grammar = (support or {}).get("grammar")
    if grammar:
        detail = f'{grammar["snippet"]} → {grammar["fix"]}' if grammar.get("snippet") else grammar.get("note")
        rewrite = f'\nCorrected sentence: {grammar["rewrite"]}' if grammar.get("rewrite") else ""
        streak = int(grammar.get("streak") or 0)
        pattern = (
            f"\nThe same kind of grammar problem has now appeared {streak} turns in a row. "
            "Treat it as a habit the student can fix, not as a brand-new mistake."
            if streak >= 2
            else ""
        )
        step1 = (
            f"1. One short Chinese sentence that names the repeating pattern: this {grammar['label']} "
            f"problem has appeared {streak} turns in a row (<=22 characters).\n"
            if streak >= 2
            else "1. One short Chinese sentence naming the problem (<=18 characters).\n"
        )
        return prompt + (
            "\n\nGrammar correction — this turn overrides rules 1 and 4:\n"
            f'The student said: "{grammar["sentence"]}"\n'
            f'Problem: {grammar["label"]} — {detail}'
            + (f'（{grammar["note"]}）' if grammar.get("note") else "")
            + rewrite + pattern + "\n"
            "Reply in exactly three short sentences:\n"
            + step1
            + "2. The corrected English sentence, said naturally; keep it short enough to copy.\n"
            "3. One question that carries the scene forward, back in character. "
            + ("Make this one easy so the student can get it right immediately." if streak >= 2 else "")
            + "\nIt is a correction, not a scolding: stay warm, never mention scores."
        )

    if not coach:
        if progress:
            count = int(progress.get("count") or 0)
            return prompt + (
                "\n\nPositive momentum — this turn only:\n"
                f"The student has been accurate and clear for {count} turns in a row.\n"
                "Reply in exactly two sentences:\n"
                "1. One specific, sincere sentence of praise: name the structure or word they used "
                "correctly. No generic 'good job'.\n"
                "2. One harder why/how follow-up that stays in character.\n"
                "Do not mention scores or the fact that you are evaluating them."
            )
        return prompt

    sounds = "".join(f" /{item}/" for item in (coach.get("symbols") or [])[:2])
    coach_phrase = coach.get("drill") or coach["word"]
    streak = int(coach.get("streak") or 0)
    # 分数越低，话说得越直；60 分以上只是"不太准"，别把学生说懵。
    tone = (
        "clearly wrong and easy to hear"
        if coach.get("severity") == "clear" or streak >= 2
        else "a little off, not a big problem"
    )
    streak_note = (
        f"\nPronunciation has been unclear for the last {streak} turns. Slow down and be patient."
        if streak >= 2
        else ""
    )
    return prompt + (
        "\n\nPronunciation coaching — this turn only, it overrides rules 1, 3 and 4:\n"
        f'The student just said: "{coach["sentence"]}"\n'
        f'The word "{coach["word"]}" /{coach["ipa"]}/ scored {coach["score"]}/100'
        f'{f", the sounds{sounds} came out wrong" if sounds else ""} — treat it as {tone}.'
        + streak_note + "\n"
        "Reply in exactly three short sentences, in this order:\n"
        "1. Brief praise, then say that word was not quite right, naming it.\n"
        f'2. Say exactly this sentence, word for word: "Repeat after me: {coach_phrase}". '
        "Do not shorten it, do not swap words in it, do not add words to it. "
        "If it is longer than three words, put a short pause after the second word so the "
        "student can copy you in two halves.\n"
        "3. One short question that carries the scene forward, back in character. "
        "The turn must end with this question so the student knows what to say next.\n"
        "Never mention scores, numbers, IPA symbols, phonemes or the evaluator. "
        "Do not wrap the whole reply in quotation marks."
    )


def _history(session: dict) -> list[dict]:
    """Replay the whole lesson, newest-first until a soft character budget.

    Six turns was enough for a demo but dropped a name or a plan from the first
    minute. A five-minute lesson fits comfortably inside this budget, so the
    tutor can refer back to anything the student already said.
    """
    budget = 3600
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


def clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"^(Ms\.?\s*Hale\s*:\s*)", "", text, flags=re.I)
    if len(text) > 320:
        text = text[:317].rsplit(" ", 1)[0] + "…"
    return text


async def reply(session: dict, transcript: str) -> tuple[str, str]:
    """Non-streaming fallback used by the REST endpoint."""
    fallback = provider.tutor_reply(session, transcript)
    if not llm_provider.available() or time.monotonic() < _cooldown_until:
        return fallback, "demo"

    messages = [{"role": "system", "content": system_prompt(session)}, *_history(session)]
    messages.append({"role": "user", "content": transcript})
    try:
        result = await chat(messages, json_mode=False, temperature=0.85, max_tokens=160, timeout=20, thinking=False)
    except DeepSeekError as error:
        hard = error.code in {"insufficient_balance", "auth_failed", "unconfigured"}
        _trip(120 if hard else 20, str(error))
        return fallback, "fallback"

    text = clean(result.text)
    if not text:
        _trip(20, "empty response")
        return fallback, "fallback"
    return text, llm_provider.current()


def pronunciation_stats(turns: list[dict]) -> dict | None:
    """Average the per-turn word-level scores collected during the lesson.

    Real-time scoring is optional, so this returns ``None`` when the student kept
    it off — the report then falls back to a purely textual judgement instead of
    inventing numbers.
    """
    scored = [
        turn["pronunciation"] for turn in turns
        if isinstance(turn.get("pronunciation"), dict)
        and isinstance(turn["pronunciation"].get("overall_score"), (int, float))
    ]
    if not scored:
        return None

    def average(key: str) -> int | None:
        values = [item[key] for item in scored if isinstance(item.get(key), (int, float))]
        return round(sum(values) / len(values)) if values else None

    focus: list[str] = []
    weak: dict[str, dict] = {}
    for item in scored:
        focus.extend(item.get("focus_words") or [])
        for entry in item.get("weak_words") or []:
            name = (entry.get("word") or "").strip()
            if not name:
                continue
            slot = weak.setdefault(
                name.lower(),
                {"word": name, "phonemes": entry.get("phonemes") or "", "symbols": [], "score": 100, "hits": 0},
            )
            slot["hits"] += 1
            slot["score"] = min(slot["score"], int(entry.get("score") or 100))
            for symbol in entry.get("symbols") or []:
                if symbol and symbol not in slot["symbols"]:
                    slot["symbols"].append(symbol)

    weakest = sorted(weak.values(), key=lambda item: (item["score"], -item["hits"]))[:5]
    return {
        "overall": average("overall_score"),
        "accuracy": average("accuracy"),
        "fluency": average("fluency"),
        "completeness": average("completeness"),
        "turns": len(scored),
        "provider": scored[0].get("provider") or "local",
        "focus_words": list(dict.fromkeys(focus))[:6],
        "weakest": weakest,
    }


GRAMMAR_TYPE_LABELS = {
    "tense": "时态",
    "agreement": "主谓一致",
    "collocation": "动词搭配",
    "preposition": "介词",
    "article": "冠词",
    "wordform": "词性",
    "order": "语序",
    "other": "其他",
}


def grammar_stats(turns: list[dict]) -> dict | None:
    """Average the per-sentence grammar verdicts collected during the lesson."""
    scored = [
        turn["grammar"] for turn in turns
        if isinstance(turn.get("grammar"), dict)
        and isinstance(turn["grammar"].get("turn_score"), (int, float))
    ]
    if not scored:
        return None

    counts: dict[str, int] = {}
    for item in scored:
        for kind in item.get("types") or []:
            if kind in GRAMMAR_TYPE_LABELS:
                counts[kind] = counts.get(kind, 0) + 1

    return {
        "turns": len(scored),
        "average": round(sum(item["turn_score"] for item in scored) / len(scored)),
        "issue_counts": counts,
        "top_types": sorted(counts.items(), key=lambda pair: -pair[1])[:3],
        "examples": [
            {"summary": item.get("summary"), "issues": item.get("issues") or []}
            for item in scored if item.get("issues")
        ][:3],
    }


def _tense_score(grammar: dict | None) -> int | None:
    """Tense/agreement/collocation share one score: they are the same failure mode
    (choosing the wrong verb form for the moment) and students read them together."""
    if not grammar:
        return None
    counts = grammar.get("issue_counts") or {}
    bad = sum(counts.get(key, 0) for key in ("tense", "agreement", "collocation"))
    if not bad:
        return 92
    penalty = min(50, 18 * bad / max(1, grammar["turns"]))
    return max(40, round(96 - penalty * 2))


def build_profile(session: dict, measured: dict | None, grammar: dict | None) -> dict:
    """The end-of-lesson portrait: four dimensions, each with a number and a line."""
    turns = session.get("turns", [])
    spoken = [turn for turn in turns if turn.get("transcript", "").strip()]
    total_words = sum(len(turn["transcript"].split()) for turn in spoken)
    wpm = None
    rates = [
        turn["pronunciation"]["wpm"] for turn in turns
        if isinstance(turn.get("pronunciation"), dict)
        and isinstance(turn["pronunciation"].get("wpm"), (int, float))
    ]
    if rates:
        wpm = round(sum(rates) / len(rates))

    notes: dict[str, str] = {}
    if grammar:
        top = grammar["top_types"][0][0] if grammar["top_types"] else None
        notes["grammar"] = (
            f"{grammar['turns']} 句平均 {grammar['average']} 分"
            + (f"，最常错的是{GRAMMAR_TYPE_LABELS[top]}" if top else "，没有反复出现的错误")
        )
        notes["tense"] = (
            f"时态/搭配类问题 {sum(grammar['issue_counts'].get(k, 0) for k in ('tense','agreement','collocation'))} 处"
            if grammar["issue_counts"] else "时态和搭配都没有明显问题"
        )
    else:
        notes["grammar"] = "本场没有拿到语法点评"
        notes["tense"] = "本场没有拿到语法点评"

    if measured:
        notes["fluency"] = (
            f"平均语速 {wpm} 词/分，流利度 {measured.get('fluency')} 分"
            if wpm else f"流利度 {measured.get('fluency')} 分"
        )
        focus = "、".join(measured.get("focus_words") or [])
        notes["pronunciation"] = (
            f"逐词平均 {measured.get('overall')} 分" + (f"，重点词：{focus}" if focus else "")
        )
    else:
        notes["fluency"] = "本场未开启实时评分"
        notes["pronunciation"] = "本场未开启实时评分"

    dimensions = [
        {"key": "grammar", "label": "语法", "score": grammar["average"] if grammar else None, "note": notes["grammar"]},
        {"key": "tense", "label": "时态搭配", "score": _tense_score(grammar), "note": notes["tense"]},
        {"key": "fluency", "label": "流利度", "score": measured.get("fluency") if measured else None, "note": notes["fluency"]},
        {"key": "pronunciation", "label": "发音", "score": measured.get("overall") if measured else None, "note": notes["pronunciation"]},
    ]
    return {
        "dimensions": dimensions,
        "summary": "",
        "words": total_words,
        "wpm": wpm,
        "turns": len(spoken),
        "issue_counts": grammar["issue_counts"] if grammar else {},
        "examples": grammar["examples"] if grammar else [],
    }


def local_stats(session: dict) -> dict:
    turns = session.get("turns", [])
    spoken = [turn for turn in turns if turn.get("transcript", "").strip()]
    word_count = sum(len(turn["transcript"].split()) for turn in spoken)
    audio_turns = sum(1 for turn in turns if turn.get("audio_url"))
    score = min(92, 68 + len(spoken) * 4) if spoken else None
    detail = pronunciation_stats(turns)
    grammar = grammar_stats(turns)
    return {
        "overall_score": score,
        "pronunciation": detail["overall"] if detail else None,
        "pronunciation_detail": detail,
        "grammar": grammar,
        "profile": build_profile(session, detail, grammar),
        "fluency": detail["fluency"] if detail and detail.get("fluency") is not None else score,
        "expression": min(94, 64 + word_count // 3) if spoken else None,
        "turn_count": len(turns),
        "audio_turn_count": audio_turns,
        "word_count": word_count,
    }


def local_report(session: dict) -> dict:
    stats = local_stats(session)
    detail = stats.get("pronunciation_detail")
    if detail and detail.get("overall") is not None:
        summary = (
            f"本地逐词评测平均 {detail['overall']} 分（准确 {detail['accuracy']} · 流利 {detail['fluency']} "
            f"· 完整 {detail['completeness']}），共 {detail['turns']} 轮有录音。"
        )
        if detail.get("focus_words"):
            summary += f"需要重读：{'、'.join(detail['focus_words'])}。"
        if detail.get("weakest"):
            first = detail["weakest"][0]
            if first.get("phonemes"):
                summary += f"最弱的是 {first['word']} /{first['phonemes']}/（{first['score']} 分）。"
    else:
        summary = "本次没有开启逐轮发音评测，因此没有音素级分数；录音与转写都已保存。"
    profile = dict(stats.get("profile") or {})
    profile["summary"] = f"本场 {stats['turn_count']} 轮、{stats['word_count']} 词，先把这三句读顺，再补时态。"
    grammar = stats.get("grammar") or {}
    return {
        **stats,
        "profile": profile,
        "demo": True,
        "summary": f"本次完成 {stats['turn_count']} 轮对话，共 {stats['word_count']} 个英文词。演示报告只统计流程，不评价语言水平。",
        "pronunciation_summary": summary,
        "highlights": [],
        "next_step": (
            f"下一场重点练{GRAMMAR_TYPE_LABELS[grammar['top_types'][0][0]]}。"
            if grammar and grammar.get("top_types")
            else "从本轮记录里挑一句，去跟读训练。"
        ),
    }


async def make_report(session: dict) -> dict:
    """DeepSeek-written report; silently degrades to local statistics."""
    stats = local_stats(session)
    measured = stats.get("pronunciation_detail")
    if not llm_provider.available() or time.monotonic() < _cooldown_until:
        return local_report(session)

    lines = []
    for turn in session.get("turns", []):
        if not turn.get("transcript"):
            continue
        lines.append(f"Student: {turn['transcript']}\nYou: {turn.get('tutor_reply', '')}")
    if not lines:
        return local_report(session)

    scene = session.get("scene") or {}
    grade = session.get("grade") or DEFAULT_GRADE
    knowledge = "；".join(scene.get("knowledge") or [])
    measured_line = ""
    if measured and measured.get("overall") is not None:
        weakest = "；".join(
            f"{item['word']} /{item['phonemes']}/ {item['score']} 分"
            + (f"（弱音素 /{'/ /'.join(item['symbols'])}/）" if item.get("symbols") else "")
            for item in (measured.get("weakest") or [])
        )
        measured_line = (
            f"Measured pronunciation (local per-word acoustic model, averaged over "
            f"{measured['turns']} recorded turns): overall {measured['overall']}, "
            f"accuracy {measured['accuracy']}, fluency {measured['fluency']}, "
            f"completeness {measured['completeness']}. "
            f"Words to re-drill: {', '.join(measured.get('focus_words') or []) or 'none'}. "
            + (f"Weakest words with IPA: {weakest}. " if weakest else "")
            + "\n"
        )
    grammar = stats.get("grammar")
    grammar_line = ""
    if grammar:
        detail = "、".join(
            f"{GRAMMAR_TYPE_LABELS.get(kind, kind)} {count} 次"
            for kind, count in grammar["top_types"]
        )
        grammar_line = (
            f"Measured grammar (per-sentence LLM check over {grammar['turns']} sentences): "
            f"average {grammar['average']}/100. Issue frequency: {detail or 'none'}.\n"
        )
    messages = [
        {
            "role": "system",
            "content": (
                "You assess a Chinese high-school student's spoken English after a role-play. "
                "Return JSON only with keys: overall_score (0-100), expression (0-100), fluency (0-100), "
                "summary (Chinese, 40-70 chars, encouraging and concrete), pronunciation_summary (Chinese, "
                "one sentence; use the measured numbers when they are given and never invent "
                "音素级 scores), "
                "highlights (array of 1-2 short English sentences the student actually said, verbatim), "
                "next_step (Chinese, one actionable suggestion), "
                "profile_summary (Chinese, 45-70 chars: what kind of learner this is, based on the "
                "measured numbers, no flattery), "
                "profile_notes (object with exactly four Chinese strings, each <=26 chars, keys: "
                "grammar, tense, fluency, pronunciation — each MUST start from the measured number "
                "given for that dimension; never use a different number). "
                f"{report_block(grade)} "
                "Be honest but kind. Do not mention that you are a model."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Scene: {scene.get('title_zh') or session.get('topic_title')} / {scene.get('goal') or ''}\n"
                f"Grammar points this scene targets: {knowledge or '（未指定）'}\n"
                f"Student grade: {grade}（CEFR {profile(grade)['cefr']}）\n"
                + measured_line + grammar_line +
                "\nMeasured profile (cite these exact numbers, do not invent others): "
                + "；".join(
                    f"{item['label']} {item['score'] if item['score'] is not None else '未测'}"
                    for item in (stats.get("profile") or {}).get("dimensions", [])
                )
                + "\n\n" + "\n\n".join(lines)
            ),
        },
    ]
    try:
        result = await chat(messages, json_mode=True, temperature=0.4, max_tokens=600, timeout=40, thinking=False)
        data = json.loads(result.text)
    except (DeepSeekError, ValueError) as error:
        if isinstance(error, DeepSeekError):
            _trip(120 if error.code == "insufficient_balance" else 20, str(error))
        return local_report(session)

    def number(key: str) -> int | None:
        try:
            return max(0, min(100, int(data.get(key))))
        except (TypeError, ValueError):
            return stats.get(key)

    # 注意不要叫 profile：模块顶部导入的 profile(grade) 是年级画像函数，
    # 同名局部变量会让上面那句 f-string 直接 UnboundLocalError。
    portrait = dict(stats.get("profile") or {})
    notes = data.get("profile_notes") if isinstance(data.get("profile_notes"), dict) else {}
    for dimension in portrait.get("dimensions", []):
        written = str(notes.get(dimension["key"]) or "").strip()
        if written:
            dimension["note"] = written[:60]
    portrait["summary"] = str(data.get("profile_summary") or "").strip()[:120]

    return {
        **stats,
        "profile": portrait,
        "demo": False,
        "overall_score": number("overall_score"),
        "expression": number("expression"),
        "fluency": number("fluency"),
        "summary": str(data.get("summary") or "")[:160],
        "pronunciation_summary": str(data.get("pronunciation_summary") or "")[:200],
        "highlights": [str(item)[:120] for item in (data.get("highlights") or [])][:2],
        "next_step": str(data.get("next_step") or "")[:160],
    }
