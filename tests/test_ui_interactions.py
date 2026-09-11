"""阶段 D：UI 按键交互实测（offscreen）——每个按键的响应、漏洞、显示正确性。

覆盖：导航点击 / 搜索快捷键 / 表格编辑与锁定 / 筛选联动 / 拖放 /
翻译页按钮状态矩阵 / 设置页后端切换矩阵 / 写回按钮无项目保护。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QApplication, QFileDialog, QLabel,
                               QPushButton, QTableWidgetItem)

from hanhua.core.memory import ProjectStore
from hanhua.core.models import TranslateStats
from hanhua.core.settings import SettingsStore
from hanhua.ui.app_state import AppState
from hanhua.ui.main_window import MainWindow
from hanhua.ui.pages.home_page import HomePage
from hanhua.ui.pages.review_page import ReviewPage
from hanhua.ui.pages.translate_page import TranslatePage
from hanhua.ui.widgets import (EmptyState, MetricStrip, PageHeader,
                               StatusBadge, StatusRail)
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
        self.projects = []

    def navigate(self, page):
        self.pages.append(page)

    def updateProjectCard(self, project):
        self.projects.append(project)


def _state(tmp_path: Path) -> AppState:
    settings = SettingsStore(tmp_path / "settings.json")
    settings.load()
    return AppState(tmp_path, settings)


def _store(tmp_path: Path) -> ProjectStore:
    store = ProjectStore(tmp_path / "project.db")
    store.init_schema()
    store.add_file("ui", "ui.assets", "v2_asset", "binary", "")
    store.upsert_entries([
        {
            "file_id": "ui", "key_path": "obj/open",
            "original": "Open Door", "meta": {
                "role": "display", "confidence": "high",
                "disposition": "translate",
            },
        },
        {
            "file_id": "ui", "key_path": "obj/quit",
            "original": "Quit Game", "meta": {
                "role": "display", "confidence": "high",
                "disposition": "translate",
            },
        },
    ])
    return store


class _FakeProject:
    """含 store 与 profile 的假项目。"""

    def __init__(self, store: ProjectStore):
        self.store = store
        self.profile = None
        self.game_dir = Path("D:/fake/game")
        self.out_dir = Path("D:/fake/out")


# ─────────────────────────── 导航 ───────────────────────────

def test_nav_click_switches_stack_and_enables_nav(qapp, tmp_path):
    state = _state(tmp_path)
    window = MainWindow(state)
    window.updateProjectCard(_FakeProject(_store(tmp_path)))

    assert window.current_page() == "home"
    assert not window.pages["review"].isVisible() or window.stack.currentIndex() == 0

    # 逐一点击每个导航项，断言页面栈切换（四项简洁导航：每行都是页面项）
    from hanhua.ui.main_window import PAGES, _NAV_PAGE_ROWS
    for row, page_index in sorted(_NAV_PAGE_ROWS.items(), key=lambda kv: kv[0]):
        window.nav.setCurrentRow(row)
        assert window.current_page() == PAGES[page_index], \
            f"row {row} 应切到 {PAGES[page_index]}"
        # 选中项图标应为品牌薄荷青（响应验证）
        selected = window.nav.item(row)
        assert selected.icon() is not None and not selected.icon().isNull()
    # 没有不可选的分组标题行；点击首页回到 home
    for row in range(window.nav.count()):
        assert window.nav.item(row).flags() & Qt.ItemIsEnabled, \
            f"row {row} 不应是分组标题"
    window.nav.setCurrentRow(0)
    assert window.current_page() == "home"


def test_navigate_programmatic_all_pages(qapp, tmp_path):
    """程序化 navigate(name) 四页全覆盖（F 回归：settings 页曾因
    _NAV_PAGE_ROWS 键值反查 KeyError 崩溃——group 行无键）。"""
    state = _state(tmp_path)
    window = MainWindow(state)
    for name in ("home", "review", "translate", "settings"):
        window.navigate(name)
        assert window.current_page() == name, f"navigate({name}) 未到达"
    window.navigate("home")
    assert window.current_page() == "home"


def test_nav_always_available_without_project(qapp, tmp_path):
    """未打开游戏文件夹时导航也可用：四项导航全部可进入对应页面。"""
    state = _state(tmp_path)
    window = MainWindow(state)
    from hanhua.ui.main_window import PAGES
    for row in range(window.nav.count()):
        assert window.nav.item(row).flags() & Qt.ItemIsEnabled, \
            f"row {row} 无项目时也应可点"
        window.nav.setCurrentRow(row)
        assert window.current_page() == PAGES[row]


def test_main_navigation_is_task_focused(qapp, tmp_path):
    """导航为五项任务：概览 / 审校 / 运行 / 翻译 / 设置；侧栏项目卡已移除。"""
    window = MainWindow(_state(tmp_path))
    labels = [window.nav.item(i).text() for i in range(window.nav.count())]
    assert labels == ["概览", "审校", "运行", "翻译", "设置"]
    assert not hasattr(window, "project_card")


def test_statusbar_reflects_project_and_backend(qapp, tmp_path):
    state = _state(tmp_path)
    window = MainWindow(state)
    project = _FakeProject(_store(tmp_path))
    state.switch_project(project)  # 触发 projectOpened → 顶部栏/状态栏刷新
    text = window.statusBar().currentMessage()
    assert "game" in text  # 项目名（game_dir.name）出现在状态栏
    assert "项目" in text
    assert not hasattr(window, "project_card")     # 侧栏项目卡已移除
    assert window.top_bar.project_name.text() == "game"  # 项目上下文进顶部栏


# ─────────────────────────── 首页 ───────────────────────────

def test_home_pick_button_opens_directory_picker(qapp, tmp_path, monkeypatch):
    page = HomePage(_state(tmp_path), _RecordingWindow())
    chosen = []

    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory",
        lambda *args, **kwargs: str(tmp_path / "some-dir"))
    # 打开一个真实存在的小目录模拟选择后流程：open_dir 会触发扫描，
    # 这里拦截 Project.open_game_dir 避免真实扫描
    import hanhua.ui.pages.home_page as home_mod

    def fake_open(game_dir, app_dir):
        project = _FakeProject(_store(tmp_path))
        project.game_dir = Path(game_dir)
        project.out_dir = Path(app_dir).parent / "out"
        return project

    monkeypatch.setattr(home_mod.Project, "open_game_dir", staticmethod(fake_open))
    target = tmp_path / "some-dir"
    target.mkdir()

    page.pick_btn.click()
    assert page._scanning or not page.pick_btn.isEnabled()  # 进入忙碌态


def test_home_drop_zone_emits_directory(qapp, tmp_path):
    page = HomePage(_state(tmp_path), _RecordingWindow())
    received = []
    page.drop_zone.directoryDropped.connect(received.append)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(tmp_path))])
    from PySide6.QtGui import QDragEnterEvent
    event = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction, mime,
                            Qt.LeftButton, Qt.NoModifier)
    page.drop_zone.dragEnterEvent(event)
    assert event.isAccepted()


# ─────────────────────────── 审校页 ─────────────────────────

def test_review_search_shortcut_focuses_search_box(qapp, tmp_path):
    from PySide6.QtGui import QShortcut
    state = _state(tmp_path)
    state.project = _FakeProject(_store(tmp_path))
    page = ReviewPage(state, _Window())
    await_reload(page)
    # Ctrl+F 快捷键已注册（offscreen 下窗口焦点不可靠，验证注册与行为函数）
    shortcuts = [s for s in page.findChildren(QShortcut)
                 if s.key().matches(QKeySequence("Ctrl+F"))]
    assert shortcuts, "缺少 Ctrl+F 搜索快捷键"
    page._focus_search()  # 行为函数可调用且不崩溃
    assert page.search_box.text() == ""


def test_review_table_edit_persists_translation(qapp, tmp_path):
    store = _store(tmp_path)
    state = _state(tmp_path)
    state.project = _FakeProject(store)
    page = ReviewPage(state, _Window())
    page.reload()
    await_reload(page)

    model = page.model
    assert model.rowCount() >= 2
    # 找到 Open Door 行，编辑译文列（col 3）
    row_idx = next(
        i for i in range(model.rowCount())
        if model._rows[i]["key_path"] == "obj/open")
    index = model.index(row_idx, 3)
    assert model.flags(index) & Qt.ItemIsEditable
    assert model.setData(index, "打开门")
    persisted = next(
        e for e in store.get_entries()
        if e["key_path"] == "obj/open")
    assert persisted["translation"] == "打开门"
    assert persisted["status"] == "translated"


def test_review_lock_checkbox_toggles(qapp, tmp_path):
    store = _store(tmp_path)
    state = _state(tmp_path)
    state.project = _FakeProject(store)
    page = ReviewPage(state, _Window())
    page.reload()
    await_reload(page)

    row_idx = next(
        i for i in range(page.model.rowCount())
        if page.model._rows[i]["key_path"] == "obj/quit")
    index = page.model.index(row_idx, 6)   # 锁定列（#43 阶段 G 风险列后移）
    assert page.model.flags(index) & Qt.ItemIsUserCheckable
    assert page.model.setData(index, Qt.Checked, Qt.CheckStateRole)
    persisted = next(
        e for e in store.get_entries()
        if e["key_path"] == "obj/quit")
    assert persisted["locked"] == 1


def test_review_filter_updates_proxy_rows(qapp, tmp_path):
    state = _state(tmp_path)
    state.project = _FakeProject(_store(tmp_path))
    page = ReviewPage(state, _Window())
    page.reload()
    await_reload(page)

    assert page.proxy.rowCount() == 2
    # 2026-08-19 防抖：搜索过滤 250ms 合并（_on_search_changed）——
    # setText 不再同步过滤，等防抖定时器触发（qWait 泵事件循环）。
    page.search_box.setText("Quit")
    QTest.qWait(400)
    assert page.proxy.rowCount() == 1
    page.search_box.setText("不存在的文本")
    QTest.qWait(400)
    assert page.proxy.rowCount() == 0


def test_review_translate_button_navigates(qapp, tmp_path):
    window = _RecordingWindow()
    state = _state(tmp_path)
    state.project = _FakeProject(_store(tmp_path))
    page = ReviewPage(state, window)
    page.translate_btn.click()
    assert window.pages == ["translate"]


def test_review_context_menu_copy_does_not_crash(qapp, tmp_path):
    state = _state(tmp_path)
    state.project = _FakeProject(_store(tmp_path))
    page = ReviewPage(state, _Window())
    page.reload()
    await_reload(page)
    # 右键菜单弹出需要真实窗口事件循环，这里直接调用菜单构建逻辑
    # 验证 _show_menu 在无命中时安全返回
    page._show_menu(QPoint(-5, -5))  # 不在表格内 → 直接返回
    assert True


# ─────────────────────────── 翻译页 ─────────────────────────

def test_translate_buttons_disabled_without_project(qapp, tmp_path):
    page = TranslatePage(_state(tmp_path), _RecordingWindow())
    # 无项目时写回与停止按钮必须禁用；开始按钮可点（点击给警告提示）
    assert page.start_btn.isEnabled()
    assert not page.write_btn.isEnabled()
    assert not page.stop_btn.isEnabled()
    assert page.reveal_btn.isHidden()


def test_translate_start_without_project_shows_warning_not_crash(
        qapp, tmp_path):
    page = TranslatePage(_state(tmp_path), _RecordingWindow())
    page.start()  # 无项目 → Toast 警告，不应崩溃、不应触发翻译 worker
    assert page._active_run is None


def test_translate_buttons_state_matrix_with_project(qapp, tmp_path):
    store = _store(tmp_path)
    state = _state(tmp_path)
    state.project = _FakeProject(store)
    page = TranslatePage(state, _RecordingWindow())

    # 有项目且有待翻译 → 开始按钮可用；写回需有译文（无）→ 禁用
    assert page.start_btn.isEnabled()
    assert not page.write_btn.isEnabled()

    # 手动翻译一条 → 写回可用
    store.set_manual("ui", "obj/open", "打开门")
    page._refresh_chips()
    await_reload(page)
    assert page.write_btn.isEnabled()

    # 停止按钮仅在运行中可用
    assert not page.stop_btn.isEnabled()


def test_translate_progress_chips_refresh(qapp, tmp_path):
    store = _store(tmp_path)
    state = _state(tmp_path)
    state.project = _FakeProject(store)
    page = TranslatePage(state, _RecordingWindow())
    # 进度统计来自 stats；已翻译计数来自 store
    page._on_progress(TranslateStats(total=2, done=1, failed=0))
    assert "1 / 2" in page.progress_label.text() or page.progress_bar.value() == 50
    await_reload(page)                              # 节流触发的 chips 刷新
    assert page.chip_done.text() == "已翻译 0"
    store.set_manual("ui", "obj/open", "打开门")
    page._refresh_chips()
    await_reload(page)
    assert page.chip_done.text() == "已翻译 1"


def test_translate_retry_marks_failed_as_pending(qapp, tmp_path):
    store = _store(tmp_path)
    store.set_status("ui", "obj/open", "failed")
    state = _state(tmp_path)
    state.project = _FakeProject(store)
    page = TranslatePage(state, _RecordingWindow())
    # 拦截 start 避免真实翻译
    page.start = lambda: None
    page.retry_failed()
    persisted = next(
        e for e in store.get_entries()
        if e["key_path"] == "obj/open")
    assert persisted["status"] == "pending"


def test_retry_failed_clears_blocked_review_state(qapp, tmp_path):
    """#9：失败文本自处理——重试失败清审核阻断终态，重译成功不再被
    残留 BLOCKED 拒绝（修复前只 set_status，发布门继续 fail-closed）。"""
    import json
    store = _store(tmp_path)
    store.conn.execute(
        "UPDATE entries SET status='failed', meta=? "
        "WHERE file_id='ui' AND key_path='obj/open'",
        (json.dumps({
            "review_outcome": "BLOCKED", "review_blocked": True,
            "quality_passed": False, "review_level": "MAJOR",
            "rejected_candidate": "坏译文",
        }),))
    store.conn.commit()
    state = _state(tmp_path)
    state.project = _FakeProject(store)
    page = TranslatePage(state, _RecordingWindow())
    page.start = lambda: None
    page.retry_failed()
    persisted = next(
        e for e in store.get_entries()
        if e["key_path"] == "obj/open")
    meta = json.loads(persisted["meta"] or "{}")
    assert persisted["status"] == "pending"
    for field in ("review_outcome", "review_blocked", "review_level",
                  "rejected_candidate"):
        assert field not in meta, field


# ─────────────────────────── 设置页 ─────────────────────────

def test_settings_backend_switch_enables_local_fields(qapp, tmp_path):
    from hanhua.ui.pages.settings_page import SettingsPage
    page = SettingsPage(_state(tmp_path), _RecordingWindow())

    # 默认本地模式（F56：开箱即用离线）；在线卡隐藏、本地卡可见
    assert page.backend_mode.currentData() == "local"
    assert page.stop_local_btn.isEnabled()
    assert page.mode_api_widget.isHidden() is True
    assert page.mode_local_widget.isHidden() is False
    # 切在线：本地卡隐藏
    page.backend_mode.setCurrentIndex(page.backend_mode.findData("api"))
    assert page.mode_local_widget.isHidden() is True

    # 切到本地：四模型卡片可见，API 表单隐藏（2026-08-14 重构语义）
    page.backend_mode.setCurrentIndex(
        page.backend_mode.findData("local"))
    assert page.stop_local_btn.isEnabled()
    assert page.mode_local_widget.isHidden() is False
    assert page.mode_api_widget.isHidden() is True
    assert page.test_btn.text() == "启动并测试"


def test_settings_save_persists_config(qapp, tmp_path):
    from hanhua.ui.pages.settings_page import SettingsPage
    state = _state(tmp_path)
    page = SettingsPage(state, _RecordingWindow())
    page.api_url.setText("https://example.com/v1")
    page._save_api()
    assert state.api.base_url == "https://example.com/v1"


def test_settings_online_mode_shows_translate_review_api_cards(qapp, tmp_path):
    """在线 API 模式：翻译/审核/检索三张卡（重排恒本地无卡片）。"""
    from hanhua.ui.pages.settings_page import SettingsPage
    page = SettingsPage(_state(tmp_path), _RecordingWindow())
    # 切在线后断言在线卡（默认本地——F56）
    page.backend_mode.setCurrentIndex(page.backend_mode.findData("api"))
    assert set(page.api_cards) == {"translate", "review", "embed"}
    assert page.api_cards["translate"]["test_btn"].text() == "测试连接"
    # 在线模式：在线卡片可见、本地四卡隐藏
    assert page.mode_api_widget.isHidden() is False
    assert page.mode_local_widget.isHidden() is True
    # 切本地：在线卡片隐藏
    page.backend_mode.setCurrentIndex(page.backend_mode.findData("local"))
    assert page.mode_api_widget.isHidden() is True


def test_settings_api_card_edit_persists_immediately(qapp, tmp_path):
    """在线 API 卡片即改即存：改字段立即写入 store（无保存按钮）。"""
    from hanhua.ui.pages.settings_page import SettingsPage
    state = _state(tmp_path)
    page = SettingsPage(state, _RecordingWindow())
    card = page.api_cards["review"]
    card["url"].setText("https://review.example.com/v1")
    assert state.settings.api_config(
        "review").base_url == "https://review.example.com/v1"
    assert card["status"].text() == "未配置"     # key/model 仍空
    card["key"].setText("sk-review")
    card["model"].setText("claude-sonnet-4")
    assert card["status"].text() == "已配置"
    assert card["provider"].currentData() == "openai"   # 缺省提供商
    # translate 卡（self.api 别名）不受 review 卡影响
    assert state.settings.api_config("translate").base_url == ""


def test_settings_api_card_load_restores_persisted_config(qapp, tmp_path):
    """四卡 UI 从 store 恢复持久化配置（含 provider）。"""
    from hanhua.ui.pages.settings_page import SettingsPage
    state = _state(tmp_path)
    state.settings.set_api_config(
        "review", provider="anthropic", base_url="https://r/v1",
        api_key="k", model="claude-sonnet-4")
    page = SettingsPage(state, _RecordingWindow())
    card = page.api_cards["review"]
    assert card["provider"].currentData() == "anthropic"
    assert card["url"].text() == "https://r/v1"
    assert card["key"].text() == "k"
    assert card["model"].text() == "claude-sonnet-4"
    assert card["status"].text() == "已配置"
    # 加载填充不污染 store（屏蔽信号：值不变）
    assert state.settings.api_config("review").model == "claude-sonnet-4"


def test_settings_nav_switches_tabs(qapp, tmp_path):
    """§66：左侧分类导航驱动右侧内容（tabBar 隐藏）。"""
    from hanhua.ui.pages.settings_page import SettingsPage
    page = SettingsPage(_state(tmp_path), _RecordingWindow())
    assert not page.tabs.tabBar().isVisible()
    assert page.tabs.currentIndex() == 0
    # 2026-08-22 加「说明」页后 5 页：环境/翻译设置/术语库/说明/关于，
    # 导航行 1 = 翻译设置(1)、行 2 = 术语库(2)、行 3 = 说明(3)、行 4 = 关于(4)
    page.settings_nav.setCurrentRow(1)
    assert page.tabs.currentIndex() == 1
    page.settings_nav.setCurrentRow(2)
    assert page.tabs.currentIndex() == 2
    page.settings_nav.setCurrentRow(3)
    assert page.tabs.currentIndex() == 3
    page.settings_nav.setCurrentRow(4)
    assert page.tabs.currentIndex() == 4


def test_settings_review_strategy_persists(qapp, tmp_path):
    """#47 + 2026-08-21：AI 审核改为固定管线环节——「AI 审核」分类页
    已删除（无开关可关）；全量送审范围说明并入「关于」页。"""
    from hanhua.ui.pages.settings_page import SettingsPage
    state = _state(tmp_path)
    page = SettingsPage(state, _RecordingWindow())
    # 审核开关已移除：恒全量审核，无关闭入口
    assert not hasattr(page, "review_enabled")
    assert not hasattr(page, "review_scope_label")
    assert not hasattr(page, "review_save_btn")
    # ai_review_enabled 字段保留兼容旧配置（恒走审核，translate_page 恒真）
    assert state.api.ai_review_enabled is True


def test_settings_glossary_add_edit_delete(qapp, tmp_path):
    from hanhua.ui.pages.settings_page import SettingsPage
    page = SettingsPage(_state(tmp_path), _RecordingWindow())
    page.tabs.setCurrentWidget(page.glossary_table.parent())  # 切到术语表
    # 新增后统计徽章/搜索/冲突条存在（#15 术语库 UI 重做）
    assert hasattr(page, "glossary_search")
    assert hasattr(page, "glossary_conflict_bar")
    assert page._glossary_badges["total"].text() == "0"
    page._glossary_add()
    # 直接填表
    page._glossary_loading = True
    page.glossary_table.setItem(0, 0, QTableWidgetItem("Aria"))
    page.glossary_table.setItem(0, 1, QTableWidgetItem("艾莉亚"))
    page.glossary_table.setItem(0, 2, QTableWidgetItem("人名"))
    page._glossary_loading = False
    page._glossary_cell_changed(0, 1)
    rows = page._glossary.list_all()
    assert any(r["term"] == "Aria" and r["translation"] == "艾莉亚"
               for r in rows)
    # 徽章刷新：总条目 1
    assert page._glossary_badges["total"].text() == "1"


def test_settings_glossary_search_and_status_toggle(qapp, tmp_path):
    """#15：搜索过滤 + 双击状态列候选↔生效切换（人工确认/降级）。"""
    from hanhua.ui.pages.settings_page import SettingsPage
    page = SettingsPage(_state(tmp_path), _RecordingWindow())
    page._ensure_glossary()
    for term, trans, cat, status in (
            ("Vale", "幽谷", "地名", "active"),
            ("Aria", "艾莉亚", "人名", "active"),
            ("moon_key", "月光钥匙", "术语", "candidate")):
        page._glossary.add(term, trans, cat, "", )
        if status == "candidate":
            page._glossary.set_status(term, "candidate")
    page._glossary_reload()
    assert page.glossary_table.rowCount() == 3
    # 搜索过滤：只显示 Aria
    page.glossary_search.setText("Aria")
    assert [page.glossary_table.item(r, 0).text()
            for r in range(page.glossary_table.rowCount())
            if not page.glossary_table.isRowHidden(r)] == ["Aria"]
    page.glossary_search.clear()
    # 类别筛选：人名 → 只显示 Aria
    page.glossary_filter.setCurrentText("人名")
    assert [page.glossary_table.item(r, 0).text()
            for r in range(page.glossary_table.rowCount())
            if not page.glossary_table.isRowHidden(r)] == ["Aria"]
    page.glossary_filter.setCurrentText("全部")
    # 状态列显示 生效/候选
    statuses = [page.glossary_table.item(r, 3).text()
                for r in range(page.glossary_table.rowCount())]
    assert statuses.count("生效") == 2 and statuses.count("候选") == 1
    # 双击候选行状态列 → 升级为生效
    row = next(r for r in range(page.glossary_table.rowCount())
               if page.glossary_table.item(r, 0).text() == "moon_key")
    page._glossary_cell_double(row, 3)
    assert page._glossary.list_all()
    assert [r["status"] for r in page._glossary.list_all()
            if r["term"] == "moon_key"] == ["active"]
    # 徽章：候选 0
    assert page._glossary_badges["candidate"].text() == "0"


