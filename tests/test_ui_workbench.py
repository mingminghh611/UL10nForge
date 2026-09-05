from __future__ import annotations

import os
import json
from pathlib import Path
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import QShowEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from hanhua.core.memory import ProjectStore
from hanhua.core.memory_lifecycle import (
    MemoryCleanupFailure,
    MemoryCleanupSummary,
)
from hanhua.core.models import FontConfig, TranslateStats
from hanhua.core.project import WritebackStage
from hanhua.core.settings import SettingsStore
from hanhua.ui.app_state import AppState
from hanhua.ui.design_system import TOKENS
from hanhua.ui.icons import LineIcon
from hanhua.ui.main_window import MainWindow
from hanhua.ui.pages.home_page import HomePage, _DirectoryDropZone
from hanhua.ui.pages.review_page import EntryFilterProxy, EntryTableModel, ReviewPage
from hanhua.ui.pages.translate_page import TranslatePage
from conftest import await_reload


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _Window:
    def navigate(self, _page):
        pass

    def updateProjectCard(self, _project):
        pass


class _RecordingWindow(_Window):
    def __init__(self):
        self.pages = []

    def navigate(self, page):
        self.pages.append(page)


def _state(tmp_path: Path) -> AppState:
    settings = SettingsStore(tmp_path / "settings.json")
    settings.load()
    return AppState(tmp_path, settings)


def test_aurora_tokens_have_distinct_semantic_accents():
    from hanhua.ui.design_system import TOKENS
    accents = {TOKENS.accent, TOKENS.info, TOKENS.ai,
               TOKENS.warning, TOKENS.error}
    assert len(accents) == 5
    assert TOKENS.page_enter_ms <= 220
    assert TOKENS.control_height >= 36


def test_activity_feed_caps_visible_rows(qapp):
    from hanhua.ui.widgets import ActivityFeed
    feed = ActivityFeed(max_items=3)
    for index in range(5):
        feed.append_event("success", f"原文 {index}", f"译文 {index}")
    assert feed.count() == 3
    assert "原文 4" in feed.latest_text()


def test_filter_chip_is_checkable_and_accessible(qapp):
    from hanhua.ui.widgets import FilterChip
    chip = FilterChip("高风险", value="high")
    assert chip.isCheckable()
    assert chip.accessibleName() == "筛选：高风险"


def test_safety_bar_set_ready_drives_button_and_reason(qapp):
    from PySide6.QtWidgets import QPushButton
    from hanhua.ui.widgets import SafetyBar
    bar = SafetyBar(QPushButton("写回游戏"))
    bar.set_ready(False, "仍有 4 条翻译失败")
    assert not bar.button.isEnabled()
    assert "4 条" in bar.reason_label.text()
    assert bar.property("status") == "blocked"
    bar.set_ready(True, "全部通过质量门，可以安全写回")
    assert bar.button.isEnabled()
    assert bar.property("status") == "ready"


def test_home_is_accessible_five_stage_workbench_without_emoji(qapp, tmp_path):
    page = HomePage(_state(tmp_path), _Window())

    assert TOKENS.control_height >= 44
    assert isinstance(page.dz_icon, LineIcon)
    assert [card.step_id for card in page.pipeline_cards] == [
        "scan", "translation", "review", "writeback", "verify",
    ]
    assert page.pick_btn.minimumHeight() >= 44
    assert page.pick_btn.accessibleName() == "选择 Unity 游戏文件夹"
    assert hasattr(page, "runtime_value")
    assert hasattr(page, "tool_value")
    assert not hasattr(page, "cache_value")         # 缓存/置信度并入流水线卡片
    assert not hasattr(page, "confidence_value")
    visible_text = " ".join(widget.text() for widget in (
        page.findChildren(QLabel) + page.findChildren(QPushButton)
    ))
    assert "📁" not in visible_text


def test_home_project_mode_hides_large_drop_zone(qapp, tmp_path):
    page = HomePage(_state(tmp_path), _Window())
    page.state.project = type("Project", (), {"store": _StoreRows([])})()
    page._refresh_dashboard()
    assert page.project_hero.isVisibleTo(page)
    assert not page.welcome_panel.isVisibleTo(page)


def test_reduced_motion_disables_looping_animations(qapp, monkeypatch):
    monkeypatch.setenv("HANHUA_REDUCED_MOTION", "1")
    from hanhua.ui.design_system import motion_enabled
    assert motion_enabled() is False


def test_translate_log_always_visible_no_toggle(qapp, tmp_path):
    """2026-08-22 收起日志按钮已删（Splitter 可拖），日志恒可见。"""
    page = TranslatePage(_state(tmp_path), _Window())
    assert page.log_view.isVisibleTo(page)
    assert not hasattr(page, "log_toggle")


def test_write_safety_bar_explains_disabled_state(qapp, tmp_path):
    page = TranslatePage(_state(tmp_path), _Window())
    page.write_safety.set_ready(False, "仍有 4 条翻译失败")
    assert not page.write_btn.isEnabled()
    assert "4 条" in page.write_safety.reason_label.text()


def test_review_filter_chip_updates_proxy(qapp, tmp_path):
    page = ReviewPage(_state(tmp_path), _Window())
    page.model.setEntries([
        {"original": "Save", "translation": "", "status": "pending",
         "file_id": "f", "key_path": "a", "locked": False, "meta": {}},
        {"original": "Load", "translation": "载入", "status": "translated",
         "file_id": "f", "key_path": "b", "locked": False, "meta": {}},
    ])
    page.filter_chips["pending"].click()
    assert page.proxy.rowCount() == 1


def test_review_pending_chip_excludes_low_archive(qapp, tmp_path):
    """#8：跳过/留档文本不进「待翻译」——low 置信度留档（引擎消息/
    噪音，不可自动翻译）在待翻译胶囊与 summary 计数中都不显示，
    与翻译页 chips 同源口径（is_actionable_translation）。"""
    rows = [
        {"original": "Hello", "translation": "", "status": "pending",
         "file_id": "f", "key_path": "a", "locked": False,
         "meta": json.dumps({"confidence": "high"})},
        {"original": "Address already in use", "translation": "",
         "status": "pending", "file_id": "f", "key_path": "b",
         "locked": False,
         "meta": json.dumps({"confidence": "low",
                             "reason": "il2cpp_sentence"})},
        {"original": "Locked", "translation": "", "status": "pending",
         "file_id": "f", "key_path": "c", "locked": True, "meta": {}},
    ]
    page = ReviewPage(_state(tmp_path), _Window())
    page.model.setEntries(rows)
    page.filter_chips["pending"].click()
    assert page.proxy.rowCount() == 1, "待翻译胶囊只显示可翻译条目"
    page._refresh_summary()
    assert "待翻译 1" in page.summary_label.text()


def test_review_save_has_inline_feedback(qapp, tmp_path):
    page = ReviewPage(_state(tmp_path), _Window())
    assert page.save_feedback.text() == ""


def test_translate_page_exposes_actionable_stats_and_safe_writeback_state(
        qapp, tmp_path):
    page = TranslatePage(_state(tmp_path), _Window())

    assert hasattr(page, "chip_skipped")
    assert hasattr(page, "quality_reason_label")
    assert not hasattr(page, "metric_speed")        # 实时速度舱已移除（速度在设置页显示）
    assert not hasattr(page, "chip_high")           # 技术性置信/缓存/诊断已移除
    assert not hasattr(page, "scan_diagnostics_btn")
    for button in (page.start_btn, page.stop_btn, page.retry_btn, page.write_btn):
        assert button.minimumHeight() >= 44
        assert button.accessibleName()


def test_translate_progress_uses_actionable_scope_and_collapses_skips(
        qapp, tmp_path):
    rows = [
        {
            "file_id": "code", "key_path": f"skip/{index}",
            "original": f"Method{index}", "translation": "",
            "status": "skipped", "locked": 0,
            "meta": json.dumps({
                "role": "structural", "confidence": "low",
                "quality_reasons": ["structural_text"],
            }),
        }
        for index in range(1700)
    ]
    rows.extend({
        "file_id": "ui", "key_path": f"prompt/{index}",
        "original": f"Open door {index}", "translation": "",
        "status": "pending", "locked": 0,
        "meta": json.dumps({"role": "display", "confidence": "high"}),
    } for index in range(300))
    rows.append({
        "file_id": "ui", "key_path": "history/settings",
        "original": "Settings", "translation": "设置",
        "status": "translated", "locked": 0,
        "meta": json.dumps({"role": "display", "confidence": "high"}),
    })
    state = _state(tmp_path)
    state.project = type("Project", (), {"store": _StoreRows(rows)})()
    page = TranslatePage(state, _Window())

    page._refresh_chips()
    await_reload(page)

    # #2：未开始翻译时进度切全量口径 = 可处理总数（actionable 待翻译 +
    # 已翻译），与 chips 同源：1 已翻译 / 301 可处理（1700 跳过与
    # low 留档不计入）。
    assert page.progress_label.text() == "1 / 301 条"
    assert page.progress_bar.value() == 0
    assert page.chip_done.text() == "已翻译 1"
    assert page.chip_skipped.isHidden()

    page._on_progress(TranslateStats(total=300, done=300))

    assert page.progress_label.text() == "300 / 300 条"
    # 2026-08-20 全链路 3-3-3-1 进度条：翻译段映射到 0-30% 段，
    # 300/300 → ratio 1.0 → 30（不再占满 0-100 整根条）
    assert page.progress_bar.value() == 30


