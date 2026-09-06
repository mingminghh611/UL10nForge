# -*- coding: utf-8 -*-
"""全量运行日志（0.41.0 任务七）回归测试。

背景（用户实证）：GUI 运行区只有两块有界内存视图——实时处理流
（ActivityFeed，max_items=120 丢最旧）与运行记录（log_view，
MaximumBlockCount=2000 静默截断），start()/切换项目还会 clear()。
长跑翻译的批次进度、审核判定、写回审计明细事后无处复盘。

RunLog 是运行信息单一落盘出口：append-only、线程安全、永不抛错。
本文件锁住以下不变量（问题集 F 节）：
1. 同一 game_dir 恒定映射同一日志文件（跨会话追加）；
2. 跨会话追加无 BOM 叠加（utf-8 非 utf-8-sig）；
3. 多行事件续行缩进不丢内容；
4. begin_session 分节格式；
5. IO 失败静默自禁用，绝不抛错阻断主流程；
6. get_run_log 同路径共享实例；
7. translate_page/home_page 的 UI 漏斗（_log_line/_feed_event/
   _runlog_event）确实落盘。
"""

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from hanhua.core.run_log import (
    RunLog, close_all_run_logs, get_run_log, run_log_path)


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _clean_cache():
    """每个用例前后清空模块缓存，避免句柄/实例跨用例泄漏。"""
    close_all_run_logs()
    yield
    close_all_run_logs()


# ── 1. 路径映射 ──

def test_path_stable_and_sanitized(tmp_path):
    game = tmp_path / "My Game: <Cool>"
    log = run_log_path(tmp_path, game)
    assert log.parent == tmp_path / "run-logs"
    name = log.name
    assert name.startswith("My Game_ _Cool_") or "_" in name
    assert name.endswith(".log")
    # 同一目录恒定映射同一文件
    assert run_log_path(tmp_path, game) == log


def test_path_distinguishes_same_name_dirs(tmp_path):
    a = tmp_path / "games" / "Hentai" / "SameName"
    b = tmp_path / "other" / "SameName"
    assert run_log_path(tmp_path, a) != run_log_path(tmp_path, b)


def test_path_windows_unsafe_chars_stripped(tmp_path):
    game = tmp_path / 'bad|name?"<>'
    log = run_log_path(tmp_path, game)
    for ch in '\\/:*?"<>|':
        assert ch not in log.name


# ── 2. 追加与编码 ──

def test_append_across_sessions_no_bom(tmp_path):
    game = tmp_path / "gameA"
    log1 = get_run_log(tmp_path, game)
    log1.begin_session("第一轮")
    log1.event("translate", "hello")
    log1.close()
    # 模拟新会话（缓存已清）：重开继续追加
    log2 = RunLog(run_log_path(tmp_path, game))
    log2.begin_session("第二轮")
    log2.event("translate", "world")
    log2.close()
    raw = run_log_path(tmp_path, game).read_bytes()
    # utf-8 追加模式不能叠 BOM：文件里至多一个 BOM 且我们根本不写
    assert raw.count(b"\xef\xbb\xbf") == 0
    text = raw.decode("utf-8")
    assert "第一轮" in text and "第二轮" in text
    assert "hello" in text and "world" in text


def test_multiline_event_indented(tmp_path):
    log = RunLog(tmp_path / "run-logs" / "x.log")
    log.event("writeback", "拒绝条目 2 条：\na: too long\nb: broken")
    log.close()
    text = log.path.read_text(encoding="utf-8")
    lines = text.strip().splitlines()
    assert len(lines) == 3
    assert "[writeback]" in lines[0]
    assert lines[1].startswith("  a: too long")
    assert lines[2].startswith("  b: broken")


def test_event_format_and_flush(tmp_path):
    log = RunLog(tmp_path / "run-logs" / "x.log")
    log.event("translate", "本批完成 5 条", "success")
    log.close()
    line = log.path.read_text(encoding="utf-8").strip()
    assert "[translate][success] 本批完成 5 条" in line
    # 时间戳 HH:MM:SS.mmm
    ts = line.split(" ")[0]
    assert len(ts.split(":")) == 3 and "." in ts


def test_begin_session_separator(tmp_path):
    log = RunLog(tmp_path / "run-logs" / "x.log")
    log.begin_session("开始翻译 · Demo")
    log.close()
    text = log.path.read_text(encoding="utf-8").strip()
    assert text.startswith("════ ") and "开始翻译 · Demo" in text
    assert text.endswith(" ════")


def test_flush_per_event(tmp_path):
    """逐条 flush：不 close 也已在磁盘上（崩溃不丢已记录事件）。"""
    log = RunLog(tmp_path / "run-logs" / "x.log")
    log.event("translate", "crash-proof")
    text = log.path.read_text(encoding="utf-8")
    assert "crash-proof" in text
    log.close()


