"""Environment loading without exposing provider credentials to the browser."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(ROOT / ".env")


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    ai_provider: str = os.getenv("AI_PROVIDER", "demo").strip().lower()
    speech_eval_provider: str = os.getenv("SPEECH_EVAL_PROVIDER", "local").strip().lower()
    # 本地发音评测进程（开源版只保留评分编排，模型权重不入库）。
    local_pronounce_url: str = os.getenv("LOCAL_PRONOUNCE_URL", "http://127.0.0.1:8010").strip().rstrip("/")
    local_pronounce_timeout: float = _float("LOCAL_PRONOUNCE_TIMEOUT", 10.0)
    pron_feedback: str = os.getenv("PRON_FEEDBACK", "on").strip().lower()
    xfyun_appid: str = os.getenv("XFYUN_APP_ID", "").strip()
    xfyun_api_key: str = os.getenv("XFYUN_API_KEY", "").strip()
    xfyun_api_secret: str = os.getenv("XFYUN_API_SECRET", "").strip()
    xfyun_ise_host: str = os.getenv("XFYUN_ISE_HOST", "").strip()
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "").strip()
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "").strip().rstrip("/")
    deepseek_vision_model: str = os.getenv("DEEPSEEK_VISION_MODEL", "your-chat-model").strip()
    # 本地模型（Ollama，OpenAI 兼容接口），用于离线/省钱演示。
    qwen_base_url: str = os.getenv("QWEN_BASE_URL", "http://127.0.0.1:11434/v1").strip().rstrip("/")
    qwen_model: str = os.getenv("QWEN_MODEL", "your-local-model").strip()
    qwen_api_key: str = os.getenv("QWEN_API_KEY", "").strip()
    doubao_api_key: str = os.getenv("DOUBAO_API_KEY", "").strip()
    doubao_asr_url: str = os.getenv("DOUBAO_ASR_WS_URL", "").strip()
    doubao_asr_resource_id: str = os.getenv("DOUBAO_ASR_RESOURCE_ID", "").strip()
    doubao_tts_url: str = os.getenv("DOUBAO_TTS_WS_URL", "").strip()
    doubao_tts_resource_id: str = os.getenv("DOUBAO_TTS_RESOURCE_ID", "").strip()
    doubao_tts_voice: str = os.getenv("DOUBAO_TTS_VOICE", "").strip()
    # 中文解释用的音色。与 doubao_tts_voice 相同时走单音色（中英文同一个声音，
    # 这也是默认值）；设成不同值就自动切双音色——英文音色读中文会返回 0 字节音频。
    doubao_tts_voice_zh: str = os.getenv("DOUBAO_TTS_VOICE_ZH", "").strip()
    doubao_tts_sample_rate: int = int(os.getenv("DOUBAO_TTS_SAMPLE_RATE", "24000"))

    @property
    def doubao_configured(self) -> bool:
        return bool(self.doubao_api_key and self.doubao_asr_url and self.doubao_tts_url)

    @property
    def deepseek_configured(self) -> bool:
        return bool(self.deepseek_api_key and self.deepseek_base_url)

    @property
    def xfyun_configured(self) -> bool:
        return bool(
            self.xfyun_appid and self.xfyun_api_key
            and self.xfyun_api_secret and self.xfyun_ise_host
        )

    @property
    def speech_mode(self) -> str:
        provider = self.speech_eval_provider
        if provider in ("demo", "off", "none", "disabled"):
            return "demo"
        if provider == "xfyun":
            return "xfyun" if self.xfyun_configured else "demo"
        if provider == "local":
            return "local" if self.local_pronounce_url else "demo"
        # auto: the local model needs no credentials, so it wins by default.
        if self.local_pronounce_url:
            return "local"
        return "xfyun" if self.xfyun_configured else "demo"

    @property
    def pron_feedback_enabled(self) -> bool:
        """The coach analysis rides on the active chat model; no backend means no analysis."""
        from .llm_provider import available

        return self.pron_feedback in ("on", "1", "true", "yes") and available()


settings = Settings()
