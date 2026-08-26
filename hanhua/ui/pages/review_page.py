"""审校页 v3（2026-08-20 #16）：双栏工作区——文本列表 / 翻译工作区。

筛选：状态胶囊（全部/待翻译/已翻译/待审核/已重译/失败/已锁定）+
搜索框 + 来源文件下拉（#16：译文一多难以浏览，按来源文件筛选——
选项由当前条目动态聚合，reload 后保留选择）。左栏列表沿用表格模型
（EntryTableModel 六列 + 筛选 + 右键菜单，护栏契约不变）；中栏展示
选中条目的原文、译文编辑与上下文（§24 Context 区域）。选中联动：
表格 selectionChanged → 中栏刷新；保存/锁定后恢复选中，避免
reload 丢焦点。
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import (QAbstractTableModel, QModelIndex,
                            QSortFilterProxyModel, Qt, QThreadPool, QTimer)
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox,
                               QComboBox, QFrame, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMenu, QPlainTextEdit,
                               QPushButton, QSplitter, QStackedLayout,
                               QStyledItemDelegate, QTableView, QVBoxLayout,
                               QWidget)

from dataclasses import replace

from hanhua.core.agent_memory import AgentMemory
from hanhua.core.glossary import GlossaryStore
from hanhua.core.manual_correction import manual_correction
from hanhua.core import models
from hanhua.core.models import (TextEntry, entry_from_row,
                                is_actionable_translation)
from hanhua.core.reviewer import review_entries
from hanhua.core.translator import (create_client, translate_interactive,
                                    merge_translation_references)
from hanhua.ui.app_state import AppState
from hanhua.ui import theme
from hanhua.ui.design_system import TOKENS
from hanhua.ui.widgets import (FilterChip, STATUS_COLOR,
                               STATUS_TEXT, EmptyState, PageHeader,
                               StatusBadge, Toast, Worker)

# 审核流程落盘的 review_level → 判定文案（§27 语义换算）
_REVIEW_LEVEL_TEXT = {
    "PASS": "通过", "MINOR": "轻微建议", "MAJOR": "较大问题",
    "CRITICAL": "严重问题", "RETRANSLATED": "已重译",
}


def _row_meta(row: dict) -> dict:
    raw = row.get("meta", {})
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


# 审核待处理终态（#38/#47）：「待审核」筛选/胶囊以此为准。
# 2026-08-15 数字关系统一：口径收敛到 models.REVIEW_PENDING_OUTCOMES /
# models.needs_review（概览「待人工」卡与审校页胶囊同一套判定），
# 此处保留别名兼容既有引用。
_REVIEW_PENDING_OUTCOMES = set(models.REVIEW_PENDING_OUTCOMES)


def _needs_review(meta: dict) -> bool:
    """待审核判定：review_outcome 终态未收敛，或遗留 review_issue。"""
    return models.needs_review(meta)


def _display_status(row: dict) -> str:
    """状态列显示：审核态优先（#47 全量审校后状态真相在终态而非机械态）。

    优先级：已重译（重译收敛待人工确认）→ 已通过（APPROVED 系）→
    待审核（未收敛终态）→ 机械状态。修复「受限/已翻译」无法区分
    BLOCKED/待确认的显示盲区（原状态列只有 4 个机械态）。
    """
    meta = _row_meta(row)
    if meta.get("retranslated"):
        return "retranslated"
    outcome = meta.get("review_outcome")
    if outcome in ("APPROVED", "APPROVED_MINOR"):
        return "approved"
    if outcome in _REVIEW_PENDING_OUTCOMES:
        return "needs_review"
    return row["status"]


def _is_sample_row(row: dict) -> bool:
    """留档样本行（识别 L1/R5）：meta 含 skipped_count（提取器限量
    样本标志，回写后仍保留）。样本无 file_offset/定位键，写回永远
    失败；人工翻译还会污染跳过统计与导出——审校页禁止编辑。"""
    return "skipped_count" in _row_meta(row)


class StatusDelegate(QStyledItemDelegate):
    """状态列：6px 语义色圆点 + 文字。"""

    def paint(self, painter: QPainter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        status = index.data(Qt.DisplayRole) or "pending"
        color = QColor(STATUS_COLOR.get(status, theme.TEXT_DISABLED))
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(option.rect.left() + 14, option.rect.center().y() - 3, 6, 6)
        painter.setPen(QPen(QColor(theme.TEXT_SECONDARY)))
        painter.drawText(option.rect.adjusted(28, 0, -6, 0), Qt.AlignVCenter | Qt.AlignLeft,
                         STATUS_TEXT.get(status, status))
        painter.restore()


class ReasonDelegate(QStyledItemDelegate):
    """失败原因列：仅失败行显示珊瑚色原因，其余显示占位符。"""

    def paint(self, painter: QPainter, option, index):
        painter.save()
        source = index.model().mapToSource(index)
        row = source.model()._rows[source.row()]
        failed = row.get("status") == "failed"
        reason = index.data(Qt.DisplayRole) or ""
        if failed and reason:
            painter.setPen(QPen(QColor(theme.ERROR)))
        else:
            painter.setPen(QPen(QColor(theme.TEXT_DISABLED)))
            reason = reason or "—"
        painter.drawText(option.rect.adjusted(10, 0, -6, 0),
                         Qt.AlignVCenter | Qt.AlignLeft, reason)
        painter.restore()


class ColumnDividerDelegate(QStyledItemDelegate):
    """在指定列右缘画 1px 分隔轨（原文/译文对照带）。"""

    def paint(self, painter: QPainter, option, index):
        super().paint(painter, option, index)
        painter.save()
        painter.setPen(QPen(QColor(theme.BORDER)))
        painter.drawLine(option.rect.right(), option.rect.top(),
                         option.rect.right(), option.rect.bottom())
        painter.restore()


class EntryTableModel(QAbstractTableModel):
    """审校表格：状态 / 来源 / 原文 / 译文 / 失败原因 / 锁定。

    置信度、角色等识别元数据对普通用户不可操作，已从表格移除（仍保留在
    meta 中供筛选与写回判断使用）。
    """
    # #43 阶段 G：风险列（重构指令 §13）——risk_level/risk_score 透出，
    # 无风险字段显示 —（旧记录/未评估条目不打扰）
    COLS = ["状态", "来源", "原文", "译文", "失败原因", "风险", "锁定"]

    def __init__(self, state: AppState):
        super().__init__()
        self.state = state
        self._rows: list[dict] = []
        # 经验记忆（AgentMemory）：人工修正回流用，懒创建（同
        # translate_page 模式，app_dir/agent_memory.db）
        self._agent_memory: AgentMemory | None = None
        # 错误模式库（#43 阶段 B）：人工修正沉淀，懒创建（同模式）
        self._error_patterns = None

    def setEntries(self, rows: list[dict]):
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.COLS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        meta = _row_meta(row)
        col = index.column()
        if role == Qt.DisplayRole:
            if col == 0:
                # #47：审核态优先（已重译/已通过/待审核），无审核证据回退机械态
                return _display_status(row)
            if col == 1:
                # 来源列显示短路径，完整路径放 tooltip
                source = meta.get("source", row["file_id"])
                if isinstance(source, str) and source:
                    return Path(source).name
                return source
            if col == 2:
                return row["original"]
            if col == 3:
                return row["translation"]
            if col == 4:
                reasons = meta.get("quality_reasons", [])
                if isinstance(reasons, list):
                    return "、".join(str(reason) for reason in reasons)
                return str(reasons) if reasons else ""
            if col == 5:
                # 风险列：risk_score + risk_level（有字段才显示）
                score = meta.get("risk_score")
                level = meta.get("risk_level") or ""
                if isinstance(score, (int, float)):
                    return f"{int(score)} {level}".strip()
                return level or "—"
        if role == Qt.EditRole and col == 3:
            return row["translation"]
        # #48 富文本纯文本化：Qt 6 移除了 TextFormatRole（PySide6 6.11
        # Qt.ItemDataRole 无此枚举）——该 role 视图不再查询，原分支为
        # 死代码且引用已删枚举会崩溃。原文/译文含富文本标签的纯文本
        # 显示由 DisplayRole 返回原串 + 视图默认纯文本处理保证。
        if role == Qt.CheckStateRole and col == 6:
            return Qt.Checked if row["locked"] else Qt.Unchecked
        if role == Qt.ToolTipRole and col == 1:
            return meta.get("source", row["file_id"])
        if role == Qt.ToolTipRole and col == 2:
            return row["original"]
        if role == Qt.ToolTipRole and col == 4:
            reasons = meta.get("quality_reasons", [])
            return "\n".join(str(reason) for reason in reasons) if isinstance(reasons, list) else str(reasons or "")
        return None

    def setData(self, index, value, role=Qt.EditRole):
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.EditRole and col == 3:
            # 留档样本行禁止人工翻译（无定位键写回失败 + 污染跳过统计）
            if _is_sample_row(row):
                return False
            text = str(value).strip()
            # #8：相同文本也允许保存——用户「确认/复核」语义同样有效
            # （幂等写入；人工修正终态照常落盘）。旧逻辑 text == 当前
            # 译文直接拒绝 → 保存必失败并回填旧文本（用户 2 秒回退
            # 的另一来源）。
            # Phase B-2（审计 §6 P1-6）：人工修正统一回流——清旧审核
            # 状态、写 MANUAL/APPROVED 终态、提交/撤销记忆、经验记忆
            # 最高权重写入、矢量 outbox + 审计日志
            try:
                profile = self.state.profile
            except (AttributeError, RuntimeError):
                profile = None
            lang = (f"{getattr(profile, 'source_lang', '') or 'auto'}→"
                    f"{getattr(profile, 'target_lang', '') or 'zh-CN'}")
            game_name = str(getattr(profile, "game_name", "") or "")
            if self._agent_memory is None:
                self._agent_memory = AgentMemory(
                    self.state.app_dir / "agent_memory.db")
                self._agent_memory.init_schema()
            if self._error_patterns is None:
                from hanhua.core.error_patterns import ErrorPatternStore
                self._error_patterns = ErrorPatternStore(
                    self.state.app_dir / "error_patterns.db")
            try:
                manual_correction(
                    self.state.project.store,
                    row["file_id"], row["key_path"], text,
                    model=str(self.state.api.model or ""), lang=lang,
                    agent_memory=self._agent_memory,
                    game_name=game_name,
                    error_patterns=self._error_patterns)
            except Exception:
                # 回流失败不应让审校编辑失败：退回基础写入（仍原子
                # 清旧审核状态 + 写人工终态），记忆/日志尽力而为
                self.state.project.store.apply_manual_correction(
                    row["file_id"], row["key_path"], text)
            persisted = next(
                item for item in self.state.project.store.get_entries()
                if item["file_id"] == row["file_id"]
                and item["key_path"] == row["key_path"]
            )
            for field in ("translation", "status", "meta"):
                row[field] = persisted[field]
            self.dataChanged.emit(
                self.index(index.row(), 0), self.index(index.row(), 6))
            self.state.entriesChanged.emit()
            return True
        if role == Qt.CheckStateRole and col == 6:
            locked = value == Qt.Checked
            self.state.project.store.set_locked(row["file_id"], row["key_path"], locked)
            row["locked"] = locked
            self.dataChanged.emit(index, index)
            return True
        return False

    def flags(self, index):
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == 3:
            # 留档样本行（meta 含 skipped_count）不可编辑：无定位键写回
            # 永远失败，人工翻译污染跳过统计与导出
            if not _is_sample_row(self._rows[index.row()]):
                f |= Qt.ItemIsEditable
        if index.column() == 6:
            f |= Qt.ItemIsUserCheckable
        return f


class EntryFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.search = ""
        self.status = ""
        self.file_id = ""
        self.locked_only = False

    def setFilters(self, search="", status="", file_id="", locked_only=False):
        self.search = search.lower()
        self.status = status
        self.file_id = file_id
        self.locked_only = locked_only
        self.beginFilterChange()
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, row, parent):
        # 2026-08-19 卡顿修复：行级缓存解析后的 meta 与小写搜索串——
        # _row_meta 每行每次过滤都 json.loads（万行 × 每次筛选/每个防抖
        # 前的按键），.lower() 同样按调用重算。缓存以 (id(row-dict)) 为
        # 键挂在模型行上（setEntries 换新行 dict 自然失效），复杂度从
        # O(过滤次数×N×json解析) 降为 O(N×json解析) + O(过滤次数×N)。
        model: EntryTableModel = self.sourceModel()
        r = model._rows[row]
        cached = r.get("_filter_cache")
        if cached is None or cached[0] is not r.get("meta"):
            cached = (r.get("meta"), _row_meta(r),
                      r["original"].lower(),
                      (r["translation"] or "").lower())
            r["_filter_cache"] = cached
        if self.search and self.search not in cached[2] \
                and self.search not in cached[3]:
            return False
        if self.status == "needs_review":
            # #47：待审核 = 未收敛终态（NEEDS_REVISION/BLOCKED/
            # REVIEW_ERROR）∪ 遗留 review_issue ∪ 机械失败——原「审核」
            # 胶囊并入（质量门未过/被标记/高风险最终都落未收敛终态，
            # 两胶囊展示同一集合，重复展示合并为单一胶囊）
            if not (_needs_review(cached[1]) or r["status"] == "failed"):
                return False
        elif self.status == "retranslated":
            # #47：重译收敛待人工确认（有问题的文本重返审校）
            if not cached[1].get("retranslated"):
                return False
        elif self.status == "pending":
            # #2/#8：待翻译 = 引擎实际会翻的条目（与翻译页 chips 同源
            # 口径 is_actionable_translation）。low 置信度留档（引擎消息/
            # 疑似噪音，跳过翻译）与 locked/structural 等不显示在待翻译
            # 胶囊——跳过的文本不进「待翻译」。
            if not is_actionable_translation(entry_from_row(r)):
                return False
        elif self.status and r["status"] != self.status:
            return False
        if self.file_id and r["file_id"] != self.file_id:
            return False
        if self.locked_only and not r["locked"]:
            return False
        return True


class ReviewPage(QWidget):
    def __init__(self, state: AppState, window):
        super().__init__()
        self.state = state
        self.window = window
        self._loading = True
        self._current_row: int | None = None
        # #2：reload 后台化竞态防护——每次 reload 递增 token，worker 完成
        # 时 token 不符（项目已切换/更新 reload 已发出）则丢弃结果。
        self._reload_token = 0
        # #38：单条「重新审核」worker 状态（token 防重入 + 引用保存防
        # wrapper 丢失，同 _reload_worker 模式）
        self._review_token = 0
        self._review_running = False
        self._review_worker = None
        # AI 翻译填充（2026-08-15）：防重入 + worker 引用保存
        self._ai_translating = False
        self._ai_translate_worker = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 18)
        lay.setSpacing(12)

        # ── 页面抬头（操作按钮放入右上角动作区） ──
        header = PageHeader("文本审校", "三栏工作区：筛选文本、精修译文、查看 AI 审核")
        self.translate_btn = QPushButton("开始翻译 →")
        self.translate_btn.setProperty("primary", True)
        self.translate_btn.setMinimumHeight(48)
        self.translate_btn.setAccessibleName("进入自动翻译")
        self.translate_btn.setCursor(Qt.PointingHandCursor)
        header.set_actions([self.translate_btn])
        lay.addWidget(header)

        # ── 工具栏（搜索框 + 来源文件下拉；状态筛选收敛到下方胶囊） ──
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("搜索原文或译文…（Ctrl+F）")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setMinimumHeight(44)
        self.search_box.setAccessibleName("搜索原文或译文")
        toolbar.addWidget(self.search_box, 1)
        # #16 来源文件筛选：译文一多难以浏览，按来源文件聚焦一个文件
        # 的条目。选项由 _on_reload_rows 从当前条目聚合（见 _refresh_file_filter）
        self.file_filter_box = QComboBox()
        self.file_filter_box.addItem("全部来源", "")
        self.file_filter_box.setMinimumHeight(44)
        # 宽度给足短文件名（列表本体走下拉弹层，不挤占搜索框）
        self.file_filter_box.setMinimumContentsLength(18)
        self.file_filter_box.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.file_filter_box.setToolTip("按来源文件筛选列表")
        self.file_filter_box.setAccessibleName("按来源文件筛选")
        self.file_filter_box.currentIndexChanged.connect(
            self._on_file_filter_changed)
        toolbar.addWidget(self.file_filter_box)
        lay.addLayout(toolbar)

        # ── 筛选胶囊（§21 精简：唯一状态筛选入口） ──
        chip_row = QHBoxLayout()
        chip_row.setSpacing(8)
        self.filter_chips: dict[str, FilterChip] = {}
        _chip_group = QButtonGroup(self)
        _chip_group.setExclusive(True)
        for key, label, kind in (
            ("all", "全部", ""),
            ("pending", "待翻译", ""),
            ("translated", "已翻译", ""),
            # #47（2026-08-14）：「审核」「待审核」两胶囊展示同集合
            # （未收敛终态 ≈ 质量门未过/被标记），合并为单一「待审核」——
            # 含机械失败（失败条目不审即留，等同待办）。「已重译」为
            # 重译收敛待人工确认的新增筛选（有问题的文本重返审校）。
            ("needs_review", "待审核", ""),
            ("retranslated", "已重译", ""),
            ("failed", "失败", ""),
            ("locked", "已锁定", ""),
        ):
            chip = FilterChip(label, value=key, kind=kind)
            chip.setMinimumHeight(TOKENS.control_height)
            _chip_group.addButton(chip)
            chip.clicked.connect(self._on_chip_clicked)
            self.filter_chips[key] = chip
            chip_row.addWidget(chip)
        self.filter_chips["all"].setChecked(True)
        chip_row.addStretch(1)
        self.summary_label = QLabel("")
        self.summary_label.setProperty("class", "subtitle")
        chip_row.addWidget(self.summary_label)
        lay.addLayout(chip_row)

        # ── 三栏工作区（§21：25% / 50% / 25%） ──
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        lay.addWidget(self.splitter, 1)

        # ── 左栏：文本列表（表格模型 + 筛选 + 右键菜单，契约不变） ──
        self.model = EntryTableModel(state)
        self.proxy = EntryFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        # #8：内联编辑期间 reload 挂起（编辑完成关闭 editor 后补跑）。
        # PySide6 中 closeEditor 信号在 itemDelegate 上（view 上同名属性是
        # protected 方法，不是信号）。
        self._pending_reload = False
        # 2026-08-14 卡顿优化：页面不可见期间广播触发的 reload 置脏，
        # 切回页面（showEvent）时补跑——隐藏页全量重建纯浪费
        self._reload_dirty = False
        self.table.itemDelegate().closeEditor.connect(self._on_editor_closed)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.resizeSection(0, 74)
        for column, width in ((1, 110), (4, 100), (5, 44)):
            header.setSectionResizeMode(column, QHeaderView.Fixed)
            header.resizeSection(column, width)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setItemDelegateForColumn(0, StatusDelegate(self.table))
        # 原文列右缘画 1px 分隔轨，与译文列形成对照带
        self.table.setItemDelegateForColumn(2, ColumnDividerDelegate(self.table))
        self.table.setItemDelegateForColumn(4, ReasonDelegate(self.table))
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_menu)
        self.empty_state = EmptyState(
            "database", "暂无文本条目", "接入游戏后，识别出的原文与译文会出现在这里")
        self._stack = QStackedLayout()
        self._stack.addWidget(self.table)
        self._stack.addWidget(self.empty_state)
        self._stack.setCurrentWidget(self.table)
        self._stack_host = QWidget()
        self._stack_host.setLayout(self._stack)
        self.splitter.addWidget(self._stack_host)

        # ── 中栏：翻译工作区（原文 / 译文编辑 / 上下文 / 质量门） ──
        self._detail_stack = QStackedLayout()
        self.detail_panel = self._build_detail_panel()
        self.detail_empty = EmptyState(
            "database", "选择条目查看详情",
            "在左侧列表选中一条文本，这里展示原文、译文与上下文")
        self._detail_stack.addWidget(self.detail_panel)
        self._detail_stack.addWidget(self.detail_empty)
        self._detail_stack.setCurrentWidget(self.detail_empty)
        self._detail_host = QWidget()
        self._detail_host.setLayout(self._detail_stack)
        self.splitter.addWidget(self._detail_host)

        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([380, 640])

        self.search_box.textChanged.connect(self._on_search_changed)
        self.translate_btn.clicked.connect(lambda: self.window.navigate("translate"))
        # Ctrl+F 聚焦搜索框（搜索框 placeholder 已提示）
        _search_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        _search_shortcut.activated.connect(self._focus_search)
        # 选中联动：左栏选中 → 中栏/右栏刷新
        self.table.selectionModel().selectionChanged.connect(
            self._on_selection_changed)

        self.state.projectOpened.connect(lambda _p: self.reload())
        self.state.entriesChanged.connect(self._auto_reload)
        self.reload()

    # ── 中栏构建 ───────────────────────────────────────────
    def _build_detail_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("detailPanel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.detail_badge = StatusBadge("pending")
        self.detail_source = QLabel("")
        self.detail_source.setObjectName("detailSection")
        head.addWidget(self.detail_badge)
        head.addWidget(self.detail_source, 1)
        lay.addLayout(head)

        lay.addWidget(self._section_label("原文"))
        self.detail_original = QLabel("")
        self.detail_original.setObjectName("detailOriginal")
        self.detail_original.setWordWrap(True)
        self.detail_original.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # #48：原文含富文本标签（<b>…</b>），AutoText 会把标签渲染成
        # 粗体——用户看到「无标签原文」误以为审校模型看到的也是纯文本
        #（实际模型 prompt 带标签）。强制 PlainText 原样显示，与模型
        # 输入一致，标签结构可核对。
        self.detail_original.setTextFormat(Qt.PlainText)
        lay.addWidget(self.detail_original)

        lay.addWidget(self._section_label("译文"))
        self.detail_edit = QPlainTextEdit()
        self.detail_edit.setObjectName("detailEdit")
        self.detail_edit.setPlaceholderText("在此输入或修改译文…")
        self.detail_edit.setFixedHeight(96)
        # #8：编辑 dirty 追踪——改过未保存时 reload/重填不覆盖用户输入
        self._detail_dirty = False
        self.detail_edit.textChanged.connect(
            lambda: setattr(self, "_detail_dirty", True))
        lay.addWidget(self.detail_edit)

        ops = QHBoxLayout()
        ops.setSpacing(8)
        self.save_btn = QPushButton("保存译文")
        self.save_btn.setMinimumHeight(TOKENS.control_height)
        self.save_btn.clicked.connect(self._save_detail)
        # 内联保存反馈（§23：保存后「已保存」，1500ms 后自动清空）
        self.save_feedback = QLabel("")
        self.save_feedback.setObjectName("saveFeedback")
        self.save_feedback.setMinimumHeight(TOKENS.control_height)
        self.save_feedback.setAlignment(Qt.AlignVCenter)
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.setInterval(1500)
        self._feedback_timer.timeout.connect(
            lambda: self.save_feedback.setText(""))
        ops.addWidget(self.save_feedback)
        self.copy_src_btn = QPushButton("复制原文")
        self.copy_src_btn.setMinimumHeight(TOKENS.control_height)
        self.copy_src_btn.clicked.connect(
            lambda: self._copy(self.detail_original.text()))
        self.copy_tr_btn = QPushButton("复制译文")
        self.copy_tr_btn.setMinimumHeight(TOKENS.control_height)
        self.copy_tr_btn.clicked.connect(
            lambda: self._copy(self.detail_edit.toPlainText()))
        # 2026-08-15 用户要求：失败文本的 AI 翻译填充——与复制按钮
        # 平行，用翻译模型对原文直接翻译并填充译文框（中文显式指令 +
        # 术语库引用，2026-08-26 修复回显误报），人工确认后保存。
        self.ai_translate_btn = QPushButton("AI 翻译")
        self.ai_translate_btn.setMinimumHeight(TOKENS.control_height)
        self.ai_translate_btn.setToolTip(
            "用翻译模型对原文直接翻译并填充译文框"
            "（中文显式指令 + 术语库引用，人工确认后保存）")
        self.ai_translate_btn.clicked.connect(self._ai_translate_current)
        self.lock_check = QCheckBox("锁定（不翻译）")
        self.lock_check.setMinimumHeight(TOKENS.control_height)
        self.lock_check.toggled.connect(self._toggle_detail_lock)
        # #38：单条人工「重新审核」——修复译文后强制送审（force_send
        # 绕过分流直放），PASS → APPROVED 可写回；MAJOR/CRITICAL →
        # NEEDS_REVISION 保留等待人工。
        self.review_btn = QPushButton("重新审核")
        self.review_btn.setMinimumHeight(TOKENS.control_height)
        self.review_btn.setToolTip(
            "对当前译文重新执行 AI 审核：通过即可写回发布；"
            "未通过则按审核意见修改后再次点击")
        self.review_btn.clicked.connect(self._review_current)
        self.review_btn.setEnabled(False)
        for w in (self.save_btn, self.copy_src_btn, self.copy_tr_btn,
                  self.ai_translate_btn, self.lock_check, self.review_btn):
            ops.addWidget(w)
        lay.addLayout(ops)

        lay.addWidget(self._section_label("上下文"))
        self.detail_context = QLabel("")
        self.detail_context.setObjectName("detailContext")
        self.detail_context.setWordWrap(True)
        lay.addWidget(self.detail_context)

        lay.addWidget(self._section_label("质量门"))
        self.detail_reason = QLabel("")
        self.detail_reason.setObjectName("detailReason")
        self.detail_reason.setWordWrap(True)
        lay.addWidget(self.detail_reason)
        lay.addStretch(1)
        return panel

    @staticmethod
    def _section_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("detailSection")
        return label

    # ── 选中联动 ───────────────────────────────────────────
    def _on_selection_changed(self, selected, _deselected):
        # 用户主动切行 = 放弃未保存编辑；挂起的 reload 补跑
        self._detail_dirty = False
        indexes = selected.indexes()
        if self._pending_reload:
            # 切行优先：reload 补跑会把选中恢复到旧行（_current_row），
            # 吞掉用户切行意图——先在旧模型上取目标主键，补跑后按主键
            # 找回重选（#8）。
            self._pending_reload = False
            target = None
            if indexes:
                src = self.proxy.mapToSource(indexes[0]).row()
                if 0 <= src < len(self.model._rows):
                    row = self.model._rows[src]
                    target = (row["file_id"], row["key_path"])
            self.reload()
            if target is not None:
                self._restore_selection(*target)
            return
        if not indexes:
            self._current_row = None
            self._detail_stack.setCurrentWidget(self.detail_empty)
            return
        # 映射修正（#16 实证：点任意行都显示同一个 Animation Track）——
        # indexes 来自视图 selectionModel，视图 model 是 proxy；此前
        # mapToSource 误收 self.model.index(proxy_row, 0)（源模型索引），
        # 行号错位导致永远指向错误行。indexes[0] 本身就是 proxy 索引，
        # 直接映射即可。
        src = self.proxy.mapToSource(indexes[0])
        self._current_row = src.row()
        self._fill_detail(self.model._rows[self._current_row])

    def _fill_detail(self, row: dict):
        # #8：用户正在编辑（焦点在编辑框）或改过未保存（dirty）时不覆盖
        # ——后台翻译每批完成/保存触发的 reload → 重选行 → 重填会把
        # 正在输入的内容 2 秒打回原文本。
        if self.detail_edit.hasFocus() or self._detail_dirty:
            return
        meta = _row_meta(row)
        self.detail_badge.setStatus(_display_status(row))
        source = meta.get("source", row["file_id"])
        name = Path(source).name if isinstance(source, str) and source else str(source)
        self.detail_source.setText(name)
        self.detail_original.setText(row["original"])
        self.detail_edit.setPlainText(row["translation"] or "")
        self._detail_dirty = False  # 重填即干净状态（setPlainText 会触发 textChanged）
        self.lock_check.blockSignals(True)
        self.lock_check.setChecked(bool(row["locked"]))
        self.lock_check.blockSignals(False)
        # #38：有译文可审（translated 行）；failed 行无有效候选（机械门
        # 兜底会直接 BLOCKED 清译文），pending 行无译文——均不可审。
        self.review_btn.setEnabled(
            not self._review_running and bool(row["translation"])
            and row["status"] == "translated")
        self.detail_context.setText(self._context_text(meta))
        self.detail_reason.setText(self._quality_text(row, meta))
        self._detail_stack.setCurrentWidget(self.detail_panel)

    def _context_text(self, meta: dict) -> str:
        """§24 Context 区域：文件/类型/场景/前文/后文。"""
        lines = []
        for label, key in (("场景", "scene"), ("位置", "ui_position"),
                           ("类型", "text_type")):
            value = meta.get(key)
            if value:
                lines.append(f"{label}：{value}")
        if meta.get("ctx_before"):
            lines.append(f"前文：{meta['ctx_before']}")
        if meta.get("ctx_after"):
            lines.append(f"后文：{meta['ctx_after']}")
        return "\n".join(lines) or "—"

    def _quality_text(self, row: dict, meta: dict) -> str:
        """质量门 + 审核判定摘要（来自翻译/审核流程落盘字段）。"""
        parts = []
        reasons = meta.get("quality_reasons") or []
        if row["status"] == "failed":
            parts.append("✗ 翻译失败（右键可标记重试）")
        elif meta.get("quality_passed"):
            parts.append("✓ 已通过质量门")
        if reasons:
            parts.append("未通过：" + "、".join(str(r) for r in reasons))
        level = meta.get("review_level")
        if level in _REVIEW_LEVEL_TEXT:
            text = f"AI 审核：{_REVIEW_LEVEL_TEXT[level]}"
            if meta.get("review_reason"):
                text += f" · {meta['review_reason']}"
            parts.append(text)
        if meta.get("review_blocked"):
            parts.append("⚠ 审核阻断：多轮未通过")
        # #47：BLOCKED 时坏译文在 rejected_candidate（发布译文已清空）——
        # 人工复核需对照原坏译文判断，不展示则无从下手
        candidate = meta.get("rejected_candidate")
        if candidate:
            parts.append("✗ 原译文：" + str(candidate)[:80])
        # #43 阶段 G（重构指令 §13）：风险评分/等级透出（有字段才显示）
        score = meta.get("risk_score")
        rlevel = meta.get("risk_level") or ""
        if isinstance(score, (int, float)):
            parts.append(f"风险 {int(score)}/100"
                         + (f"（{rlevel}）" if rlevel else ""))
        elif rlevel:
            parts.append(f"风险（{rlevel}）")
        return "\n".join(parts) if parts else "—"

    # ── 中栏操作（复用 model.setData 持久化路径） ────────────
    def _save_detail(self):
        if self._current_row is None:
            return
        row = self.model._rows[self._current_row]
        fid, key = row["file_id"], row["key_path"]
        if self.model.setData(self.model.index(self._current_row, 3),
                              self.detail_edit.toPlainText()):
            self.save_feedback.setText("已保存")
            self._feedback_timer.start()
            self._detail_dirty = False
            if self._pending_reload:
                self.reload()  # 编辑结束，补跑挂起的刷新
        else:
            # 相同文本现在也允许保存（#8）；只剩留档样本不可编辑
            Toast.show(self, "该行为留档样本，不可编辑")
            self._fill_detail(row)
        self._restore_selection(fid, key)

    def _toggle_detail_lock(self, checked: bool):
        if self._current_row is None:
            return
        row = self.model._rows[self._current_row]
        fid, key = row["file_id"], row["key_path"]
        self.state.project.store.set_locked(fid, key, checked)
        row["locked"] = checked
        self.state.entriesChanged.emit()
        self._restore_selection(fid, key)

    def _restore_selection(self, file_id: str, key_path: str):
        """reload 重建模型后按主键找回行并重新选中（保持中栏焦点）。"""
        for row_idx, row in enumerate(self.model._rows):
            if row["file_id"] == file_id and row["key_path"] == key_path:
                src = self.proxy.mapFromSource(self.model.index(row_idx, 0))
                self.table.selectRow(src.row())
                break

    # ── #38 单条重新审核（人工强制送审闭环入口） ──────────────
    def _review_current(self):
        """对当前选中译文重新执行 AI 审核（force_send 无条件送审）。

        与批量审核共用 review_entries 管线：终态化（PASS/MINOR →
        APPROVED/APPROVED_MINOR，MAJOR/CRITICAL → NEEDS_REVISION）、
        记忆门禁、词对沉淀全部生效；translator=None 时不重译，保留
        译文等待人工按审核意见修改。
        """
        if self._current_row is None or self.state.project is None:
            return
        if self._review_running:
            return
        # 2026-08-15 用户实证「翻译时点审校会卡」：翻译进行中 1.8B 常驻
        # 显存，此时启动 4B 审核模型换入显存不足 → 启动慢/失败，界面
        # 卡数分钟。翻译期间拒绝送审并提示（翻译完成后自动/手动重审）。
        if self.state.translation_running:
            Toast.show(self, "翻译进行中：审核模型与翻译模型争用显存，"
                             "请等翻译完成后再审核", "warning")
            return
        row = self.model._rows[self._current_row]
        if not row.get("translation") or row["status"] != "translated":
            return
        meta = _row_meta(row)
        entry = TextEntry(
            file_id=row["file_id"], key_path=row["key_path"],
            original=row["original"], translation=row["translation"],
            status="translated", locked=bool(row.get("locked")),
            id=row.get("id"), meta=meta,
            confidence=str(meta.get("confidence", "medium")),
        )
        store = self.state.project.store
        app_dir = self.state.resource_dir
        data_dir = self.state.app_dir
        game_name = (self.state.project.name
                     if hasattr(self.state.project, "name") else "") or ""
        self._review_running = True
        self._review_token += 1
        token = self._review_token
        self.review_btn.setEnabled(False)
        self.review_btn.setText("审核中…")
        # 在线 API 模式：审核走云端端点（对应 kind 配置）
        online_review_cfg = (
            self.state.settings.api_config("review")
            if self.state.api.mode == "api" else None)
        worker = Worker(self._run_single_review, entry, store,
                        app_dir, game_name, online_review_cfg,
                        data_dir)
        self._review_worker = worker
        worker.signals.finished.connect(
            lambda r: self._on_review_done(token, r))
        worker.signals.error.connect(
            lambda err: self._on_review_done(token, ("error", err)))
        QThreadPool.globalInstance().start(worker)

    @staticmethod
    def _run_single_review(entry: TextEntry, store, app_dir, game_name: str,
                           online_review_cfg=None, data_dir=None):
        """后台线程：单条强制送审。translator=None → 不触发反馈重译，
        判定结果直接终态化（人工再审语义）。"""
        return review_entries(
            [entry], None, game_name=game_name,
            translator=None, memory=store, store=store,
            app_dir=app_dir, data_dir=data_dir,
            model_name="", lang="zh-CN",
            force_send=True, online_review_cfg=online_review_cfg)

    # ── AI 翻译填充（2026-08-15 用户要求：失败文本一键 AI 翻译） ──

    def _ai_translate_current(self):
        """用翻译模型翻译当前选中条目的原文，填充译文框（不自动保存）。"""
        if self._current_row is None or self._ai_translating:
            return
        # 翻译进行中 1.8B 单槽被批量翻译占用，此处请求排队且进度不可见
        # （2026-08-15 卡顿治理）：拒绝并提示等翻译完成
        if self.state.translation_running:
            Toast.show(self, "翻译进行中：翻译模型正被批量翻译占用，"
                             "请等翻译完成后再用 AI 翻译", "warning")
            return
        row = self.model._rows[self._current_row]
        original = str(row.get("original") or "").strip()
        if not original:
            Toast.show(self, "原文为空，无需翻译", "warning")
            return
        api = self.state.api
        if api.mode == "api" and not (api.base_url and api.api_key
                                      and api.model):
            Toast.show(self, "请先在设置中配置 API 模型", "warning")
            return
        # 术语库参考（翻译意图信号 + 术语译名约束）：与批量翻译同源，
        # 防止简单文本被小模型回显（Out of the Loop studio 实证）
        glossary_pairs = []
        try:
            g = GlossaryStore(self.state.app_dir / "glossary.db")
            g.init_schema()
            glossary_pairs = [
                (row_["term"], row_["translation"])
                for row_ in g.list_all()
                if row_.get("status", "active") == "active"]
            g.close()
        except Exception:  # noqa: BLE001 术语库不可用不阻断 AI 翻译
            glossary_pairs = []
        refs = merge_translation_references(glossary_pairs)
        self._ai_translating = True
        self.ai_translate_btn.setEnabled(False)
        self.ai_translate_btn.setText("翻译中…")
        worker = Worker(
            self._run_ai_translate, api, original,
            self.state.local_model, refs, "zh-CN")
        self._ai_translate_worker = worker
        worker.signals.finished.connect(self._on_ai_translate_done)
        worker.signals.error.connect(self._on_ai_translate_error)
        QThreadPool.globalInstance().start(worker)

    @staticmethod
    def _run_ai_translate(api, original: str,
                          local_model, refs, target_lang: str) -> str:
        """后台线程：本地模式先确保服务运行，再单条翻译。

        2026-08-26 修复「模型未产出译文」误报——1.8B 模型在英文 prompt
        下回显简单原文（'Out of the Loop studio' 实证）被剥空后误报未
        产出。统一收口到 translator.translate_interactive 多级降级链
        （中文显式指令 → 整体译名 → 专名保留引用），与「翻译」工具页
        同源，消除此前的品牌名拆分、重复重试、异常处理不一致。返回空串
        交 UI 提示（模型确无法产出，非回显误报）。
        """
        if api.mode == "local":
            runtime = local_model.ensure_running(api)
            api = replace(api, base_url=runtime.endpoint,
                          api_key=runtime.api_key, model=runtime.model)
        client = create_client(api)
        return translate_interactive(client, original, target_lang, refs)

    def _on_ai_translate_done(self, result) -> None:
        self._ai_translating = False
        self.ai_translate_btn.setEnabled(True)
        self.ai_translate_btn.setText("AI 翻译")
        out = str(result or "").strip()
        if not out:
            # 2026-08-26：translate_interactive 已尽四级降级（中文指令 →
            # 整体译名 → 专名引用 → 原文兜底）——走到这里（out 为空）只剩
            # 一种可能：原文本身为空。译文区绝不空白，仍填充原文本让用户
            # 明确可见（绝无空输出）。
            if self._current_row is not None:
                out = str(self.model._rows[self._current_row]
                          .get("original") or "")
            if out:
                Toast.show(self, "未能产出译文，已填回原文供参考", "warning")
            else:
                Toast.show(self, "原文为空，无可翻译内容", "warning")
                return
        self.detail_edit.setPlainText(out)
        self._detail_dirty = True
        Toast.show(self, "AI 译文已填充，确认无误后点「保存译文」", "success")

    def _on_ai_translate_error(self, err: str) -> None:
        self._ai_translating = False
        self.ai_translate_btn.setEnabled(True)
        self.ai_translate_btn.setText("AI 翻译")
        Toast.show(self, f"AI 翻译失败：{err}", "error")

    def _on_review_done(self, token: int, result) -> None:
        if token != self._review_token:
            return
        self._review_running = False
        self.review_btn.setText("重新审核")
        if isinstance(result, tuple) and result and result[0] == "error":
            Toast.show(self, f"重新审核失败：{result[1]}", "error")
        else:
            summary = result or {}
            outcomes = summary.get("outcomes") or {}
            approved = (outcomes.get("APPROVED", 0)
                        + outcomes.get("APPROVED_MINOR", 0))
            if not summary.get("used"):
                Toast.show(self, "审核模型不可用，未送审", "warning")
            elif approved:
                Toast.show(self, "审核通过：可写回发布", "success")
            else:
                reason = ""
                flagged = summary.get("flagged") or []
                if flagged:
                    reason = (flagged[0].reason or
                              flagged[0].suggestion or "")
                Toast.show(self, "仍需修改：" + (reason or "请按审核意见调整后重新审核"),
                           "warning")
        # 终态已原子落库：刷新列表（编辑保护下挂起则稍后补跑）
        self.reload()

    # ── 既有逻辑（护栏契约不变） ────────────────────────────
    def _focus_search(self):
        self.search_box.setFocus()
        self.search_box.selectAll()

    def _on_chip_clicked(self):
        """筛选胶囊：唯一状态筛选入口（下拉/复选框已移除），直接重算。"""
        if self._loading:
            return
        self._apply_filters()

    def _on_search_changed(self, _text: str):
        """搜索输入防抖（2026-08-19 卡顿修复）：每个按键触发一次全量
        过滤重算（filterAcceptsRow × 万行 + json.loads meta），连续输入
        时万行模型反复重建代理行映射——输入 10 个字符 = 10 次全量过滤。
        250ms 防抖合并为一次（QTimer 而非 sleep：不阻塞主线程）。"""
        from PySide6.QtCore import QTimer
        if getattr(self, "_search_timer", None) is None:
            self._search_timer = QTimer(self)
            self._search_timer.setSingleShot(True)
            self._search_timer.setInterval(250)
            self._search_timer.timeout.connect(self._apply_filters)
        self._search_timer.start()

    def _apply_filters(self):
        if self._loading:
            return
        # 胶囊即唯一状态真相：locked 走锁定，其余状态键直接映射到 proxy.status
        checked = next(
            (key for key, chip in self.filter_chips.items() if chip.isChecked()),
            "all")
        status, locked = ("", True) if checked == "locked" else (
            "", False) if checked == "all" else (checked, False)
        self.proxy.setFilters(
            search=self.search_box.text(),
            status=status,
            file_id=self.file_filter_box.currentData() or "",
            locked_only=locked)
        self._refresh_summary()

    def _on_file_filter_changed(self, _index: int) -> None:
        """来源文件下拉切换（#16）。构造期 addItem 触发的首个信号由
        _loading 守卫在 _apply_filters 里挡掉。"""
        self._apply_filters()

    def _refresh_file_filter(self, rows: list[dict]) -> None:
        """从当前条目聚合来源文件选项（#16）。reload 后调用——
        blockSignals 重建选项避免中途触发过滤；当前选中项仍在列表
        则维持，否则回「全部来源」（file_id 消失于新数据集时不过滤
        出空表）。显示短名 + 条目数，完整 file_id 放 userData。"""
        current = self.file_filter_box.currentData() or ""
        # 去重保持出现顺序（同文件条目相邻，天然按首次出现排序）
        seen: dict[str, int] = {}
        for r in rows:
            fid = r.get("file_id") or ""
            if fid:
                seen[fid] = seen.get(fid, 0) + 1
        box = self.file_filter_box
        box.blockSignals(True)
        box.clear()
        box.addItem("全部来源", "")
        for fid, count in seen.items():
            label = f"{Path(fid).name}（{count}）"
            box.addItem(label, fid)
        if current and current in seen:
            box.setCurrentIndex(box.findData(current))
        box.blockSignals(False)

    def _auto_reload(self):
        """entriesChanged 广播触发的自动刷新（翻译页每 ≥1s 广播一次）。

        2026-08-14 卡顿优化：翻译进行中（state.translation_running）挂起
        全量重建（万级行 × 筛选在广播频率下持续卡主线程——用户实证
        「翻译时工具明显卡顿」）——挂起后由翻译结束的广播自然补跑；
        页面不可见时置脏跳过，showEvent 切回时补跑。用户主动操作
        （锁定/标记/保存/切行）走 reload() 直跑，不受此守卫影响。
        """
        if self.state.translation_running:
            self._pending_reload = True
            return
        if not self.isVisible():
            self._reload_dirty = True
            return
        self.reload()

    def showEvent(self, event):
        """切回页面时补跑置脏/挂起的 reload（挂起编辑不在此补，由
        closeEditor 路径负责——避免覆盖正在编辑的内容）。"""
        super().showEvent(event)
        if (self._reload_dirty or self._pending_reload) \
                and not self.state.translation_running:
            self._reload_dirty = False
            self.reload()

    def reload(self):
        if self.state.project is None:
            self._loading = False
            self.model.setEntries([])
            self._refresh_file_filter([])
            self._stack.setCurrentWidget(self.empty_state)
            self._detail_stack.setCurrentWidget(self.detail_empty)
            self._refresh_summary()
            return
        if self.table.state() == QAbstractItemView.EditingState:
            # #8：表格内联编辑中不重建模型——entriesChanged（后台翻译
            # 每批完成等）触发的 reload 会销毁 delegate editor，正在输入
            # 的内容 ~2 秒被打回原文本。挂起 reload，editor 关闭后执行。
            self._pending_reload = True
            return
        self._reload_now()

    def _on_editor_closed(self, _editor, _hint):
        """内联编辑关闭后补跑挂起的 reload（#8 编辑保护）。"""
        if self._pending_reload:
            self._pending_reload = False
            self.reload()

    def _reload_now(self):
        if self._pending_reload:
            self._pending_reload = False
        if self._detail_dirty or self.detail_edit.hasFocus():
            # #8：用户在编辑（已改未保存 / 焦点在编辑框）——后台翻译
            # 批完成的 reload 会覆盖编辑框（2 秒打回原文本）。挂起，
            # 保存成功或切行后补跑。
            self._pending_reload = True
            return
        store = self.state.project.store
        self._loading = True
        self._reload_token += 1
        token = self._reload_token
        # #2：全量读取（get_entries + meta JSON 解析）放后台线程——万级
        # 条目在主线程同步加载会在打开文件夹完成瞬间冻结 UI（#2 实证：
        # 10092 条 × DB 读 + 二次 JSON 解析 ≈ 秒级）。模型构建保留主线程。
        worker = Worker(
            lambda: [r for r in store.get_entries()
                     if r["status"] != "skipped"])  # #3 跳过不进审校列表
        # 引用必须保存：worker 是局部变量，函数返回后 Python wrapper
        # 引用丢失 → finished 连接的 lambda 随之失效（实证：worker 在
        # 池线程跑完但主线程收不到信号）。同 translate_page._worker /
        # home signals_holder 模式。
        self._reload_worker = worker
        worker.signals.finished.connect(
            lambda rows: self._on_reload_rows(token, rows))
        worker.signals.error.connect(
            lambda err: self._on_reload_error(token, err))
        QThreadPool.globalInstance().start(worker)

    def _on_reload_rows(self, token: int, rows: list[dict]) -> None:
        """后台读取完成：token 过期（项目已切换/更新 reload 已发出）丢弃。"""
        if token != self._reload_token or self.state.project is None:
            return
        self.model.setEntries(rows)
        self._stack.setCurrentWidget(
            self.table if rows else self.empty_state)
        self._loading = False
        # #16：先刷新来源下拉（新数据集聚合文件选项），再应用过滤
        self._refresh_file_filter(rows)
        self._apply_filters()
        # 保留选中（#16：翻译/审校保存触发的 reload 重建模型会丢选中，
        # 中栏焦点跳到空白）。按主键找回重选；行不存在（被筛选/删除）
        # 则清空回空态。
        if self._current_row is not None \
                and 0 <= self._current_row < len(self.model._rows):
            row = self.model._rows[self._current_row]
            self._restore_selection(row["file_id"], row["key_path"])
        else:
            self._current_row = None
            self._detail_stack.setCurrentWidget(self.detail_empty)
        self._refresh_summary()

    def _on_reload_error(self, token: int, err: str) -> None:
        if token != self._reload_token:
            return
        self._loading = False
        self.model.setEntries([])
        self._refresh_file_filter([])
        self._stack.setCurrentWidget(self.empty_state)
        self._refresh_summary()

    def _refresh_summary(self):
        """结果计数：总数显示筛选后行数，其余统计来自模型行。

        #19：隐藏跳过统计；待审核胶囊在无不合格条目时隐藏（空胶囊
        会让用户以为存在待办而逐个翻找，实则空转）。计数基于模型行
        （与 store 同源，项目未接入时也能自洽）。
        #2/#8：待翻译用 is_actionable_translation 口径（与翻译页 chips
        一致）——low 置信度留档（引擎消息/噪音，跳过翻译）不计入，
        与翻译页「待翻译」数字一致，跳过的文本不显示在待翻译里。
        #47：待审核 = 未收敛 ∪ 机械失败（与筛选口径一致）；已重译胶囊
        同样无条目时隐藏。
        """
        rows = self.model._rows
        pending = sum(1 for r in rows
                      if is_actionable_translation(entry_from_row(r)))
        translated = sum(1 for r in rows if r["status"] == "translated")
        failed = sum(1 for r in rows if r["status"] == "failed")
        failed_s = f" · 失败 {failed}" if failed else ""
        self.summary_label.setText(
            f"共 {self.proxy.rowCount()} 条 · 待翻译 {pending} · "
            f"已翻译 {translated}{failed_s}")
        needs_review = sum(
            1 for r in rows
            if _needs_review(_row_meta(r)) or r["status"] == "failed")
        chip = self.filter_chips.get("needs_review")
        if chip is not None:
            chip.setVisible(needs_review > 0)
        retranslated = sum(
            1 for r in rows if _row_meta(r).get("retranslated"))
        chip = self.filter_chips.get("retranslated")
        if chip is not None:
            chip.setVisible(retranslated > 0)

    # ── 右键菜单（多选批量生效） ──
    def _show_menu(self, pos):
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        rows = self._selected_rows()
        if not rows:
            return
        menu = QMenu(self)
        if len(rows) == 1:
            row = rows[0]
            if row["locked"]:
                menu.addAction("解锁", lambda: self._toggle_lock(rows, False))
            else:
                menu.addAction("锁定（不翻译）", lambda: self._toggle_lock(rows, True))
            menu.addSeparator()
            menu.addAction("复制原文", lambda: self._copy(row["original"]))
            menu.addAction("复制译文", lambda: self._copy(row["translation"] or ""))
            if row["status"] == "failed":
                menu.addSeparator()
                menu.addAction("标记为待翻译（重新翻译）",
                               lambda: self._mark_pending(rows))
        else:
            menu.addAction(f"锁定选中 {len(rows)} 条（不翻译）",
                           lambda: self._toggle_lock(rows, True))
            menu.addAction(f"解锁选中 {len(rows)} 条",
                           lambda: self._toggle_lock(rows, False))
            menu.addSeparator()
            menu.addAction(f"标记选中 {len(rows)} 条为待翻译",
                           lambda: self._mark_pending(rows))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _selected_rows(self) -> list[dict]:
        """当前选中行（含右键点击所在行），转为原始模型行。"""
        selected = {self.proxy.mapToSource(i).row()
                    for i in self.table.selectionModel().selectedRows()}
        index = self.table.indexAt(self.table.viewport().mapFromGlobal(
            self.table.viewport().cursor().pos()))
        if index.isValid():
            selected.add(self.proxy.mapToSource(index).row())
        return [self.model._rows[row] for row in sorted(selected)]

    def _toggle_lock(self, rows: list[dict], locked: bool):
        for row in rows:
            self.state.project.store.set_locked(
                row["file_id"], row["key_path"], locked)
        self.state.entriesChanged.emit()
        Toast.show(self,
                   f"已锁定 {len(rows)} 条，翻译时跳过" if locked
                   else f"已解锁 {len(rows)} 条")

    def _mark_pending(self, rows: list[dict]):
        targets = [row for row in rows if not _is_sample_row(row)]
        if not targets:
            Toast.show(self, "留档样本行仅审计用，不可标记翻译")
            return
        for row in targets:
            # #9：重置待译须清旧审核终态——只 set_status 会让 BLOCKED
            # 残留继续拒绝重译成功的译文（失败文本无法自己处理）
            self.state.project.store.reset_to_pending(
                row["file_id"], row["key_path"])
        self.state.entriesChanged.emit()
        Toast.show(self, f"已标记 {len(targets)} 条为待翻译")

    def _copy(self, text: str):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)
        Toast.show(self, "已复制到剪贴板")