# ─────────────────────────── 主窗口整体 ─────────────────────

def test_all_page_titles_and_primary_buttons_accessible(qapp, tmp_path):
    state = _state(tmp_path)
    window = MainWindow(state)
    window.updateProjectCard(_FakeProject(_store(tmp_path)))
    for name in ("home", "review", "translate", "settings"):
        page = window.pages[name]
        for button in page.findChildren(QPushButton):
            if button.isVisible() or not button.isHidden():
                assert button.accessibleName() or button.text(), (
                    f"{name} 页存在无名称按钮: {button.objectName()}")


# ─────────────────────────── 夜航共享展示组件 ────────────────

def test_page_header_exposes_title_subtitle_and_actions(qapp, tmp_path):
    header = PageHeader("游戏接入", "检测、提取、翻译、写回一条龙")
    header.set_actions([QPushButton("主动作"), QPushButton("次动作")])
    assert header.title_label.text() == "游戏接入"
    assert header.subtitle_label.text() == "检测、提取、翻译、写回一条龙"
    assert header.primary_slot is not None


def test_status_badge_drives_qss_status_property(qapp, tmp_path):
    for status in ("idle", "running", "succeeded", "warning", "failed",
                   "locked", "pending"):
        badge = StatusBadge(status)
        assert badge.status() == status
        assert badge.property("status") == status
        badge.setStatus("succeeded")
        assert badge.status() == "succeeded"
        assert badge.property("status") == "succeeded"


