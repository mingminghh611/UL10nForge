"""UL10nForge v2 entry point — starts local FastAPI studio and opens the browser."""
from __future__ import annotations

import argparse
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from ul10n.core.db import GlobalStore
from ul10n.server.app import create_app


def _open_browser_later(url: str, delay: float = 0.8) -> None:
    def _go() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_go, daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="UL10nForge v2 — Unity 游戏智能汉化工作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 = 使用设置中的默认端口 (18765)")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    parser.add_argument("--no-overlay", action="store_true", help="不启用游戏内悬浮编辑窗快捷键 (Alt+L)")
    args = parser.parse_args()

    app_root = Path(__file__).resolve().parent
    port = args.port
    if port <= 0:
        gs = GlobalStore(Path.home() / ".ul10nforge")
        try:
            port = gs.load_settings().server_port
        finally:
            gs.close()

    url = f"http://{args.host}:{port}"
    print(f"UL10nForge v2 已启动：{url}")
    if not args.no_overlay:
        print("游戏内实时译文悬浮窗已待命：游戏中按 Alt+L 呼出/隐藏，可直接修改并热同步到游戏")
    if not args.no_browser:
        _open_browser_later(url)

    app = create_app(app_root=app_root)
    if not args.no_overlay:
        from ul10n.overlay.hud import InGameOverlayHUD

        hk = app.state.services.settings().overlay_hotkey or "Alt+L"
        overlay = InGameOverlayHUD(app.state.services, hotkey_label=hk)
        app.state.services.overlay = overlay
        overlay.start()
    uvicorn.run(app, host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
