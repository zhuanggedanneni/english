"""本地逐词发音评测服务（开源版）。

只做一件事：给一段录音和它对应的文字，返回逐词音标与逐音素分数。
上游的完整评测服务还带数据库、录音库、网页和 LLM 复盘，那些对"上课时打
个分"没有用，反而要多起一个 PostgreSQL。所以这里只保留评分那一半：

    POST /api/oral/score    multipart: file=WAV, reference_text=原句, language=en
    POST /api/oral/warmup   预热模型（启动时自动跑一次）
    GET  /api/health        进程与模型状态

模型：任意兼容的 wav2vec2 音素分类模型（权重不随仓库分发）
音标：espeak-ng（需自行安装，或通过 ESPEAK_ROOT 指向本地副本）
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PKG = ROOT.parent

# espeak-ng 在 Windows 上用 ANSI 接口读语言数据表，路径里只要有中文就会
# “Illegal byte sequence” 然后直接退出。包里可以放在中文目录（用户就是这么
# 放的），但数据目录必须落在纯英文路径上，所以第一次启动时镜像一份出去。
ESPEAK_ASCII_CANDIDATES = (
    os.environ.get("LOCALAPPDATA"),
    r"C:\ProgramData",
    tempfile.gettempdir(),
)


def _espeak_data_dir(bundled: Path) -> Path:
    if str(bundled).isascii():
        return bundled
    for base in ESPEAK_ASCII_CANDIDATES:
        if not base or not str(base).isascii():
            continue
        target = Path(base) / "pronounce-pack" / "espeak-ng-data"
        try:
            if not (target / "phontab").exists():
                shutil.copytree(bundled, target, dirs_exist_ok=True)
            return target
        except OSError:
            continue
    return bundled


def _prepare_environment() -> None:
    """Point model and espeak paths at local copies when they exist."""
    model_dir = Path(os.environ.get("PHONEME_MODEL_PATH", str(PKG / "models" / "phoneme-model")))
    defaults = {
        "SCORING__PHONEME__PHONEME_MODEL_ID": str(model_dir),
        "SCORING__PHONEME__DEVICE": "cpu",
        "SCORING__PHONEME__LANGUAGE": "en-us",
    }
    # 本地权重存在时完全离线；否则允许 transformers 按模型 ID 下载。
    if model_dir.exists():
        defaults["HF_HUB_OFFLINE"] = "1"
        defaults["TRANSFORMERS_OFFLINE"] = "1"
    espeak_dir = Path(os.environ.get("ESPEAK_ROOT", str(PKG / "tools" / "espeak")))
    library = espeak_dir / "libespeak-ng.dll"
    data = espeak_dir / "espeak-ng-data"
    if library.exists():
        defaults["PHONEMIZER_ESPEAK_LIBRARY"] = str(library)
    if data.exists():
        defaults["ESPEAK_DATA_PATH"] = str(_espeak_data_dir(data))
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


_prepare_environment()

import torch  # noqa: E402  (must come after the env vars above)
import uvicorn  # noqa: E402
from fastapi import FastAPI, File, Form, HTTPException, UploadFile  # noqa: E402

from app.services.factory import get_scoring_service  # noqa: E402

# 评分和对话共享一台机器：留一半核心给音频收发，别把 CPU 抢干。
torch.set_num_threads(max(1, min(4, (os.cpu_count() or 2) // 2)))

MODEL_READY = {"loaded": False, "seconds": None}
# 启动线程和主站的 /warmup 请求会同时进来。transformers 5 的模型类是惰性
# 导入的，两个线程同时第一次 import 会撞车（ImportError），所以这里做单飞：
# 正在加载就立刻返回 pending，让调用方稍后再问。
_WARMING = threading.Event()
_LOAD_LOCK = threading.Lock()


def _warmup() -> dict:
    """Load the two heavyweight pieces once so the first turn is not the slow one."""
    import wave

    if MODEL_READY["loaded"]:
        return {"ok": True, "elapsed_s": MODEL_READY["seconds"], "cached": True}
    if _WARMING.is_set():
        return {"ok": True, "pending": True}
    if not _LOAD_LOCK.acquire(blocking=False):
        return {"ok": True, "pending": True}

    _WARMING.set()
    handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    handle.close()
    started = time.perf_counter()
    try:
        with wave.open(handle.name, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x00\x00" * 16000)
        for attempt in range(2):
            try:
                get_scoring_service().score(handle.name, "hello", [], language="en")
                break
            except ImportError:
                # 惰性导入还没就绪：等一拍再来，第二次一定成功
                if attempt:
                    raise
                time.sleep(1.0)
        seconds = round(time.perf_counter() - started, 1)
        MODEL_READY.update(loaded=True, seconds=seconds)
        print(f"[warmup] model ready in {seconds}s", flush=True)
        return {"ok": True, "elapsed_s": seconds}
    except Exception as error:  # noqa: BLE001 - report, never crash the server
        MODEL_READY.update(loaded=False, seconds=None)
        print(f"[warmup] failed: {error}", flush=True)
        return {"ok": False, "error": str(error)}
    finally:
        _WARMING.clear()
        _LOAD_LOCK.release()
        try:
            os.unlink(handle.name)
        except OSError:
            pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 后台线程：服务先能响应，模型慢慢加载。
    print(f"[scorer] espeak data: {os.environ['ESPEAK_DATA_PATH']}", flush=True)
    threading.Thread(target=_warmup, name="warmup", daemon=True).start()
    yield


app = FastAPI(title="本地逐词发音评测", version="1.0", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "local-pronounce",
        "model": Path(os.environ["SCORING__PHONEME__PHONEME_MODEL_ID"]).name,
        **MODEL_READY,
    }


@app.get("/api/version")
def version() -> dict:
    return {
        "name": "local-phoneme-scorer",
        "version": "1.0",
        "hf_offline": os.environ.get("HF_HUB_OFFLINE") == "1",
    }


@app.post("/api/oral/score")
def score_only(
    reference_text: str = Form(...),
    language: str = Form("en"),
    align: bool = Form(False),
    file: UploadFile = File(...),
):
    """Score one recording against the words the speaker was supposed to say."""
    text = " ".join(reference_text.split()).strip()
    if not text:
        raise HTTPException(status_code=400, detail="reference_text is required")

    data = file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty audio")

    suffix = Path(file.filename or "recording.wav").suffix or ".wav"
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    handle.write(data)
    handle.close()

    started = time.perf_counter()
    try:
        # 课上已经有 ASR 转写，词是已知的，跳过强制对齐省一遍模型。
        result = get_scoring_service().score(handle.name, text, [], language=language)
    except Exception as error:  # noqa: BLE001 - 把原因交给调用方
        raise HTTPException(status_code=500, detail=f"scoring failed: {error}") from error
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass

    return {
        "reference": text,
        "language": language,
        "accuracy": round(result.accuracy_score, 2),
        "fluency": round(result.fluency_score, 2),
        "completeness": round(result.completeness_score, 2),
        "word_count": len(result.word_scores),
        "words": [ws.model_dump() for ws in result.word_scores],
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "align_ms": 0,
        "aligned": bool(align),
    }


@app.post("/api/oral/warmup")
def warmup() -> dict:
    return _warmup()


if __name__ == "__main__":
    port = int(os.getenv("SCORER_PORT", "8010"))
    print(f"[scorer] http://127.0.0.1:{port}  (model: {os.environ['SCORING__PHONEME__PHONEME_MODEL_ID']})", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