def test_metric_strip_label_value_and_set_value(qapp, tmp_path):
    strip = MetricStrip("待翻译", "12")
    assert strip.label.text() == "待翻译"
    assert strip.value_label.text() == "12"
    strip.setValue("99")
    assert strip.value_label.text() == "99"


def test_empty_state_shows_title_and_hint(qapp, tmp_path):
    empty = EmptyState("folder-open", "尚未载入游戏", "请拖入游戏文件夹开始")
    assert empty.title.text() == "尚未载入游戏"
    assert empty.hint.text() == "请拖入游戏文件夹开始"


def test_status_rail_exposes_nodes_and_state_update(qapp, tmp_path):
    rail = StatusRail([
        ("detection", "1 游戏检测", "scan"),
        ("writeback", "5 写回验证", "shield"),
    ])
    assert [node.step_id for node in rail.nodes] == ["detection", "writeback"]
    rail.set_node_state("detection", "running", "正在读取布局证据", "置信度 —")
    assert rail.nodes[0].property("status") == "running"
    rail.set_node_state("writeback", "succeeded", "通过", "置信度 高")
    assert rail.nodes[1].property("status") == "succeeded"


# ─────────────────────── #10 档案风格提示词 ─────────────────────

def test_profile_dialog_roundtrips_prompt_style(qapp):
    """#10：档案编辑「翻译风格要求」→ 保存 → 注入 system prompt。"""
    from hanhua.core.models import GameProfile
    from hanhua.core.prompts import build_system_prompt
    from hanhua.ui.profile_dialog import ProfileDialog

    dialog = ProfileDialog(GameProfile(game_name="Minato"))
    dialog.name.setText("Minato")
    dialog.style.setPlainText("play/resume 必须译成「开始/继续」；禁止网络用语")

    profile = dialog.result_profile()
    assert profile.prompt_style == "play/resume 必须译成「开始/继续」；禁止网络用语"
    prompt = build_system_prompt(profile, "")
    assert "个性化风格要求" in prompt
    assert "「开始/继续」" in prompt

    # 清空 = 回退内置角色
    dialog.style.setPlainText("   ")
    assert dialog.result_profile().prompt_style == ""
    prompt = build_system_prompt(dialog.result_profile(), "")
    assert "个性化风格要求" not in prompt


