import faulthandler
import sys
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QtMsgType, qInstallMessageHandler
from PySide6.QtWidgets import QApplication

from hanhua.core.memory_lifecycle import clear_all_project_records
from hanhua.core.settings import SettingsStore
from hanhua.ui.app_state import AppState
from hanhua.ui.main_window import MainWindow
from hanhua.ui.theme import apply_theme

APP_DIR = Path.home() / ".hanhua"


def _install_crash_hooks() -> Path:
    """三层崩溃捕获（2026-08-14 闪退排查：此前入口无任何钩子，崩溃
    完全静默无法诊断）。

    1. faulthandler：Python 级崩溃（segfault/C++ 越界）dump 调用栈；
    2. sys.excepthook：主线程未捕获异常落盘；
    3. qInstallMessageHandler：PySide6 事件循环内槽异常（默认只打印
       stderr——桌面启动无 stderr 即静默）经 Qt 消息系统转发，一并落盘。
    崩溃后查 ~/.hanhua/logs/crash.log。
    """
    log_dir = APP_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    crash_log = log_dir / "crash.log"
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(crash_log, "a", encoding="utf-8") as f:
            f.write(f"\n===== 启动 {stamp} =====\n")
        # faulthandler 需要常驻句柄（append 模式）
        _dump_handle = open(crash_log, "a", encoding="utf-8")
        faulthandler.enable(_dump_handle)
    except OSError:
        return crash_log

    def _write_entries(entries: str) -> None:
        try:
            with open(crash_log, "a", encoding="utf-8") as f:
                f.write(entries)
        except OSError:
            pass

    def _excepthook(exc_type, exc, tb):
        _write_entries("".join(traceback.format_exception(exc_type, exc, tb)))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook

    def _qt_handler(mode: QtMsgType, _context, message: str):
        if mode == QtMsgType.QtFatalMsg:
            # qFatal（如 QObject 操作已删对象）默认 abort——先落盘再
            # 交给默认处理器，日志不丢
            _write_entries(f"[QtFatal] {message}\n")
        elif mode in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg):
            _write_entries(f"[Qt{mode.name}] {message}\n")

    qInstallMessageHandler(_qt_handler)
    return crash_log


def main():
    _install_crash_hooks()
    # GIL 饿死防护（2026-08-20）：PySide6 全局线程池默认 maxThreadCount=
    # CPU 核数（本机 32），扫描/翻译期间几十个 worker 挤进全局池争抢
    # GIL，事件循环被饿死、GUI 卡死。压到 4（重 IO/CPU 混合任务经验
    # 值）：单扫描任务独占一个槽，界面始终留有余量保持响应。
    # 放在 main() 而非 MainWindow.__init__：测试进程直接构造 MainWindow，
    # 全局池压到 4 会让泄漏的 worker 占满槽位 → await_reload 轮询超时
    # （全量套件 17 个 UI 测试顺序依赖失败，实测定位）。
    from PySide6.QtCore import QThreadPool
    QThreadPool.globalInstance().setMaxThreadCount(4)
    app = QApplication(sys.argv)
    app.setApplicationName("汉化助手")
    app.setOrganizationName("hanhua")
    apply_theme(app)
    settings = SettingsStore(APP_DIR / "settings.json")
    settings.load()
    memory_cleanup = clear_all_project_records(APP_DIR)
    state = AppState(
        APP_DIR,
        settings,
        resource_dir=Path(__file__).resolve().parent,
        memory_cleanup=memory_cleanup,
    )
    app.aboutToQuit.connect(state.close)
    # 全量运行日志（0.41.0 任务七）：退出时统一关闭全部日志句柄，
    # 保证逐条 flush 之外再无缓冲丢失（失败静默，绝不阻断退出）
    from hanhua.core.run_log import close_all_run_logs
    app.aboutToQuit.connect(close_all_run_logs)
    win = MainWindow(state)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