def test_translation_start_log_uses_same_zero_actionable_scope(
        qapp, tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "project.db")
    store.init_schema()
    store.add_file("ui", "ui.assets", "v2_asset", "binary", "")
    store.upsert_entries([
        {
            "file_id": "ui", "key_path": "code/awake", "original": "Awake",
            "meta": {"role": "structural", "confidence": "high"},
        },
        {
            "file_id": "ui", "key_path": "uncertain/open",
            "original": "Open", "meta": {
                "role": "display", "confidence": "low",
            },
        },
    ])
    state = _state(tmp_path)
    state.api.mode = "api"
    state.api.base_url = "https://example.invalid/v1/chat/completions"
    state.api.api_key = "test-key"
    state.api.model = "test-model"
    state.project = type("Project", (), {"store": store, "profile": None})()
    page = TranslatePage(state, _Window())
    queued = []
    page._pool = type(
        "Pool", (), {"start": lambda _self, worker: queued.append(worker)})()

    class FakeGlossary:
        def __init__(self, _path):
            pass

        def init_schema(self):
            pass

        def list_all(self):
            return []

        def format_for_prompt(self):
            return ""

        def known_names_for(self, _collected=None):
            return []

        def learn_proper_names(self, *_args, **_kwargs):
            return 0

        def close(self):
            pass

    client = type("Client", (), {
        "url": "https://example.invalid/v1/chat/completions",
        "chat": lambda _self, *_args: pytest.fail(
            "zero actionable scope reached provider"),
    })()
    monkeypatch.setattr(
        "hanhua.ui.pages.translate_page.GlossaryStore", FakeGlossary)
    monkeypatch.setattr(
        "hanhua.ui.pages.translate_page.create_client", lambda _api: client)

    page.start()
    logs = []
    signals = type("Signals", (), {
        "progress": type("Signal", (), {"emit": lambda _self, _value: None})(),
        "log": type("Signal", (), {"emit": lambda _self, value: logs.append(value)})(),
        # 2026-08-14 新增：审核完成/失败都经 review_summary 回主线程
        "review_summary": type(
            "Signal", (), {"emit": lambda _self, value: logs.append(value)})(),
    })()
    stats = page._translate_worker(page._active_run, signals)

    assert stats.total == 0
    assert any("待翻译 0 条" in line for line in logs)
    assert any("低置信度" in line for line in logs)   # 留档提示（uncertain/open 1 条）


def test_main_window_statusbar_reports_local_backend(qapp, tmp_path):
    state = _state(tmp_path)
    state.api.mode = "local"
    state.api.local_model_path = str(tmp_path / "models" / "Hy-MT2.gguf")

    window = MainWindow(state)

    status = window.statusBar().currentMessage()
    assert "本地" in status
    assert "Hy-MT2" in status
    assert "未启动" in status


def test_main_window_statusbar_omits_startup_cleanup_details(qapp, tmp_path):
    """状态栏不再展示启动内存清理诊断（开发者信息，普通用户不可操作）。"""
    settings = SettingsStore(tmp_path / "settings.json")
    settings.load()
    state = AppState(
        tmp_path,
        settings,
        memory_cleanup=MemoryCleanupSummary(
            discovered_databases=2,
            cleared_databases=2,
            cleared_entries=2,
            cleared_memory=1,
        ),
    )

    window = MainWindow(state)
    status = window.statusBar().currentMessage()

    assert "清理" not in status
    assert "翻译记忆" not in status


def test_review_page_filters_and_explains_recognition_evidence(qapp, tmp_path):
    state = _state(tmp_path)
    page = ReviewPage(state, _Window())

    assert EntryTableModel.COLS == [
        "状态", "来源", "原文", "译文", "失败原因", "风险", "锁定",
    ]
    for control in (page.search_box, page.translate_btn):
        assert control.minimumHeight() >= 44
        assert control.accessibleName()

    rows = [
        {
            "file_id": "ui.assets", "key_path": "obj/1", "original": "Continue",
            "translation": "继续", "status": "translated", "locked": 0,
            "meta": json.dumps({
                "confidence": "high", "role": "display", "quality_reasons": [],
            }),
        },
        {
            "file_id": "code.assets", "key_path": "obj/2", "original": "Awake",
            "translation": "", "status": "skipped", "locked": 0,
            "meta": json.dumps({
                "confidence": "low", "role": "structural",
                "quality_reasons": ["structural_text"],
            }),
        },
    ]
    model = EntryTableModel(state)
    model.setEntries(rows)
    proxy = EntryFilterProxy()
    proxy.setSourceModel(model)
    proxy.setFilters(status="skipped")

    assert proxy.rowCount() == 1
    assert proxy.index(0, 1).data() == "code.assets"
    assert proxy.index(0, 4).data() == "structural_text"


def test_review_risk_column_and_filter(qapp, tmp_path):
    """#43 阶段 G：风险列显示 risk_score/risk_level；#47 合并后风险标记
    不单独成筛选——只有未收敛终态/机械失败进「待审核」（全量送审后
    终态才是真相，risk 列仍逐行透出）。"""
    state = _state(tmp_path)
    rows = [
        {"file_id": "f", "key_path": "k1", "original": "Resume",
         "translation": "简历", "status": "translated", "locked": 0,
         "meta": json.dumps({"risk_score": 65, "risk_level": "HIGH"})},
        {"file_id": "f", "key_path": "k2", "original": "Hello",
         "translation": "你好", "status": "translated", "locked": 0,
         "meta": json.dumps({})},
    ]
    model = EntryTableModel(state)
    model.setEntries(rows)
    proxy = EntryFilterProxy()
    proxy.setSourceModel(model)
    # 风险列：有字段 → 「65 HIGH」；无字段 → —
    assert model.index(0, 5).data() == "65 HIGH"
    assert model.index(1, 5).data() == "—"
    # #47：「待审核」只看终态——仅带风险标记（无未收敛终态）不进
    proxy.setFilters(status="needs_review")
    assert proxy.rowCount() == 0


def test_review_context_menu_uses_table_coordinate_lookup(qapp, tmp_path):
    page = ReviewPage(_state(tmp_path), _Window())
    page._show_menu(QPoint(0, 0))


def test_review_table_can_unlock_a_checked_entry(qapp, tmp_path):
    recorded = []
    store = type("Store", (), {
        "set_locked": lambda _self, file_id, key_path, locked: recorded.append(
            (file_id, key_path, locked)),
    })()
    state = _state(tmp_path)
    state.project = type("Project", (), {"store": store})()
    model = EntryTableModel(state)
    row = {
        "file_id": "ui.assets", "key_path": "obj/1", "original": "Continue",
        "translation": "继续", "status": "translated", "locked": 1,
        "meta": "{}",
    }
    model.setEntries([row])

    assert model.setData(
        model.index(0, 6), Qt.Unchecked, Qt.CheckStateRole) is True  # 锁定列
    assert recorded == [("ui.assets", "obj/1", False)]
    assert row["locked"] is False


def test_review_clearing_manual_translation_syncs_pending_quality_state(
        qapp, tmp_path):
    store = ProjectStore(tmp_path / "project.db")
    store.init_schema()
    store.add_file("ui.assets", "ui.assets", "v2_asset", "binary", "")
    store.upsert_entries([{
        "file_id": "ui.assets", "key_path": "obj/1",
        "original": "Continue", "meta": {"confidence": "high"},
    }])
    store.set_manual("ui.assets", "obj/1", "继续")
    state = _state(tmp_path)
    state.project = type("Project", (), {"store": store})()
    model = EntryTableModel(state)
    row = store.get_entries()[0]
    model.setEntries([row])

    assert model.setData(model.index(0, 3), "", Qt.EditRole) is True

    persisted = store.get_entries()[0]
    assert persisted["translation"] == ""
    assert persisted["status"] == "pending"
    assert json.loads(persisted["meta"])["quality_passed"] is False
    assert row["status"] == persisted["status"]
    assert row["meta"] == persisted["meta"]


def test_review_page_auto_reloads_on_construction_and_project_opened(
        qapp, tmp_path):
    """构造时自动 reload；projectOpened 信号触发 reload。
    回归：信号连接曾被误放进 _focus_search()，打开项目后表格空白。"""
    store = ProjectStore(tmp_path / "project.db")
    store.init_schema()
    store.add_file("ui.assets", "ui.assets", "v2_asset", "binary", "")
    store.upsert_entries([{
        "file_id": "ui.assets", "key_path": "obj/1",
        "original": "Continue", "meta": {"confidence": "high"},
    }])
    state = _state(tmp_path)
    page = ReviewPage(state, _Window())

    # 无项目时：构造即 reload，summary 显示 0 条，筛选未卡死
    assert "共 0 条" in page.summary_label.text()
    assert page._loading is False

    # 打开项目：projectOpened 信号自动触发 reload
    state.project = type("Project", (), {"store": store})()
    state.projectOpened.emit(state.project)
    await_reload(page)
    assert page.model.rowCount() == 1
    assert "共 1 条" in page.summary_label.text()

    # 条目变化信号同样触发刷新
    state.entriesChanged.emit()
    await_reload(page)
    assert page.model.rowCount() == 1