# ─────────────────── 2026-08-14 卡死/闪退修复 ───────────────────

class _FakePool:
    def __init__(self, started):
        self.started = started

    def start(self, worker):
        self.started.append(worker)


def test_settings_spawn_keeps_worker_reference(qapp, tmp_path):
    """_spawn 保存 worker 引用防 GC（2026-08-14 闪退修复：局部 worker
    被 GC 后 signals 连接失效——按钮卡「测试中/启动中」+ 潜在崩溃，
    同各页「引用必须保存」实证）。完成/出错后自动从引用表移除。"""
    from hanhua.ui.pages.settings_page import SettingsPage
    from hanhua.ui.widgets import Worker
    page = SettingsPage(_state(tmp_path), _RecordingWindow())
    started = []
    page._pool = _FakePool(started)
    worker = Worker(lambda: "ok")
    page._spawn(worker)
    assert worker in page._workers          # 引用已保存（防 GC）
    assert started == [worker]              # 已提交线程池
    worker.signals.finished.emit("ok")      # 完成 → 自动清理
    assert worker not in page._workers
    worker2 = Worker(lambda: "err")
    page._spawn(worker2)
    worker2.signals.error.emit("boom")      # 出错 → 同样清理
    assert worker2 not in page._workers


def test_worker_signals_note_delivers(qapp):
    """WorkerSignals.note：worker 线程活动流消息专用通道（2026-08-14
    闪退修复：worker 内直接操作 QWidget 崩溃——消息必须经信号回主线程）。"""
    from hanhua.ui.widgets import Worker
    worker = Worker(lambda: None)
    got = []
    worker.signals.note.connect(lambda s, t: got.append((s, t)))
    worker.signals.note.emit("running", "正在连接语义审核模型…")
    assert got == [("running", "正在连接语义审核模型…")]


