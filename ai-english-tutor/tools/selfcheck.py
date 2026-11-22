"""开箱自检：不依赖浏览器，逐项确认包和两个服务是不是好的。

    python tools\\selfcheck.py

只读：不启动、不修改任何东西。想先启动服务再自检，就先双击 启动.bat。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
import wave
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG / "scorer"))  # 评测代码在 scorer/app 下
OK, BAD, WARN = "[ OK ]", "[FAIL]", "[WARN]"
results: list[tuple[str, bool, str]] = []


def check(label: str, condition: bool, detail: str = "", warn: bool = False) -> None:
    results.append((label, condition, detail))
    print(f"{OK if condition else (WARN if warn else BAD)} {label}" + (f" — {detail}" if detail else ""))


def http(url: str, timeout: float = 3.0, body: bytes | None = None,
         headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, OSError) as error:
        return 0, str(error).encode()


def silence_wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


def multipart(fields: dict[str, str], filename: str, payload: bytes) -> tuple[bytes, str]:
    boundary = "----selfcheck7f3a"
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: audio/wav\r\n\r\n".encode() + payload + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def main() -> int:
    print("=" * 60)
    print("AI 英语外教 开箱自检")
    print("=" * 60)

    print(f"{sys.prefix}  ·  Python {sys.version.split()[0]}")
    for module in ("torch", "transformers", "fastapi", "uvicorn", "soundfile", "sqlalchemy", "pydantic_settings"):
        try:
            __import__(module)
            check(f"依赖 {module}", True)
        except Exception as error:  # noqa: BLE001
            check(f"依赖 {module}", False, str(error))

    model_dir = Path(os.environ.get("PHONEME_MODEL_PATH", str(PKG / "models" / "phoneme-model")))
    weights = next(
        (path for path in (model_dir / "pytorch_model.bin", model_dir / "model.safetensors") if path.exists()),
        None,
    )
    check("音素模型权重", bool(weights) and weights.stat().st_size > 100 * 1024 * 1024,
          f"{weights.stat().st_size / 1e9:.2f} GB" if weights else "缺失（权重不随仓库分发）")
    check("模型配置", (model_dir / "config.json").exists())
    espeak_dir = Path(os.environ.get("ESPEAK_ROOT", str(PKG / "tools" / "espeak")))
    check("espeak 库", (espeak_dir / "libespeak-ng.dll").exists() or bool(shutil.which("espeak-ng")),
          str(espeak_dir), warn=True)
    check("espeak 数据", (espeak_dir / "espeak-ng-data" / "phontab").exists() or bool(shutil.which("espeak-ng")),
          "使用系统 espeak-ng", warn=True)

    try:
        from app.services.scoring.backends import get_backend  # noqa: F401
        check("评测代码可导入", True)
    except Exception as error:  # noqa: BLE001
        check("评测代码可导入", False, str(error))

    app_health = f"http://127.0.0.1:8765/api/health"
    status, payload = http(app_health)
    if status == 200:
        data = json.loads(payload)
        speech = data.get("speech_eval") or {}
        check("主站 8765", True, data.get("provider", {}).get("label", ""))
        check("主站指向本地评测", speech.get("mode") == "local", str(speech.get("label")))
    else:
        check("主站 8765", False, "没启动？双击 启动.bat", warn=True)

    scorer_health = "http://127.0.0.1:8010/api/health"
    status, payload = http(scorer_health)
    if status == 200:
        data = json.loads(payload)
        check("评测服务 8010", True, f"模型加载={'完成' if data.get('loaded') else '进行中'}")
        body, content_type = multipart(
            {"reference_text": "hello there", "language": "en"}, "selfcheck.wav", silence_wav())
        status, payload = http("http://127.0.0.1:8010/api/oral/score", timeout=120,
                               body=body, headers={"Content-Type": content_type})
        if status == 200:
            data = json.loads(payload)
            words = data.get("words") or []
            check("评测接口出分", len(words) >= 1,
                  f"{data.get('accuracy')} 分 / {len(words)} 词 / {data.get('elapsed_ms')} ms")
        else:
            check("评测接口出分", False, payload[:200].decode(errors="replace"))
    else:
        check("评测服务 8010", False, "没启动？双击 启动.bat", warn=True)

    failed = [label for label, ok, _ in results if not ok]
    print("=" * 60)
    print("全部通过，可以直接演示。" if not failed else f"有 {len(failed)} 项没通过：{', '.join(failed)}")
    print(f"日志：{PKG / 'logs'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