def test_review_page_suspends_broadcast_reload_while_translating(
        qapp, tmp_path):
    """2026-08-14 卡顿优化：翻译进行中（state.translation_running）广播
    重载挂起——万级行全量重建在 1s 广播频率下持续卡主线程；翻译结束
    广播自然补跑（translate 页先复位标志再 emit）。"""
    store = ProjectStore(tmp_path / "project.db")
    store.init_schema()
    store.add_file("ui.assets", "ui.assets", "v2_asset", "binary", "")
    store.upsert_entries([{
        "file_id": "ui.assets", "key_path": "obj/1",
        "original": "Continue", "meta": {"confidence": "high"},
    }])
    state = _state(tmp_path)
    state.project = type("Project", (), {"store": store})()
    page = ReviewPage(state, _Window())
    await_reload(page)
    assert page.model.rowCount() == 1

    # 翻译进行中：广播不触发重建（页面未显示 → 挂起优先于可见性分支）
    state.translation_running = True
    state.entriesChanged.emit()
    QTest.qWait(50)
    assert page._pending_reload is True
    assert page.model.rowCount() == 1

    # 翻译结束（页面不可见）：广播置脏；切回页面补跑
    state.translation_running = False
    state.entriesChanged.emit()
    QTest.qWait(50)
    assert page._reload_dirty is True
    page.showEvent(QShowEvent())
    await_reload(page)
    assert page._pending_reload is False
    assert page.model.rowCount() == 1


def test_review_page_defers_broadcast_reload_when_hidden(qapp, tmp_path):
    """页面不可见时广播不重建（隐藏页全量重建纯浪费）；切回页面补跑。"""
    store = ProjectStore(tmp_path / "project.db")
    store.init_schema()
    store.add_file("ui.assets", "ui.assets", "v2_asset", "binary", "")
    store.upsert_entries([{
        "file_id": "ui.assets", "key_path": "obj/1",
        "original": "Continue", "meta": {"confidence": "high"},
    }])
    state = _state(tmp_path)
    state.project = type("Project", (), {"store": store})()
    page = ReviewPage(state, _Window())
    await_reload(page)
    assert page.model.rowCount() == 1

    # 库新增条目后广播——页面未显示 → 不重建、置脏
    store.upsert_entries([{
        "file_id": "ui.assets", "key_path": "obj/2",
        "original": "Quit", "meta": {"confidence": "high"},
    }])
    state.entriesChanged.emit()
    QTest.qWait(50)
    assert page._reload_dirty is True
    assert page.model.rowCount() == 1          # 旧数据未重建

    # showEvent（切回页面）→ 补跑，拿到新条目
    page.showEvent(QShowEvent())
    await_reload(page)
    assert page.model.rowCount() == 2
    assert page._reload_dirty is False


def test_review_page_broadcast_reload_refreshes_when_visible(qapp, tmp_path):
    """页面可见 + 非翻译中：广播照常重建（回归：挂起守卫不放行时也
    不能吞掉正常刷新）。"""
    store = ProjectStore(tmp_path / "project.db")
    store.init_schema()
    store.add_file("ui.assets", "ui.assets", "v2_asset", "binary", "")
    store.upsert_entries([{
        "file_id": "ui.assets", "key_path": "obj/1",
        "original": "Continue", "meta": {"confidence": "high"},
    }])
    state = _state(tmp_path)
    state.project = type("Project", (), {"store": store})()
    page = ReviewPage(state, _Window())
    page.show()
    QTest.qWait(30)
    await_reload(page)
    assert page.isVisible()
    assert page.model.rowCount() == 1

    store.upsert_entries([{
        "file_id": "ui.assets", "key_path": "obj/2",
        "original": "Quit", "meta": {"confidence": "high"},
    }])
    state.entriesChanged.emit()
    await_reload(page)
    assert page.model.rowCount() == 2
    assert page._reload_dirty is False


def test_translate_page_toggles_translation_running_flag(qapp, tmp_path):
    """2026-08-14 卡顿优化：翻译页维护 AppState.translation_running——
    _on_finished 先复位再广播（审校页挂起补跑）；_on_error 复位并广播
    （错误后审校页可见失败状态，不再停在翻译前快照）。"""
    state = _state(tmp_path)
    page = TranslatePage(state, _Window())
    assert state.translation_running is False

    # 结束路径：先复位标志，再广播
    state.translation_running = True
    page._on_finished(TranslateStats(total=2, done=2))
    assert state.translation_running is False

    # 出错路径：复位 + 广播（审校页挂起的 reload 由此补跑）
    state.translation_running = True
    page._on_error("boom")
    assert state.translation_running is False


def test_home_enters_review_when_scan_is_unblocked_but_not_complete(
        qapp, tmp_path, monkeypatch):
    state = _state(tmp_path)
    window = _RecordingWindow()
    page = HomePage(state, window)
    monkeypatch.setattr(page, "_render_report", lambda _report: None)
    monkeypatch.setattr(page, "_refresh_profile_card", lambda: None)
    monkeypatch.setattr(
        "hanhua.ui.pages.home_page.Toast.show", lambda *args, **kwargs: None)
    report = type("Report", (), {
        "text_files": 1, "v2_files": 2,
        "unblocked": True, "completable": False,
    })()
    project = object()

    page._on_scan_done((project, report))

    assert window.pages == ["review"]


class _StoreRows:
    def __init__(self, rows):
        self.rows = rows

    def get_entries(self, status=None):
        if status is None:
            return self.rows
        return [row for row in self.rows if row["status"] == status]

    def count(self, status):
        return sum(row["status"] == status for row in self.rows)


def test_drop_zone_accepts_only_one_local_directory(qapp, tmp_path):
    directory = tmp_path / "game"
    directory.mkdir()
    file_path = tmp_path / "game.exe"
    file_path.write_bytes(b"MZ")
    zone = _DirectoryDropZone()

    valid = QMimeData()
    valid.setUrls([QUrl.fromLocalFile(str(directory))])
    file_drop = QMimeData()
    file_drop.setUrls([QUrl.fromLocalFile(str(file_path))])
    multiple = QMimeData()
    multiple.setUrls([
        QUrl.fromLocalFile(str(directory)),
        QUrl.fromLocalFile(str(tmp_path)),
    ])
    remote = QMimeData()
    remote.setUrls([QUrl("https://example.invalid/game")])

    assert zone.local_directory(valid) == directory.resolve()
    assert zone.local_directory(file_drop) is None
    assert zone.local_directory(multiple) is None
    assert zone.local_directory(remote) is None


def test_switch_project_closes_previous_store_and_advances_generation(
        qapp, tmp_path):
    state = _state(tmp_path)
    closed = []
    first = type("Project", (), {
        "store": type("Store", (), {
            "close": lambda _self: closed.append("first"),
        })(),
    })()
    second = type("Project", (), {
        "store": type("Store", (), {
            "close": lambda _self: closed.append("second"),
        })(),
    })()

    first_generation = state.switch_project(first)
    second_generation = state.switch_project(second)

    assert first_generation == 1
    assert second_generation == 2
    assert closed == ["first"]
    assert state.is_current_project(second, second_generation)
    assert not state.is_current_project(first, first_generation)


def test_active_project_lease_defers_store_close_until_worker_exits(
        qapp, tmp_path):
    state = _state(tmp_path)
    closed = []
    first = type("Project", (), {
        "store": type("Store", (), {
            "close": lambda _self: closed.append("first"),
        })(),
    })()
    second = type("Project", (), {
        "store": type("Store", (), {"close": lambda _self: None})(),
    })()
    generation = state.switch_project(first)

    with state.project_lease(first, generation) as acquired:
        assert acquired is True
        state.switch_project(second)
        assert closed == []

    assert closed == ["first"]


def test_app_close_defers_active_project_store_until_worker_exits(
        qapp, tmp_path, monkeypatch):
    state = _state(tmp_path)
    closed = []
    project = type("Project", (), {
        "store": type("Store", (), {
            "close": lambda _self: closed.append("project"),
        })(),
    })()
    generation = state.switch_project(project)
    monkeypatch.setattr(state.local_model, "stop", lambda: None)

    with state.project_lease(project, generation) as acquired:
        assert acquired is True
        state.close()
        assert closed == []

    assert closed == ["project"]


def test_reactivated_leased_project_is_not_closed_when_old_lease_exits(
        qapp, tmp_path, monkeypatch):
    state = _state(tmp_path)
    closed = []
    first = type("Project", (), {
        "store": type("Store", (), {
            "close": lambda _self: closed.append("first"),
        })(),
    })()
    second = type("Project", (), {
        "store": type("Store", (), {"close": lambda _self: None})(),
    })()
    generation = state.switch_project(first)
    monkeypatch.setattr(state.local_model, "stop", lambda: None)

    with state.project_lease(first, generation) as acquired:
        assert acquired is True
        state.switch_project(second)
        state.switch_project(first)

    assert closed == []
    state.close()
    assert closed == ["first"]