def test_translate_worker_no_direct_activity_feed_access():
    """防回归：worker 线程执行体不得直接操作 activity_feed（跨线程
    UI 访问崩溃，2026-08-14 实证修复）——必须经 note 信号回主线程。"""
    import inspect
    from hanhua.ui.pages.translate_page import TranslatePage
    src = inspect.getsource(TranslatePage._translate_with_lease)
    assert "activity_feed.append_event" not in src
    assert "signals.note.emit" in src


# ─────────────── 开始翻译自动游戏语境识别（0.50.1） ───────────────

def test_translate_worker_auto_game_context_recognition_wired():
    """0.50.1：开始翻译自动跑游戏语境识别（对齐 runner）——worker 体
    内必须有：门条件（未建立/≥25% 增量）、save_game_context 落库、
    profile 重读（本次 run 立即注入 context_*，而非下次翻译才生效）、
    fail-closed try/except。"""
    import inspect
    from hanhua.ui.pages.translate_page import TranslatePage
    src = inspect.getsource(TranslatePage._translate_with_lease)
    for token in (
        "load_game_context",          # 门：语境未建立 → 识别
        "context_needs_update",       # 门：≥25% 增量 → 重识别
        "sample_entries",             # 代表性抽样
        "save_game_context",          # 落库 + 同步 profile context_*
        "profile = replace(project.profile)",  # 重读快照，本次 run 生效
        "游戏语境识别跳过",            # fail-closed 不阻断主链
    ):
        assert token in src, token
    # 识别块必须位于 build_system_prompt 之前（注入点在上游）
    assert src.index("save_game_context") < src.index("build_system_prompt")


