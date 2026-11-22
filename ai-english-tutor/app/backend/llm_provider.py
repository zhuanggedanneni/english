"""Runtime switch between DeepSeek (cloud) and local Qwen (Ollama).

The choice is process-wide and persisted to ``data/llm_provider.json`` so a
demo does not need a restart (or a code change) to fall back to the local model.
Every module that talks to a chat model asks this module which backend is
active; the acoustic model and Doubao speech services are not affected.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "llm_provider.json"

DEEPSEEK = "deepseek"
QWEN = "qwen"
PROVIDERS = (DEEPSEEK, QWEN)
LABELS = {DEEPSEEK: "DeepSeek API", QWEN: "Qwen 本地"}


def _load() -> str:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        value = str(data.get("provider") or "").strip().lower()
    except (OSError, ValueError):
        value = ""
    if value in PROVIDERS:
        return value
    env = os.getenv("LLM_PROVIDER", "").strip().lower()
    return env if env in PROVIDERS else DEEPSEEK


_current = _load()


def current() -> str:
    return _current


def label(name: str | None = None) -> str:
    return LABELS.get(name or _current, LABELS[DEEPSEEK])


def set_current(name: str) -> str:
    global _current
    value = (name or "").strip().lower()
    if value not in PROVIDERS:
        raise ValueError("模型只能是 deepseek 或 qwen")
    _current = value
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps({"provider": value}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass
    return _current


def available() -> bool:
    """Whether the active backend can be called at all (key / address present)."""
    from .config import settings

    if _current == QWEN:
        return bool(settings.qwen_base_url)
    return bool(settings.deepseek_api_key)


def model_name() -> str:
    from .config import settings

    return settings.qwen_model if _current == QWEN else settings.deepseek_vision_model


def status() -> dict:
    return {
        "provider": _current,
        "label": label(),
        "model": model_name(),
        "available": available(),
        "options": [
            {"id": DEEPSEEK, "label": LABELS[DEEPSEEK], "model": _deepseek_model()},
            {"id": QWEN, "label": LABELS[QWEN], "model": _qwen_model()},
        ],
    }


def _deepseek_model() -> str:
    from .config import settings

    return settings.deepseek_vision_model


def _qwen_model() -> str:
    from .config import settings

    return settings.qwen_model