def test_stale_queued_write_never_targets_new_project_or_calls_back(
        qapp, tmp_path, monkeypatch):
    row = {
        "file_id": "ui.assets", "key_path": "obj/1", "original": "Continue",
        "translation": "继续", "status": "translated", "locked": 0,
        "meta": json.dumps({"quality_passed": True, "confidence": "high"}),
    }

    class Store(_StoreRows):
        def __init__(self):
            super().__init__([dict(row)])
            self.closed = False

        def close(self):
            self.closed = True

    calls = []

    class Project:
        def __init__(self, name):
            self.name = name
            self.store = Store()
            self.out_dir = tmp_path / f"{name}_汉化"

        def write_all(self, *, font_config=None, allow_partial=False,
                      allow_unverified_font_candidate=False):
            calls.append((self.name, font_config))
            return {"text_files": 1}

    state = _state(tmp_path)
    first = Project("A")
    second = Project("B")
    state.switch_project(first)
    state.analysis_report = _report(unblocked=True)
    page = TranslatePage(state, _Window())
    queued = []
    page._pool = type(
        "Pool", (), {"start": lambda _self, worker: queued.append(worker)})()
    callbacks = []
    monkeypatch.setattr(page, "_on_written", lambda result: callbacks.append(result))
    monkeypatch.setattr(
        "hanhua.ui.pages.translate_page.Toast.show", lambda *args, **kwargs: None)

    page.write_back()
    assert len(queued) == 1
    state.switch_project(second)
    queued[0].run()

    assert first.store.closed is True
    assert calls == []
    assert callbacks == []


def test_writeback_stage_progress_and_duplicate_run_guard(
        qapp, tmp_path, monkeypatch):
    row = {
        "file_id": "ui.assets", "key_path": "obj/1", "original": "Continue",
        "translation": "继续", "status": "translated", "locked": 0,
        "meta": json.dumps({"quality_passed": True, "confidence": "high"}),
    }

    class Project:
        def __init__(self):
            self.store = _StoreRows([row])
            self.out_dir = tmp_path / "game_汉化"

        def write_all(self, *, font_config=None, stage_cb=None,
                allow_partial=False, allow_unverified_font_candidate=False):
            assert font_config is not None
            for phase, message in (
                    ("copying", "正在复制原游戏"),
                    ("verifying", "正在重开并验证汉化输出"),
                    ("published", "汉化游戏已发布")):
                stage_cb(WritebackStage(phase, message))
            report = _report(unblocked=True, completable=True)
            return {
                "text_files": 1,
                "verification": {
                    "input_protected": True,
                    "reopen_verified": True,
                    "changed_files": 1,
                    "written_translations": 1,
                    "font_level": "disabled",
                },
                "analysis_report": report,
            }

    state = _state(tmp_path)
    state.switch_project(Project())
    state.analysis_report = _report(unblocked=True)
    page = TranslatePage(state, _Window())
    queued = []
    page._pool = type(
        "Pool", (), {"start": lambda _self, worker: queued.append(worker)})()
    monkeypatch.setattr(
        "hanhua.ui.pages.translate_page.Toast.show", lambda *args, **kwargs: None)

    page.write_back()
    page.write_back()

    assert len(queued) == 1
    assert page.write_btn.isEnabled() is False
    queued[0].run()
    qapp.processEvents()
    await_reload(page)                          # 写回完成后 chips 刷新

    log = page.log_view.toPlainText()
    assert "正在复制原游戏" in log
    assert "正在重开并验证汉化输出" in log
    assert "汉化游戏已发布" in log
    assert page.write_btn.isEnabled() is True


def test_project_switch_cannot_clear_active_write_guard(
        qapp, tmp_path, monkeypatch):
    row = {
        "file_id": "ui.assets", "key_path": "obj/1", "original": "Quit",
        "translation": "退出", "status": "translated", "locked": 0,
        "meta": json.dumps({"quality_passed": True, "confidence": "high"}),
    }

    class Store(_StoreRows):
        def close(self):
            pass

    class Project:
        def __init__(self, name):
            self.store = Store([row])
            self.out_dir = tmp_path / f"{name}_汉化"

        def write_all(self, *, font_config=None, stage_cb=None,
                allow_partial=False, allow_unverified_font_candidate=False):
            return {"text_files": 1}

    first = Project("first")
    second = Project("second")
    state = _state(tmp_path)
    state.switch_project(first)
    state.analysis_report = _report(unblocked=True)
    page = TranslatePage(state, _Window())
    queued = []
    page._pool = type(
        "Pool", (), {"start": lambda _self, worker: queued.append(worker)})()
    monkeypatch.setattr(
        "hanhua.ui.pages.translate_page.Toast.show", lambda *args, **kwargs: None)

    page.write_back()
    state.switch_project(second)
    state.analysis_report = _report(unblocked=True)
    state.switch_project(first)
    state.analysis_report = _report(unblocked=True)
    page.write_back()

    assert len(queued) == 1
    assert page._write_running is True


def test_writeback_error_remains_visible_after_worker_drain(
        qapp, tmp_path, monkeypatch):
    row = {
        "file_id": "ui.assets", "key_path": "obj/1", "original": "Quit",
        "translation": "退出", "status": "translated", "locked": 0,
        "meta": json.dumps({"quality_passed": True, "confidence": "high"}),
    }
    project = type("Project", (), {
        "store": _StoreRows([row]),
        "out_dir": tmp_path / "failed_汉化",
        "write_all": lambda _self, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("disk unavailable")),
    })()
    state = _state(tmp_path)
    state.switch_project(project)
    state.analysis_report = _report(unblocked=True)
    page = TranslatePage(state, _Window())
    queued = []
    page._pool = type(
        "Pool", (), {"start": lambda _self, worker: queued.append(worker)})()
    monkeypatch.setattr(
        "hanhua.ui.pages.translate_page.Toast.show", lambda *args, **kwargs: None)

    page.write_back()
    queued[0].run()
    qapp.processEvents()
    await_reload(page)                          # 写回失败后 chips 刷新

    assert "写回失败：disk unavailable" in page.log_view.toPlainText()
    assert page.progress_label.text() == "写回失败：disk unavailable"
    assert page.write_btn.isEnabled() is True