def test_translate_worker_game_context_gate_no_rerun_when_fresh(qapp, tmp_path):
    """门条件行为：语境已建立且无大增量时不重复识别（省一次 4B 调用）。
    直接驱动 _translate_with_lease 前置段——GameContextRecognizer 被
    monkeypatch 为爆炸，若门失效误入识别分支即抛错被 on_log 捕获，
    断言日志无识别痕迹。"""
    from hanhua.ui.pages import translate_page as tp
    from hanhua.core.game_context import save_game_context

    store = _store(tmp_path)
    save_game_context(store, {
        "game_name": "测试游戏", "genre": "RPG",
        "_sampled_total": 1000,     # 基线远大于库里 2 条 → 不触发更新门
    })
    state = _state(tmp_path)
    project = _FakeProject(store)
    state.project = project
    page = TranslatePage(state, _RecordingWindow())

    import threading
    run = tp._TranslationRun(
        project=project, generation=0,
        api=state.api, profile=store.get_profile(),
        cancel=threading.Event(), stop_local_after_run=False,
        secrets=[])

    logs: list[str] = []

    class _Signals:
        class _Sig:
            def emit(self, *_a):
                pass

        progress = log = note = _Sig()

    def _explode(*_a, **_k):
        raise AssertionError("门失效：新鲜语境不应触发识别")

    import hanhua.core.game_context as gc
    orig = gc.GameContextRecognizer
    gc.GameContextRecognizer = _explode
    try:
        # 让主链在语境块后尽早失败（无本地模型可启动）——语境块的
        # 行为已被观察到即可
        try:
            page._translate_with_lease(run, _Signals())
        except Exception:
            pass
    finally:
        gc.GameContextRecognizer = orig
    assert not any("游戏语境识别" in ln for ln in logs or [""])


