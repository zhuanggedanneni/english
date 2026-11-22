"""一键启动 / 停止（开源版）。

    python tools/launcher.py          启动评测服务 + 主站，然后打开浏览器
    python tools/launcher.py stop     停止两个服务

之所以不用两个 .bat 各起一个窗口：那样关窗口就丢进程、报错也看不到。
这里把两个子进程和它们的日志都管起来，Ctrl+C 或关窗口时一起收掉。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
PY = Path(sys.executable)
LOGS = PKG / "logs"
PIDS = LOGS / "pids.json"
APP_PORT = int(os.getenv("APP_PORT", "8765"))
SCORER_PORT = int(os.getenv("SCORER_PORT", "8010"))
# 主站有自签证书就走 https（app/run.py 检测同一份文件）；绑 0.0.0.0 手机/平板也能连。
CERTS = PKG / "app" / "certs"
USE_HTTPS = (CERTS / "cert.pem").exists() and (CERTS / "key.pem").exists()
APP_SCHEME = "https" if USE_HTTPS else "http"

SERVICES = {
    "scorer": {
        "cwd": PKG / "scorer",
        "args": [str(PKG / "scorer" / "serve.py")],
        "env": {"SCORER_PORT": str(SCORER_PORT)},
        "log": LOGS / "scorer.log",
        "health": f"http://127.0.0.1:{SCORER_PORT}/api/health",
        "label": "本地发音评测",
    },
    "app": {
        "cwd": PKG / "app",
        "args": [str(PKG / "app" / "run.py")],
        "env": {"PORT": str(APP_PORT), "HOST": "0.0.0.0"},
        "log": LOGS / "app.log",
        "health": f"{APP_SCHEME}://127.0.0.1:{APP_PORT}/api/health",
        "label": "英语 AI 外教",
    },
}


def say(text: str) -> None:
    print(text, flush=True)


def alive(url: str, timeout: float = 2.0) -> bool:
    # 自签证书：健康检查跳过校验，浏览器端由用户点一次"继续访问"。
    context = None
    if url.startswith("https://"):
        import ssl

        context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=context) as response:
            return 200 <= response.status < 500
    except (urllib.error.URLError, OSError):
        return False


def lan_ips() -> list[str]:
    """本机局域网 IPv4（排除虚拟网卡段之外的都列上，用户自己挑）。"""
    found: list[str] = []
    try:
        import socket

        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("223.5.5.5", 53))
        found.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    return [ip for ip in found if not ip.startswith("127.")]


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, check=False)


def stop() -> int:
    if not PIDS.exists():
        say("没有找到正在运行的服务（logs/pids.json 不存在）。")
        return 0
    try:
        data = json.loads(PIDS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    stopped = 0
    for name, pid in data.items():
        if isinstance(pid, int):
            kill_tree(pid)
            say(f"已停止 {name}（PID {pid}）")
            stopped += 1
    PIDS.unlink(missing_ok=True)
    if not stopped:
        say("没有记录到进程。")
    return 0


def stream(name: str, pipe, log_path: Path) -> None:
    """镜像子进程输出到控制台和日志文件。

    日志文件可能被占用/只读（杀毒软件、上一份进程没退干净）。那种情况下
    不能让线程炸掉，否则启动器会连 pids.json 都写不进去——用户看到的就是
    "服务明明起来了，窗口却报错退出"。所以打不开就只走控制台。
    """
    handle = None
    try:
        handle = open(log_path, "a", encoding="utf-8", errors="replace")
    except OSError as error:
        print(f"[{name}] 日志不可写（{error}），本次只输出到窗口", flush=True)
    try:
        for raw in pipe:
            line = raw.rstrip()
            if line:
                print(f"[{name}] {line}", flush=True)
            if handle is not None:
                try:
                    handle.write(line + "\n")
                    handle.flush()
                except OSError:
                    handle = None
    finally:
        if handle is not None:
            handle.close()


def launch(name: str, spec: dict, handles: dict) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(spec["env"])
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    log_path = spec["log"]
    try:
        if log_path.exists() and log_path.stat().st_size > 4 * 1024 * 1024:
            log_path.unlink()  # 日志超过 4MB 就重开，别让它无限长
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # 不可写就交给 stream() 降级处理
    process = subprocess.Popen(
        [str(PY)] + spec["args"],
        cwd=str(spec["cwd"]),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    import threading

    thread = threading.Thread(target=stream, args=(name, process.stdout, log_path), daemon=True)
    thread.start()
    handles[name] = process
    return process


def start() -> int:
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        say(f"（提示）日志目录不可写：{error}；本次只在窗口里看输出。")
    for name, spec in SERVICES.items():
        if alive(spec["health"]):
            say(f"{spec['label']} 已经在 {name} 端口运行，先跳过。"
                f"（要重启请先执行 python tools/launcher.py stop）")

    handles: dict[str, subprocess.Popen] = {}
    running: dict[str, int] = {}
    say("─" * 58)
    for name, spec in SERVICES.items():
        if alive(spec["health"]):
            continue
        process = launch(name, spec, handles)
        running[name] = process.pid
        say(f"启动 {spec['label']}（PID {process.pid}）→ 日志 logs/{spec['log'].name}")

    if running:
        try:
            PIDS.write_text(json.dumps(running, indent=2), encoding="utf-8")
        except OSError as error:
            say(f"（提示）进程号写不进 logs/pids.json：{error}；"
                f"关闭本窗口仍会一起停止，但 停止.bat 找不到它们。")

    say("─" * 58)
    for name, spec in SERVICES.items():
        # 评测服务要先加载模型（约 35 秒）才第一次出分，但接口几秒就能响应。
        deadline = time.time() + 120
        while time.time() < deadline:
            if alive(spec["health"]):
                say(f"{spec['label']} 已就绪：{spec['health']}")
                break
            if name in handles and handles[name].poll() is not None:
                say(f"{spec['label']} 启动失败，详见 logs/{spec['log'].name}")
                break
            time.sleep(1)
        else:
            say(f"{spec['label']} 等待超时，详见 logs/{spec['log'].name}")

    url = f"{APP_SCHEME}://127.0.0.1:{APP_PORT}"
    say("─" * 58)
    say(f"浏览器打开：{url}")
    if USE_HTTPS:
        say("（自签证书：浏览器第一次会提示不安全，点「高级 → 继续访问」即可）")
        lan = lan_ips()
        if lan:
            say("手机/平板等其他设备访问：" + "、".join(f"{APP_SCHEME}://{ip}:{APP_PORT}" for ip in lan))
    say("关闭这个窗口，或在窗口按 Ctrl+C，两个服务会一起停止。")
    webbrowser.open(url)

    try:
        while True:
            time.sleep(1)
            for name, process in list(handles.items()):
                if process.poll() is not None:
                    say(f"{SERVICES[name]['label']} 已退出（代码 {process.returncode}），"
                        f"详见 logs/{SERVICES[name]['log'].name}")
                    handles.pop(name, None)
            if not handles:
                say("两个服务都已退出。")
                return 0
    except KeyboardInterrupt:
        say("\n正在停止…")
        for process in handles.values():
            kill_tree(process.pid)
        PIDS.unlink(missing_ok=True)
        say("已停止。")
        return 0


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "start"
    if action in ("stop", "down", "kill"):
        return stop()
    return start()


if __name__ == "__main__":
    sys.exit(main())