def test_active_write_holds_project_lease_across_switch(
        qapp, tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class Store(_StoreRows):
        def __init__(self):
            super().__init__([])
            self.closed = False

        def close(self):
            self.closed = True

    class Project:
        def __init__(self):
            self.store = Store()

        def write_all(self, *, font_config=None, allow_partial=False,
                      allow_unverified_font_candidate=False):
            assert self.store.closed is False
            entered.set()
            assert release.wait(timeout=2)
            assert self.store.closed is False
            return {"text_files": 1}

    first = Project()
    second = Project()
    state = _state(tmp_path)
    generation = state.switch_project(first)
    page = TranslatePage(state, _Window())
    result = []
    worker = threading.Thread(
        target=lambda: result.append(page._write_worker(
            first, generation, FontConfig())))

    worker.start()
    assert entered.wait(timeout=2)
    state.switch_project(second)
    assert first.store.closed is False
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result == [{"text_files": 1}]
    assert first.store.closed is True


def test_write_task_captures_font_settings_at_queue_time(
        qapp, tmp_path, monkeypatch):
    row = {
        "file_id": "ui.assets", "key_path": "obj/1", "original": "Continue",
        "translation": "继续", "status": "translated", "locked": 0,
        "meta": json.dumps({"quality_passed": True, "confidence": "high"}),
    }
    captured = []
    store = _StoreRows([row])
    project = type("Project", (), {
        "store": store,
        "out_dir": tmp_path / "game_汉化",
        "write_all": lambda _self, *, font_config=None, stage_cb=None,
             allow_partial=False, allow_unverified_font_candidate=False:
             captured.append(font_config) or {"text_files": 1},
    })()
    state = _state(tmp_path)
    state.settings.font = FontConfig(
        enabled=False, filename="DingTalk JinBuTi.ttf")
    state.switch_project(project)
    state.analysis_report = _report(unblocked=True)
    page = TranslatePage(state, _Window())
    queued = []
    page._pool = type(
        "Pool", (), {"start": lambda _self, worker: queued.append(worker)})()
    monkeypatch.setattr(page, "_on_written", lambda _result: None)
    monkeypatch.setattr(
        "hanhua.ui.pages.translate_page.Toast.show", lambda *args, **kwargs: None)

    page.write_back()
    state.settings.font.enabled = True
    state.settings.font.filename = "联想小新黑体 常规.ttf"
    queued[0].run()

    assert captured == [FontConfig(
        enabled=False, filename="DingTalk JinBuTi.ttf")]
    assert captured[0] is not state.settings.font


def test_stale_queued_translation_does_not_use_new_project_or_call_back(
        qapp, tmp_path, monkeypatch):
    class Store(_StoreRows):
        def __init__(self):
            super().__init__([])
            self.closed = False

        def close(self):
            self.closed = True

    first = type("Project", (), {
        "store": Store(), "profile": None,
    })()
    second = type("Project", (), {
        "store": Store(), "profile": None,
    })()
    state = _state(tmp_path)
    state.api.mode = "api"
    state.api.base_url = "https://example.invalid/v1/chat/completions"
    state.api.api_key = "test-key"
    state.api.model = "test-model"
    state.switch_project(first)
    page = TranslatePage(state, _Window())
    queued = []
    page._pool = type(
        "Pool", (), {"start": lambda _self, worker: queued.append(worker)})()
    callbacks = []
    monkeypatch.setattr(page, "_on_finished", lambda result: callbacks.append(result))
    monkeypatch.setattr(page, "_on_error", lambda error: callbacks.append(error))
    monkeypatch.setattr(
        "hanhua.ui.pages.translate_page.create_client",
        lambda _api: pytest.fail("stale translation reached the API client"))
    monkeypatch.setattr(
        "hanhua.ui.pages.translate_page.Toast.show", lambda *args, **kwargs: None)

    page.start()
    state.switch_project(second)
    queued[0].run()

    assert first.store.closed is True
    assert callbacks == []


def test_project_switch_drains_old_translator_before_new_run_starts(
        qapp, tmp_path, monkeypatch):
    class Store(_StoreRows):
        def __init__(self):
            super().__init__([])

        def close(self):
            pass

    first = type("Project", (), {"store": Store(), "profile": None})()
    second = type("Project", (), {"store": Store(), "profile": None})()
    state = _state(tmp_path)
    state.api.mode = "api"
    state.api.base_url = "https://example.invalid/v1/chat/completions"
    state.api.api_key = "test-key"
    state.api.model = "test-model"
    state.switch_project(first)
    page = TranslatePage(state, _Window())
    queued = []
    toasts = []
    page._pool = type(
        "Pool", (), {"start": lambda _self, worker: queued.append(worker)})()
    monkeypatch.setattr(
        "hanhua.ui.pages.translate_page.Toast.show",
        lambda _parent, message, kind="info": toasts.append(message))

    page.start()
    old_run = page._active_run
    stopped = []
    old_run.attach_translator(type(
        "Translator", (), {"stop": lambda _self: stopped.append("old")})())

    state.switch_project(second)
    page.start()

    assert old_run.cancel.is_set()
    assert stopped == ["old"]
    assert len(queued) == 1
    assert any("仍在停止" in message for message in toasts)

    page._on_run_drained(old_run)
    page.start()

    assert len(queued) == 2
    assert page._active_run is not old_run


def test_active_translation_holds_project_lease_until_worker_finally(
        qapp, tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    class Store(_StoreRows):
        def __init__(self):
            super().__init__([])
            self.closed = False

        def close(self):
            self.closed = True

    first = type("Project", (), {"store": Store(), "profile": None})()
    second = type("Project", (), {"store": Store(), "profile": None})()
    state = _state(tmp_path)
    state.api.mode = "api"
    state.api.base_url = "https://example.invalid/v1/chat/completions"
    state.api.api_key = "test-key"
    state.api.model = "test-model"
    state.switch_project(first)
    page = TranslatePage(state, _Window())
    queued = []
    page._pool = type(
        "Pool", (), {"start": lambda _self, worker: queued.append(worker)})()
    monkeypatch.setattr(
        page, "_translate_with_lease",
        lambda _run, _signals: (
            entered.set(), release.wait(timeout=2), first.store.closed)[2])

    page.start()
    result = []
    worker = threading.Thread(target=lambda: result.append(queued[0].fn()))
    worker.start()
    assert entered.wait(timeout=2)

    state.switch_project(second)
    assert first.store.closed is False
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result == [False]
    assert first.store.closed is True


def test_refresh_chips_pending_uses_actionable_count(qapp, tmp_path):
    """待翻译计数与翻译引擎同源（is_actionable_translation）：低置信度
    留档（pending/low，IL2CPP 引擎消息）不计入，只计入可翻译条目；
    留档条数进 tooltip 提示（真实案例：526 条引擎消息待翻译永不减少）。"""
    rows = [
        # 可翻译：high 置信度 pending
        {"file_id": "a", "key_path": "k1", "original": "Hello",
         "translation": "", "status": "pending", "locked": 0,
         "meta": json.dumps({"confidence": "high"})},
        # 留档：low 置信度 pending（IL2CPP 引擎消息，不可自动翻译）
        {"file_id": "b", "key_path": "k2",
         "original": "Address already in use",
         "translation": "", "status": "pending", "locked": 0,
         "meta": json.dumps({"confidence": "low",
                             "reason": "il2cpp_sentence"})},
        # 已翻译不计入；失败若置信度合格下次会重试 → 用 low 排除
        {"file_id": "a", "key_path": "k3", "original": "Bye",
         "translation": "再见", "status": "translated", "locked": 0,
         "meta": json.dumps({"confidence": "high"})},
        {"file_id": "a", "key_path": "k4", "original": "Oops",
         "translation": "", "status": "failed", "locked": 0,
         "meta": json.dumps({"confidence": "low"})},
    ]
    state = _state(tmp_path)
    state.project = type("Project", (), {
        "store": _StoreRows(rows), "out_dir": tmp_path / "game_汉化",
    })()
    state.analysis_report = _report()
    page = TranslatePage(state, _Window())
    page._last_stats = None
    page._refresh_chips()
    await_reload(page)
    assert page.chip_pending.text() == "待翻译 1"
    assert "低置信度" in page.chip_pending.toolTip()
    assert "1" in page.chip_pending.toolTip()      # 留档 1 条
    # 进度条与计数同源（未运行时切全量口径）：可处理 2 条（1 待翻译 +
    # 1 已翻译），done 只计成功译出 → 1 / 2（2026-08-14 口径统一：失败
    # 可重试仍属待翻译，不再计入 done，剩余 = total - done 与「待翻译」
    # chips 恒一致）；low 留档（k2/k4）不计入
    assert page.progress_label.text() == "1 / 2 条"
    assert "剩余 1 条" in page.progress_sub.text()


def test_refresh_chips_pending_without_low_entries_has_no_tooltip(
        qapp, tmp_path):
    rows = [
        {"file_id": "a", "key_path": "k1", "original": "Hello",
         "translation": "", "status": "pending", "locked": 0,
         "meta": json.dumps({"confidence": "high"})},
    ]
    state = _state(tmp_path)
    state.project = type("Project", (), {
        "store": _StoreRows(rows), "out_dir": tmp_path / "game_汉化",
    })()
    state.analysis_report = _report()
    page = TranslatePage(state, _Window())
    page._last_stats = None
    page._refresh_chips()
    await_reload(page)
    assert page.chip_pending.text() == "待翻译 1"
    assert page.chip_pending.toolTip() == ""


def _report(*, unblocked=True, completable=False, route=()):
    return type("Report", (), {
        "unblocked": unblocked,
        "completable": completable,
        "route": route,
        "tool_results": (),
    })()


def test_play_button_dark_until_verified_writeback_then_launches_staged_exe(
        qapp, tmp_path, monkeypatch):
    """开始游戏按钮：写回前禁用（黑），写回验证通过后亮起，
    点击启动汉化副本 exe（out_dir 下、与原游戏同相对位置）。"""
    row = {
        "file_id": "ui.assets", "key_path": "obj/1", "original": "Continue",
        "translation": "继续", "status": "translated", "locked": 0,
        "meta": json.dumps({"quality_passed": True, "confidence": "high"}),
    }
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    source_exe = game_dir / "Game.exe"
    source_exe.write_bytes(b"MZfake")
    out_dir = tmp_path / "game_汉化"
    out_dir.mkdir()
    (out_dir / "Game.exe").write_bytes(b"MZfake")

    class Store(_StoreRows):
        def close(self):
            pass

    class Project:
        def __init__(self):
            self.store = Store([row])
            self.game_dir = game_dir
            self.out_dir = out_dir

        def _fingerprint(self):
            return type("Fp", (), {"executable": source_exe})()

    state = _state(tmp_path)
    state.switch_project(Project())
    page = TranslatePage(state, _Window())
    assert page.play_btn.isEnabled() is False, "写回前按钮应禁用（黑）"

    launched = []
    monkeypatch.setattr(
        "hanhua.ui.pages.translate_page.subprocess.Popen",
        lambda args, cwd=None: launched.append((list(args), cwd)))
    monkeypatch.setattr(
        "hanhua.ui.pages.translate_page.Toast.show", lambda *a, **k: None)

    result = {
        "text_files": 1,
        "verification": {
            "input_protected": True,
            "reopen_verified": True,
            "changed_files": 1,
            "written_translations": 1,
            "font_level": "disabled",
            "overall": "PASS",
        },
        "analysis_report": _report(unblocked=True, completable=True),
    }
    page._on_written(result)
    assert page.play_btn.isEnabled() is True, "写回成功后按钮应亮起"

    page.launch_game()
    assert launched == [([str(out_dir / "Game.exe")], str(out_dir))], \
        "必须启动汉化副本 exe，且 cwd 指向其所在目录"

    # 切换项目后恢复禁用
    class OtherStore(_StoreRows):
        def close(self):
            pass

    state.switch_project(type("Project", (), {
        "store": OtherStore([row]),
        "game_dir": game_dir,
        "out_dir": tmp_path / "other_汉化",
    })())
    assert page.play_btn.isEnabled() is False


def test_translate_write_uses_unblocked_route_and_real_write_ready_count(
        qapp, tmp_path, monkeypatch):
    row = {
        "file_id": "ui.assets", "key_path": "obj/1", "original": "Continue",
        "translation": "继续", "status": "translated", "locked": 0,
        "meta": json.dumps({"quality_passed": False, "confidence": "high"}),
    }
    state = _state(tmp_path)
    state.project = type("Project", (), {
        "store": _StoreRows([row]), "out_dir": tmp_path / "game_汉化",
    })()
    state.analysis_report = _report(route=(
        type("Step", (), {
            "required": True, "status": "pending", "reason": "等待写回",
        })(),
    ))
    page = TranslatePage(state, _Window())
    queued = []
    page._pool = type("Pool", (), {"start": lambda _self, worker: queued.append(worker)})()
    toasts = []
    monkeypatch.setattr(
        "hanhua.ui.pages.translate_page.Toast.show",
        lambda _parent, message, kind="info": toasts.append((message, kind)),
    )

    page._refresh_chips()
    await_reload(page)
    assert not page.write_btn.isEnabled()
    page.write_back()
    assert not queued
    assert "可写译文" in toasts[-1][0]

    row["meta"] = json.dumps({"quality_passed": True, "confidence": "high"})
    page._refresh_chips()
    await_reload(page)
    assert page.write_btn.isEnabled()
    page.write_back()
    assert len(queued) == 1


def test_translate_finish_exports_fail_record_automatically(
        qapp, tmp_path, monkeypatch):
    """翻译完成且存在失败 → 自动导出失败记录到 docs/fail record。"""
    state = _state(tmp_path)
    store = type("Store", (), {
        "get_entries": lambda _self, status=None: [{
            "file_id": "f1", "key_path": "k1",
            "original": "Hello world", "translation": "",
            "status": "failed", "locked": 0,
            "meta": json.dumps({"source": "game/a.txt",
                                "quality_reasons": ["request_error"]}),
        }],
        "count": lambda _self, status: 1 if status == "failed" else 0,
        "get_files": lambda _self: [],
        "get_entries_full": lambda _self: [],
    })()
    state.project = type("Project", (), {
        "store": store,
        "profile": type("Profile", (), {"game_name": "Bloody Battle"})(),
    })()
    page = TranslatePage(state, _Window())
    toasts = []
    monkeypatch.setattr(
        "hanhua.ui.pages.translate_page.Toast.show",
        lambda _parent, message, kind="info": toasts.append((message, kind)),
    )

    stats = TranslateStats(total=2, done=1, failed=1, requests=2)
    page._on_finished(stats)

    exported = list((state.resource_dir / "docs" / "fail record").glob(
        "Bloody Battle fail record *.txt"))
    assert len(exported) == 1
    text = exported[0].read_text(encoding="utf-8")
    assert "Hello world" in text and "request_error" in text
    assert "失败记录已导出" in toasts[-1][0]


# ── #13/#15 回归：流水线 rail 与首页分数实时刷新 ─────────────────

def test_home_rail_follows_pipeline_phase_broadcasts(qapp, tmp_path):
    """翻译/写回阶段广播 → rail 节点实时更新（#15：此前 rail 只在
    _render_report 扫描完成后更新一次，翻译全程卡在旧状态）。"""
    page = HomePage(_state(tmp_path), _Window())
    state = page.state

    state.pipelinePhase.emit(
        "translation", "running", "正在翻译…", "已完成 10 / 100 条")
    node = next(c for c in page.pipeline_cards
                if c.step_id == "translation")
    assert node.property("status") == "running"
    assert "10 / 100" in node.metrics_label.text()

    state.pipelinePhase.emit(
        "writeback", "succeeded", "写回验证通过", "变更文件 3 · 写入译文 42")
    write_node = next(c for c in page.pipeline_cards
                      if c.step_id == "writeback")
    assert write_node.property("status") == "succeeded"
    assert "写入译文 42" in write_node.metrics_label.text()

    # 未知节点 step_id 忽略（不崩）
    state.pipelinePhase.emit("nonsense", "running", "x", "y")
    assert next(c for c in page.pipeline_cards
                if c.step_id == "writeback").property("status") == "succeeded"


def test_home_rail_follows_scan_progress_events(qapp, tmp_path):
    """扫描阶段 PipelineEvent → rail 实时更新（#15：扫描期间 rail 不再
    只显示「检测 running」）。"""
    from hanhua.core.project import PipelineEvent
    page = HomePage(_state(tmp_path), _Window())

    # 2026-08-15 流水线重做：detection/text_scan/tool_analysis 子阶段
    # 合并到「识别」节点（状态取最严重：running > succeeded）
    by_id = {c.step_id: c for c in page.pipeline_cards}

    # 初始全部 succeeded → 识别节点 succeeded
    page._on_scan_progress(PipelineEvent(
        "detection", "succeeded", "Mono · Unity 2022"))
    page._on_scan_progress(PipelineEvent(
        "text_scan", "succeeded", "结构化文本文件 12 个"))
    page._on_scan_progress(PipelineEvent(
        "tool_analysis", "succeeded", "Il2CppDumper 通过"))
    assert by_id["scan"].property("status") == "succeeded"

    # 任一 running → 合并状态升级为 running（最严重）
    page._on_scan_progress(PipelineEvent(
        "tool_analysis", "running", "Il2CppDumper 交叉验证"))
    assert by_id["scan"].property("status") == "running"

    # 非 rail 阶段（binary_scan）忽略不崩
    page._on_scan_progress(PipelineEvent("binary_scan", "succeeded", "x"))


def test_translate_progress_broadcasts_refresh_throttled(qapp, tmp_path):
    """翻译批进度广播节流（#13：每 ≥1s 才 emit entriesChanged，驱动首页
    分数边翻边刷而不刷屏；批粒度 progress 本身照常更新）。"""
    page = TranslatePage(_state(tmp_path), _Window())
    page.state.project = type(
        "Project", (), {"store": _StoreRows([])})()
    emits = []
    page.state.entriesChanged.connect(lambda: emits.append(1))

    page._on_progress(TranslateStats(total=300, done=10))
    page._on_progress(TranslateStats(total=300, done=20))   # 1s 内 → 节流
    assert len(emits) == 1

    page._last_phase_emit = 0.0                             # 模拟时间流逝
    page._on_progress(TranslateStats(total=300, done=30))
    assert len(emits) == 2
    assert page.progress_label.text() == "30 / 300 条"      # 批粒度更新不受节流


# ── #16 回归：审校页选中映射 / reload 保留选中 / AI 面板降级 ──────

def test_review_page_selection_maps_proxy_to_source(qapp, tmp_path):
    """proxy 筛选后选中行 → 中栏显示正确的源行（#16：mapToSource 误收
    源模型索引导致点任意行都显示同一行——Animation Track 实证）。"""
    page = ReviewPage(_state(tmp_path), _Window())
    page.model.setEntries([
        {"id": 1, "file_id": "f", "key_path": "a", "original": "Alpha",
         "translation": "", "status": "pending", "locked": False,
         "meta": {"role": "display"}},
        {"id": 2, "file_id": "f", "key_path": "b", "original": "Beta",
         "translation": "", "status": "translated", "locked": False,
         "meta": {"role": "display"}},
        {"id": 3, "file_id": "f", "key_path": "c", "original": "Gamma",
         "translation": "", "status": "pending", "locked": False,
         "meta": {"role": "display"}},
    ])
    # 搜索 "am" 只命中 Gamma（Alpha/Beta 均不含）→ proxy 仅 1 行
    # （源行 2）：proxy 行号 0 ≠ 源行号 2，直接覆盖错位映射
    # 2026-08-19 防抖：过滤 250ms 合并，等防抖定时器触发
    page.search_box.setText("am")
    QTest.qWait(400)
    assert page.proxy.rowCount() == 1

    proxy_row = 0
    proxy_index = page.proxy.index(proxy_row, 0)
    page.table.selectRow(proxy_row)
    page._on_selection_changed(
        page.table.selectionModel().selection(), None)

    src_row = page.proxy.mapToSource(proxy_index).row()
    assert src_row == 2                # 错位：proxy 0 ≠ 源 2
    assert page._current_row == src_row
    assert page.detail_original.text() == page.model._rows[src_row]["original"]
    # 选中 proxy 第 0 行（源第 2 行 Gamma）→ 中栏显示 Gamma（修复前
    # mapToSource 收源索引会把 proxy 行 0 当源行 0 → 始终显示 Alpha）
    assert page.detail_original.text() == "Gamma"


def test_review_page_reload_keeps_selection(qapp, tmp_path):
    """reload 重建模型后选中焦点保留（#16：翻译/保存触发的刷新丢选中）。"""
    store = ProjectStore(tmp_path / "m.db")
    store.init_schema()
    store.add_file("f", "ui.txt", "plain", "utf-8", "lf")
    store.upsert_entries([
        {"file_id": "f", "key_path": "a", "original": "Alpha",
         "translation": "", "status": "pending", "meta": {}},
        {"file_id": "f", "key_path": "b", "original": "Beta",
         "translation": "", "status": "pending", "meta": {}},
    ])
    state = _state(tmp_path)
    page = ReviewPage(state, _Window())
    state.project = type("Project", (), {"store": store})()
    page.reload()
    await_reload(page)

    page.table.selectRow(1)
    page._on_selection_changed(
        page.table.selectionModel().selection(), None)
    selected_before = page.model._rows[page._current_row]["key_path"]
    assert selected_before == "b"

    page.reload()                                   # 模拟 entriesChanged 刷新
    await_reload(page)
    assert page._current_row is not None
    assert page.model._rows[page._current_row]["key_path"] == "b"
    assert page.detail_original.text() == "Beta"


def test_review_page_same_text_save_and_reload_edit_protected(qapp, tmp_path):
    """#8：相同文本也能保存 + 编辑中 reload 不覆盖编辑框（2 秒回退修复）。

    - 修复前：setData 拒绝相同文本（text == 当前译文直接 return False）→
      手动复核保存必失败，保存后回填旧值
    - 修复前：后台翻译批完成触发 entriesChanged → reload 重建模型 →
      正在编辑的 detail 框 ~2 秒被 store 值打回
    """
    store = ProjectStore(tmp_path / "m.db")
    store.init_schema()
    store.add_file("f", "ui.txt", "plain", "utf-8", "lf")
    store.upsert_entries([
        {"file_id": "f", "key_path": "a", "original": "Alpha",
         "translation": "", "status": "translated", "meta": {}},
        {"file_id": "f", "key_path": "b", "original": "Beta",
         "translation": "", "status": "pending", "meta": {}},
    ])
    store.update_translation("f", "a", "测试译文")  # 真实翻译写入链路
    state = _state(tmp_path)
    page = ReviewPage(state, _Window())
    state.project = type("Project", (), {"store": store})()
    page.reload()
    await_reload(page)
    page.table.selectRow(0)
    page._on_selection_changed(
        page.table.selectionModel().selection(), None)
    assert page.detail_edit.toPlainText() == "测试译文"

    # ① 相同文本直接保存成功（幂等写入，不再被拒绝）
    page.detail_edit.setPlainText("测试译文")      # 与 store 相同
    page._save_detail()
    row = next(r for r in store.get_entries() if r["key_path"] == "a")
    assert row["translation"] == "测试译文"
    assert page._detail_dirty is False

    # ② 编辑中（已改未保存）reload 挂起，编辑框不被覆盖
    page.detail_edit.setPlainText("正在输入的新译文")
    assert page._detail_dirty is True
    page.reload()                                   # 模拟后台批完成触发
    assert page._pending_reload is True             # 已挂起
    assert page.detail_edit.toPlainText() == "正在输入的新译文"

    # ③ 保存后补跑挂起 reload，store 生效、编辑框保持用户输入
    page._save_detail()
    assert page._pending_reload is False
    row = next(r for r in store.get_entries() if r["key_path"] == "a")
    assert row["translation"] == "正在输入的新译文"
    assert page.detail_edit.toPlainText() == "正在输入的新译文"

    # ④ 切行 = 放弃未保存编辑；挂起 reload 补跑并填充新行
    page.detail_edit.setPlainText("未保存的修改")
    assert page._detail_dirty is True
    page.reload()
    assert page._pending_reload is True
    page.table.selectRow(1)
    page._on_selection_changed(
        page.table.selectionModel().selection(), None)
    assert page._pending_reload is False
    assert page.detail_original.text() == "Beta"    # 新行正常显示


# ── #19 回归：翻译完成度用待翻译总数口径 ──────────────────────

def test_home_health_translate_pct_uses_actionable_scope(qapp, tmp_path):
    """「翻译完成」分母 = 待翻译总数（translated + actionable），排除
    skipped 与低置信度留档（#19：此前用文本总数含 skipped 致虚低）。"""
    rows = [
        {"id": 1, "file_id": "ui", "key_path": "a", "original": "Resume",
         "translation": "继续", "status": "translated", "locked": 0,
         "meta": json.dumps({"role": "display", "confidence": "high",
                             "quality_passed": True})},
        {"id": 2, "file_id": "ui", "key_path": "b", "original": "Quit",
         "translation": "", "status": "pending", "locked": 0,
         "meta": json.dumps({"role": "display", "confidence": "high"})},
        {"id": 3, "file_id": "code", "key_path": "s1", "original": "Method1",
         "translation": "", "status": "skipped", "locked": 0,
         "meta": json.dumps({"role": "structural", "confidence": "low"})},
        # 低置信度引擎消息留档：不自动翻，不算进分母
        {"id": 4, "file_id": "engine", "key_path": "m1", "original": "error",
         "translation": "", "status": "pending", "locked": 0,
         "meta": json.dumps({"role": "log", "confidence": "low"})},
    ]
    state = _state(tmp_path)
    page = HomePage(state, _Window())
    state.project = type("Project", (), {"store": _StoreRows(rows)})()
    page._refresh_dashboard()
    await_reload(page)

    # 文本总数 4 不变（显示在 hero_sub）
    assert "共 4 条文本" in page.hero_sub.text()
    # 分母 = translated(1) + actionable(1) = 2 → 完成度 50%（此前 1/4 = 25%）
    assert "1 已翻译（50%）" in page.hero_sub.text()


def test_home_health_translate_pct_all_done_is_100(qapp, tmp_path):
    """全部待翻译条目完成 → 100%（低置信度留档不拖低）。"""
    rows = [
        {"id": 1, "file_id": "ui", "key_path": "a", "original": "Resume",
         "translation": "继续", "status": "translated", "locked": 0,
         "meta": json.dumps({"role": "display", "confidence": "high",
                             "quality_passed": True})},
        {"id": 2, "file_id": "engine", "key_path": "m1", "original": "error",
         "translation": "", "status": "pending", "locked": 0,
         "meta": json.dumps({"role": "log", "confidence": "low"})},
    ]
    state = _state(tmp_path)
    page = HomePage(state, _Window())
    state.project = type("Project", (), {"store": _StoreRows(rows)})()
    page._refresh_dashboard()
    await_reload(page)
    assert "1 已翻译（100%）" in page.hero_sub.text()


def test_review_page_mark_pending_clears_review_state(qapp, tmp_path):
    """#9：审校页右键「标记为待翻译（重新翻译）」清审核阻断终态。

    修复前只 set_status：BLOCKED 残留继续拒绝重译成功的译文——失败
    文本无法通过重译自己处理，只能人工改。
    """
    import json
    from hanhua.core.memory import ProjectStore
    store = ProjectStore(tmp_path / "m.db")
    store.init_schema()
    store.add_file("f", "ui.txt", "plain", "utf-8", "lf")
    store.upsert_entries([
        {"file_id": "f", "key_path": "a", "original": "Alpha",
         "translation": "", "status": "failed", "locked": False,
         "meta": json.dumps({
             "review_outcome": "BLOCKED", "review_blocked": True,
             "quality_passed": False, "review_level": "MAJOR",
             "rejected_candidate": "坏译文", "quality_reasons": ["semantic"],
         })},
    ])
    state = _state(tmp_path)
    page = ReviewPage(state, _Window())
    state.project = type("Project", (), {"store": store})()
    page.reload()
    await_reload(page)
    row = page.model._rows[0]
    page._mark_pending([row])
    persisted = next(
        e for e in store.get_entries() if e["key_path"] == "a")
    meta = json.loads(persisted["meta"] or "{}")
    assert persisted["status"] == "pending"
    for field in ("review_outcome", "review_blocked", "review_level",
                  "rejected_candidate", "quality_reasons", "review_issue"):
        assert field not in meta, field


def _wait_review(page, timeout_ms=8000):
    """#38：等单条「重新审核」worker 结束（_review_running 复位）。"""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while page._review_running and time.monotonic() < deadline:
        QTest.qWait(10)
    assert not page._review_running, "单条重新审核超时"


def test_review_page_review_button_force_reviews_and_approves(
        qapp, tmp_path, monkeypatch):
    """#38：审校页「重新审核」按钮——人工强制送审，PASS → APPROVED。

    默认分流对无信号条目直放（按钮会点了没反应）；force_send 无条件
    送审。translator=None：PASS 直接终态 APPROVED（写回发布门放行）。
    """
    import json
    from hanhua.core.models import TextEntry
    from hanhua.core.reviewer import ReviewResult
    store = ProjectStore(tmp_path / "m.db")
    store.init_schema()
    store.add_file("f", "ui.txt", "plain", "utf-8", "lf")
    store.upsert_entries([
        {"file_id": "f", "key_path": "a", "original": "Loading",
         "meta": {"role": "display", "disposition": "translate"}},
    ])
    # 翻译完成路径写入译文（upsert_entries 不保留译文——识别器批量语义）
    store.batch_update_translation_results([
        TextEntry("f", "a", "Loading", translation="加载中",
                  status="translated", meta={"quality_passed": True}),
    ])
    state = _state(tmp_path)
    page = ReviewPage(state, _Window())
    state.project = type("Project", (), {"store": store, "name": "demo"})()
    page.reload()
    await_reload(page)

    sent = []
    class _FakeReviewer:
        usable = True
        def __init__(self, app_dir=None, service=None, online_cfg=None, config=None):
            pass
        def review_batch(self, items, *, on_progress=None,
                         cancellation_event=None):
            sent.extend(items)
            return {it.entry_id: ReviewResult(it.entry_id, level="PASS")
                    for it in items}, 0
    monkeypatch.setattr("hanhua.core.reviewer.SemanticReviewer",
                        _FakeReviewer)

    # 选中 translated 行 → 「重新审核」启用
    src = page.model.index(0, 0)
    page.table.selectRow(page.proxy.mapFromSource(src).row())
    QTest.qWait(20)
    assert page.review_btn.isEnabled()

    page.review_btn.click()
    _wait_review(page)
    await_reload(page)                       # 完成后 reload 落库刷新
    assert sent, "按钮必须强制送审（fake reviewer 被调用）"
    persisted = next(e for e in store.get_entries() if e["key_path"] == "a")
    meta = json.loads(persisted["meta"] or "{}")
    assert meta.get("review_outcome") == "APPROVED"
    assert meta.get("review_level") == "PASS"
    assert persisted["status"] == "translated"


def test_review_page_review_button_major_keeps_needs_revision(
        qapp, tmp_path, monkeypatch):
    """#38：MAJOR 判定（translator=None）→ 终态 NEEDS_REVISION。

    译文保留等待人工按审核意见修改，不自动重译、不发布。
    """
    import json
    from hanhua.core.models import TextEntry
    from hanhua.core.reviewer import ReviewResult
    store = ProjectStore(tmp_path / "m.db")
    store.init_schema()
    store.add_file("f", "ui.txt", "plain", "utf-8", "lf")
    store.upsert_entries([
        {"file_id": "f", "key_path": "a", "original": "Loading",
         "meta": {"role": "display", "disposition": "translate"}},
    ])
    store.batch_update_translation_results([
        TextEntry("f", "a", "Loading", translation="装载",
                  status="translated", meta={"quality_passed": True}),
    ])
    state = _state(tmp_path)
    page = ReviewPage(state, _Window())
    state.project = type("Project", (), {"store": store, "name": "demo"})()
    page.reload()
    await_reload(page)

    class _FakeReviewer:
        usable = True
        def __init__(self, app_dir=None, service=None, online_cfg=None, config=None):
            pass
        def review_batch(self, items, *, on_progress=None,
                         cancellation_event=None):
            return {it.entry_id: ReviewResult(
                it.entry_id, level="MAJOR", reason="LOADING 误译为装载",
                suggestion="加载中") for it in items}, 0
    monkeypatch.setattr("hanhua.core.reviewer.SemanticReviewer",
                        _FakeReviewer)

    src = page.model.index(0, 0)
    page.table.selectRow(page.proxy.mapFromSource(src).row())
    QTest.qWait(20)
    page.review_btn.click()
    _wait_review(page)
    await_reload(page)
    persisted = next(e for e in store.get_entries() if e["key_path"] == "a")
    meta = json.loads(persisted["meta"] or "{}")
    assert meta.get("review_outcome") == "NEEDS_REVISION"
    assert meta.get("review_level") == "MAJOR"
    assert meta.get("need_revision") is True
    assert meta.get("quality_passed") is False     # 发布门双重拒绝
    assert persisted["translation"] == "装载"     # 译文保留等待人工修改
    # #38：待审核筛选必须命中未收敛终态（review_issue 死代码修复——
    # 终态统一落 review_outcome，筛选不再只看永不写入的 review_issue）
    page.proxy.setFilters(status="needs_review")
    assert page.proxy.rowCount() == 1
    assert not page.filter_chips["needs_review"].isHidden()


def test_translate_progress_throttles_chips_keeps_progress_live(qapp, tmp_path):
    """#6：翻译 UI 卡顿——计数刷新（全量 O(N) 查库）≥1s 节流，进度条
    仍按批粒度实时更新。修复前每批 _refresh_chips 全量扫描（万级条目
    × 每批一次）卡住主线程。"""
    calls = []
    page = TranslatePage(_state(tmp_path), _Window())
    page.state.project = type(
        "Project", (), {"store": _StoreRows([])})()
    page._refresh_chips = lambda: calls.append(1)

    page._on_progress(TranslateStats(total=300, done=10))
    page._on_progress(TranslateStats(total=300, done=20))   # 1s 内 → 节流
    assert len(calls) == 1
    # 进度条/数字批粒度实时更新（不受节流影响）
    assert page.progress_label.text() == "20 / 300 条"
    # 2026-08-20 全链路 3-3-3-1 进度条：翻译段 0-30%，20/300*30 ≈ 2
    assert page.progress_bar.value() == 2

    page._last_chip_refresh = 0.0                           # 模拟时间流逝
    page._on_progress(TranslateStats(total=300, done=30))
    assert len(calls) == 2
    assert page.progress_label.text() == "30 / 300 条"


# ── 多 Unity 玩家目录（ambiguous 布局）GUI 选择入口 ─────────────

def _make_gui_same_root_players(tmp_path):
    """同根双玩家（ned-flanders 布局）：A.exe/A_Data + B.exe/B_Data。"""
    from tests.test_project import _make_same_root_mono_players
    return _make_same_root_mono_players(tmp_path)


def test_home_single_player_dir_skips_selection_dialog(qapp, tmp_path):
    """单玩家目录：不弹选择框，open_dir 走原路径（player 选择器为
    None）。"""
    import hanhua.ui.pages.home_page as home_mod
    source = _make_gui_same_root_players(tmp_path)
    single = source.parent / "solo"
    single.mkdir()
    (single / "solo.exe").write_bytes(b"")
    page = HomePage(_state(tmp_path), _Window())
    called = []
    real = home_mod.HomePage._resolve_player_selection
    monkey_sel = lambda self, path: called.append(path) or None
    page.__class__._resolve_player_selection = monkey_sel
    try:
        page.open_dir(single)
    finally:
        page.__class__._resolve_player_selection = real
    assert called == [single]


def test_home_ambiguous_dir_prompts_selection_and_scans_selected_player(
        qapp, tmp_path, monkeypatch):
    """多玩家目录：弹选择对话框 → 用户选中 A 玩家 → 扫描 worker 收到
    player_root/player_executable 选择器（同根不同玩家自动隔离 DB）。"""
    import hanhua.ui.pages.home_page as home_mod
    source = _make_gui_same_root_players(tmp_path)
    state = _state(tmp_path / "app")
    page = HomePage(state, _Window())
    captured = {}
    monkeypatch.setattr(
        home_mod, "_select_player_candidate",
        lambda candidates: captured.setdefault(
            "candidates", candidates) and (
            candidates[0].player_root, candidates[0].executable))
    monkeypatch.setattr(
        page, "_set_busy", lambda busy: None)
    from PySide6.QtCore import QThreadPool
    real_pool = QThreadPool.globalInstance
    started = []
    monkeypatch.setattr(
        home_mod.QThreadPool, "globalInstance",
        staticmethod(lambda: type("P", (), {
            "start": staticmethod(lambda worker: started.append(worker))})()))
    monkeypatch.setattr(home_mod.Toast, "show", lambda *a, **k: None)

    page.open_dir(source)

    assert len(captured["candidates"]) == 2   # A、B 两个玩家候选
    assert len(started) == 1
    # 扫描闭包必须携带选择器（player A）
    fake_project = type("P", (), {"scan_all": lambda self, event_cb=None, csv_overwrite_source=False: None})()
    monkeypatch.setattr(
        home_mod.Project, "open_game_dir", staticmethod(
            lambda gd, ad, player_root=None, player_executable=None:
            captured.update(opened=(player_root, player_executable))
            or fake_project))
    started[0].fn()
    root, exe = captured["opened"]
    assert Path(root).name == source.name or Path(root) == source
    assert str(exe).endswith("A.exe")


def test_home_ambiguous_dir_cancel_keeps_project_closed(
        qapp, tmp_path, monkeypatch):
    """用户取消选择 → 不启动任何扫描（不进入忙碌态，DB 不建）。"""
    import hanhua.ui.pages.home_page as home_mod
    source = _make_gui_same_root_players(tmp_path)
    state = _state(tmp_path / "app")
    page = HomePage(state, _Window())
    toasts = []
    monkeypatch.setattr(home_mod, "_select_player_candidate", lambda c: None)
    monkeypatch.setattr(
        home_mod.Toast, "show", lambda _p, msg, kind="info":
        toasts.append(msg))
    started = []
    monkeypatch.setattr(
        home_mod.QThreadPool, "globalInstance",
        staticmethod(lambda: type("P", (), {
            "start": staticmethod(lambda worker: started.append(worker))})()))
    monkeypatch.setattr(page, "_set_busy", lambda busy: toasts.append(
        f"busy={busy}"))

    page.open_dir(source)

    assert started == []                       # 未启动扫描
    assert page._scanning is False             # 未进入忙碌态
    assert any("多个游戏" in t for t in toasts)  # 取消 toast 说明原因


def test_home_scan_done_ambiguous_fallback_hint_is_actionable(
        qapp, tmp_path, monkeypatch):
    """兜底路径（探测失败/直接 blocked）：ambiguous blocked 报告给出
    指向明确成因的提示，而非笼统的「请查看阻断步骤」。"""
    import hanhua.ui.pages.home_page as home_mod
    state = _state(tmp_path)
    window = _RecordingWindow()
    page = HomePage(state, window)
    toasts = []
    monkeypatch.setattr(
        home_mod.Toast, "show",
        lambda _p, msg, kind="info": toasts.append(msg))
    monkeypatch.setattr(page, "_render_report", lambda _r: None)
    monkeypatch.setattr(page, "_refresh_profile_card", lambda: None)
    monkeypatch.setattr(
        "hanhua.ui.app_state.AppState.switch_project", lambda *a, **k: 0)
    fingerprint = type("F", (), {"evidence": ("ambiguous_player_layout",),
                                 "runtime": "unknown",
                                 "unity_version": "unknown"})()
    report = type("Report", (), {
        "text_files": 0, "v2_files": 0, "unblocked": False,
        "fingerprint": fingerprint,
    })()

    page._on_scan_done((object(), report))

    assert any("多个 Unity 游戏" in t for t in toasts)