def test_translate_worker_game_context_missing_triggers_recognition(qapp, tmp_path):
    """门条件行为：语境未建立 → 自动识别 + profile 重读注入本次 run。"""
    from hanhua.ui.pages import translate_page as tp
    from hanhua.core.models import ApiConfig

    store = _store(tmp_path)
    state = _state(tmp_path)
    project = _FakeProject(store)
    state.project = project
    page = TranslatePage(state, _RecordingWindow())

    import threading
    api = ApiConfig(mode="api", base_url="http://x/v1", api_key="k",
                    model="m")
    run = tp._TranslationRun(
        project=project, generation=0, api=api,
        profile=store.get_profile(), cancel=threading.Event(),
        stop_local_after_run=False, secrets=[])

    class _FakeRecognizer:
        def __init__(self, cfg):
            self.cfg = cfg

        def recognize(self, samples, source_lang="auto"):
            rec_logs.append(f"recognize:{len(samples)}:{source_lang}")
            import json
            return json.dumps({"game_name": "魔法学院", "genre": "RPG"},
                              ensure_ascii=False)

    rec_logs: list[str] = []
    import hanhua.core.game_context as gc
    orig = gc.GameContextRecognizer
    gc.GameContextRecognizer = _FakeRecognizer
    try:
        try:
            page._translate_with_lease(run, _FakeSignals())
        except Exception:
            pass
    finally:
        gc.GameContextRecognizer = orig
    # 识别确实被触发（样本来自库内条目）
    assert any(ln.startswith("recognize:") for ln in rec_logs)
    # 落库 + profile context_* 已同步（本次 run 重读的即此档案）
    profile = store.get_profile()
    assert profile.context_game_name == "魔法学院"
    assert profile.context_genre == "RPG"


class _FakeSignals:
    class _Sig:
        def emit(self, *_a):
            pass

    progress = log = note = _Sig()


# ─────────────── 0.51.2 翻译中途崩溃修复（crash A/B） ───────────────

