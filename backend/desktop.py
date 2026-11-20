"""遥操测试台桌面版：在本进程里起后端，用原生窗口（Qt WebEngine）打开界面。

运行：python desktop.py（在 backend 目录）；关掉窗口即退出，后端随之停止。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time

import uvicorn
import webview

from bench.app import app


def free_port(preferred: int = 8765) -> int:
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", preferred)) != 0:
            return preferred
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _webengine_sandbox_blocked() -> bool:
    """Ubuntu 24.04 默认用 AppArmor 限制非特权用户命名空间，pip 装的 Qt WebEngine 沙箱起不来，窗口会白屏或直接退出。"""
    try:
        with open("/proc/sys/kernel/apparmor_restrict_unprivileged_userns") as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def main() -> None:
    if _webengine_sandbox_blocked():
        # 界面只加载本机 127.0.0.1 的页面，关掉沙箱不引入外部网页风险
        os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    port = free_port(int(os.environ.get("PORT", "8765")))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    else:
        sys.exit("后端没有起来")
    webview.create_window("遥操测试台 Teleop Bench", f"http://127.0.0.1:{port}/#latency",
                          width=1440, height=920, min_size=(1100, 720))
    webview.start(gui="qt")
    server.should_exit = True


if __name__ == "__main__":
    main()
