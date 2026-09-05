"""首页 v3（Aurora Forge §16~18）：欢迎态 / 项目态双容器。

- 欢迎态 `welcome_panel`：大拖放区 + 运行时概要（未打开项目时的接入入口）。
- 项目态 `project_panel`：英雄区（项目名 + 主行动）、数据带（四指标）、
  健康度 + 下一步推荐、五步任务轨道、游戏档案。
打开项目后不保留大拖放框（spec：不长期保留大拖放区域）。
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtGui import (QColor, QDragEnterEvent, QDropEvent, QPainter,
                           QPen)
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QProgressBar, QPushButton,
                               QStackedWidget, QVBoxLayout, QWidget)

from hanhua.core import models
from hanhua.core.game_context import (SAMPLE_BUDGET, context_needs_update,
                                      game_context_summary,
                                      load_game_context, sample_entries)
from hanhua.core.models import (TextEntry, entry_from_row,
                                is_actionable_translation)
from hanhua.core.project import Project
from hanhua.ui.app_state import AppState
from hanhua.ui.design_system import TOKENS
from hanhua.ui.icons import LineIcon
from hanhua.ui.widgets import (MetricChip, PageHeader, StatCard, StatusRail,
                               Toast, Worker)


class _DirectoryDropZone(QFrame):
    """拖放区：任务轨道网格背景 + 五种状态（empty/drag-active/
    scanning/ready/blocked）。"""

    directoryDropped = Signal(object)
    activeChanged = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setObjectName("dropZone")
        self.setProperty("state", "empty")
        self.setMinimumHeight(150)

    @staticmethod
    def local_directory(mime_data) -> Path | None:
        if not mime_data.hasUrls():
            return None
        urls = mime_data.urls()
        if len(urls) != 1 or not urls[0].isLocalFile():
            return None
        path = Path(urls[0].toLocalFile())
        return path.resolve() if path.is_dir() else None

    def set_state(self, state: str):
        """empty / drag-active / scanning / ready / blocked。"""
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)

    def paintEvent(self, event):
        super().paintEvent(event)
        # 任务轨道网格：24px 间距、1px 线、低对比度品牌色
        painter = QPainter(self)
        pen = QPen(Qt.GlobalColor.transparent)
        pen.setColor(QColor(101, 168, 255, 9))
        pen.setWidth(1)
        painter.setPen(pen)
        spacing = 24
        for x in range(0, self.width(), spacing):
            painter.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), spacing):
            painter.drawLine(0, y, self.width(), y)
        painter.end()

    def dragEnterEvent(self, event: QDragEnterEvent):
        if self.local_directory(event.mimeData()) is not None:
            event.acceptProposedAction()
            self.activeChanged.emit(True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.activeChanged.emit(False)
        event.accept()

    def dropEvent(self, event: QDropEvent):
        self.activeChanged.emit(False)
        path = self.local_directory(event.mimeData())
        if path is None:
            event.ignore()
            return
        event.acceptProposedAction()
        self.directoryDropped.emit(path)


def _select_player_candidate(candidates) -> tuple | None:
    """多 Unity 玩家（ambiguous 布局）选择对话框。

    candidates 为 player_layout.discover_player_candidates 的结果（含
    player_root/executable/data_dir 绝对路径）。返回被选中的
    (player_root, executable) 元组；取消返回 None。
    """
    dialog = QDialog()
    dialog.setWindowTitle("选择游戏")
    dialog.setMinimumWidth(480)
    lay = QVBoxLayout(dialog)
    hint = QLabel(
        "该文件夹包含多个 Unity 游戏（检测到多个玩家布局）。\n"
        "请选择要翻译的一个：")
    hint.setWordWrap(True)
    lay.addWidget(hint)
    # QListWidget 默认无父窗口焦点策略问题：offscreen 测试下仍需
    # setFocus 才能可靠响应键盘；点击选择为主交互
    list_widget = QListWidget()
    for candidate in candidates:
        try:
            root_text = str(candidate.player_root)
            exe_text = str(candidate.executable)
        except AttributeError:
            continue
        item = QListWidgetItem(f"{exe_text}\n    （玩家目录：{root_text}）")
        item.setData(Qt.UserRole, (candidate.player_root,
                                   candidate.executable))
        list_widget.addItem(item)
    if list_widget.count() == 0:
        return None
    list_widget.setCurrentRow(0)
    list_widget.setFocus()
    lay.addWidget(list_widget, 1)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok
        | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Ok).setText("开始扫描")
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    lay.addWidget(buttons)
    list_widget.itemDoubleClicked.connect(lambda *_: dialog.accept())
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    item = list_widget.currentItem()
    if item is None:
        return None
    return item.data(Qt.UserRole)


class HomePage(QWidget):
    def __init__(self, state: AppState, window):
        super().__init__()
        self.state = state
        self.window = window
        self._scanning = False
        # 合并节点「识别」的当前状态（扫描子阶段合并，2026-08-15）
        self._scan_node_state = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 22, 28, 18)
        outer.setSpacing(14)
        col = QVBoxLayout()
        col.setSpacing(14)
        outer.addLayout(col)

        # ── 页面抬头 ──
        col.addWidget(PageHeader(
            "概览",
            "项目状态总览——拖入游戏文件夹开始，或打开项目掌握进度",
        ))

        # ── 双态容器（欢迎 / 项目） ──
        self._panels = QStackedWidget()
        self.welcome_panel = QWidget()
        self.project_panel = QWidget()
        self._panels.addWidget(self.welcome_panel)
        self._panels.addWidget(self.project_panel)
        col.addWidget(self._panels, 1)
        self.welcome_panel.setVisible(True)
        self.project_panel.setVisible(False)

        self._build_welcome_panel()
        self._build_project_panel()

        self.profile_edit_btn.clicked.connect(self._edit_profile)
        self.pick_btn.clicked.connect(self._pick_dir)
        # 设计文档 §24：游戏语境状态卡——未建立/已建立/需要更新 + 按钮
        self.context_recog_btn.clicked.connect(self._start_context_recognition)
        self.context_view_btn.clicked.connect(self._view_game_context)
        self.context_reanalyze_btn.clicked.connect(
            self._start_context_recognition)
        # #2：数据带统计后台化竞态防护——每次刷新递增 token，worker
        # 完成时 token 不符（项目已切换/更新刷新已发出）则丢弃。
        self._dashboard_token = 0
        self._dashboard_worker = None
        self._dashboard_loading = False
        # 游戏语境状态卡：后台化 token/worker 引用 + 翻译中挂起标志
        # （2026-08-22 卡顿根治，见 _refresh_context_card 注释）。
        self._context_token = 0
        self._context_worker = None
        self._context_loading = False
        self._pending_context_refresh = False
        self.drop_zone.directoryDropped.connect(self.open_dir)
        self.drop_zone.activeChanged.connect(self._set_drop_active)
        self.hero_btn.clicked.connect(lambda: self.window.navigate("translate"))
        # 双态刷新：打开项目与条目变化（翻译/审校后）都要更新
        state.projectOpened.connect(lambda _p: self._refresh_dashboard())
        state.entriesChanged.connect(self._refresh_dashboard)
        # 2026-08-22 卡顿根治：entriesChanged 不再直连 _refresh_context_card
        # ——_refresh_dashboard 末尾已调用它，直连导致每次广播双重执行；
        # 且 _count_actionable 是主线程全量 O(N) 扫描，翻译中每 ≥1s 的
        # 广播叠加万级条目 = 「每批完成十几条就卡几秒」的直接元凶
        # （_refresh_context_card 内部已有 translation_running 挂起）。
        state.projectOpened.connect(lambda _p: self._refresh_context_card())
        # 流水线 rail 实时刷新（#15）：扫描阶段事件 + 翻译/审核/写回阶段
        # 广播——此前 rail 只在 _render_report（扫描完成）更新一次，扫描
        # 与翻译全程 rail 卡在「游戏检测」节点。
        state.pipelinePhase.connect(self._on_pipeline_phase)

    # ── 欢迎态（§15 大拖放区） ────────────────────────────
    def _build_welcome_panel(self):
        lay = QVBoxLayout(self.welcome_panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        # 拖放区（任务入口）
        self.drop_zone = _DirectoryDropZone()
        dz = QVBoxLayout(self.drop_zone)
        dz.setSpacing(8)
        dz.addStretch(1)
        icon_row = QHBoxLayout()
        icon_row.addStretch(1)
        icon_row.addWidget(LineIcon("brand", 26, "#58F0C6"))
        icon_row.addSpacing(10)
        self.dz_icon = LineIcon("folder", 42)
        icon_row.addWidget(self.dz_icon)
        icon_row.addStretch(1)
        self.dz_title = QLabel("将游戏文件夹拖到此处")
        self.dz_title.setAlignment(Qt.AlignCenter)
        self.dz_title.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.dz_hint = QLabel("检测 Unity 布局、提取文本、工具交叉验证、翻译与安全写回")
        self.dz_hint.setAlignment(Qt.AlignCenter)
        self.dz_hint.setProperty("class", "subtitle")
        self.pick_btn = QPushButton("选择文件夹…")
        self.pick_btn.setProperty("primary", True)
        self.pick_btn.setFixedWidth(160)
        self.pick_btn.setMinimumHeight(48)
        self.pick_btn.setAccessibleName("选择 Unity 游戏文件夹")
        self.pick_btn.setAccessibleDescription("选择包含游戏可执行文件和 Data 目录的文件夹")
        self.pick_btn.setCursor(Qt.PointingHandCursor)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self.pick_btn)
        btn_row.addStretch(1)
        self.scan_bar = QProgressBar()
        # #14 扫描进度条：determinate 百分比（阶段加权，见
        # _on_scan_progress）；薄荷青→天蓝渐变填充由 QSS #scanBar 提供
        self.scan_bar.setObjectName("scanBar")
        self.scan_bar.setRange(0, 100)
        self.scan_bar.setValue(0)
        self.scan_bar.setTextVisible(True)
        self.scan_bar.setFormat("正在准备扫描… %p%")
        self.scan_bar.setVisible(False)
        dz.addLayout(icon_row)
        dz.addWidget(self.dz_title)
        dz.addWidget(self.dz_hint)
        dz.addLayout(btn_row)
        dz.addWidget(self.scan_bar)
        dz.addStretch(1)
        lay.addWidget(self.drop_zone)

        # 运行时概要
        self.runtime_strip = QFrame()
        self.runtime_strip.setObjectName("card")
        runtime_row = QHBoxLayout(self.runtime_strip)
        runtime_row.setContentsMargins(14, 12, 14, 12)
        runtime_row.setSpacing(10)
        self.runtime_value = MetricChip("运行时", "未检测")
        self.tool_value = MetricChip("自动工具", "待校验")
        runtime_row.addWidget(self.runtime_value)
        runtime_row.addWidget(self.tool_value)
        runtime_row.addStretch(1)
        lay.addWidget(self.runtime_strip)

    # ── 项目态（§16~18 英雄区 / 数据带 / 健康度+推荐 / 轨道 / 档案） ──
    def _build_project_panel(self):
        lay = QVBoxLayout(self.project_panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        # 英雄区（§16）：项目名 + 副题 + 主行动
        self.project_hero = QFrame()
        self.project_hero.setObjectName("heroCard")
        hero = QHBoxLayout(self.project_hero)
        hero.setContentsMargins(22, 18, 22, 18)
        hero.setSpacing(14)
        hero_text = QVBoxLayout()
        hero_text.setSpacing(4)
        self.hero_title = QLabel("项目已就绪")
        self.hero_title.setObjectName("heroTitle")
        self.hero_sub = QLabel("正在准备项目摘要…")
        self.hero_sub.setObjectName("heroSub")
        hero_text.addWidget(self.hero_title)
        hero_text.addWidget(self.hero_sub)
        hero.addLayout(hero_text, 1)
        self.hero_btn = QPushButton("开始翻译")
        self.hero_btn.setProperty("primary", True)
        self.hero_btn.setMinimumHeight(TOKENS.primary_height)
        self.hero_btn.setAccessibleName("前往运行页开始翻译")
        hero.addWidget(self.hero_btn)
        lay.addWidget(self.project_hero)

        # 数据带（2026-08-15 重做：用户要求去掉文本总数/待审核/高风险，
        # 只保留已翻译 + 选取重要信息——失败数与待人工处理数。待人工
        # 口径与审校页「待审核」胶囊同源（models.needs_review），两处
        # 数字不再互相矛盾）
        self.data_strip = QFrame()
        self.data_strip.setObjectName("dataStrip")
        strip = QHBoxLayout(self.data_strip)
        strip.setContentsMargins(16, 14, 16, 14)
        strip.setSpacing(10)
        self.stat_translated = StatCard("已翻译", 0)
        self.stat_failed = StatCard("失败", 0)
        self.stat_needs_work = StatCard("待人工处理", 0)
        for card in (self.stat_translated, self.stat_failed,
                     self.stat_needs_work):
            strip.addWidget(card, 1)
        lay.addWidget(self.data_strip)

        # 任务流水线（2026-08-15 重做：识别 → 翻译 → 审校 → 写回 →
        # 发布验证。原五节点「游戏检测/文本扫描/自动工具分析」合并为
        # 「识别」单节点；翻译与审校拆开，随 pipelinePhase 广播即时
        # 更新，不再共用「翻译质量」模糊节点）
        rail_head = QHBoxLayout()
        rail_title = QLabel("任务流水线")
        rail_title.setProperty("class", "pageTitle")
        rail_head.addWidget(rail_title)
        rail_head.addStretch(1)
        lay.addLayout(rail_head)
        definitions = (
            ("scan", "1 识别", "scan"),
            ("translation", "2 翻译", "translate"),
            ("review", "3 审校", "shield"),
            ("writeback", "4 写回", "shield"),
            ("verify", "5 发布验证", "shield"),
        )
        self.pipeline_rail = StatusRail(definitions)
        # 兼容既有测试：pipeline_cards 指向节点列表（每项含 step_id）
        self.pipeline_cards = self.pipeline_rail.nodes
        lay.addWidget(self.pipeline_rail)

        # 游戏档案（#17 重做：原 64px 单行卡易被忽视——改为两行大卡：
        # 标题行（图标 + 标题 + 生效说明 + 编辑按钮）+ 摘要多行，
        # ACCENT 渐变底与英雄区同族但更醒目；空档案时用引导文案）
        self.profile_card = QFrame()
        self.profile_card.setObjectName("profileCard")
        pc = QVBoxLayout(self.profile_card)
        pc.setContentsMargins(18, 14, 18, 14)
        pc.setSpacing(6)
        pc_head = QHBoxLayout()
        pc_head.setSpacing(10)
        pc_icon = LineIcon("profile", 28)
        pc_title = QLabel("游戏档案")
        pc_title.setProperty("class", "pageTitle")
        pc_hint = QLabel("填写后注入翻译提示词，直接影响译文风格")
        pc_hint.setProperty("class", "subtitle")
        self.profile_edit_btn = QPushButton("编辑档案")
        self.profile_edit_btn.setProperty("primary", True)
        self.profile_edit_btn.setMinimumHeight(44)
        self.profile_edit_btn.setAccessibleName("编辑当前游戏档案")
        pc_head.addWidget(pc_icon)
        pc_head.addWidget(pc_title)
        pc_head.addSpacing(6)
        pc_head.addWidget(pc_hint)
        pc_head.addStretch(1)
        pc_head.addWidget(self.profile_edit_btn)
        self.profile_summary = QLabel("尚未填写。填写后翻译将贴合本游戏的世界观与文风。")
        self.profile_summary.setProperty("class", "subtitle")
        self.profile_summary.setWordWrap(True)
        pc.addLayout(pc_head)
        pc.addWidget(self.profile_summary)
        self.profile_card.setHidden(True)
        lay.addWidget(self.profile_card)

        # 游戏语境状态卡（设计文档 §21-24：未建立/已建立/需要更新 +
        # 开始识别/重新分析/查看游戏介绍按钮。不展示 Token/推理深度/
        # 置信度等系统内部信息——§24 明确禁止）。识别在首次翻译前自动
        # 提示（§21），此处为手动入口；识别结果与翻译/审校/重排共享
        # 同一份数据（§11/§15-17）。
        self.context_card = QFrame()
        self.context_card.setObjectName("profileCard")
        cc = QVBoxLayout(self.context_card)
        cc.setContentsMargins(18, 14, 18, 14)
        cc.setSpacing(6)
        cc_head = QHBoxLayout()
        cc_head.setSpacing(10)
        cc_icon = LineIcon("profile", 28)
        cc_title = QLabel("游戏语境")
        cc_title.setProperty("class", "pageTitle")
        self.context_status = QLabel("尚未建立")
        self.context_status.setProperty("class", "subtitle")
        self.context_recog_btn = QPushButton("开始识别")
        self.context_recog_btn.setProperty("primary", True)
        self.context_recog_btn.setMinimumHeight(44)
        self.context_recog_btn.setAccessibleName("开始游戏语境识别")
        self.context_view_btn = QPushButton("查看游戏介绍")
        self.context_view_btn.setMinimumHeight(44)
        self.context_reanalyze_btn = QPushButton("重新分析")
        self.context_reanalyze_btn.setMinimumHeight(44)
        cc_head.addWidget(cc_icon)
        cc_head.addWidget(cc_title)
        cc_head.addSpacing(6)
        cc_head.addWidget(self.context_status)
        cc_head.addStretch(1)
        cc_head.addWidget(self.context_view_btn)
        cc_head.addWidget(self.context_reanalyze_btn)
        cc_head.addWidget(self.context_recog_btn)
        self.context_summary_label = QLabel(
            "翻译前建议先分析游戏背景，模型将自动获得足够的游戏语境。")
        self.context_summary_label.setProperty("class", "subtitle")
        self.context_summary_label.setWordWrap(True)
        cc.addLayout(cc_head)
        cc.addWidget(self.context_summary_label)
        self.context_card.setHidden(True)
        lay.addWidget(self.context_card)

    # ── 拖放 ──
    def _set_drop_active(self, active: bool):
        if self._scanning:
            return
        self.drop_zone.set_state("drag-active" if active else "empty")
        self.dz_title.setText(
            "松开以打开游戏" if active else "将游戏文件夹拖到此处")

    def dragEnterEvent(self, event: QDragEnterEvent):
        if self.drop_zone.local_directory(event.mimeData()) is not None:
            event.acceptProposedAction()
            self._set_drop_active(True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._set_drop_active(False)

    def dropEvent(self, event: QDropEvent):
        self._set_drop_active(False)
        path = self.drop_zone.local_directory(event.mimeData())
        if path is not None:
            event.acceptProposedAction()
            self.open_dir(path)
        else:
            event.ignore()

    def _pick_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择游戏文件夹")
        if path:
            self.open_dir(path)

    # ── 打开项目 ──
    def open_dir(self, path: Path):
        if self._scanning:
            return
        path = Path(path)
        if not path.is_dir():
            Toast.show(self, "请选择有效的文件夹", "warning")
            return
        # 多 Unity 玩家目录（一个根目录打包多个游戏，如 ned-flanders）：
        # Project 核心支持 player_root/player_executable 选择器，但此前
        # GUI 无入口——扫描直接 ambiguous blocked。这里预先探测候选，
        # >1 个时弹选择对话框，选中后带选择器重扫（同根不同玩家自动
        # 隔离到不同 DB）。
        selection = self._resolve_player_selection(path)
        if selection == "ambiguous":
            return
        player_root, player_executable = (
            selection if selection is not None else (None, None))
        self._set_busy(True)
        # 扫描事件经 Worker 信号转发（M1：event_cb 此前未接线，rail 的
        # 检测/扫描/工具分析节点全程卡在首个 running）
        signals_holder = {}

        def run_scan():
            return self._scan_worker(
                str(path), str(self.state.app_dir),
                player_root=str(player_root) if player_root is not None else None,
                player_executable=(str(player_executable)
                                   if player_executable is not None else None),
                event_cb=signals_holder["signals"].progress.emit)

        worker = Worker(run_scan)
        signals_holder["signals"] = worker.signals
        worker.signals.progress.connect(self._on_scan_progress)
        worker.signals.finished.connect(self._on_scan_done)
        worker.signals.error.connect(self._on_scan_error)
        QThreadPool.globalInstance().start(worker)

    def _resolve_player_selection(self, path: Path):
        """返回 (player_root, executable) / None（单玩家或探测失败按原
        逻辑）/ "ambiguous"（多玩家但用户取消选择）。探测失败静默降级——
        后续 scan_all 自会给出 blocked/失败事件，不在此预判。"""
        try:
            from hanhua.core.tooling.player_layout import (
                discover_player_candidates)
            candidates = discover_player_candidates(path)
        except Exception:
            return None
        if len(candidates) <= 1:
            return None
        selected = _select_player_candidate(candidates)
        if selected is None:
            Toast.show(
                self, "已取消：该文件夹包含多个游戏，请选择其中一个", "warning")
            return "ambiguous"
        return selected

    @staticmethod
    def _scan_worker(path_str: str, app_dir: str,
                     player_root: str | None = None,
                     player_executable: str | None = None,
                     event_cb=None):
        proj = Project.open_game_dir(
            path_str, app_dir,
            player_root=player_root, player_executable=player_executable)
        # B16b：csv 覆盖源列走 extract_csv_text 内自动判据（默认开）——
        # 此处传 True 会强制覆盖并绕过中文目标列在场/小表护栏（宁漏勿坏）
        report = proj.scan_all(event_cb=event_cb)
        return proj, report

    def _on_scan_progress(self, event) -> None:
        """扫描阶段事件 → rail 实时更新 + 进度条百分比（#14）。

        2026-08-15 流水线重做：detection/text_scan/tool_analysis/
        binary_scan 全部映射到合并节点「识别」——状态取最严重
        （failed > running > succeeded）。

        2026-08-20（#14）：进度条按阶段加权——检测 0-5%、结构化文本
        5-15%、二进制资源 15-85%（唯一带 current/total 的事件，线性插值
        ——大游戏 scan_v2 数分钟，是用户最需要看进度的一段）、工具分析
        与收尾 85-100%。事件到达顺序与权重起点单调对齐，条只前进不回退
        （text_scan succeeded 在 binary_scan running 之后到达时不再把
        百分比拉回去）。"""
        phase = getattr(event, "phase", "") or ""
        status = getattr(event, "status", "") or ""
        message = getattr(event, "message", "") or ""
        if not phase or not status:
            return
        if phase not in {"detection", "text_scan", "tool_analysis",
                         "binary_scan"}:
            return
        self._scan_node_state = self._merge_scan_state(
            self._scan_node_state, status)
        self.pipeline_rail.set_node_state(
            "scan", self._scan_node_state, message, "")
        self._update_scan_bar(phase, status, message, event)

    # 各阶段进度条区间终点（#14 阶段加权）：检测 5 / 文本 15 /
    # 二进制 85 / 工具分析+收尾 100。事件只在到达时取「本阶段终点」
    # 与「当前值」的较大者，保证单调不减。
    _SCAN_PHASE_WEIGHTS = {
        "detection": 5,
        "text_scan": 15,
        "binary_scan": 85,
        "tool_analysis": 100,
    }

    def _update_scan_bar(self, phase: str, status: str, message: str,
                         event) -> None:
        """PipelineEvent → scan_bar 百分比与文本（#14）。"""
        if not self._scanning:
            return
        start = {"detection": 0, "text_scan": 5,
                 "binary_scan": 15, "tool_analysis": 85}.get(phase, 0)
        end = self._SCAN_PHASE_WEIGHTS.get(phase, 100)
        if status == "failed":
            return
        if phase == "binary_scan" and status == "running":
            current = getattr(event, "current", 0) or 0
            total = getattr(event, "total", 0) or 0
            if total > 0:
                pct = start + int(
                    (end - start) * min(current, total) / total)
            else:
                pct = start
            label = f"扫描 Unity 资源 {current}/{total} · %p%"
        else:
            # 阶段 succeeded/blocked/running：直接推进到该阶段区间终点
            pct = end
            label = f"{message} · %p%" if message else "%p%"
        # 只前进不回退（阶段事件乱序到达时保持最大值）
        pct = max(pct, self.scan_bar.value())
        self.scan_bar.setFormat(label)
        self.scan_bar.setValue(pct)

    @staticmethod
    def _merge_scan_state(current: str, incoming: str) -> str:
        """合并扫描子阶段状态：failed 最严重，running 次之，其余取
        succeeded。"""
        order = {"failed": 0, "running": 1, "succeeded": 2, "pending": 3}
        rank = {v: k for k, v in order.items()}
        cur_rank = order.get(current, 99)
        inc_rank = order.get(incoming, 99)
        best = min(cur_rank, inc_rank)
        return rank.get(best, current or incoming)

    def _on_pipeline_phase(self, step_id: str, status: str,
                           detail: str, metrics: str) -> None:
        """翻译/审核/写回阶段广播 → rail 节点实时更新（#15）。"""
        self.pipeline_rail.set_node_state(
            step_id, status, detail, metrics)

    def _on_scan_done(self, result):
        proj, report = result
        self._set_busy(False)
        self.state.switch_project(proj, report)
        self._render_report(report)
        self._refresh_profile_card()
        self._refresh_context_card()
        self.window.updateProjectCard(proj)
        summary = f"{report.text_files} 个文本文件 · {report.v2_files} 个二进制资源"
        morph_warnings = [w for w in getattr(report, "warnings", ())
                          if w.startswith("未知文本形态")]
        if morph_warnings:
            Toast.show(self, "\n".join(morph_warnings), "warning")
        if report.unblocked:
            self.drop_zone.set_state("ready")
            Toast.show(self, f"分析通过：{summary}", "success")
            self.window.navigate("review")
            self._warn_skip_rate(report)
            self._warn_unexplained_gaps(report)
        else:
            self.drop_zone.set_state("blocked")
            fingerprint = getattr(report, "fingerprint", None)
            if (fingerprint is not None
                    and "ambiguous_player_layout" in getattr(
                        fingerprint, "evidence", ())):
                # 兜底：候选探测失败/未走选择对话框时仍落到这里——
                # 指明成因与出路，而非笼统的「请查看阻断步骤」
                Toast.show(
                    self,
                    "该文件夹包含多个 Unity 游戏。请重新拖入，并在弹出"
                    "的选择框中指定其中一个（或直接拖入单个游戏的子文件夹）",
                    "warning")
            else:
                Toast.show(
                    self, f"分析受限：{summary}，请查看阻断步骤", "warning")

    def _on_scan_error(self, err: str):
        self._set_busy(False)
        self.pipeline_rail.set_node_state(
            "scan", "failed", err[:80], "置信度 low")
        Toast.show(self, f"扫描失败：{err}", "error")

    def _warn_skip_rate(self, report) -> None:
        """跳过率告警（识别模块哑信号教训，2026-08-20）：大量 status=
        skipped 的候选串意味着「识别器看到了但没敢收」——此前完全静默，
        用户以为文本提全了。阈值：skipped ≥ 2000 且 ≥ 识别条目 80% 时
        弹 warning 提示去审校页看跳过原因（top 形态 + 计数），不阻断。"""
        counts = dict(getattr(report, "status_counts", ()) or {})
        skipped = counts.get("skipped", 0)
        recognized = getattr(report, "recognized_entries", 0) or 0
        if skipped < 2000 or recognized == 0 or skipped < recognized * 0.8:
            return
        reasons = getattr(report, "skipped_reasons", None) or {}
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:3]
        detail = "、".join(f"{k}（{v} 条）" for k, v in top) or "见审校页"
        Toast.show(
            self,
            f"注意：本作有 {skipped} 条候选文本被识别器跳过（{detail}）。\n"
            f"这些文本不会被翻译——如发现游戏内文本未汉化，"
            f"可能是跳过策略过严导致，请在 Issue 中反馈。",
            "warning")

    def _warn_unexplained_gaps(self, report) -> None:
        """覆盖率盲区告警（0.38.0 覆盖率接线）：census 全树普查 − 提取
        池的未解释残差 = 识别链路盲区（既没有载体覆盖、也没有规则归因
        的文本）。阈值：unexplained ≥ 50 且 ≥ census 缺口总量 10% 时弹
        warning（top 样本展示），不阻断。"""
        gaps = getattr(report, "recognition_gaps", None) or {}
        total = gaps.get("gap_total", 0)
        unexplained = gaps.get("unexplained", 0)
        if unexplained < 50 or total == 0 or unexplained < total * 0.1:
            return
        samples = gaps.get("unexplained_samples") or []
        sample_text = "\n".join(
            f"· {s[:40]}" for s in samples[:3])
        Toast.show(
            self,
            f"注意：普查发现 {unexplained} 条文本未进入识别池且无法归因"
            f"（共 {total} 条缺口）——可能是识别盲区。\n{sample_text}",
            "warning")

    def _set_busy(self, busy: bool):
        self._scanning = busy
        self.scan_bar.setVisible(busy)
        if busy:
            # #14：新扫描从 0 起（determinate 百分比）
            self.scan_bar.setValue(0)
            self.scan_bar.setFormat("正在准备扫描… %p%")
        self.pick_btn.setEnabled(not busy)
        self.drop_zone.set_state("scanning" if busy else "empty")
        self.dz_title.setText(
            "正在扫描文本与二进制资源…" if busy else "将游戏文件夹拖到此处")
        if busy:
            # 2026-08-15 流水线重做：检测节点合并进「识别」
            self._scan_node_state = "running"
            self.pipeline_rail.set_node_state(
                "scan", "running", "正在读取 Unity 布局证据", "置信度 —")

    def _render_report(self, report):
        fingerprint = report.fingerprint
        self.runtime_value.setValue(
            f"{fingerprint.runtime.upper()} · Unity {fingerprint.unity_version}")
        status_text = " · ".join(
            f"{item.tool_id} {item.state}" for item in report.tool_statuses)
        self.tool_value.setValue(status_text)
        tool_by_id = {item.tool_id: item for item in report.tool_results}
        route = {step.step_id: step for step in report.route}
        # 2026-08-15 流水线重做：原生 route step（detection/text_scan/
        # tool_analysis/translation_quality/font/writeback）映射到新五
        # 节点（识别/翻译/审校/写回/发布验证）
        scan_steps = [route.get(sid) for sid in
                      ("detection", "text_scan", "tool_analysis")
                      if route.get(sid) is not None]
        if scan_steps:
            worst = min(scan_steps, key=lambda s: (
                {"failed": 0, "blocked": 1, "running": 2,
                 "pending": 3}.get(s.status, 4)))
            self._render_route_node("scan", worst, tool_by_id)
        tq = route.get("translation_quality")
        if tq is not None:
            # 扫描完成时翻译/审校均为 pending（运行期由 pipelinePhase
            # 实时广播驱动）；翻译质量步骤的终态渲染到「翻译」节点，
            # 「审校」节点留给 pipelinePhase 的 review 广播
            self._render_route_node("translation", tq, tool_by_id)
        wb = route.get("writeback")
        if wb is not None:
            font_block = next((item for item in report.route
                               if item.required
                               and item.status in {"blocked", "failed"}
                               and item.step_id in {"font", "font_injection"}),
                              None)
            if font_block is not None:
                self._render_route_node("writeback", font_block, tool_by_id)
            else:
                self._render_route_node("writeback", wb, tool_by_id)
            # 发布验证：写回成功 → PASS；否则跟随写回状态
            self.pipeline_rail.set_node_state(
                "verify",
                "succeeded" if wb.status == "succeeded" else "pending",
                "发布副本验证通过" if wb.status == "succeeded"
                else "等待写回完成",
                "")

    def _render_route_node(self, step_id: str, step, tool_by_id) -> None:
        """单个 route 步骤 → rail 节点渲染（与原 _render_report 口径一致）。"""
        tool = tool_by_id.get(step.backend)
        # tool_results 的 tool_id 与 route.backend 非同一命名空间
        # （检测= native_fingerprint / 质量门= quality_gate / 写回=
        # native_atomic_writer），Mono 游戏 tool_results 甚至为空——
        # 必须判空，否则扫描完成必崩（C1）
        cache = ("命中" if tool is not None and tool.cache_hit is True
                 else "未命中" if tool is not None
                 and tool.cache_hit is False else "—")
        elapsed = f"{tool.elapsed_ms} ms" if tool and tool.elapsed_ms else "—"
        metrics = f"置信度 {step.confidence} · 缓存 {cache} · 耗时 {elapsed}"
        self.pipeline_rail.set_node_state(
            step_id, step.status, step.reason, metrics)

    @staticmethod
    def _entry_from_row(row: dict) -> TextEntry:
        """与翻译页同源（统一口径见 models.entry_from_row）。"""
        return entry_from_row(row)

    # ── 双态切换（§16：项目打开后隐藏大拖放框） ─────────────
    def _refresh_dashboard(self):
        """有项目 → 项目态；无项目 → 欢迎态。

        2026-08-21 防御：projectOpened 信号可能在项目切换过渡期命中，
        state.project 此刻未必是完整 Project（crash.log 实证
        AttributeError: 'object' has no attribute 'store'）。用 hasattr
        守卫，非完整项目按「无项目」处理而非崩溃。
        """
        project = self.state.project
        has_project = (project is not None
                       and getattr(project, "store", None) is not None)
        self._panels.setCurrentIndex(0 if not has_project else 1)
        self.welcome_panel.setVisible(not has_project)
        self.project_panel.setVisible(has_project)
        if not has_project:
            return
        self._refresh_project_state()
        self._refresh_context_card()
        # 翻译结束后（translation_running 已复位）的 entriesChanged 广播：
        # 语境卡挂起的刷新在这里补跑——_refresh_context_card 内部读的是
        # 复位后的标志，pending 时它本就会正常执行，无需额外分支。

    def _refresh_project_state(self):
        """数据带 + 健康度 + 推荐 + 英雄区（#2：全量统计后台线程）。"""
        project = self.state.project
        store = project.store
        self._dashboard_token += 1
        self._dashboard_loading = True
        token = self._dashboard_token
        worker = Worker(self._collect_dashboard_stats, store)
        # 引用必须保存（局部 worker 函数返回后 wrapper 引用丢失，
        # finished 连接失效——同 review_page #2 实证）。
        self._dashboard_worker = worker
        worker.signals.finished.connect(
            lambda stats: self._on_dashboard_stats(token, stats))
        worker.signals.error.connect(
            lambda err: self._on_dashboard_error(token, err))
        QThreadPool.globalInstance().start(worker)

    @staticmethod
    def _collect_dashboard_stats(store) -> tuple:
        """后台线程统计：2026-08-15 数据带重做——只算（已翻译 / 失败 /
        待人工处理）。待人工口径与审校页「待审核」胶囊同源
        （models.needs_review ∪ status=failed），两处数字一致。"""
        rows = store.get_entries()
        total = len(rows)
        translated = 0
        actionable = 0
        failed = 0
        needs_work = 0
        for row in rows:
            entry = HomePage._entry_from_row(row)
            if entry.status == "translated":
                translated += 1
                if models.needs_review(entry.meta):
                    needs_work += 1
            elif entry.status == "failed":
                # failed 算翻译失败数，同时保持原口径：failed 条目若
                # 引擎会翻（is_actionable）仍计入待翻译分母（不永久卡死）
                failed += 1
                needs_work += 1
                if is_actionable_translation(entry):
                    actionable += 1
            elif is_actionable_translation(entry):
                actionable += 1
        return (total, translated, actionable, failed, needs_work)

    def _on_dashboard_stats(self, token: int, stats: tuple) -> None:
        """后台统计完成：渲染数据带与英雄区（主线程）。"""
        if token != self._dashboard_token:
            return
        self._dashboard_loading = False
        total, translated, actionable, failed, needs_work = stats
        self.stat_translated.setValue(translated)
        self.stat_failed.setValue(failed)
        self.stat_needs_work.setValue(needs_work)

        # 英雄区（测试桩可能无 profile/game_dir，容错显示纯统计）
        project = self.state.project
        profile = getattr(project, "profile", None)
        game_dir = getattr(project, "game_dir", None)
        name = getattr(game_dir, "name", None) or "Unity 项目"
        if profile is not None:
            src = getattr(profile, "source_lang", "") or "—"
            dst = getattr(profile, "target_lang", "") or "—"
            lang = f"{src} → {dst} · "
        else:
            lang = ""
        translate_pct = (100.0 * translated / (translated + actionable)
                         if (translated + actionable) else 0.0)
        self.hero_title.setText(name)
        self.hero_sub.setText(
            f"{lang}共 {total} 条文本 · "
            f"{translated} 已翻译（{translate_pct:.0f}%）")

    def _on_dashboard_error(self, token: int, err: str) -> None:
        if token != self._dashboard_token:
            return
        self._dashboard_loading = False
        self.hero_sub.setText(f"统计数据读取失败：{err[:60]}")

    def _refresh_profile_card(self):
        self.profile_card.setHidden(False)
        p = self.state.project.profile
        # #17：摘要覆盖全部会影响翻译的字段（含 #10 个性化风格要求），
        # 让用户在概览页就能确认档案实际生效的内容
        if p.game_name or p.world_setting or p.tone_notes or p.prompt_style:
            parts = [p.game_name + (f"（{p.genre}）" if p.genre else "")]
            if p.world_setting:
                parts.append(f"世界观：{p.world_setting[:60]}{'…' if len(p.world_setting) > 60 else ''}")
            if p.tone_notes:
                parts.append(f"文风：{p.tone_notes[:60]}{'…' if len(p.tone_notes) > 60 else ''}")
            if p.prompt_style:
                parts.append(f"风格：{p.prompt_style[:60]}{'…' if len(p.prompt_style) > 60 else ''}")
            self.profile_summary.setText("　".join(parts))
        else:
            self.profile_summary.setText("尚未填写。填写后翻译将贴合本游戏的世界观与文风。")

    def _edit_profile(self):
        from hanhua.ui.profile_dialog import ProfileDialog
        dialog = ProfileDialog(self.state.project.profile, self)
        if dialog.exec():
            self.state.project.save_profile(dialog.result_profile())
            self._refresh_profile_card()
            Toast.show(self, "游戏档案已保存，下次翻译开始时生效（进行中的任务"
                             "仍使用开始时的档案）", "success")

    # ── 游戏语境识别（设计文档 §3-24） ────────────────────────
    def _refresh_context_card(self):
        """状态卡渲染：未建立 / 已建立 / 需要更新（§23 三态）。

        2026-08-22 卡顿根治：
        1. 翻译进行中（state.translation_running）挂起重刷——上下文卡
           的三态判定需要 _count_actionable 全量 O(N) 扫描，翻译中每 ≥1s
           的 entriesChanged 广播叠加万级条目会卡主线程数秒（同审校页
           _auto_reload 挂起模式），挂起并记 _pending_context_refresh，
           翻译结束广播时补跑一次；
        2. 重活（load_game_context + _count_actionable）移后台 Worker
           （token 防竞态，同 _refresh_project_state 模式），主线程只
           做纯渲染。无项目/无 store 的早退路径仍同步。
        """
        project = self.state.project
        store = getattr(project, "store", None)
        if store is None:
            self.context_card.setHidden(True)
            return
        self.context_card.setHidden(False)
        if getattr(self.state, "translation_running", False):
            # 翻译中：全量扫描挂起，等 _on_finished 的 entriesChanged 补跑
            self._pending_context_refresh = True
            return
        self._pending_context_refresh = False
        self._context_token += 1
        token = self._context_token
        self._context_loading = True
        worker = Worker(self._collect_context_state, store)
        # 引用必须保存（局部 worker 函数返回后 wrapper 引用丢失，
        # finished 连接失效——同 #2 实证）。
        self._context_worker = worker
        worker.signals.finished.connect(
            lambda result: self._on_context_state(token, result))
        worker.signals.error.connect(
            lambda err: self._on_context_state_error(token, err))
        QThreadPool.globalInstance().start(worker)

    @staticmethod
    def _collect_context_state(store) -> tuple:
        """后台线程：读 Game Context + 全量数一遍可翻译条目
        （context_needs_update 判定基线用），主线程零扫描。"""
        ctx = load_game_context(store)
        if not ctx:
            return (ctx, 0)
        actionable = 0
        for row in store.get_entries():
            entry = HomePage._entry_from_row(row)
            if is_actionable_translation(entry):
                actionable += 1
        return (ctx, actionable)

    def _on_context_state(self, token: int, result: tuple) -> None:
        """后台统计完成：渲染三态状态卡（主线程纯渲染）。"""
        if token != self._context_token:
            return
        self._context_loading = False
        ctx, actionable = result
        if not ctx:
            self.context_status.setText("尚未建立")
            self.context_recog_btn.setVisible(True)
            self.context_view_btn.setVisible(False)
            self.context_reanalyze_btn.setVisible(False)
            self.context_summary_label.setText(
                "翻译前建议先分析游戏背景，模型将自动获得足够的游戏语境。")
            return
        # 已有上下文：需更新判定（§23 新增大量文本后提示）
        # （ctx 与 actionable 都来自后台统计结果；context_needs_update
        # 内部会再读一次 KV——轻量单键读取，主线程可接受。store 重新
        # 取一次防项目切换后引用旧 store）
        store = getattr(self.state.project, "store", None)
        if store is not None and context_needs_update(store, actionable):
            status_text = "需要更新"
        else:
            status_text = "已建立"
        self.context_status.setText(status_text)
        self.context_recog_btn.setVisible(False)
        self.context_view_btn.setVisible(True)
        self.context_reanalyze_btn.setVisible(True)
        summary = game_context_summary(ctx)
        self.context_summary_label.setText(
            summary or "游戏语境已建立（点击「查看游戏介绍」查看详情）")

    def _on_context_state_error(self, token: int, err: str) -> None:
        """后台统计失败：不阻断状态卡，显示降级文案。"""
        if token != self._context_token:
            return
        self._context_loading = False
        self.context_status.setText("已建立")
        self.context_recog_btn.setVisible(False)
        self.context_view_btn.setVisible(True)
        self.context_reanalyze_btn.setVisible(True)
        self.context_summary_label.setText(
            "游戏语境已建立（详情读取失败）")

    def _resume_context_refresh_if_pending(self):
        """翻译结束（translation_running 复位）后的 entriesChanged
        广播路径补跑挂起的状态卡刷新（_on_finished 已复位标志，这里
        由普通 _refresh_context_card 调用路径自然覆盖，此方法供
        showEvent/显式刷新兜底）。"""
        if self._pending_context_refresh:
            self._refresh_context_card()

    def _start_context_recognition(self):
        """开始/重新分析游戏语境（Worker 后台识别，复制 translate_tool_page
        模式：Worker 内 ensure_running → create_client → chat → 解析）。"""
        project = self.state.project
        store = getattr(project, "store", None)
        if store is None:
            Toast.show(self, "请先打开游戏项目", "warning")
            return
        api = self.state.api
        if api.mode == "api" and not (api.base_url and api.api_key
                                      and api.model):
            Toast.show(self, "请先在设置中配置 API 模型", "warning")
            return
        # 取样（后台线程做全量扫描 + 分类抽样）
        self.context_status.setText("正在识别…")
        self.context_recog_btn.setEnabled(False)
        self.context_reanalyze_btn.setEnabled(False)
        self.context_summary_label.setText(
            "正在分析代表性文本（UI 对白/任务/物品/技能/剧情…）…")
        worker = Worker(self._recognize_worker, api, store,
                        self.state.resource_dir, self.state.local_model)
        # 引用必须保存（worker 局部变量会丢 wrapper）
        self._context_worker = worker
        worker.signals.finished.connect(self._on_context_done)
        worker.signals.error.connect(self._on_context_error)
        QThreadPool.globalInstance().start(worker)

    @staticmethod
    def _recognize_worker(api, store, resource_dir, local_model):
        """后台识别线程：取样 → 本地/云端统一识别 → 解析 → 落库。

        返回 (ctx: dict, raw: str)。失败抛异常由 error 信号处理。
        """
        from hanhua.core.game_context import (GameContextRecognizer,
                                              parse_game_context,
                                              save_game_context)
        rows = store.get_entries()
        samples = sample_entries(rows)
        if not samples:
            raise RuntimeError("没有可识别的文本样本（项目为空）")
        config = api
        if api.mode == "local":
            # 2026-08-31 语境识别改用 4B 审核模型：1.8B 翻译模型输出
            # JSON schema 能力弱，识别结果常为空/纯「未知」（黑屏空介绍
            # 与「没有实际作用」的直接根因）。走与审核相同的
            # ReviewModelService（review_runtime.json 独立签名/端口 8081、
            # --reasoning off 稳定输出 JSON，与翻译 1.8B 互不干扰）；4B
            # 缺失（只装了翻译模型）时同链路报「审核模型缺失」——识别
            # 失败走 error 降级，不阻断翻译。
            from hanhua.core.review_server import ReviewModelService
            service = ReviewModelService(resource_dir)
            try:
                info = service.ensure_running()
            except Exception as exc:  # noqa: BLE001 识别服务不可用 → 明确报错
                raise RuntimeError(
                    f"游戏语境识别需要 4B 审核模型（本地模式）：{exc}") from exc
            from hanhua.core.models import ApiConfig
            from dataclasses import replace
            config = replace(api, base_url=info["base_url"],
                             api_key=info["api_key"],
                             model="game-context")
        # 原文语言：从游戏档案读取（store 是 ProjectStore，只有 get_profile()）
        profile = store.get_profile()
        source_lang = getattr(profile, "source_lang", "") or "auto"
        recognizer = GameContextRecognizer(config)
        raw = recognizer.recognize(samples, source_lang=source_lang)
        ctx = parse_game_context(raw)
        ctx["_sampled_total"] = len(rows)
        save_game_context(store, ctx)
        return ctx, raw

    def _on_context_done(self, result):
        ctx, _raw = result
        self.context_recog_btn.setEnabled(True)
        self.context_reanalyze_btn.setEnabled(True)
        # 识别完成时 translation_running 可能为 True（识别入口在翻译中
        # 仍可用）——_refresh_context_card 会挂起并记 pending，由翻译
        # 结束广播补跑；非翻译中立即刷新。
        self._pending_context_refresh = True
        self._resume_context_refresh_if_pending()
        from hanhua.core.game_context import game_context_summary
        summary = game_context_summary(ctx)
        Toast.show(
            self,
            f"游戏语境已建立：{summary or '识别完成'}", "success")

    def _on_context_error(self, err: str):
        self.context_recog_btn.setEnabled(True)
        self.context_reanalyze_btn.setEnabled(True)
        self.context_status.setText("识别失败")
        self.context_summary_label.setText(
            f"识别失败：{err[:80]}（可直接开始翻译，游戏语境将不注入）")
        Toast.show(self, f"游戏语境识别失败：{err}", "error")

    def _view_game_context(self):
        """查看游戏介绍（§11 用户可见的游戏介绍 = 同一份 Game Context 数据）。"""
        from hanhua.ui.game_context_dialog import GameContextDialog
        store = getattr(self.state.project, "store", None)
        ctx = load_game_context(store) if store is not None else {}
        dialog = GameContextDialog(ctx, self)
        dialog.exec()