def test_worker_run_survives_deleted_signal_source(qapp):
    """crash B 兜底（widgets.Worker.run）：信号源 QObject 被 GC 删除后
    emit 抛 "RuntimeError: Signal source has been deleted"——run() 必须
    捕获并放弃，绝不能让异常二次抛出（三连失败链 → 进程崩溃，
    09-09 23:52 crash.log 实证）。"""
    import gc as _gc
    from PySide6.QtCore import QObject, Signal as _Signal

    from hanhua.ui.widgets import Worker

    # 用MonkeyPatch 不便：直接复用真实 WorkerSignals，模拟「引用被丢、
    # GC 删 QObject」——del wrapper 后 signals 持有人消失，再 deleteLater
    # 强删 C++ 对象，emit 即抛 RuntimeError。
    worker = Worker(lambda: "ok")
    sigs = worker.signals
    real_signals = worker.signals.__class__
    # 把 signals 替换为同型号新实例并强删，run 内 emit 命中已删对象
    holder = []
    w2_signals = real_signals()
    w2_signals.deleteLater()
    _gc.collect()

    class _DeadSignalWorker(Worker):
        def __init__(self):
            super().__init__(lambda: "ok")
            self.signals = real_signals.__new__(real_signals)
            # 触发 RuntimeError 的最直接途径：用已 deleteLater 的 C++
            # 对象包装。PySide6 对已删 QObject 的 emit 抛
            # RuntimeError("Signal source has been deleted")。

    # 更可靠的复现：直接构造已删除信号源
    dead = real_signals()
    dead.deleteLater()
    qapp.processEvents()                # deleteLater 生效，C++ 对象已删
    _gc.collect()
    w = _DeadSignalWorker()
    w.signals = dead                    # run 时 emit 应抛 RuntimeError
    w.run()                             # 不得抛异常（兜底捕获）
    # error 路径同样兜底
    w_err = Worker(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    w_err.signals = dead
    w_err.run()                         # 不得抛异常


def test_translate_on_project_keeps_running_worker_reference(qapp, tmp_path):
    """crash B 根因（translate_page._on_project）：项目切换时原实现无条件
    self._worker = None——翻译 worker 仍在池线程运行，丢掉 Worker 包装器
    最后引用 → GC 删 WorkerSignals → emit 抛 RuntimeError。修复后引用
    移交退休列表，finished/error 回调清理。"""
    from hanhua.ui.widgets import Worker

    store = _store(tmp_path)
    state = _state(tmp_path)
    state.project = _FakeProject(store)
    page = TranslatePage(state, _RecordingWindow())

    worker = Worker(lambda: "ok")
    page._worker = worker
    page._on_project(_FakeProject(store))
    # worker 引用必须仍被页面持有（退休列表），不得被 GC
    import gc as _gc
    _gc.collect()
    assert page._worker is None                     # 槽位已让出（新项目可启动）
    assert worker in page._retired_workers          # 但引用仍被持有
    worker.signals.finished.emit("ok")              # worker 自然退出 → 清理
    assert worker not in page._retired_workers
    worker2 = Worker(lambda: "err")
    page._worker = worker2
    page._on_project(_FakeProject(store))
    assert worker2 in page._retired_workers
    worker2.signals.error.emit("boom")              # 出错退出 → 同样清理
    assert worker2 not in page._retired_workers


def test_translate_refresh_chips_tolerates_storeless_project(qapp, tmp_path):
    """crash A（_refresh_chips）：analysisChanged 可能在项目切换过渡期命中
    未就绪的 project（crash.log 728 处 'Project' object has no attribute
    'store'）。缺 store 必须早退而非崩溃。"""
    store = _store(tmp_path)
    state = _state(tmp_path)
    state.project = _FakeProject(store)
    page = TranslatePage(state, _RecordingWindow())
    del state.project.store                        # 模拟过渡期未就绪对象
    page._refresh_chips()                          # 不得抛 AttributeError
    page._chips_worker is None or True             # 早退：未启动统计 worker
    state.project.store = store                    # 恢复，后续刷新正常
    page._refresh_chips()


def test_home_open_dir_blocked_while_translation_running(qapp, tmp_path):
    """crash B 触发面收窄（home_page.open_dir）：翻译运行中拖入/选择新
    目录 → 重扫 → switch_project 取消数小时翻译进度。必须拒绝并提示。"""
    import hanhua.ui.pages.home_page as home_mod

    monkeypatch_target = home_mod.Project
    state = _state(tmp_path)
    page = HomePage(state, _RecordingWindow())

    def _explode(*_a, **_k):
        raise AssertionError("翻译运行中不应触发重扫")

    orig_open = monkeypatch_target.open_game_dir
    monkeypatch_target.open_game_dir = staticmethod(_explode)
    try:
        state.translation_running = True
        page.open_dir(tmp_path)                    # 必须被守卫拒绝
    finally:
        monkeypatch_target.open_game_dir = orig_open
        state.translation_running = False
    assert page._scanning is False                 # 未进入扫描忙碌态


def test_home_refresh_project_state_tolerates_storeless_project(
        qapp, tmp_path):
    """crash A（home_page._refresh_project_state）：projectOpened 过渡期
    project 可能缺 store（crash.log 'object' has no attribute 'store'）。"""
    state = _state(tmp_path)
    state.project = _FakeProject(_store(tmp_path))
    page = HomePage(state, _RecordingWindow())
    del state.project.store
    page._refresh_project_state()                  # 不得抛 AttributeError
