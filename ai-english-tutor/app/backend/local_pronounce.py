"""Local per-word pronunciation scoring through the local scorer service.

The acoustic model (wav2vec2 CTC + espeak IPA) lives in a separate process
(port 8010) because it needs its own CPU-only PyTorch stack. This module only
speaks HTTP, so the lesson server keeps its light dependency set and a model
hiccup can never take the conversation down.

What the model gives back — and what iFlytek ISE did not:

* one row per word, aligned to the words the student actually said;
* the expected IPA for that word (``stˈɔːr``);
* a CTC score per phoneme symbol, so "store" can be 91 overall while its final
  /r/ is 34.

The numbers are raw acoustic evidence. Turning them into advice is the job of
``backend/pron_feedback.py``, which is a different model call with a different
prompt — never the tutor.
"""
from __future__ import annotations

import io
import time
import wave
from typing import Any

import httpx

from .config import settings

STRESS_MARKS = "ˈˌ"

# Articles are scored against their strong form (/eɪ/, /ðiː/) while real speech
# almost always reduces them (/ə/, /ðə/). The word still shows in the grid, but
# it never becomes a "re-drill this" target or a coaching topic.
REDUCED_FORMS = {"a", "an", "the", "of", "to"}


class LocalPronounceError(RuntimeError):
    """The local scoring service is down, slow, or unhappy."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


def strip_stress(ipa: str) -> str:
    return "".join(char for char in (ipa or "") if char not in STRESS_MARKS)


DIPHTHONGS = {"eɪ", "aɪ", "ɔɪ", "aʊ", "oʊ", "əʊ", "ɪə", "eə", "ʊə"}


def ipa_units(ipa: str) -> list[str]:
    """Split IPA the way the acoustic model scores it.

    The model tokenises one character at a time, except that a length mark stays
    attached to its vowel: "uː" is one token, "eɪ" is two ("e" + "ɪ"). Matching
    that keeps each CTC score under the right symbol.
    """
    text = strip_stress(ipa)
    units: list[str] = []
    index = 0
    while index < len(text):
        if index + 1 < len(text) and text[index + 1] == "ː":
            units.append(text[index:index + 2])
            index += 2
        else:
            units.append(text[index])
            index += 1
    return units


def _phoneme_table(word_result: dict[str, Any]) -> list[tuple[str, int]]:
    """Pair each IPA symbol with its score, folding diphthongs back together.

    "eɪ" costs two model tokens with very different scores (the glide is always
    weaker); averaging them gives the learner one number for one sound.
    """
    units = ipa_units(word_result.get("expected_phonemes") or "")
    scores = word_result.get("phoneme_scores") or []
    if not units or len(units) != len(scores):
        return []

    scaled: list[tuple[str, int]] = []
    for unit, score in zip(units, scores):
        try:
            scaled.append((unit, max(0, min(100, round(float(score))))))
        except (TypeError, ValueError):
            return []

    pairs: list[tuple[str, int]] = []
    cursor = 0
    while cursor < len(scaled):
        if cursor + 1 < len(scaled) and scaled[cursor][0] + scaled[cursor + 1][0] in DIPHTHONGS:
            symbol = scaled[cursor][0] + scaled[cursor + 1][0]
            pairs.append((symbol, round((scaled[cursor][1] + scaled[cursor + 1][1]) / 2)))
            cursor += 2
        else:
            pairs.append(scaled[cursor])
            cursor += 1
    return pairs


def _wav_seconds(audio_bytes: bytes) -> float | None:
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as handle:
            rate = handle.getframerate() or 16000
            return round(handle.getnframes() / rate, 2)
    except (wave.Error, EOFError):
        return None


def normalize(payload: dict[str, Any]) -> dict[str, Any]:
    """Map the local scorer response onto the assessment contract the UI already uses."""
    words: list[dict[str, Any]] = []
    for item in payload.get("words") or []:
        table = _phoneme_table(item)
        # Diphthong glides score low for the model on every speaker (the two
        # halves are separate CTC tokens), so they would flag "way" and "pie"
        # every single turn. Only consonants and steady vowels become advice.
        weak = [
            {"symbol": symbol, "score": score}
            for symbol, score in sorted(table, key=lambda pair: pair[1])
            if score < 55 and symbol not in DIPHTHONGS
        ][:2]
        try:
            score = max(0, min(100, round(float(item.get("accuracy_score") or 0))))
        except (TypeError, ValueError):
            score = 0
        words.append({
            "word": (item.get("word") or "").strip(),
            "score": score,
            "phonemes": strip_stress(item.get("expected_phonemes") or ""),
            "phonemes_stressed": item.get("expected_phonemes") or "",
            "phoneme_scores": [score for _, score in table],
            "weak_phonemes": weak,
        })

    def number(key: str) -> int:
        try:
            return max(0, min(100, round(float(payload.get(key) or 0))))
        except (TypeError, ValueError):
            return 0

    accuracy, fluency, completeness = number("accuracy"), number("fluency"), number("completeness")
    scored = [word for word in words if word["word"]]
    weak_words = sorted(
        [
            word for word in scored
            if word["score"] < 72 and word["word"].lower() not in REDUCED_FORMS
        ],
        key=lambda word: word["score"],
    )
    focus = [word["word"] for word in weak_words[:4]]
    overall = round(0.5 * accuracy + 0.3 * fluency + 0.2 * completeness)

    if weak_words:
        summary = (
            f"{len(scored)} 个词，整体 {overall} 分；"
            f"{len(weak_words)} 个词偏弱：" + "、".join(focus) + "。"
        )
    else:
        summary = f"{len(scored)} 个词，整体 {overall} 分，逐词都没有明显问题。"

    return {
        "provider": "local-pronounce",
        "engine": "wav2vec2 CTC · espeak IPA",
        "scale": "100",
        "overall_score": overall,
        "accuracy": accuracy,
        "fluency": fluency,
        "completeness": completeness,
        "word_results": words,
        "focus_words": focus,
        # Compact form kept on the turn record so the end-of-lesson report can
        # quote the exact word, its IPA and the phoneme that failed.
        "weak_words": [
            {
                "word": word["word"],
                "score": word["score"],
                "phonemes": word["phonemes"],
                "symbols": [item["symbol"] for item in word["weak_phonemes"]],
            }
            for word in weak_words[:3]
        ],
        "summary": summary,
        "suggestion": "",
        "scored_ms": payload.get("elapsed_ms"),
        "language": payload.get("language") or "en",
    }


class LocalPronouncer:
    """HTTP client for the local scoring endpoint, with a kept-alive socket."""

    provider = "local-pronounce"

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        self.base_url = (base_url or settings.local_pronounce_url).rstrip("/")
        self.timeout = timeout or settings.local_pronounce_timeout
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        # One client per process keeps the TCP connection warm; creating one per
        # turn costs ~30 ms and a fresh TLS-free handshake each time.
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def ping(self) -> bool:
        try:
            response = await self._get_client().get(f"{self.base_url}/api/version", timeout=2.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def warmup(self) -> bool:
        """Load the acoustic models before the student speaks.

        Cold start is ~25 s (two wav2vec2 checkpoints); warm turns are 0.8–2 s.
        """
        try:
            response = await self._get_client().post(
                f"{self.base_url}/api/oral/warmup", data={"language": "en"}, timeout=120.0
            )
            return response.status_code == 200 and bool(response.json().get("ok"))
        except (httpx.HTTPError, ValueError):
            return False

    async def assess_pronunciation(self, target_text: str, content_type: str, audio_bytes: bytes) -> dict:
        text = " ".join((target_text or "").split())
        if not text:
            raise LocalPronounceError("这一轮没有识别到英文单词，无法评测。")
        if not audio_bytes:
            raise LocalPronounceError("这一轮没有录到音频。")

        started = time.perf_counter()
        try:
            response = await self._get_client().post(
                f"{self.base_url}/api/oral/score",
                data={"reference_text": text, "language": "en", "align": "false"},
                files={"file": ("turn.wav", audio_bytes, "audio/wav")},
            )
        except httpx.TimeoutException as error:
            raise LocalPronounceError(
                f"本地评测超过 {self.timeout:.0f} 秒没有返回，本轮先跳过。"
            ) from error
        except httpx.HTTPError as error:
            raise LocalPronounceError(
                "本地评测服务没响应（8010 端口未启动？）"
            ) from error

        if response.status_code != 200:
            detail = ""
            try:
                detail = str(response.json().get("detail") or "")
            except ValueError:
                detail = response.text[:120]
            raise LocalPronounceError(f"本地评测返回 {response.status_code}：{detail}", status=response.status_code)

        result = normalize(response.json())
        result["audio_seconds"] = _wav_seconds(audio_bytes)
        result["content_type"] = content_type
        result["roundtrip_ms"] = round((time.perf_counter() - started) * 1000)
        return result


local_pronouncer = LocalPronouncer()
