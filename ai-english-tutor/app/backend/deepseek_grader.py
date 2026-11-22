"""Photo grading adapter for handwritten dictation.

All image recognition is routed through DeepSeek. The prompt asks for strict
JSON, but recognition is compared locally so a chatty model cannot fake a pass.
"""
from __future__ import annotations

import json
import re

from .config import settings
from .deepseek_client import DeepSeekError, chat, image_part


class PhotoGraderError(RuntimeError):
    def __init__(self, message: str, *, code: str = "grader_error", hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint


class PhotoResult:
    def __init__(self, status: str, provider: str, message: str, items: list[dict], hint: str = ""):
        self.status = status
        self.provider = provider
        self.message = message
        self.items = items
        self.hint = hint


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _extract_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I | re.S)
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if not match:
        raise PhotoGraderError("批改结果不是 JSON。", code="bad_response", hint="模型没有按 JSON 格式返回，可直接重拍重试。")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as error:
        raise PhotoGraderError("批改结果无法解析。", code="bad_response") from error


def _prompt(expectations: list[dict]) -> str:
    numbered = "\n".join(f"{index + 1}. {item['word']}" for index, item in enumerate(expectations))
    return (
        "你是高中英语听写批改器。图片是一张学生手写的英语听写纸。\n"
        "本次标准答案按题号排列如下：\n"
        f"{numbered}\n"
        "逐题读出学生在对应位置写下的英文，并判断拼写是否正确。\n"
        "规则：只比较字母拼写，忽略大小写、题号、涂改和无关笔迹；不要评价字迹；"
        "看不清时 recognized 填 null 且 correct 填 null，不要猜。\n"
        '只返回 JSON：{"items":[{"index":1,"recognized":"achieve","correct":true,"note":""}]}'
    )


async def grade_handwriting(image_bytes: bytes, content_type: str, expectations: list[dict]) -> PhotoResult:
    if not settings.deepseek_configured:
        return PhotoResult(
            status="pending",
            provider="DeepSeek（待配置）",
            message="照片已保存。配置 DeepSeek Key 后会自动识别手写拼写并逐词批改。",
            items=[
                {"word_id": item["id"], "expected": item["word"], "recognized": None, "correct": None}
                for item in expectations
            ],
            hint="在 .env 填写 DEEPSEEK_API_KEY 并重启服务。",
        )

    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": _prompt(expectations)},
            image_part(image_bytes, content_type or "image/jpeg"),
        ],
    }]
    try:
        result = await chat(messages, json_mode=True, temperature=0, max_tokens=900)
        parsed = _extract_json(result.text)
    except DeepSeekError as error:
        raise PhotoGraderError(str(error), code=error.code, hint=error.hint) from error

    returned: dict[int, dict] = {}
    for row in parsed.get("items", []):
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        returned[index] = row

    items = []
    for index, expected in enumerate(expectations, start=1):
        row = returned.get(index, {})
        raw = row.get("recognized")
        recognized = None if raw is None else str(raw).strip()
        if recognized is None:
            correct = None
        else:
            correct = _normalize(recognized) == _normalize(expected["word"])
        items.append({
            "word_id": expected["id"],
            "expected": expected["word"],
            "recognized": recognized,
            "correct": correct,
        })

    graded = [item for item in items if item["correct"] is not None]
    correct_count = sum(1 for item in graded if item["correct"])
    if not graded:
        return PhotoResult(
            status="unreadable",
            provider=f"deepseek:{result.model}",
            message="照片里没有识别到可判断的手写内容，请重拍一张更清晰的照片。",
            items=items,
            hint="确保整页四角入镜、光线均匀，字迹不要被手指遮挡。",
        )
    return PhotoResult(
        status="graded",
        provider=f"deepseek:{result.model}",
        message=f"识别完成：{correct_count}/{len(graded)} 个拼写正确。"
        + ("" if len(graded) == len(items) else f" 另有 {len(items) - len(graded)} 个未看清。"),
        items=items,
    )