# ── 3. 永不抛错 ──

def test_io_failure_disables_silently(tmp_path):
    # 父路径是一个文件 → mkdir/open 抛 OSError → 静默禁用
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    log = RunLog(blocker / "sub" / "x.log")
    log.begin_session("会话")            # 不抛
    log.event("translate", "内容")       # 不抛
    assert log._disabled
    log.close()


def test_get_run_log_shares_instance(tmp_path):
    game = tmp_path / "gameB"
    a = get_run_log(tmp_path, game)
    b = get_run_log(tmp_path, game)
    assert a is b
    # 不同游戏不同实例
    c = get_run_log(tmp_path, tmp_path / "gameC")
    assert c is not a


def test_thread_safety(tmp_path):
    log = RunLog(tmp_path / "run-logs" / "x.log")
    errors = []

    def writer(n):
        try:
            for i in range(50):
                log.event("translate", f"thread-{n}-{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.close()
    assert not errors
    lines = log.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 8 * 50


def test_close_all_run_logs_idempotent():
    close_all_run_logs()
    close_all_run_logs()


# ── 4. UI 漏斗接线 ──

def test_translate_page_log_line_persists(qapp, tmp_path):
    """_log_line = log_view.appendPlainText 的全量替身：界面行必须落盘。"""
    from hanhua.ui.pages.translate_page import TranslatePage
    from hanhua.ui.app_state import AppState
    from hanhua.core.settings import SettingsStore

    settings = SettingsStore(tmp_path / "settings.json")
    settings.load()
    state = AppState(tmp_path, settings, resource_dir=tmp_path)

    class _FakeProject:
        game_dir = tmp_path / "DemoGame"

    state.project = _FakeProject()
    # 构造期 _on_project → _refresh_chips 会触 project.store——
    # 换一个跳过统计的子类（本测试只验证日志漏斗）
    from hanhua.ui.pages.translate_page import TranslatePage as _TP

    class _PageStub(_TP):
        def _refresh_chips(self):
            return

    page = _PageStub(state, window=None)
    page.show()
    try:
        page._log_line("测试运行记录行")
        page._feed_event("success", "测试处理流事件")
        page._runlog("writeback", "拒绝明细", "warning")
        log_file = run_log_path(tmp_path, tmp_path / "DemoGame")
        text = log_file.read_text(encoding="utf-8")
        assert "测试运行记录行" in text
        assert "[stream][success] 测试处理流事件" in text
        assert "[writeback][warning] 拒绝明细" in text
        # 界面视图同步可见
        assert "测试运行记录行" in page.log_view.toPlainText()
    finally:
        state.project = None
        page.deleteLater()


def test_translate_page_runlog_without_project(qapp, tmp_path):
    """project=None 时 _runlog 静默短路（不抛错不落盘）。"""
    from hanhua.ui.pages.translate_page import TranslatePage
    from hanhua.ui.app_state import AppState
    from hanhua.core.settings import SettingsStore

    settings = SettingsStore(tmp_path / "settings.json")
    settings.load()
    state = AppState(tmp_path, settings, resource_dir=tmp_path)
    state.project = None
    page = TranslatePage(state, window=None)
    page.show()
    page._runlog("translate", "无项目时不应落盘")   # 不抛即过
    page._runlog_begin("无项目")                    # 不抛即过
    assert not (tmp_path / "run-logs").exists() or \
        not list((tmp_path / "run-logs").glob("*.log"))


def test_home_page_scan_event_persists(qapp, tmp_path):
    """扫描事件经 _runlog_event 落盘；_scan_game_dir=None 时短路。"""
    from hanhua.ui.pages.home_page import HomePage
    from hanhua.ui.app_state import AppState
    from hanhua.core.settings import SettingsStore

    settings = SettingsStore(tmp_path / "settings.json")
    settings.load()
    state = AppState(tmp_path, settings, resource_dir=tmp_path)
    page = HomePage(state, window=None)
    page.show()
    # 未在扫描期：静默短路
    assert page._scan_game_dir is None
    page._runlog_event("scan", "扫描期外不应落盘")
    # 扫描期（open_dir 设置 _scan_game_dir）：事件落盘
    page._scan_game_dir = tmp_path / "ScannedGame"
    page._runlog_event("scan", "detection: running", "running")
    page._scan_game_dir = None
    log_file = run_log_path(tmp_path, tmp_path / "ScannedGame")
    text = log_file.read_text(encoding="utf-8")
    assert "[scan][running] detection: running" in text
    assert "扫描期外不应落盘" not in text
