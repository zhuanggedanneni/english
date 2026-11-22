"""iFlytek ISE streaming evaluation adapter.

The browser records 16 kHz mono PCM16 and the API server owns the APIKey and
APISecret. Audio is never sent to iFlytek from client-side JavaScript, so the
secret stays out of the static page.
"""
from __future__ import annotations

import array
import asyncio
import base64
import hashlib
import hmac
import io
import json
import sys
import wave
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import urlencode

import websockets

from .config import settings

ISE_HOST = settings.xfyun_ise_host
ISE_PATH = "/v2/open-ise"
MAX_PCM_SECONDS = 300


class IseError(RuntimeError):
    def __init__(self, message: str, code: int | str | None = None):
        super().__init__(message)
        self.code = code


@dataclass
class WavInfo:
    pcm: bytes
    duration_seconds: float


def wav_to_pcm16k(wav_bytes: bytes) -> WavInfo:
    if not wav_bytes:
        raise ValueError("音频为空，请重新录音")
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as source:
            channels = source.getnchannels()
            width = source.getsampwidth()
            rate = source.getframerate()
            frames = source.readframes(source.getnframes())
    except (wave.Error, EOFError) as error:
        raise ValueError("讯飞评测需要 16k 单声道 WAV 音频，请使用浏览器重新录制") from error
    if width != 2:
        raise ValueError("音频位深必须是 16 bit，请重新录音")
    if rate <= 0 or not frames:
        raise ValueError("录音内容为空，请再读一次")
    samples = array.array("h")
    samples.frombytes(frames[: len(frames) - (len(frames) % 2)])
    if sys.byteorder != "little":
        samples.byteswap()
    if channels > 1:
        mono = array.array("h", [0]) * (len(samples) // channels)
        for index in range(len(mono)):
            base = index * channels
            mono[index] = int(sum(samples[base:base + channels]) / channels)
        samples = mono
    if rate != 16000:
        ratio = rate / 16000
        output_length = max(1, int(len(samples) / ratio))
        resampled = array.array("h", [0]) * output_length
        for index in range(output_length):
            start = int(index * ratio)
            end = min(len(samples), max(start + 1, int((index + 1) * ratio)))
            resampled[index] = int(sum(samples[start:end]) / (end - start))
        samples = resampled
    duration = len(samples) / 16000
    if duration > MAX_PCM_SECONDS:
        raise ValueError("单次评测不能超过 5 分钟")
    if duration < 0.35:
        raise ValueError("录音太短，请完整读出单词或句子")
    return WavInfo(pcm=samples.tobytes(), duration_seconds=round(duration, 2))


def signed_url() -> str:
    if not ISE_HOST:
        raise IseError("未配置讯飞评测地址，请设置 XFYUN_ISE_HOST。")
    date = format_datetime(datetime.now(timezone.utc), usegmt=True)
    origin = f"host: {ISE_HOST}\ndate: {date}\nGET {ISE_PATH} HTTP/1.1"
    signature = base64.b64encode(
        hmac.new(settings.xfyun_api_secret.encode(), origin.encode(), hashlib.sha256).digest()
    ).decode()
    authorization_origin = (
        f'api_key="{settings.xfyun_api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(authorization_origin.encode()).decode()
    return f"wss://{ISE_HOST}{ISE_PATH}?" + urlencode({
        "authorization": authorization,
        "date": date,
        "host": ISE_HOST,
    })


def _attribute_float(element: ET.Element, name: str) -> float:
    try:
        return float(element.attrib.get(name, "0"))
    except (TypeError, ValueError):
        return 0.0


def _percentage(value: float, scale: float) -> int:
    return max(0, min(100, round(value * scale)))


def parse_result(xml_text: str, target_text: str) -> dict:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as error:
        raise IseError("讯飞返回的评测结果无法解析") from error
    chapter = root.find(".//read_chapter")
    if chapter is None:
        chapter = root.find(".//read_sentence")
    if chapter is None:
        raise IseError("讯飞没有返回朗读章节评分")
    raw_scores = [
        _attribute_float(chapter, "accuracy_score"),
        _attribute_float(chapter, "fluency_score"),
        _attribute_float(chapter, "integrity_score"),
        _attribute_float(chapter, "total_score"),
    ]
    scale = 20.0 if max(raw_scores) <= 5.0001 else 1.0
    accuracy = _percentage(raw_scores[0], scale)
    fluency = _percentage(raw_scores[1], scale)
    completeness = _percentage(raw_scores[2], scale)
    overall = _percentage(raw_scores[3], scale)

    word_results = []
    for node in chapter.findall(".//word"):
        content = (node.attrib.get("content") or "").strip()
        if not content or not any(ch.isalnum() for ch in content):
            continue
        score = _percentage(_attribute_float(node, "total_score"), scale)
        dp_message = int(node.attrib.get("dp_message", "0") or 0)
        status = "good" if score >= 85 else "fair" if score >= 70 else "focus"
        if dp_message == 1:
            status = "missing"
        word_results.append({
            "word": content,
            "score": score,
            "status": status,
            "omitted": dp_message == 1,
        })

    focus_words = [item["word"] for item in word_results if item["status"] in {"focus", "missing"}]
    if not focus_words:
        suggestion = "整体清晰。可以再录一遍，尝试把重音和句子节奏读得更自然。"
    else:
        preview = "、".join(focus_words[:4])
        suggestion = f"优先重读 {preview}。先慢速读准每个音，再用正常语速连起来。"
    if completeness < 75:
        suggestion = "这次有漏读或未识别到完整内容。先完整读完，再追求速度。"
    if overall >= 90 and not focus_words:
        summary = "讯飞评测：发音清晰，完整度好。"
    elif overall >= 75:
        summary = "讯飞评测：基本读准，仍有少量词需要巩固。"
    else:
        summary = "讯飞评测：建议先逐词读准，再连成整句。"

    return {
        "demo": False,
        "provider": "xfyun-ise",
        "overall_score": overall,
        "accuracy": accuracy,
        "completeness": completeness,
        "fluency": fluency,
        "word_results": word_results,
        "focus_words": focus_words,
        "summary": summary,
        "suggestion": suggestion,
        "scale": "100",
    }


async def evaluate_pcm(pcm: bytes, target_text: str) -> dict:
    if not settings.xfyun_configured:
        raise IseError("讯飞评测凭证未配置")
    # A single English word is reliably scored by read_sentence on en_vip;
    # read_word rejects short word recordings with ret=8195 on this account.
    business = {
        "category": "read_sentence",
        "rstcd": "utf8",
        "group": "pupil",
        "sub": "ise",
        "tte": "utf-8",
        "cmd": "ssb",
        "auf": "audio/L16;rate=16000",
        "ent": "en_vip",
        "aus": 1,
        "aue": "raw",
        "result_level": "complete",
        "text": "\ufeff" + target_text,
    }
    try:
        async with websockets.connect(signed_url(), open_timeout=20, close_timeout=5, max_size=None) as socket:
            await socket.send(json.dumps({
                "common": {"app_id": settings.xfyun_appid},
                "business": business,
                "data": {"status": 0},
            }))
            chunk_size = 1280  # 40 ms of 16 kHz / 16-bit mono
            chunks = [pcm[i:i + chunk_size] for i in range(0, len(pcm), chunk_size)]
            for index, chunk in enumerate(chunks):
                is_last = index == len(chunks) - 1
                await socket.send(json.dumps({
                    "business": {"aue": "raw", "cmd": "auw", "aus": 4 if is_last else 2},
                    "data": {
                        "status": 2 if is_last else 1,
                        "data": base64.b64encode(chunk).decode(),
                        "data_type": 1,
                    },
                }))
            while True:
                message = json.loads(await asyncio.wait_for(socket.recv(), timeout=45))
                if message.get("code") not in (0, None):
                    raise IseError(str(message.get("message") or "讯飞评测失败"), message.get("code"))
                if (message.get("data") or {}).get("status") == 2:
                    payload = message["data"].get("data")
                    if not payload:
                        raise IseError("讯飞返回了空结果")
                    xml_text = base64.b64decode(payload).decode("utf-8", "replace")
                    return parse_result(xml_text, target_text)
    except asyncio.TimeoutError as error:
        raise IseError("讯飞评测超时，请检查网络后重试") from error
    except websockets.WebSocketException as error:
        raise IseError(f"无法连接讯飞评测服务：{error}") from error
