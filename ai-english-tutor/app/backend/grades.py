"""Per-grade language profile for the spoken conversation.

The tutor is not graded by a textbook chapter, so "高一" on its own means
nothing to the model: it will happily answer a grade-1 student in C2 English
and then wonder why the student goes quiet. Each profile therefore pins down
what the AI may *use* (structures, sentence length, vocabulary range) and what
the student should be *pushed* to produce. Both halves matter — the first keeps
the input comprehensible, the second turns a chat into practice.
"""
from __future__ import annotations

GRADE_PROFILES: dict[str, dict] = {
    "高一": {
        "cefr": "A2",
        "vocabulary": "约 1600 词（必修一至必修二）",
        "sentence_words": 12,
        "allowed": [
            "一般现在时、一般过去时、一般将来时（will / be going to）",
            "现在进行时",
            "can / must / have to / should 等情态动词",
            "there be 句型、祈使句",
            "简单宾语从句 I think (that)... / I know (that)...",
        ],
        "avoid": ["现在完成时", "被动语态", "定语从句", "虚拟语气", "非谓语作状语"],
        "functions": ["用 because / so 说明原因和结果", "用 I'd like / Could I... 提出请求", "描述一次经历的时间、地点、人物"],
        "topics": ["校园生活", "饮食与购物", "问路出行", "身体健康", "兴趣爱好"],
    },
    "高二": {
        "cefr": "B1",
        "vocabulary": "约 2800 词（必修三至选择性必修二）",
        "sentence_words": 16,
        "allowed": [
            "现在完成时（含 for / since）",
            "被动语态（一般现在时与一般过去时）",
            "定语从句 who / which / that",
            "if 条件句（真实条件与第一类虚拟）",
            "动词不定式作目的状语",
        ],
        "avoid": ["名词性从句", "倒装句", "虚拟语气（与过去事实相反）", "with 复合结构"],
        "functions": ["用 Why not / We could / You'd better 提建议", "比较两个选项的优劣", "说明经历与结果 have done"],
        "topics": ["环境保护", "文化差异", "科技与生活", "志愿服务", "旅行与出行"],
    },
    "高三": {
        "cefr": "B1+ 至 B2",
        "vocabulary": "约 3500 词（选择性必修三及选修）",
        "sentence_words": 20,
        "allowed": [
            "名词性从句 what / that / whether",
            "非谓语动词（doing / done / to do）作主语、定语或状语",
            "倒装与强调（Not only... / It is ... that ...）",
            "虚拟语气基础（If I were you / It is time that...）",
            "with 复合结构",
        ],
        "avoid": ["生僻学术词汇", "超过 25 词的复杂长句"],
        "functions": ["用 On one hand... On the other hand... 组织论证", "让步与反驳 Even so / That may be true, but...", "推测 may / might / must have done", "概括并给出结论"],
        "topics": ["社会议题", "职业规划", "科技伦理", "全球议题", "教育与成长"],
    },
}

DEFAULT_GRADE = "高一"


def profile(grade: str) -> dict:
    return GRADE_PROFILES.get((grade or "").strip(), GRADE_PROFILES[DEFAULT_GRADE])


def level_block(grade: str) -> str:
    """What the AI may speak — the ceiling for its own English."""
    item = profile(grade)
    allowed = "；".join(item["allowed"])
    avoid = "、".join(item["avoid"])
    return (
        f"The student is 中国{grade}（CEFR {item['cefr']}，{item['vocabulary']}）。\n"
        f"Speak at exactly this level: at most {item['sentence_words']} words per sentence, "
        f"and only these structures: {allowed}.\n"
        f"Do NOT use: {avoid}.\n"
        "If the student seems stuck, simplify your sentence instead of switching to Chinese."
    )


def practice_block(grade: str, knowledge: list[str] | None = None) -> str:
    """What the student should be pushed to produce."""
    item = profile(grade)
    functions = "；".join(item["functions"])
    lines = [f"Push the student to produce {grade}-level output: {functions}."]
    if knowledge:
        lines.append("This scene's target points: " + "；".join(knowledge) + ".")
    lines.append(
        "Weave one target structure into your own sentence as a model, then ask a question "
        "that makes the student use it. Never name the grammar point out loud."
    )
    return "\n".join(lines)


def report_block(grade: str) -> str:
    item = profile(grade)
    return (
        f"Judge the student against 中国{grade} level（CEFR {item['cefr']}）："
        f"句子长度、{ '、'.join(item['allowed'][:2]) } 的掌握程度。"
        f"超出该年级的语法失误不必扣分，未达到该年级应有的表达要指出。"
    )
