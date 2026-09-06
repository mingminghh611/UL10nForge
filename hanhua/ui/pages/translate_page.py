"""翻译页：批量深度翻译（进度/日志/停止/重试失败）+ 写回。"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from PySide6.QtCore import (QEasingCurve, QPropertyAnimation, Qt,
                            QThreadPool)
from PySide6.QtWidgets import (QCheckBox, QFrame, QGraphicsOpacityEffect,
                               QHBoxLayout, QLabel, QPlainTextEdit,
                               QProgressBar, QPushButton, QSplitter,
                               QVBoxLayout, QWidget)

from hanhua.core.agent_memory import AgentMemory
from hanhua.core.batch_translator import BatchTranslator
from hanhua.core.glossary import GlossaryStore
from hanhua.core.knowledge import KnowledgeBase
from hanhua.core.local_model import LocalModelError, sanitize_exception
from hanhua.core.memory import settle_translation_memory
from hanhua.core.models import (GameProfile, REVIEW_PENDING_OUTCOMES,
                                TextEntry, entry_from_row,
                                is_actionable_translation)
from hanhua.core.prompts import build_system_prompt, collect_known_names
from hanhua.core.quality import is_write_ready
from hanhua.core.reviewer import review_entries
from hanhua.core.translator import create_client
from hanhua.ui.app_state import AppState
from hanhua.ui.design_system import motion_enabled
from hanhua.ui.widgets import (ActivityFeed, PageHeader,
                               SafetyBar, Toast, Worker)

@dataclass(eq=False)
class _TranslationRun:
    project: object
    generation: int
    api: object
    profile: GameProfile
    cancel: threading.Event
    stop_local_after_run: bool
    secrets: list[str]
    translator: BatchTranslator | None = None
    local_started: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)

    def attach_translator(self, translator: BatchTranslator) -> None:
        with self.lock:
            self.translator = translator
            cancelled = self.cancel.is_set()
        if cancelled:
            translator.stop()

    def detach_translator(self) -> None:
        with self.lock:
            self.translator = None

    def request_stop(self, local_model) -> None:
        self.cancel.set()
        if self.api.mode == "local":
            local_model.cancel_start()
        with self.lock:
            translator = self.translator
        if translator is not None:
            translator.stop()


MAX_LOCAL_RECOVERIES = 2  # 单次翻译运行允许的服务故障恢复次数

# 全链路进度分段权重（2026-08-20）：翻译/审校判定/审校处置/写回共享
# 同一根 progress_bar，按 3-3-3-1 权重映射到 0-100 并接续前段末值单调
# 推进——杜绝此前审核首条把翻译满条 100 归零再爬、写回又从 5% 起跳的
# 「倒退/jumping back」。审校内部分两个子阶段：判定（review_batch 逐条
# 4B 判定，有 on_progress）+ 处置（反馈重译 + 再审收敛，串行 2-30s/条，
# 原本静默——现经 on_disposition_progress 回调驱动 60-90% 段）。
# 识别 scan 保留自己的 #scanBar，不在此条。
_PIPELINE_STAGE_RANGE = {
    "translate": (0, 30),            # 翻译 3 份
    "review_verdict": (30, 60),      # 审校判定 3 份
    "review_disposition": (60, 90),  # 审校处置 3 份
    "writeback": (90, 100),         # 写回 1 份
}

# 分段图例尺配置：(键, 权重 stretch, 阶段色, 文字)——与 _PIPELINE_STAGE_RANGE
# 同源。权重 3-3-3-1 决定图例尺四段宽度比例，阶段色与进度条填色一致。
_PIPELINE_SEGMENTS = (
    ("translate", 3, "#58E6C2", "翻译 30%"),
    ("review_verdict", 3, "#A78BFA", "审校判定 30%"),
    ("review_disposition", 3, "#63B3FF", "审校处置 30%"),
    ("writeback", 1, "#F5B84B", "写回 10%"),
)


def _hex_to_rgba(hex_color: str, alpha: float = 1.0) -> str:
    """#RRGGBB → rgba(r, g, b, a)，供 QSS 背景用（QSS 不支持 opacity）。"""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha})"


# ── 动效时长（2026-08-21 Task #4 配色与动效优化） ──
# 图例尺跨段淡入淡出 220ms OutCubic（略长于 page_enter，让高亮切换在
# 进度到位后落定——视觉上「先到位再点亮」）。reduced-motion 时
# _update_segment_active 直切不构造动画。进度条值本身不动画：
# setValue 同步可断言、跨段接续靠 _pipeline_floor 单调下限保证不倒退。
_SEGMENT_FADE_MS = 220                            # 图例尺跨段淡入淡出
# 图例尺分段透明度：激活段 1.0（满色），未到段 0.55（半透明仍可见
# 文字与色相，不至于暗到看不出是哪段）。跨段切换在这两个值间淡入淡出。
_SEG_DIM_OPACITY = 0.55
_SEG_ACTIVE_OPACITY = 1.0


def _critical_local_failures(store) -> bool:
    """失败条目中是否存在服务坏状态（HTTP 502，CUDA OOM 后 llama-server
    持续返回 502）→ 需要重启服务恢复。质量失败（无 502）不触发恢复。"""
    for row in store.get_entries(status="failed"):
        meta = row.get("meta", {})
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                continue
        if not isinstance(meta, dict):
            continue
        detail = meta.get("request_error_detail")
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except (json.JSONDecodeError, TypeError):
                continue
        if isinstance(detail, dict) and detail.get("status") == 502:
            return True
    return False


def _write_ready_count(store) -> int:
    return sum(
        is_write_ready(
            row.get("status", ""), row.get("translation", ""),
            row.get("meta", "{}"),
        )
        for row in store.get_entries()
    )


class TranslatePage(QWidget):
    def __init__(self, state: AppState, window):
        super().__init__()
        self.state = state
        self.window = window
        self._worker: Worker | None = None
        self._write_worker_task: Worker | None = None
        self._active_run: _TranslationRun | None = None
        self._running = False
        self._write_running = False
        # 写回预演（0.39.0 M4，Dry Run）在跑标志——与正式写回/翻译互斥
        self._dry_run_running = False
        self._write_terminal_message = ""
        self._last_stats = None
        # P4（2026-09-06 fromivan）：本轮语义审核的完整汇总 dict（
        # review_entries 返回值）——写回后导出记录时传入 export_records，
        # 让 review/review-report.md 与 summary §3.5 随记录落盘（此前
        # 审核内容只在运行日志临时出现，记录文档不含审核明细）。
        self._last_review_summary: dict | None = None
        self._stream_last_done = 0
        self._last_review: tuple[int, int] | None = None
        self._last_review_emit = 0.0
        # 全链路进度条分段驱动（2026-08-20）：翻译/审校判定/审校处置/
        # 写回共享同一根 progress_bar，按 3-3-3-1 权重映射到 0-30 /
        # 30-60 / 60-90 / 90-100 四段并接续前段末值单调推进——杜绝此前
        # 审核首条把翻译满条 100 归零再爬、写回又从 5% 起跳的
        # 「倒退/jumping back」。_pipeline_floor 记录已达到的最大值，
        # 跨段切换时不回退（新段 lo = 旧段 hi，自然接续）。
        # 识别 scan 保留自己的 #scanBar，不进此条。
        self._pipeline_floor = 0
        # 当前激活段（图例尺高亮用）——start() 复位为 None
        self._active_segment: str | None = None
        # 图例尺分段激活切换动画：跨段时旧段从满色淡出到半透明、新段从
        # 半透明淡入到满色，220ms OutCubic。每段持一个 QGraphicsOpacity
        # effect + 动画，互不影响。reduced-motion 直切。
        # 进度条值本身不动画（setValue 同步可断言）；跨段接续靠
        # _pipeline_floor 单调下限保证 30→31 不倒退。
        self._seg_effect: dict[str, QGraphicsOpacityEffect] = {}
        self._seg_fade: dict[str, QPropertyAnimation] = {}
        # 进度节流（#13 实证：home 分数与流水线 rail 只在全部完成后才
        # 更新，翻译时「突然一下完成几十条」——批粒度 progress 直接
        # emit entriesChanged 会让首页 O(N) 重扫刷屏）。每 ≥1s 才广播
        # 一次实时刷新，进度条/日志仍按批粒度更新不受影响。
        self._last_phase_emit = 0.0
        # #6：进度驱动的 UI 计数刷新节流——_refresh_chips 全量 O(N)
        # 查库（万级条目 × 每批一次会卡主线程）。与 entriesChanged 广播
        # 共用 ≥1s 节流，翻译完成后必定全刷（_on_finished）。
        self._last_chip_refresh = 0.0
        # #2：计数刷新后台化竞态防护——每次 _refresh_chips 递增 token，
        # worker 完成时 token 不符（项目已切换/更新刷新已发出）则丢弃。
        self._chips_token = 0
        self._chips_worker = None
        self._chips_loading = False
        self._pool = QThreadPool.globalInstance()
        # 经验记忆（AgentMemory）：跨游戏持久、证据驱动——懒创建于
        # app_dir/agent_memory.db；每次翻译运行前 session_reset，写回后
        # session_report 随记录文档落盘（用户可追踪记忆成长）
        self._agent_memory: AgentMemory | None = None
        # 知识检索统一门面（审计 Phase C，P1-1 修复）：ContextStore/
        # VectorRecall/RerankGate 生产接线——懒创建于 app_dir（与
        # glossary/knowledge/agent_memory 同库目录），跨次翻译复用。
        self._knowledge_retrieval = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 18)
        lay.setSpacing(12)

        # ── 页面抬头 ──
        lay.addWidget(PageHeader(
            "翻译",
            "批量深度翻译 · 记忆命中复用 · 质量门拦截 · 安全写回",
        ))

        # ── 任务摘要条（细长：进度 + 即时文本，不套大卡片） ──
        # 2026-08-20 全链路分段进度条：翻译/审校判定/审校处置/写回共享
        # 同一根 progress_bar，按 3-3-3-1 权重接续推进不倒退。下方分段
        # 图例尺（segment legend）四段按权重比例 stretch、各带阶段色与
        # 文字，进度条上 30%/60%/90% 分界一目了然。审校内部分判定 +
        # 处置两个子阶段——处置阶段原本静默（反馈重译串行 2-30s/条无
        # 进度回调），现经 on_disposition_progress 回调驱动 60-90% 段。
        strip = QFrame()
        strip_v = QVBoxLayout(strip)
        strip_v.setContentsMargins(0, 0, 0, 0)
        strip_v.setSpacing(4)
        strip_row = QHBoxLayout()
        strip_row.setContentsMargins(0, 0, 0, 0)
        strip_row.setSpacing(12)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_label = QLabel("尚未开始")
        self.progress_label.setStyleSheet("font-size: 14px; font-weight: 600;")
        strip_row.addWidget(self.progress_bar, 1)
        strip_row.addWidget(self.progress_label)
        strip_v.addLayout(strip_row)

        # 分段图例尺：翻译 30% · 审校判定 30% · 审校处置 30% · 写回 10%
        # （与 _PIPELINE_STAGE_RANGE / _PIPELINE_SEGMENTS 权重同源）。四段
        # 按比例 stretch，阶段色 + 居中文字 + 圆角小条。当前激活段高亮
        # （_update_segment_active），未到段半透明。
        # 2026-08-21 Task #4：每段挂一个 QGraphicsOpacityEffect，跨段切换
        # 时旧段从满色（opacity 1.0）淡出到半透明（0.55）、新段从 0.55
        # 淡入到 1.0，220ms OutCubic——高亮不再硬切。reduced-motion 直切
        # （effect 初始 1.0，_update_segment_active 直接 setOpacity 跳变）。
        legend = QHBoxLayout()
        legend.setContentsMargins(0, 0, 0, 0)
        legend.setSpacing(4)
        self._segment_widgets: dict[str, QFrame] = {}
        for key, weight, color, text in _PIPELINE_SEGMENTS:
            seg = QFrame()
            seg.setObjectName(f"seg{key}")
            seg.setMinimumHeight(18)
            seg.setMaximumHeight(18)
            seg.setToolTip(
                f"{text}（进度条 {self._seg_lo(key)}-{self._seg_hi(key)}% 段）")
            seg_lbl = QLabel(text, seg)
            seg_lbl.setAlignment(Qt.AlignCenter)
            seg_lbl.setStyleSheet(
                "color: #090B12; font-size: 10px; font-weight: 700;"
                "background: transparent;")
            seg_layout = QVBoxLayout(seg)
            seg_layout.setContentsMargins(0, 0, 0, 0)
            seg_layout.addWidget(seg_lbl)
            self._apply_segment_style(seg, color, active=False)
            # 半透明底（opacity 0.55）对应未激活态；满色（1.0）对应激活态
            effect = QGraphicsOpacityEffect(seg)
            effect.setOpacity(_SEG_DIM_OPACITY)
            seg.setGraphicsEffect(effect)
            self._seg_effect[key] = effect
            self._segment_widgets[key] = seg
            legend.addWidget(seg, weight)
        strip_v.addLayout(legend)
        lay.addWidget(strip)

        # ── 次级行：剩余量 + 轻量状态计数（跳过项默认隐藏） ──
        sub_row = QHBoxLayout()
        sub_row.setSpacing(14)
        self.progress_sub = QLabel("在开始前，请确认设置页的 API 与游戏档案已配置")
        self.progress_sub.setProperty("class", "subtitle")
        self.chip_pending = QLabel("待翻译 —")
        self.chip_done = QLabel("已翻译 —")
        self.chip_failed = QLabel("失败 —")
        self.chip_skipped = QLabel("跳过 —")
        self.chip_skipped.setHidden(True)
        for chip in (self.chip_pending, self.chip_done, self.chip_failed,
                     self.chip_skipped):
            chip.setProperty("class", "subtitle")
        sub_row.addWidget(self.progress_sub)
        sub_row.addStretch(1)
        sub_row.addWidget(self.chip_pending)
        sub_row.addWidget(self.chip_done)
        sub_row.addWidget(self.chip_failed)
        sub_row.addWidget(self.chip_skipped)
        lay.addLayout(sub_row)

        # ── 运行区双栏（左：实时处理流；右：运行记录） ──
        # 2026-08-20 重做：原版只有一个处理流卡片 + 折叠日志，
        # 处置审校时根本来不及看。改为水平 QSplitter 左右并排两个竖
        # 栏，日志默认可见且大，用户可拖中缝调节比例。MetricStrip
        # （待翻译/tokens）信息冗余已删——顶部 chips 已显示同样口径。
        console_split = QSplitter(Qt.Horizontal)
        console_split.setObjectName("consoleSplit")
        console_split.setChildrenCollapsible(False)
        console_split.setHandleWidth(12)

        # 左栏：实时处理流
        stream_frame = QFrame()
        stream_frame.setObjectName("card")
        sf = QVBoxLayout(stream_frame)
        sf.setContentsMargins(14, 10, 14, 10)
        sf.setSpacing(6)
        st_head = QHBoxLayout()
        st_title = QLabel("实时处理流")
        st_title.setProperty("class", "pageTitle")
        self.stream_status = QLabel("等待开始")
        self.stream_status.setObjectName("streamStatus")
        self.stream_status.setProperty("class", "subtitle")
        self.stream_status.setProperty("phase", "idle")
        st_head.addWidget(st_title)
        st_head.addStretch(1)
        st_head.addWidget(self.stream_status)
        # ActivityFeed（§6.4）：状态着色的事件流，容量上限自动裁剪
        self.activity_feed = ActivityFeed(max_items=120)
        sf.addLayout(st_head)
        sf.addWidget(self.activity_feed, 1)

        # 右栏：运行记录（默认可见，可收起腾给处理流）
        log_frame = QFrame()
        log_frame.setObjectName("card")
        lf = QVBoxLayout(log_frame)
        lf.setContentsMargins(14, 10, 14, 10)
        lf.setSpacing(6)
        log_head = QHBoxLayout()
        log_title = QLabel("运行记录")
        log_title.setProperty("class", "pageTitle")
        # 2026-08-22 用户指令：删掉「收起日志」按钮（右栏用 Splitter
        # 拖动即可调节，不需要额外折叠开关），保留 复制/清空。
        self.copy_log_btn = QPushButton("复制")
        self.clear_log_btn = QPushButton("清空")
        for button in (self.copy_log_btn, self.clear_log_btn):
            button.setProperty("ghost", True)
            button.setMinimumHeight(32)
            button.setCursor(Qt.PointingHandCursor)
        log_head.addWidget(log_title)
        log_head.addStretch(1)
        log_head.addWidget(self.copy_log_btn)
        log_head.addWidget(self.clear_log_btn)
        lf.addLayout(log_head)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        lf.addWidget(self.log_view, 1)

        console_split.addWidget(stream_frame)
        console_split.addWidget(log_frame)
        console_split.setStretchFactor(0, 1)
        console_split.setStretchFactor(1, 1)
        console_split.setSizes([520, 520])
        lay.addWidget(console_split, 1)

        self.quality_reason_label = QLabel("质量门失败原因：无")
        self.quality_reason_label.setProperty("class", "reasonIdle")
        self.quality_reason_label.setWordWrap(True)
        self.quality_reason_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.quality_reason_label)

        # ── 底部操作区：翻译控制 + SafetyBar 安全写回（§6.3） ──
        ctl = QHBoxLayout()
        ctl.setSpacing(10)
        self.start_btn = QPushButton("开始翻译")
        self.start_btn.setProperty("primary", True)
        self.stop_btn = QPushButton("停止")
        self.retry_btn = QPushButton("重试失败")
        self.play_btn = QPushButton("开始游戏")
        self.reveal_btn = QPushButton("在文件夹中显示")
        self.reveal_btn.setProperty("ghost", True)
        for button, name in (
            (self.start_btn, "开始自动翻译"), (self.stop_btn, "停止自动翻译"),
            (self.retry_btn, "重试失败译文"), (self.play_btn, "启动汉化副本进入游戏"),
            (self.reveal_btn, "打开汉化输出目录"),
        ):
            button.setMinimumHeight(44)
            button.setAccessibleName(name)
        self.play_btn.setToolTip(
            "写回验证通过后亮起，点击直接启动汉化副本进入游戏")
        self.partial_check = QCheckBox("允许部分写入")
        self.partial_check.setChecked(False)
        self.partial_check.setToolTip(
            "存在拒绝/截断条目时强制发布（默认阻断，不勾选）")
        self.partial_check.setAccessibleName("允许部分写入并发布")
        # 写回预演按钮（0.39.0 M4，设计文档 §62「分析写回但不修改游戏」）：
        # 走 Worker 后台跑 build_writeback_plan（与正式写回同一分类链、
        # 零磁盘零 store 副作用），报告四类计数进日志面板。预演不替代
        # 写回守卫——按钮常亮可先看风险面，确认后仍点「写回游戏」。
        self.dry_run_btn = QPushButton("写回预演")
        self.dry_run_btn.setMinimumHeight(44)
        self.dry_run_btn.setAccessibleName("写回预演（只分析不修改游戏）")
        self.dry_run_btn.setToolTip(
            "只分析不写盘：预计写回/需要人工/拒绝/高风险四类计数，"
            "与正式写回同一套判定规则")
        # 2026-08-22 用户指令：checkbox 旁加几个字的小说明
        self.partial_hint = QLabel("有拒绝/截断时仍可发布")
        self.partial_hint.setProperty("class", "subtitle")
        self.stop_btn.setEnabled(False)
        self.retry_btn.setEnabled(False)
        self.play_btn.setEnabled(False)
        self.reveal_btn.setHidden(True)
        ctl.addWidget(self.start_btn)
        ctl.addWidget(self.stop_btn)
        ctl.addWidget(self.retry_btn)
        ctl.addStretch(1)
        ctl.addWidget(self.partial_check)
        ctl.addWidget(self.partial_hint)
        ctl.addWidget(self.dry_run_btn)
        ctl.addWidget(self.reveal_btn)
        ctl.addWidget(self.play_btn)
        lay.addLayout(ctl)

        # 安全写回栏：写回按钮由 SafetyBar.set_ready 统一管理（禁用时
        # 显示具体原因；status 驱动左侧主题色）
        self.write_btn = QPushButton("写回游戏")
        self.write_btn.setMinimumHeight(44)
        self.write_btn.setAccessibleName("安全写回游戏副本")
        self.write_btn.setEnabled(False)
        self.write_safety = SafetyBar(self.write_btn)
        lay.addWidget(self.write_safety)

        self.start_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)
        self.retry_btn.clicked.connect(self.retry_failed)
        self.write_btn.clicked.connect(self.write_back)
        self.dry_run_btn.clicked.connect(self.dry_run_writeback)
        self.play_btn.clicked.connect(self.launch_game)
        self.reveal_btn.clicked.connect(self.reveal_output)
        self.copy_log_btn.clicked.connect(self._copy_log)
        self.clear_log_btn.clicked.connect(self._clear_log)
        # §35「允许部分写入」改变后立即刷新写回原因
        self.partial_check.toggled.connect(
            lambda _checked: self._refresh_chips())
        self.state.projectOpened.connect(self._on_project)
        self.state.projectAboutToChange.connect(self._on_project_changing)
        self.state.settingsChanged.connect(lambda: self._refresh_chips())
        # 2026-08-26 写回按键失灵：写回使能并入 analysis_report.unblocked
        # 判定后，报告变化（写回后 set_analysis_report 更新终态、或重新
        # 分析）必须触发刷新，否则按钮状态与报告脱节（报告被阻断但按钮
        # 仍亮着）。analysisChanged 是报告更新的唯一广播，接上。
        self.state.analysisChanged.connect(lambda _r: self._refresh_chips())
        self._set_primary(self.start_btn)
        self._on_project(None)

    def _set_primary(self, primary_btn):
        """任一时刻只有一个主按钮：开始翻译 ⇄ 写回游戏。"""
        for button in (self.start_btn, self.write_btn):
            is_primary = button is primary_btn
            if button.property("primary") != is_primary:
                button.setProperty("primary", is_primary)
                button.style().unpolish(button)
                button.style().polish(button)

    # ── 全链路进度条分段驱动（2026-08-20，3-3-3-1 权重） ──
    # 翻译/审校判定/审校处置/写回共享同一根 progress_bar，四段按权重
    # 接续：translate 0-30 · review_verdict 30-60 · review_disposition
    # 60-90 · writeback 90-100。_pipeline_floor 记录已达到的最大值，
    # 跨段切换或乱序信号到达时不回退（新段 lo = 旧段 hi，自然接续）。
    # 图例尺 _segment_widgets 四段按 3-3-3-1 stretch，当前激活段高亮。
    def _seg_lo(self, key: str) -> int:
        return _PIPELINE_STAGE_RANGE[key][0]

    def _seg_hi(self, key: str) -> int:
        return _PIPELINE_STAGE_RANGE[key][1]

    def _apply_segment_style(self, seg: QFrame, color: str, *,
                             active: bool) -> None:
        """图例尺分段样式：激活段满色 + 实边，未到段半透明 + 淡边。"""
        bg = color if active else _hex_to_rgba(color, 0.22)
        border = color if active else _hex_to_rgba(color, 0.40)
        seg.setStyleSheet(
            f"background: {bg}; border: 1px solid {border};"
            f"border-radius: 4px;")

    def _fade_segment(self, key: str, target_opacity: float) -> None:
        """图例尺分段透明度过渡（220ms OutCubic）。

        激活段 → 1.0（满色），离开段 → 0.55（半透明）。reduced-motion
        直接 setOpacity 跳变。旧动画未完成时停掉重开（避免快速跨段时
        两个淡入淡出动画抢同一个 effect 的 opacity 属性）。

        2026-08-21 修复「无法开始翻译」：旧动画自然结束后 finished
        回调里 deleteLater 销毁了 C++ 对象，但 _seg_fade 仍持 Python
        引用（悬空）——快速跨段切换时 old.stop() 抛 RuntimeError
        （libshiboken: Internal C++ object already deleted）。此异常
        在 start()→_reset_pipeline_progress→_update_segment_active
        路径抛出，致 worker 从未启动、_active_run 不清，第二次点击
        命中「上一个翻译任务仍在停止」守卫。根治：finished 回调里先从
        dict 移除引用再 deleteLater，且 stop() 包 try/except 兜底。
        """
        effect = self._seg_effect.get(key)
        if effect is None:
            return
        current = effect.opacity()
        if not motion_enabled() or abs(current - target_opacity) < 0.01:
            effect.setOpacity(target_opacity)
            return
        old = self._seg_fade.pop(key, None)
        if old is not None:
            # C++ 对象可能已被 finished→deleteLater 销毁（悬空引用），
            # stop/deleteLater 均 may raise RuntimeError——静默吞掉。
            try:
                old.stop()
                old.deleteLater()
            except RuntimeError:
                pass
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(_SEGMENT_FADE_MS)
        anim.setStartValue(current)
        anim.setEndValue(target_opacity)
        anim.setEasingCurve(QEasingCurve.OutCubic)

        def _on_finished(anim=anim, key=key):
            # 自然结束：先从 dict 移除（防悬空引用），再 deleteLater
            # 释放。只有当 dict 里仍是本动画才移除——中途被新动画替换
            # 时不误删新动画的引用。
            if self._seg_fade.get(key) is anim:
                self._seg_fade.pop(key, None)
            try:
                anim.deleteLater()
            except RuntimeError:
                pass

        anim.finished.connect(_on_finished)
        self._seg_fade[key] = anim
        anim.start()

    def _update_segment_active(self, segment: str | None) -> None:
        """高亮当前激活段，其余半透明。segment=None → 全部未激活。

        2026-08-21 Task #4：激活态由 opacity 双层叠加表达——
        _apply_segment_style 切换底色（满色/半透明底），再经 _fade_segment
        把整段不透明度在 1.0↔0.55 间淡入淡出。两层叠加让跨段切换有
        过渡而非硬切：底色立刻定到目标（标识「这是新激活段」），整段
        亮度随后滑入/淡出（视觉过渡）。
        """
        self._active_segment = segment
        for key, _weight, color, _text in _PIPELINE_SEGMENTS:
            seg = self._segment_widgets.get(key)
            if seg is not None:
                self._apply_segment_style(
                    seg, color, active=key == segment)
                self._fade_segment(
                    key,
                    _SEG_ACTIVE_OPACITY if key == segment
                    else _SEG_DIM_OPACITY)

    def _set_pipeline_progress(self, segment: str, ratio: float,
                               label_text: str,
                               sub_text: str | None = None) -> int:
        """统一驱动全链路进度条（单调不倒退）。

        segment ∈ {translate, review_verdict, review_disposition, writeback}；
        ratio ∈ [0,1] 为该段内进度；映射到 [lo, hi] 后取 max(已达到值)
        防倒退。首次调用退出忙碌动画（setRange 0,0 → 0,100）。高亮当前段。
        返回最终写入的整数值（供调用方广播 metrics 用）。

        2026-08-21 Task #4：进度值直接 setValue（同步可断言），视觉动效
        交给段图例淡入淡出（_update_segment_active → _fade_segment）。
        曾试 QVariantAnimation 滑值，但 valueChanged 需事件泵才触发，
        与同步断言 progress_bar.value() 不兼容，故值不动画、只图例动画。
        """
        lo, hi = _PIPELINE_STAGE_RANGE[segment]
        ratio = max(0.0, min(1.0, ratio))
        value = int(round(lo + (hi - lo) * ratio))
        if value < self._pipeline_floor:
            value = self._pipeline_floor          # 跨段/乱序信号不倒退
        else:
            self._pipeline_floor = value
        if self.progress_bar.maximum() == 0:     # 退出忙碌动画
            self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(value)
        self.progress_label.setText(label_text)
        if sub_text is not None:
            self.progress_sub.setText(sub_text)
        self._update_segment_active(segment)
        return value

    def _reset_pipeline_progress(self) -> None:
        """新一轮运行/项目切换/终态错误时复位进度条与 floor。"""
        self._pipeline_floor = 0
        self._active_segment = None
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self._update_segment_active(None)

    # ── 实时处理流状态文本（2026-08-21 Task #4 配色统一） ──
    # stream_status 各阶段配语义色：idle 灰 / running 薄荷青（翻译、审校
    # 判定/处置、写回）/ succeeded 绿 / warning 琥珀 / error 珊瑚 / stopped
    # 灰。由 QSS 通过 [phase="..."] 动态属性驱动；本方法统一改文本+属性+
    # repolish，杜绝散落 setText 各自硬编码样式。
    def _set_stream_status(self, text: str, *, phase: str | None = None) -> None:
        """实时处理流状态文字 + 阶段配色。phase=None 时保留当前 phase。"""
        self.stream_status.setText(text)
        if phase is not None:
            self.stream_status.setProperty("phase", phase)
            self.stream_status.style().unpolish(self.stream_status)
            self.stream_status.style().polish(self.stream_status)

    def _set_quality_reason(self, summary: str | None) -> None:
        """质量门失败原因标签：有原因时用 reasonStatus 高亮，无则 reasonIdle 弱化。

        summary=None 或空串表示「无失败」，文本回退为「无」并切到弱化样式。
        """
        if summary:
            self.quality_reason_label.setText(f"质量门失败原因：{summary}")
            self.quality_reason_label.setProperty("class", "reasonStatus")
        else:
            self.quality_reason_label.setText("质量门失败原因：无")
            self.quality_reason_label.setProperty("class", "reasonIdle")
        self.quality_reason_label.style().unpolish(self.quality_reason_label)
        self.quality_reason_label.style().polish(self.quality_reason_label)

    def _copy_log(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.log_view.toPlainText())
        Toast.show(self, "运行记录已复制到剪贴板")

    def _clear_log(self):
        self.log_view.clear()

    # ── 开始 ──
    def start(self):
        if self.state.project is None:
            Toast.show(self, "请先在首页打开游戏文件夹", "warning")
            return
        if self._active_run is not None:
            Toast.show(self, "上一个翻译任务仍在停止，请稍候", "warning")
            return
        if self._write_running:
            Toast.show(self, "正在写回文件，请稍候再开始翻译", "warning")
            return
        if self._dry_run_running:
            Toast.show(self, "写回预演进行中，请稍候", "warning")
            return
        api = replace(self.state.api)
        if (api.mode != "local"
                and not (api.base_url and api.api_key and api.model)):
            Toast.show(self, "请先在设置中配置 API", "warning")
            self.window.navigate("settings")
            return
        project = self.state.project
        generation = self.state.project_generation
        project_profile = getattr(project, "profile", None)
        run = _TranslationRun(
            project=project,
            generation=generation,
            api=replace(api),
            profile=(replace(project_profile)
                     if project_profile is not None else GameProfile()),
            cancel=threading.Event(),
            stop_local_after_run=(
                api.mode == "local" and not api.local_keep_alive),
            secrets=[api.api_key],
        )
        self._active_run = run
        self.log_view.clear()
        self._running = True
        # 2026-08-14 卡顿优化：广播级标志——审校页在翻译中挂起
        # entriesChanged 全量重建，翻译结束广播自然补跑
        self.state.translation_running = True
        # 全链路进度条复位：新一轮翻译从 0 开始，floor 清零（上一轮
        # 审校/写回的 floor 残留会让新一轮翻译首条卡在高位不动）。
        self._reset_pipeline_progress()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.retry_btn.setEnabled(False)
        self.write_btn.setEnabled(False)
        self.progress_label.setText("正在请求模型…（第一批可能需要一点时间）")
        self.progress_bar.setRange(0, 0)          # 第一批返回前为忙碌动画
        self.activity_feed.clear()
        self._stream_last_done = 0
        self.activity_feed.append_event("running", "正在请求模型…")
        self._set_stream_status("◐ 正在处理", phase="running")
        signals_holder = {}

        def run_translation():
            return self._translate_worker(run, signals_holder["signals"])

        worker = Worker(run_translation)
        signals_holder["signals"] = worker.signals
        worker.signals.progress.connect(
            lambda stats, p=project, g=generation:
            self._on_progress(stats)
            if self.state.is_current_project(p, g) else None)
        worker.signals.log.connect(
            lambda line, p=project, g=generation:
            self.log_view.appendPlainText(line)
            if self.state.is_current_project(p, g) else None)
        worker.signals.review.connect(
            lambda done, total, p=project, g=generation:
            self._on_review_progress(done, total)
            if self.state.is_current_project(p, g) else None)
        # 审校处置进度（2026-08-20 全链路 3-3-3-1）：处置阶段 60-90% 段
        # 原本静默（反馈重译串行 2-30s/条无进度回调），现经
        # on_disposition_progress → signals.review_disposition 驱动
        worker.signals.review_disposition.connect(
            lambda done, total, p=project, g=generation:
            self._on_review_disposition_progress(done, total)
            if self.state.is_current_project(p, g) else None)
        # worker 线程活动流消息（2026-08-14 闪退修复：worker 内直接调
        # activity_feed.append_event 是跨线程 UI 访问，Windows 上未定义
        # 行为会崩溃——统一经 note 信号回主线程）
        worker.signals.note.connect(
            lambda status, text, p=project, g=generation:
            self.activity_feed.append_event(status, text)
            if self.state.is_current_project(p, g) else None)
        worker.signals.review_summary.connect(
            lambda line, p=project, g=generation:
            self._on_review_summary(line)
            if self.state.is_current_project(p, g) else None)
        # 写回后地毯式审计进度（2026-08-26 任务四：audit_writeback 逐文件
        # 确定性 PASS/FAIL + 模型软复核 FLAG/不可用，worker 线程经此信号
        # 回主线程——写回审计在 GUI 实时处理流/运行记录中必须可见。
        # 2026-08-26 用户要求：实时处理流 AND 运行记录都要有信息显示，
        # 与审校/翻译一致——此处同时写入 activity_feed 与 log_view。）
        worker.signals.audit.connect(
            lambda status, text, p=project, g=generation:
            self._on_audit_event(status, text)
            if self.state.is_current_project(p, g) else None)
        worker.signals.finished.connect(
            lambda stats, p=project, g=generation:
            self._on_finished(stats)
            if stats is not None and self.state.is_current_project(p, g)
            else None)
        worker.signals.error.connect(
            lambda error, p=project, g=generation, r=run:
            self._on_error(error, tuple(r.secrets))
            if self.state.is_current_project(p, g) else None)
        worker.signals.finished.connect(
            lambda _result, r=run: self._on_run_drained(r))
        worker.signals.error.connect(
            lambda _error, r=run: self._on_run_drained(r))
        self._worker = worker
        self._pool.start(worker)

    def _translate_worker(self, run: _TranslationRun, signals):
        project = run.project
        generation = run.generation
        with self.state.project_lease(project, generation) as acquired:
            if not acquired:
                return None
            return self._translate_with_lease(run, signals)

    def _translate_with_lease(self, run: _TranslationRun, signals):
        project = run.project
        generation = run.generation

        def on_progress(stats):
            signals.progress.emit(stats)     # Qt 信号自动排队回主线程

        def on_log(line: str):
            signals.log.emit(line)

        api = replace(run.api)
        profile = replace(run.profile)
        cancel = run.cancel
        store = project.store
        runtime = None
        try:
            glossary = GlossaryStore(self.state.app_dir / "glossary.db")
            glossary.init_schema()
            glossary_rows = glossary.list_all()
            # 2026-08-14 用户要求「大大精简提示词」：术语/专名/知识不再
            # 全量拼 system_prompt（296 条术语 ≈ 2800 tokens + 25 条知识
            # 对照 ≈ 884 tokens 是 request exceeds context 根因）——
            # 全部改为按条目检索命中注入：术语 glossary_hits、知识
            # knowledge_hits（match_text 精确命中）、向量/语境参考
            # _context_reference_lines（Top-3），见 build_batch_user_prompt
            # 与 batch_translator._build_item。译例（knowledge_pairs）并入
            # glossary——native 降级重试（Hy-MT2 无 system prompt）靠
            # references 的 terms 机制带出译例
            knowledge = KnowledgeBase(self.state.app_dir / "knowledge.db")
            knowledge_pairs = knowledge.format_reference_pairs()
            # AI 辅助识别（0.38.0 任务二④）：候选层二次分类在翻译前
            # 执行——升格条目（typetree_candidate/prefilter 经模型判
            # display）必须在本函数后文 entries 构建前落库，才能进本次
            # run_scope。fail-closed：模型缺失/请求失败只留日志，
            # 绝不阻断翻译主链（识别是增益环节，宁漏勿坏）。
            try:
                from hanhua.core.ai_recognition import run_ai_recognition
                ai_report = run_ai_recognition(
                    store, self.state.resource_dir, on_log=on_log)
                if ai_report.upgraded:
                    signals.note.emit(
                        "running",
                        f"AI 辅助识别：新升格 {ai_report.upgraded} 条候选文本"
                        f"进入本次翻译")
            except Exception as exc:  # noqa: BLE001 增益环节不阻断主链
                on_log(f"AI 辅助识别跳过：{exc}")
            # 专名收集仅用于翻译后术语库学习（learn_proper_names）
            entries0 = [self._entry_from_row(r) for r in store.get_entries()]
            collected_names = collect_known_names(
                [str(e.original or "") for e in entries0])
            glossary.close()
            # 经验记忆（AgentMemory）：跨游戏持久。本次运行前重置会话统计；
            # 记忆的参考译例并入 glossary（active 记忆，混合运用参考档）；
            # 高置信短语由 BatchTranslator 翻译前直接应用（仍过质量门）
            if self._agent_memory is None:
                self._agent_memory = AgentMemory(
                    self.state.app_dir / "agent_memory.db")
                self._agent_memory.init_schema()
            agent_memory = self._agent_memory
            agent_memory.session_reset()
            agent_pairs = agent_memory.reference_pairs()
            # 知识检索统一门面（审计 Phase C，P1-1）：懒装配一次，跨次
            # 翻译复用。context/vector 证据跨游戏沉淀——翻译前 index_outbox
            # 让历史证据可被本次命中，翻译后再次索引本次新沉淀（最终一致）。
            agent_game = str(getattr(
                getattr(project, "game_dir", None), "name", "")
                or getattr(project, "name", ""))
            if self._knowledge_retrieval is None:
                from hanhua.core.knowledge_retrieval import (
                    create_knowledge_retrieval)
                self._knowledge_retrieval = create_knowledge_retrieval(
                    self.state.app_dir,
                    service_dir=self.state.resource_dir,
                    game=agent_game)
            knowledge_retrieval = self._knowledge_retrieval
            try:
                indexed = knowledge_retrieval.index_outbox()
            except Exception:  # noqa: BLE001 知识链路故障不阻断翻译
                indexed = 0
            on_log(f"知识检索：{knowledge_retrieval.capability().summary()}"
                   + (f" · 已索引 {indexed} 条" if indexed else ""))
            # 流水线 rail（#15）：翻译阶段开始即广播，首页 rail 实时转
            # running（此前 rail 全程停在扫描后的旧状态）。
            self.state.pipelinePhase.emit(
                "translation", "running", "正在准备翻译环境…", "")
            if api.mode == "local":
                on_log("正在启动本地 Hy-MT2 模型服务…")
                if cancel.is_set():
                    raise RuntimeError("translation cancelled")
                runtime = self.state.local_model.ensure_running(
                    api, cancellation_event=cancel)
                run.local_started = True
                if not self.state.is_current_project(project, generation):
                    return None
                api = replace(
                    api, base_url=runtime.endpoint, api_key=runtime.api_key,
                    model=runtime.model,
                )
                run.secrets.append(runtime.api_key)
                on_log(
                    f"本地服务已就绪：{runtime.backend.upper()} · 端口 {runtime.port}")
            # 2026-08-14：system_prompt 只含角色+精简规则（术语/专名/知识
            # 全量块已移除，全部按条目检索命中注入）
            system = build_system_prompt(profile, "")
            if cancel.is_set():
                raise RuntimeError("translation cancelled")
            if not self.state.is_current_project(project, generation):
                return None
            recoveries = 0
            while True:
                if cancel.is_set():
                    raise RuntimeError("translation cancelled")
                if not self.state.is_current_project(project, generation):
                    return None
                client = create_client(api)
                lang = (f"{profile.source_lang or 'auto'}→"
                        f"{profile.target_lang or 'zh-CN'}")
                concurrency = (runtime.parallel if api.mode == "local"
                               else api.concurrency)
                batch_size = (max(1, int(api.local_batch_size))
                              if api.mode == "local" else api.batch_size)
                translator = BatchTranslator(
                    client, batch_size=batch_size, concurrency=concurrency,
                    memory=store, model=api.model, lang=lang,
                    system_prompt=system,
                    glossary=[(row["term"], row["translation"])
                              # candidate 仅参考不强制（与
                              # format_for_prompt 对齐，F10 实证）
                              for row in glossary_rows
                              if row.get("status", "active") == "active"]
                             + knowledge_pairs + agent_pairs,
                    # 强制词对（质量门 glossary_mismatch 判定）：仅术语库
                    # active + 知识库译例。经验记忆词对只做参考注入（prompt
                    # 译例 + 精确命中直填）不做强制——reference_pairs 设计
                    # 即「参考而非强制」，并入 glossary 强制后单 token 记忆
                    # 词对成硬规则（Morfosi 64 条实证：('Locked','锁定')
                    # 命中自然句 "IT'S LOCKED." 全灭）。强制过滤在质量门
                    # （F10 语境豁免），不在记忆库。
                    glossary_force=[(row["term"], row["translation"])
                                    for row in glossary_rows
                                    if row.get("status", "active") == "active"]
                                   + knowledge_pairs,
                    agent_memory=agent_memory,
                    agent_game=agent_game,
                    # 知识检索接线（审计 Phase C，P1-1）：语境直填 +
                    # 向量相似去重/召回在生产入口首次生效
                    context_store=knowledge_retrieval.context_store,
                    context_game=agent_game,
                    vector_recall=knowledge_retrieval.vector_recall,
                    # 知识命中注入（2026-08-14 用户要求：按文本检索相关
                    # 才注入，不全量拼 prompt——match_text 按原文精确
                    # 命中历史规则）。knowledge 实例全程存活至 run 结束
                    # （_build_item 每批查询），close 在翻译完成后
                    knowledge=knowledge,
                    cancellation_event=cancel)
                run.attach_translator(translator)
                entries = [self._entry_from_row(r) for r in store.get_entries()]
                total_pending = sum(
                    is_actionable_translation(entry) for entry in entries)
                on_log(f"开始翻译：共 {len(entries)} 条，待翻译 {total_pending} 条")
                # 去重口径说明（2026-08-22 用户实证「每批条数忽大忽小」）：
                # 每批 N 条作用于去重后的唯一文本（同原文+同角色只翻一次），
                # 一次模型译文会扇出应用到所有同文条目——活动流的
                # 「本批完成 N 条」统计的是真实条目数，因此可能出现一批
                # 完成几十条（同文组大）或两三条（几乎无重复）的正常波动。
                if api.mode == "local":
                    on_log(f"本地模型逐条翻译：并发 {concurrency} 路 · "
                           f"进度粒度 {batch_size} 条（本地模式不分批请求，"
                           "「每批」仅控制进度刷新粒度）")
                else:
                    on_log(f"每批 {batch_size} 条唯一文本")
                if total_pending == 0:
                    low_pending = sum(
                        1 for e in entries
                        if e.status == "pending"
                        and e.meta.get("confidence") == "low")
                    if low_pending:
                        on_log(f"没有可翻译条目；另有 {low_pending} 条低置信度"
                               f"条目（引擎消息/疑似噪音）留档，可在文本审校"
                               f"按「低置信度」筛选查看")
                    else:
                        on_log("没有待翻译条目（全部已翻译或已锁定），"
                               "可直接点击写回游戏")
                if api.mode == "local":
                    on_log(f"模型：{api.model} · 并发 {concurrency} 路（逐条请求）")
                else:
                    on_log(f"模型：{api.model} · 并发 {concurrency} · 每批 {batch_size} 条")
                on_log(f"请求地址：{client.url}")
                stats = translator.run(entries, progress_cb=on_progress)
                run.detach_translator()
                if (api.mode != "local" or stats.failed == 0
                        or recoveries >= MAX_LOCAL_RECOVERIES
                        or not _critical_local_failures(store)):
                    break
                recoveries += 1
                on_log(f"检测到本地服务故障（HTTP 502），正在以保守模式重启服务"
                       f"（第 {recoveries}/{MAX_LOCAL_RECOVERIES} 次）…")
                try:
                    runtime = self.state.local_model.restart_conservative(
                        cancellation_event=cancel)
                except LocalModelError as exc:
                    on_log(f"保守模式重启失败：{exc}")
                    break
                if runtime is None:
                    break
                on_log(f"服务已重启：CPU 模式 · 单槽 · 端口 {runtime.port}")
                reset = 0
                for row in store.get_entries(status="failed"):
                    store.reset_to_pending(row["file_id"], row["key_path"])
                    reset += 1
                on_log(f"已将 {reset} 条失败重置为待翻译，继续…")
            # 知识命中注入结束——knowledge 实例完成使命后关闭
            # （2026-08-14 起全程存活供 _build_item 每批查询，此前
            # 构造前即 close）
            try:
                knowledge.close()
            except Exception:  # noqa: BLE001
                pass
            on_log(f"翻译完成：{stats.done} 条已翻译"
                   f"（记忆命中 {stats.from_memory}），失败 {stats.failed} 条，"
                   f"请求 {stats.requests} 次")
            # 翻译后：本次沉淀的共识证据入向量索引（Phase C 增量索引，
            # 审核沉淀的 ContextEvidence 供下次/其他游戏向量复用）
            try:
                indexed = knowledge_retrieval.index_outbox()
            except Exception:  # noqa: BLE001
                indexed = 0
            if indexed:
                on_log(f"知识检索：本次沉淀 {indexed} 条证据入向量索引")
            if entries:
                learn_g = GlossaryStore(self.state.app_dir / "glossary.db")
                learn_g.init_schema()
                learned = learn_g.learn_proper_names(
                    entries, collected_names, str(profile.game_name or ""))
                if learned:
                    on_log(f"术语库学习：新增 {learned} 条专名"
                           f"（跨游戏复用）")
                # 翻译 C6：语义审核 + C5 门禁沉淀（与 runner 同源核心）。
                # Phase A（2026-08-13 架构审计）：GUI 不再自己拼审核业务——
                # review_entries 已成为统一审核管线，传入 translator（反馈
                # 重译 + 再审收敛）、store（终态 + meta 原子落库，写回门
                # 生效——MAJOR/CRITICAL/blocked/审核错误不可写回）、memory
                # （记忆门禁）、app_dir（审核模型从资源根定位，不依赖 cwd，
                # 审计 §5 P0-8）、cancellation_event（可取消，P0-9）。
                # §68 设置：开关关闭时跳过审核；策略映射送审率上限
                # （快速 5% / 平衡 15% / 严格 30%，risk_gate 硬约束上限）。
                # 2026-08-21：开关（ai_review_enabled）已从设置页移除——
                # 语义审核是翻译管线固定环节（设计文档 §26 全链路），不再
                # 提供关闭入口；字段保留兼容旧配置，恒走审核。
                try:
                    review_summary = None
                    if True:  # 恒审核（§26 全链路闭环；开关字段保留兼容）
                        # 审核进度实时可见（用户实证：翻译完成后界面无
                        # 反馈，2.6GB 审核模型首次启动 30-120 秒 + 逐条
                        # 判定期间 UI 像卡死、写回被锁）——启动提示先于
                        # review_entries 广播，逐条进度走 signals.review
                        self.state.pipelinePhase.emit(
                            "review", "running",
                            "正在语义审核…", "")
                        online_audit = api.mode == "api"
                        signals.note.emit(
                            "running",
                            "正在连接语义审核模型…"
                            "（本地首次约 30-120 秒，逐条进度实时刷新）"
                            if not online_audit else
                            "正在调用云端语义审核接口…")
                        on_log(
                            "语义审核：正在调用云端审核接口…"
                            if online_audit else
                            "语义审核：正在启动本地审核模型"
                            "（Qwen3.5-4B，首次加载约 30-120 秒）…")
                        # 2026-08-14 全量送审：抽样（5-30%）让多数问题译文
                        # 漏网——汉化游戏少数不准是常态，只有扫完所有译文
                        # 才能揪出语境错误（用户实证「自动重译没生效」根因）。
                        # ai_review_strategy 不再参与（设置页已改全量说明）。
                        review_summary = review_entries(
                            entries, learn_g,
                            game_name=str(profile.game_name or ""),
                            # 设计文档 §16：Game Context 注入审校——审校
                            # 模型看到与翻译同一份游戏语境（游戏背景/
                            # 语言风格/相关角色/相关术语/翻译注意事项），
                            # 判定「语气不符角色设定」「术语与世界觀不匹配」
                            # 类问题有据可依。profile 已带 context_* 字段
                            #（GameProfile 扩展），直接传入。
                            profile=profile,
                            on_note=on_log,
                            on_progress=lambda done, total:
                            signals.review.emit(done, total),
                            # 审校处置进度（2026-08-20 全链路 3-3-3-1）：
                            # 处置阶段 = 反馈重译 + 再审收敛，串行 2-30s/条，
                            # 原本完全静默——经此回调驱动 60-90% 段
                            on_disposition_progress=lambda done, total:
                            signals.review_disposition.emit(done, total),
                            translator=translator,
                            memory=store,
                            store=store,
                            # 审核模型从资源根定位（models/*.gguf 在
                            # resource_dir，不在 ~/.hanhua——此前传 app_dir
                            # 导致「审核模型缺失」→ TRANSPORT_ERROR）
                            app_dir=self.state.resource_dir,
                            # 数据根分离：语境/向量检索库（context.db/
                            # vector.db）在 ~/.hanhua，与翻译阶段沉淀
                            # 同库——否则审校在资源根建空库，按需检索
                            # 注入拿不到任何证据（2026-08-14）
                            data_dir=self.state.app_dir,
                            model_name=api.model,
                            lang=lang,
                            max_send_rate=1.0,   # 全量送审（2026-08-14）
                            # 一次给多条（用户「节约时间，上下文不少」）：
                            # 20 条一批共享上下文（ctx 8192 预算：系统
                            # prompt+输出 ≈2200 固定，典型条目 50-100
                            # token → 20 条 ≈ 4-5k 安全；长文本批溢出
                            # 自动整组逐条兜底不崩；>20 输出数组截断/
                            # 漏条目概率上升，收益递减——20 是平衡点）
                            review_batch_size=20,
                            cancellation_event=cancel,
                            # 在线 API 模式：审核走云端端点（对应 kind 配置；
                            # Service 内部判完整性，缺项自动回退本地）
                            online_review_cfg=(
                                self.state.settings.api_config("review")
                                if api.mode == "api" else None))
                    # P4：无论 used 与否都暂存完整汇总——写回后导出记录时
                    # 传入 export_records（review/review-report.md +
                    # summary §3.5 随记录落盘）。used=False 时 detail
                    # 为空，记录侧自然跳过。
                    if review_summary is not None:
                        self._last_review_summary = review_summary
                    if review_summary and review_summary["used"]:
                        flagged = review_summary["flagged"]
                        added = review_summary["pairs_added"]
                        rejected_n = len(review_summary["pairs_rejected"])
                        # 口径修正（2026-08-26）：日志数字与审校页「待审核」
                        # 筛选完全一致——审校页 filterAcceptsRow 用的是
                        # 终态 review_outcome ∈ REVIEW_PENDING_OUTCOMES ∪
                        # 机械失败（models.needs_review + status=="failed"），
                        # 而非判定时刻的 MAJOR/CRITICAL 计数（flagged）。
                        # 判为不合格但反馈重译+再审收敛到 APPROVED 系的
                        # 条目不显示在待审核，若用 len(flagged) 报数，去
                        # 审校页筛选总是少于日志所示。改用终态口径聚合。
                        outcomes = review_summary.get("outcomes") or {}
                        pending_manual = sum(
                            outcomes.get(k, 0) for k in REVIEW_PENDING_OUTCOMES)
                        # Phase A：终态已由管线原子落库（store 的
                        # batch_update_translation_results），此处只做汇总日志
                        line = (f"语义审核：不合格 {pending_manual} 条"
                                f"（审校页「待审核」可筛选）")
                        if review_summary["blocked"]:
                            line += (f" · 重译未收敛阻塞 "
                                     f"{review_summary['blocked']} 条"
                                     f"（坏译文已从发布槽移除）")
                        if review_summary["errors"]:
                            line += (f" · 审核错误 "
                                     f"{review_summary['errors']} 条"
                                     f"（不可发布）")
                        if review_summary["cancelled"]:
                            line += (f" · 取消 {review_summary['cancelled']} 条")
                        if review_summary["deferred_due_to_budget"]:
                            line += (f" · 预算截断 "
                                     f"{review_summary['deferred_due_to_budget']}"
                                     f" 条（人工队列）")
                        if added:
                            line += f" · 术语沉淀 {added} 条词对"
                        if rejected_n:
                            line += f" · C5 门禁拒绝 {rejected_n} 条"
                        on_log(line)
                        # 2026-08-14 用户实证「只有完成二字，过了两分钟
                        # 才出现反馈」：汇总此前只进默认折叠的日志面板——
                        # 完成时同步经 review_summary 信号回主线程弹
                        # Toast + 活动流（worker 线程不得直接碰 UI）
                        signals.review_summary.emit(line)
                        # 2026-08-15 流水线重做：审校终态广播到「审校」
                        # rail 节点（此前与翻译共用「翻译质量」节点）
                        self.state.pipelinePhase.emit(
                            "review",
                            ("succeeded"
                             if not pending_manual
                             and not review_summary["errors"] else "warning"),
                            (f"审校完成：不合格 {pending_manual} 条"
                             if pending_manual else "审校完成：全部通过"),
                            f"重译收敛 {review_summary['converged']}"
                            f" · 阻塞 {review_summary['blocked']}"
                            if review_summary.get("retranslated")
                            else "")
                        # #43 阶段 F：审核报告落盘（reviews/ 目录，风险
                        # 分布 + 失败明细；失败降级不阻断——报告是留档，
                        # 不是主流程）
                        try:
                            from hanhua.core.reviewer import (
                                write_review_report)
                            report_dir = self.state.app_dir / "reviews"
                            report_dir.mkdir(parents=True, exist_ok=True)
                            safe = re.sub(r'[\\/:*?"<>|\s]+', "_",
                                          str(profile.game_name or "game"))
                            write_review_report(
                                review_summary,
                                report_dir / f"{safe}-review-report.md",
                                game_name=str(profile.game_name or ""))
                        except Exception:  # noqa: BLE001
                            pass
                    elif review_summary is not None \
                            and not review_summary.get("used"):
                        # 未送审（分流直放/审核服务不可用）——同样要
                        # 回主线程给汇总提示，不能「审核结束却零反馈」
                        #（2026-08-14 用户实证：只有进度「完成」二字）
                        signals.review_summary.emit(
                            "语义审核：未送审（无风险条目直放，"
                            "或审核服务不可用，见日志）")
                        self.state.pipelinePhase.emit(
                            "review", "warning", "审校未送审",
                            "无风险条目直放或审核服务不可用")
                except Exception as exc:  # noqa: BLE001
                    on_log(f"语义审核失败：{exc}")
                    # 2026-08-14 用户实证「审核完成只有完成二字」：
                    # 汇总此前只在 review_entries 正常返回时 emit——管线
                    # 异常时 except 只进日志面板（默认折叠），用户看到
                    # 进度「完成」后无任何汇总。失败也要广播（Toast +
                    # 活动流），说明失败原因与后续动作，不静默。
                    signals.review_summary.emit(
                        f"语义审核失败：{str(exc)[:160]}"
                        f"（译文保持原状态，可在审校页重新审核）")
                    self.state.pipelinePhase.emit(
                        "review", "failed", "审校失败",
                        str(exc)[:60])
                # Phase B PendingEvidence（审计 §5 P0-3）：审后记忆结算——
                # APPROVED → promote（pending 记忆可命中）；判坏 → 撤销
                # （坏译连 pending 都不留）；审核关闭/不可用（无终态）→
                # 机械质量门已是最后裁决，promote（保持既有记忆行为）。
                settled = settle_translation_memory(
                    store, entries, api.model, lang)
                if settled["promoted"] or settled["revoked"]:
                    on_log(f"记忆结算：提交 {settled['promoted']} 条"
                           f" · 撤销 {settled['revoked']} 条坏记忆")
                learn_g.close()
                # 知识库学习：从「该翻未翻」回显条目沉淀特殊情况模式
                learn_kb = KnowledgeBase(self.state.app_dir / "knowledge.db")
                learned_kb, hits_kb = learn_kb.learn(
                    entries, str(profile.game_name or ""),
                    names=set(collected_names))
                # #43 阶段 G（重构指令 §16 反馈学习）：审核失败结构化
                # 沉淀（与 runner 同源 fail_case 域）——MAJOR/CRITICAL
                # 语义错误 + REVIEW_ERROR 管线错误，后续游戏按原文召回
                # 同类失败作反例（重译闭环收敛后仅剩未收敛/管线错误）
                review_failures = (review_summary or {}).get(
                    "review_failures") or []
                if review_failures:
                    failure_added = sum(
                        1 for f in review_failures
                        if learn_kb.record_review_failure(f))
                    if failure_added:
                        on_log(f"审核反例沉淀：{failure_added} 条失败案例"
                               f"（知识库 fail_case 域，后续游戏自动参考）")
                learn_kb.close()
                if learned_kb or hits_kb:
                    on_log(f"知识库学习：新增 {learned_kb} 条规则"
                           f" · 累计命中 {hits_kb} 条"
                           f"（特殊情况模式沉淀，后续游戏自动复用）")
            if stats.elapsed > 0:
                on_log(
                    f"耗时 {stats.elapsed:.1f} 秒"
                    f" · 吞吐 {stats.rate_per_minute:.0f} 条/分"
                    f" · 输入 {stats.input_tokens} tokens"
                    f" · 输出 {stats.output_tokens} tokens")
            return stats
        finally:
            run.detach_translator()
            if run.stop_local_after_run and run.local_started:
                self.state.local_model.stop()

    @staticmethod
    def _entry_from_row(row: dict) -> TextEntry:
        """DB 行 → TextEntry（统一口径见 models.entry_from_row）。"""
        return entry_from_row(row)

    def _update_progress_widgets(self, stats):
        """进度条/数字按批粒度实时更新（O(1)，不进全量刷新节流）。

        口径（2026-08-14 用户实证「剩余/待翻译/失败总数打架」统一）：
        - 剩余 = total - done（done 只计成功译出；失败可重试仍属待翻译
          ——与顶部 chips「待翻译」同源，失败不再从剩余中扣除，两侧
          数字恒一致）；
        - 失败单独显示（可重试，归入剩余但不重复计数）。

        2026-08-20 全链路分段进度条：翻译阶段映射到 0-30% 段（ratio =
        done/total），经 _set_pipeline_progress 接续推进且不倒退。
        """
        if stats is None:
            return
        n_total = stats.total
        n_done = stats.done
        n_failed = stats.failed
        ratio = (n_done / n_total) if n_total else 0.0
        self._set_pipeline_progress(
            "translate", ratio,
            label_text=f"{n_done} / {n_total} 条",
            sub_text=f"剩余 {max(0, n_total - n_done)} 条 · 失败 {n_failed} 条")

    def _on_progress(self, stats):
        self._last_stats = stats
        self._update_progress_widgets(stats)
        # #6：批粒度 progress 高频触发——计数刷新（全量 O(N) 查库）与
        # entriesChanged 广播共用 ≥1s 节流，避免万级条目下每批卡主线程。
        # 进度条/活动流/状态文本仍按批粒度实时更新（O(1)）。
        now = time.monotonic()
        if now - self._last_chip_refresh >= 1.0:
            self._last_chip_refresh = now
            self._refresh_chips()
        # 实时处理流（§34）：批粒度事件，不伪造逐条数据
        # （2026-08-22 口径说明：delta 是真实条目数——同原文条目共享
        # 一条译文一次性落库，所以「本批完成」可远大于每批唯一文本数）
        done = stats.done
        prev = self._stream_last_done
        if done > prev:
            delta = done - prev
            source = "（记忆命中）" if delta and stats.from_memory else ""
            if delta >= 20:
                source = "（同文条目共享译文，一次性落库）"
            self.activity_feed.append_event(
                "success",
                f"本批完成 {delta} 条 · 累计 {done} / {stats.total} {source}")
            self._stream_last_done = done
        if stats.total:
            self._set_stream_status(
                f"◐ 正在处理 {done + stats.failed} / {stats.total} 条",
                phase="running")
        # 实时广播（#13/#15）：节流 ≥1s 一次，驱动首页分数与流水线 rail
        # 边翻边刷——此前要等全部完成才 emit entriesChanged，首页数字
        # 全程不动。节流避免批粒度（2 条/批）高频触发首页 O(N) 重扫。
        if now - self._last_phase_emit >= 1.0:
            self._last_phase_emit = now
            self.state.entriesChanged.emit()
            self.state.pipelinePhase.emit(
                "translation", "running", "正在翻译…",
                f"已完成 {done + stats.failed} / {stats.total} 条"
                f" · {stats.rate_per_minute:.0f} 条/分")

    def _on_review_progress(self, done: int, total: int):
        """语义审核判定进度（worker 线程经 signals.review 回主线程）。

        逐条判定期间界面必须实时推进（审核在翻译 worker 内同步执行，
        无反馈 = 用户看到「卡住不动」且写回被锁）。标签逐条更新，
        activity_feed 按 ≥5 条节流一次，末条必报。

        2026-08-20 全链路分段进度条：判定阶段映射到 30-60% 段（ratio =
        done/total），经 _set_pipeline_progress 接续翻译段末值 30 单调
        推进，不再归零再爬（此前 _review_bar_reset 把翻译满条 100 归零
        再从 0 爬的「倒退」彻底消除）。
        """
        self._last_review = (done, total)
        if total > 0:
            self._set_pipeline_progress(
                "review_verdict", done / total,
                label_text=f"语义审核 {done}/{total} 条…")
            self._set_stream_status(
                f"◐ 语义审核 {done}/{total} 条", phase="running")
        now = time.monotonic()
        if done >= total or now - self._last_review_emit >= 0.8:
            self._last_review_emit = now
            self.activity_feed.append_event(
                "info" if done < total else "success",
                f"语义审核：{done}/{total} 条"
                + ("" if done < total else " · 完成"))

    def _on_review_disposition_progress(self, done: int, total: int):
        """语义审核处置进度（worker 线程经 signals.review_disposition 回主线程）。

        处置阶段 = 反馈重译（_retranslate_with_feedback）+ 再审收敛（≤2 轮），
        串行 2-30s/条——判定完成（review_batch）后到这里，此前完全静默，
        UI 停在「N/N 条完成」后界面静止数分钟。on_disposition_progress
        回调驱动 60-90% 段，让等待有可见进度。total=0（无不合格条目）
        时直接跳满该段。
        """
        if total <= 0:
            self._set_pipeline_progress(
                "review_disposition", 1.0,
                label_text="审校处置完成")
            return
        self._set_pipeline_progress(
            "review_disposition", done / total,
            label_text=f"审校处置 {done}/{total} 条…")
        self._set_stream_status(
            f"◐ 审校处置 {done}/{total} 条", phase="running")
        now = time.monotonic()
        if done >= total or now - self._last_review_emit >= 0.8:
            self._last_review_emit = now
            self.activity_feed.append_event(
                "info" if done < total else "success",
                f"审校处置：{done}/{total} 条"
                + ("" if done < total else " · 完成"))

    def _on_review_summary(self, line: str):
        """审核完成汇总弹窗（signals.review_summary 回主线程）。

        2026-08-14 用户实证「审核完成只有完成二字，过了两分钟才出现
        反馈」：汇总此前只进默认折叠的日志面板。完成时立即 Toast +
        活动流。长消息按「 · 」拆分换行（Toast 单行不换行会撑出屏幕），
        驻留 8 秒保证看完。
        """
        self.activity_feed.append_event("success", line)
        Toast.show(self, line.replace(" · ", "\n"), "success",
                   duration_ms=8000)

    def _on_audit_event(self, status: str, text: str) -> None:
        """写回审计事件：同时进实时处理流与运行记录（用户要求两者可见）。

        2026-08-26 用户实证「写回检查看不到实际信息」：audit 信号此前只
        进 activity_feed（实时处理流），运行记录（log_view）没有——与
        审校/翻译不一致。统一：状态标签（running/success/warning/error）
        驱动活动流样式，明细行同步追加到运行记录。终态（success/warning/
        error）除活动流外再 Toast 一次，保证用户一定能看到审计结论。
        """
        self.activity_feed.append_event(status, text)
        self.log_view.appendPlainText(text)
        if status in {"success", "warning", "error"}:
            Toast.show(
                self, text,
                "success" if status == "success" else
                "warning" if status == "warning" else "error",
                duration_ms=8000)

    def _on_finished(self, stats):
        self._running = False
        # 先复位再广播：审校页挂起的 reload 由本次 entriesChanged 补跑
        self.state.translation_running = False
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.retry_btn.setEnabled(stats.failed > 0)
        self._last_stats = stats
        self._refresh_chips()
        self._set_primary(self.write_btn)
        self.state.entriesChanged.emit()
        self.state.pipelinePhase.emit(
            "translation",
            "succeeded" if stats.failed == 0 else "warning",
            (f"翻译完成 {stats.done} 条"
             + (f" · 失败 {stats.failed} 条" if stats.failed else "")),
            f"耗时 {stats.elapsed:.1f} 秒 · {stats.rate_per_minute:.0f} 条/分")
        if stats.failed:
            self.activity_feed.append_event(
                "error", f"{stats.failed} 条失败（可重试）")
        else:
            self.activity_feed.append_event("success", "全部完成")
        self._set_stream_status(
            "○ 已完成",
            phase="warning" if stats.failed else "succeeded")
        if stats.failed:
            export_path = self._export_fail_record()
            Toast.show(
                self,
                f"完成，{stats.failed} 条失败可重试"
                + (f" · 失败记录已导出：{export_path}" if export_path else ""),
                "warning")
        else:
            Toast.show(self, "翻译完成", "success")

    def _export_fail_record(self, error_title: str = "", error_detail: str = ""):
        """本次汉化失败条目（及附加错误）落盘到 docs/fail record。

        所有失败路径都经过这里：翻译失败条目、写回失败、写回未通过验证、
        翻译出错——保证失败日志不丢失。
        """
        if self.state.project is None:
            return None
        from hanhua.core.fail_export import export_fail_record
        out_dir = self.state.resource_dir / "docs" / "fail record"
        try:
            return export_fail_record(
                self.state.project, out_dir,
                error_title=error_title, error_detail=error_detail)
        except OSError as exc:
            Toast.show(self, f"失败记录导出失败：{exc}", "error")
            return None

    def _export_records(self, write_result=None,
                        error_title: str = "",
                        error_detail: str = "") -> Path | None:
        """写回后自动生成完整记录文档（docs/all record/游戏名/）。

        与 runner 闭环同一文档结构；成功与失败路径都落盘，保证手动
        汉化每次写回都有记录依据（用户实测问题可复盘）。
        """
        if self.state.project is None:
            return None
        from hanhua.core.record_writer import export_records
        out_root = self.state.resource_dir / "docs" / "all record"
        api = getattr(self.state, "api", None)
        model_name = str(getattr(api, "model", "") or "")
        # 经验记忆报告随记录文档落盘（本次会话记忆活动快照）
        agent_report = None
        if self._agent_memory is not None:
            agent_report = self._agent_memory.session_report(
                game=str(getattr(
                    getattr(self.state.project, "game_dir", None),
                    "name", "") or ""))
        try:
            # P4：本轮语义审核汇总暂存传入——review/review-report.md 与
            # summary §3.5 随记录落盘；locators 把 entry_id 映射回
            # file_id:key_path，与 translated.txt/blocked.txt 同键。
            review_results = None
            review_summary = getattr(self, "_last_review_summary", None)
            if review_summary:
                locators = review_summary.get("locators") or {}
                review_results = {
                    locators.get(eid, eid): rr
                    for eid, rr in (review_summary.get("results") or {}).items()
                }
            return export_records(
                self.state.project, out_root,
                write_result=write_result,
                error_title=error_title, error_detail=error_detail,
                model_name=model_name,
                agent_report=agent_report,
                run_stats=getattr(self, "_last_stats", None),
                review_results=review_results or None,
                review_summary=review_summary)
        except Exception as exc:  # noqa: BLE001 记录导出不阻断写回主流程
            Toast.show(self, f"记录导出失败：{exc}", "error")
            return None

    def _on_error(self, err: str, secrets=()):
        self._running = False
        self.state.translation_running = False
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._refresh_chips()
        # 翻译出错同样广播：审校页挂起的 reload 补跑（否则错误后
        # 页面停在翻译前快照，用户看不到失败状态）
        self.state.entriesChanged.emit()
        self._set_primary(self.write_btn)
        self._set_stream_status("○ 已停止", phase="error")
        self.progress_label.setText("翻译出错")
        self.state.pipelinePhase.emit(
            "translation", "failed", "翻译出错", err[:60])
        diagnostic = sanitize_exception(RuntimeError(str(err)), secrets)
        Toast.show(self, f"翻译出错：{json.dumps(diagnostic, ensure_ascii=False)}", "error")
        export_path = self._export_fail_record(
            "翻译出错", json.dumps(diagnostic, ensure_ascii=False))
        if export_path:
            self.log_view.appendPlainText(f"失败记录已导出：{export_path}")

    def _on_run_drained(self, run: _TranslationRun):
        if self._active_run is run:
            self._active_run = None
            self.start_btn.setEnabled(self.state.project is not None)
            self.stop_btn.setEnabled(False)

    # ── 停止 / 重试 ──
    def stop(self):
        run = self._active_run
        requested = run is not None
        if run is not None:
            run.request_stop(self.state.local_model)
        if requested:
            self.stop_btn.setEnabled(False)
            self.log_view.appendPlainText("正在停止…未完成条目保留为待翻译，可随时继续")

    def retry_failed(self):
        store = self.state.project.store
        for r in store.get_entries(status="failed"):
            # #9：重置待译清旧审核终态，重译成功不再被残留 BLOCKED 拒绝
            store.reset_to_pending(r["file_id"], r["key_path"])
        self.state.entriesChanged.emit()
        self.log_view.appendPlainText("已标记失败条目为待翻译")
        self.start()

    # ── 写回 ──
    def dry_run_writeback(self):
        """写回预演（0.39.0 M4，§62）：只分析不落盘——后台跑
        build_writeback_plan（与 write_back_v2 / write_back_text 同一
        分类链、零磁盘零 store 副作用），报告进日志面板。不替代正式
        写回守卫（unblocked/质量门仍由 write_back() 把关），只是把
        正式写回的分类结果提前完整呈现。"""
        if self.state.project is None:
            Toast.show(self, "请先在首页打开游戏文件夹", "warning")
            return
        if self._write_running:
            Toast.show(self, "正在写回文件，预演请等写回完成", "warning")
            return
        if self._dry_run_running:
            Toast.show(self, "预演正在进行，请稍候", "warning")
            return
        project = self.state.project
        generation = self.state.project_generation
        self._dry_run_running = True
        self.dry_run_btn.setEnabled(False)
        self.log_view.appendPlainText("正在生成写回预演（不修改游戏）…")

        def run_dry_run():
            with self.state.project_lease(project, generation) as acquired:
                if not acquired:
                    return None
                from hanhua.core.unity.writeback_plan import (
                    build_writeback_plan)
                # 分诊服务与正式写回同源探测（write_all 1860-1866 同款）：
                # 模型在场才启用，缺席 → 分诊层不参与（预演口径 = 正式
                # 写回口径）。store=None 由 build_writeback_plan 内部保证
                # （判定缓存不落库，预演零副作用）。
                triage_app_dir = None
                try:
                    from hanhua.core.review_server import (
                        ReviewModelService)
                    _spec = ReviewModelService(
                        self.state.resource_dir)._spec()
                    if _spec.is_available:
                        triage_app_dir = self.state.resource_dir
                except Exception:  # noqa: BLE001 模型探测失败 = 不启用
                    triage_app_dir = None
                return build_writeback_plan(
                    project.store, project.game_dir,
                    triage_app_dir=triage_app_dir)

        worker = Worker(run_dry_run)
        worker.signals.finished.connect(
            lambda plan, p=project, g=generation:
            self._on_dry_run_done(plan)
            if plan is not None and self.state.is_current_project(p, g)
            else None)
        worker.signals.error.connect(
            lambda err, p=project, g=generation:
            (self.log_view.appendPlainText(f"写回预演失败：{err}"),
             Toast.show(self, f"写回预演失败：{err}", "error"))
            if self.state.is_current_project(p, g) else None)
        # 无论结果如何（含项目已切换被丢弃的 plan=None）都要复位按钮
        worker.signals.finished.connect(self._on_dry_run_drained)
        worker.signals.error.connect(self._on_dry_run_drained)
        self._pool.start(worker)

    def _on_dry_run_drained(self, *_args):
        self._dry_run_running = False
        self.dry_run_btn.setEnabled(True)

    def _on_dry_run_done(self, plan):
        self.log_view.appendPlainText(plan.summary())
        if plan.planned_total == 0 and plan.rejected == 0 \
                and plan.high_risk == 0 and plan.auto_revert == 0:
            Toast.show(self, "预演完成：没有可写的译文条目", "warning")
        else:
            Toast.show(
                self,
                f"预演完成：预计写回 {plan.planned_total} 条"
                f"（高风险 {plan.high_risk + plan.auto_revert}）", "success")

    def write_back(self):
        if self._write_running:
            self.log_view.appendPlainText("写回正在进行，请等待当前任务完成")
            return
        if self._dry_run_running:
            self.log_view.appendPlainText("写回预演正在进行，请等待完成")
            return
        report = self.state.analysis_report
        if report is None or not report.unblocked:
            blocked = [step.reason for step in (report.route if report else ())
                       if step.required and step.status in {"blocked", "failed"}]
            detail = blocked[0] if blocked else "分析报告尚未满足写回条件"
            self.log_view.appendPlainText(f"写回已阻断：{detail}")
            Toast.show(self, f"写回已阻断：{detail}", "warning")
            return
        if _write_ready_count(self.state.project.store) <= 0:
            detail = "没有通过质量门的可写译文"
            self.log_view.appendPlainText(f"写回已阻断：{detail}")
            Toast.show(self, f"写回已阻断：{detail}", "warning")
            return
        project = self.state.project
        generation = self.state.project_generation
        font_config = replace(self.state.settings.font)
        # 流水线 rail（#15）：写回阶段开始广播 running
        self.state.pipelinePhase.emit(
            "writeback", "running", "正在写回游戏副本…", "")
        signals_holder = {}

        def run_write():
            return self._write_worker(
                project, generation, font_config, signals_holder["signals"],
                allow_partial=self.partial_check.isChecked())

        worker = Worker(run_write)
        signals_holder["signals"] = worker.signals
        worker.signals.progress.connect(
            lambda stage, p=project, g=generation:
            self._on_write_stage(stage)
            if self.state.is_current_project(p, g) else None)
        worker.signals.finished.connect(
            lambda result, p=project, g=generation:
            self._on_written(result)
            if result is not None and self.state.is_current_project(p, g)
            else None)
        worker.signals.error.connect(
            lambda error, p=project, g=generation:
            self._on_write_error(error)
            if self.state.is_current_project(p, g) else None)
        worker.signals.log.connect(
            lambda line, p=project, g=generation:
            self.log_view.appendPlainText(line)
            if self.state.is_current_project(p, g) else None)
        # 写回后地毯式审计进度回主线程（2026-08-26 用户实证「写回检查
        # 看不到实际信息」的根因：audit.emit 发生在 _write_worker 内，但
        # 此前只在 start()（翻译 worker）挂 audit.connect，写回按钮触发的
        # 本 worker 从未连接 → 审计信息全部丢弃。这里接上：_write_worker
        # 内 signals.audit.emit 的 running/逐文件 info/success/warning/error
        # 经 _on_audit_event 同时进实时处理流与运行记录。）
        worker.signals.audit.connect(
            lambda status, text, p=project, g=generation:
            self._on_audit_event(status, text)
            if self.state.is_current_project(p, g) else None)
        worker.signals.finished.connect(
            lambda _result, w=worker: self._on_write_drained(w))
        worker.signals.error.connect(
            lambda _error, w=worker: self._on_write_drained(w))
        self._write_running = True
        self._write_terminal_message = ""
        self._write_worker_task = worker
        self.write_safety.set_ready(False, "写回进行中…")
        self.log_view.appendPlainText("正在写回…")
        self._pool.start(worker)

    def _on_write_error(self, err: str):
        message = f"写回失败：{err}"
        self._write_terminal_message = message
        self.log_view.appendPlainText(message)
        self._reset_pipeline_progress()
        self.progress_label.setText(message)
        self.state.pipelinePhase.emit(
            "writeback", "failed", "写回失败", err[:60])
        Toast.show(self, message, "error")
        export_path = self._export_fail_record("写回失败", err)
        if export_path:
            self.log_view.appendPlainText(f"失败记录已导出：{export_path}")
        record_path = self._export_records(
            error_title="写回失败", error_detail=err)
        if record_path:
            self.log_view.appendPlainText(f"完整记录已导出：{record_path}")

    def _write_worker(self, project, generation: int, font_config,
                      signals=None, *, allow_partial: bool = False):
        with self.state.project_lease(project, generation) as acquired:
            if not acquired:
                return None
            # 免实机闭环（2026-08-12 用户指令：后续游戏不做实机测试）：
            # 字体候选默认确认（PENDING_RUNTIME_ATTESTATION/CANDIDATE_ONLY
            # → WARN 放行），否则每次写回都被 runtime 门 BLOCKED、只能
            # 手动勾「允许部分写入」。条目完整性（rejected/truncated/
            # 逻辑验证）仍受 allow_partial 严格约束，不因本参数放宽。
            if signals is None:
                return project.write_all(
                    font_config=font_config,
                    allow_partial=allow_partial,
                    allow_unverified_font_candidate=True)
            result = project.write_all(
                font_config=font_config,
                stage_cb=signals.progress.emit,
                allow_partial=allow_partial,
                allow_unverified_font_candidate=True,
            )
            # 写回后地毯式审计（2026-08-26 任务四：GUI 写回链路必须可见）。
            # 与 runner all_record_runner.py 同源：第 1 层确定性结构审计
            # （字节/行数/结构/占位符/渲染一致，任何 FAIL → needs_rewrite
            # 阻断本轮闭环）+ 第 2 层审校模型软复核（Qwen3.5-4B，只审计
            # 第 1 层 PASS 且有差异的文件，FLAG 不硬拦）+ 第 2 层 b 二进制
            # 对象证据卡复核（0.39.0 M3，v2_result 由 write_all 返回值带出）。
            # 审计只读对比源目录 vs 发布目录，不修改任何文件；模型不可用
            # 标记 model_unavailable 阻断发布（与 runner 同口径）。
            try:
                if signals.audit:
                    signals.audit.emit(
                        "running", "写回后地毯式审计：结构完整性与译文"
                        "复核（只读，不修改文件）…")
                from hanhua.core.writeback_audit import (
                    audit_writeback, render_audit_report)
                audit_res = audit_writeback(
                    project.store, project.game_dir, project.out_dir,
                    run_model=True, app_dir=self.state.resource_dir,
                    online_cfg=(self.state.settings.api_config("review")
                                if self.state.api.mode == "api" else None),
                    font_enabled=bool(getattr(font_config, "enabled", False)),
                    v2_result=result.get("v2") if result else None,
                    on_note=lambda s: signals.audit.emit("info", s)
                    if signals.audit else None)
                report_text = render_audit_report(
                    audit_res, str(getattr(project.profile, "game_name", "")
                                   or project.game_dir.name))
                audit_path = project.out_dir / "writeback" / "audit.txt"
                try:
                    audit_path.parent.mkdir(parents=True, exist_ok=True)
                    audit_path.write_text(report_text, encoding="utf-8")
                except OSError:
                    pass  # 审计报告落盘失败不阻断主流程
                if audit_res.needs_rewrite:
                    failed = ", ".join(
                        f.rel_path for f in audit_res.failed_files[:5])
                    signals.audit.emit(
                        "error",
                        f"写回审计失败（结构破坏，需重写回）：{failed}"
                        + (f" 等 {len(audit_res.failed_files)} 个文件"
                           if len(audit_res.failed_files) > 5 else ""))
                elif audit_res.model_flags:
                    flags = ", ".join(
                        f"{rel}[{verdict}]" for rel, verdict, _ in
                        audit_res.model_flags[:5])
                    signals.audit.emit(
                        "warning",
                        f"写回审计通过：{len(audit_res.files)} 文件结构完整"
                        f" · 模型复核 FLAG {len(audit_res.model_flags)} 条"
                        f"（{flags}…，软复核，详见 writeback/audit.txt）")
                elif audit_res.model_unavailable:
                    # 2026-08-26 明确区分「模型可用无 FLAG」与「模型不可用
                    # 仅确定性审计」——用户要求写回检查信息明确，不可用是
                    # 覆盖缺口，必须显式告知而非静默成功。
                    signals.audit.emit(
                        "warning",
                        f"写回审计通过：{len(audit_res.files)} 文件结构完整"
                        f" · 审校模型服务不可用（仅确定性审计，报告已留档）")
                else:
                    signals.audit.emit(
                        "success",
                        f"写回审计通过：{len(audit_res.files)} 文件结构完整"
                        f" · 模型复核无 FLAG")
            except Exception as exc:  # noqa: BLE001 审计异常不阻断写回主流程
                if signals.audit:
                    signals.audit.emit(
                        "warning", f"写回审计异常（已跳过）：{exc}")
            return result

    def _on_write_stage(self, stage) -> None:
        message = str(getattr(stage, "message", "") or "")
        phase = str(getattr(stage, "phase", "") or "")
        if message:
            self.log_view.appendPlainText(message)
            self.progress_label.setText(message)
        # 写回阶段映射到 90-100% 段（2026-08-20 全链路 3-3-3-1）：
        # 七个阶段在 10% 区间内按比例分布，经 _set_pipeline_progress
        # 接续审校处置末值 90 单调推进，杜绝此前从 5% 起跳的倒退。
        phases = {
            "preflight": 0.10,
            "copying": 0.30,
            "patching": 0.55,
            "runtime_payload": 0.70,
            "verifying": 0.85,
            "publishing": 0.95,
            "published": 1.00,
        }
        if phase in phases:
            self._set_pipeline_progress(
                "writeback", phases[phase],
                label_text=message or "正在写回…")
        # 流水线 rail（#15）：写回阶段进度实时广播（阶段数少，无需节流）
        lo, hi = _PIPELINE_STAGE_RANGE["writeback"]
        pct = (lo + (hi - lo) * phases[phase]) if phase in phases else lo
        self.state.pipelinePhase.emit(
            "writeback", "running", message or "正在写回…",
            f"阶段 {phase or '—'} · {pct:.0f}%")

    def _on_write_drained(self, worker: Worker) -> None:
        if self._write_worker_task is not worker:
            return
        self._write_worker_task = None
        self._write_running = False
        self._refresh_chips()
        if self._write_terminal_message:
            self._reset_pipeline_progress()
            self.progress_label.setText(self._write_terminal_message)

    def _on_written(self, result):
        out = self.state.project.out_dir
        v2 = result.get("v2")
        font = result.get("font")
        verification = result.get("verification") or {}
        input_protected = verification.get("input_protected") is True
        reopen_verified = verification.get("reopen_verified") is True
        changed_files = int(verification.get("changed_files", 0) or 0)
        written_translations = int(
            verification.get("written_translations", 0) or 0)
        font_level = str(verification.get("font_level", "unavailable"))
        warnings = list(verification.get("warnings") or [])
        final_report = result.get("analysis_report")
        if final_report is not None:
            self.state.set_analysis_report(final_report)
        final_route = final_report.route if final_report is not None else ()
        route_blocked = any(
            step.required and step.status in {"blocked", "failed"}
            for step in final_route
        )
        route_complete = bool(
            final_report is not None and final_report.completable)
        gates = verification.get("gates") or {}
        overall = str(verification.get("overall") or "")
        verified = (input_protected and reopen_verified and route_complete
                    and overall in {"PASS", "WARN"})

        parts = [f"文本 {result.get('text_files', 0)} 个文件"]
        if v2 is not None:
            parts.append(
                f"二进制资源 {getattr(v2, 'files', 0)} 个文件、"
                f"{getattr(v2, 'entries', 0)} 条候选")
            if getattr(v2, "truncated", 0):
                parts.append(
                    f"（{v2.truncated} 条因 DLL/IL2CPP 长度限制截断）")
        if (font_level == "runtime_fallback" and font is not None
                and font.installed):
            parts.append(f"中文字体 {font.family}")
        result_label = "写回验证通过" if verified else "写回未通过验证"
        # 流水线 rail（#15）：写回结束广播终态
        self.state.pipelinePhase.emit(
            "writeback", "succeeded" if verified else "warning",
            result_label,
            f"变更文件 {changed_files} · 写入译文 {written_translations}")
        # 2026-08-15 流水线重做：发布验证节点随写回终态
        self.state.pipelinePhase.emit(
            "verify",
            "succeeded" if verified else "pending",
            "发布副本验证通过" if verified else "写回未通过验证，待重试",
            "")
        self.log_view.appendPlainText(f"{result_label}：{'，'.join(parts)} → {out}")
        self.log_view.appendPlainText(
            f"验证摘要：变更文件 {changed_files} · 实际写入译文 "
            f"{written_translations} · 原游戏输入哈希 "
            f"{'已保护' if input_protected else '发生变化'} · 输出重开验证 "
            f"{'已通过' if reopen_verified else '未通过'}")
        font_labels = {
            "runtime_fallback": "运行时中文回退",
            "disabled": "未启用",
            "unavailable": "不可验证",
        }
        font_level_text = font_labels.get(font_level, font_level)
        # Phase 4：展示 coverage 发布门终态而非旧字体层级启发式——
        # gate 与逐栈摘要与 GUI/runner/批量同一口径（计划 §8/§11）
        gate = verification.get("font_gate")
        coverage = verification.get("font_coverage")
        if gate:
            gate_text = f"{gate.get('status')} — {gate.get('detail')}"
            self.log_view.appendPlainText(f"字体发布门：{gate_text}")
            if coverage:
                stacks = coverage.get("stack_counts") or {}
                stack_text = " · ".join(
                    f"{kind}: {n}" for kind, n in sorted(stacks.items()))
                self.log_view.appendPlainText(
                    f"字体覆盖：{coverage.get('overall')}"
                    f"（{stack_text or '无消费者'}）")
                missing = coverage.get("missing") or []
                if missing:
                    self.log_view.appendPlainText("缺字：")
                    for row in missing[:16]:
                        self.log_view.appendPlainText(
                            f"  {row.get('scalar')} → {row.get('consumer')}"
                            f"（{row.get('kind')}）")
            # Phase 5：位图注入摘要（NGUI/BMFont provider 闭环）
            bitmap = verification.get("font_bitmap")
            if bitmap:
                self.log_view.appendPlainText(
                    "位图注入：" + f"provider {len(bitmap.get('providers') or [])} 个"
                    f"（{', '.join(bitmap.get('providers') or [])}）· "
                    f"注入 {bitmap.get('injected')} · "
                    f"审计 {bitmap.get('audited')} · "
                    f"未注入 {bitmap.get('pending')}")
        else:
            self.log_view.appendPlainText(
                f"字体层级：{font_level_text}")
        if gates:
            gate_parts = [
                f"{name}={item.get('status', 'N/A')}"
                for name, item in gates.items()
                if name != "overall"]
            self.log_view.appendPlainText(
                f"四态闸门：{' · '.join(gate_parts)}"
                f"（overall={overall}）")
            for name, item in gates.items():
                if name != "overall" and item.get("detail"):
                    self.log_view.appendPlainText(
                        f"  {name}: {item['detail']}")
        for warning in getattr(v2, "warnings", ()) if v2 else ():
            if warning not in warnings:
                warnings.append(warning)
        for warning in warnings:
            self.log_view.appendPlainText(f"警告：{warning}")
        rejected_entries = verification.get("rejected_entries") or []
        truncated_entries = verification.get("truncated_entries") or []
        if rejected_entries:
            self.log_view.appendPlainText(
                f"— 拒绝条目 {len(rejected_entries)} 条（默认阻断发布，"
                "需勾选“允许部分写入”后重试）—")
            for item in rejected_entries[:10]:
                self.log_view.appendPlainText(
                    f"  拒绝 {item.get('locator', '?')}: {item.get('reason', '?')}")
            if len(rejected_entries) > 10:
                self.log_view.appendPlainText(
                    f"  … 其余 {len(rejected_entries) - 10} 条")
        if truncated_entries:
            self.log_view.appendPlainText(
                f"— 截断条目 {len(truncated_entries)} 条"
                "（仅 DLL/IL2CPP 固定容量限制，Bundle/Assets 无影响）—")
            for item in truncated_entries[:10]:
                self.log_view.appendPlainText(f"  {item}")
            if len(truncated_entries) > 10:
                self.log_view.appendPlainText(
                    f"  … 其余 {len(truncated_entries) - 10} 条")
        manifest_name = verification.get("manifest")
        if manifest_name:
            self.log_view.appendPlainText(
                f"发布清单：{out / manifest_name}（全量文件 hash，含未修改文件）")
        self.reveal_btn.setHidden(not verified)
        staged_exe = self._staged_executable()
        self.play_btn.setEnabled(
            verified and staged_exe is not None and staged_exe.exists())
        if route_blocked or not route_complete or not verified:
            detail = (
                "必需能力仍被阻断" if route_blocked
                else "必需步骤尚未完成" if not route_complete
                else f"请检查输入保护与重开验证（overall={overall}）")
            export_path = self._export_fail_record("写回未通过验证", detail)
            if export_path:
                self.log_view.appendPlainText(f"失败记录已导出：{export_path}")
        if route_blocked:
            Toast.show(self, "写回未通过验证 · 必需能力仍被阻断", "error")
        elif not route_complete:
            Toast.show(self, "写回未通过验证 · 必需步骤尚未完成", "error")
        elif not verified:
            Toast.show(self, "写回未通过验证 · 请检查输入保护与重开验证", "error")
        else:
            toast = (f"写回已验证 · {changed_files} 个变更文件 · "
                     f"{written_translations} 条译文 · "
                     f"四态闸门 {overall}")
            if (font_level == "runtime_fallback" and font is not None
                    and font.installed):
                toast += f" · 中文字体 {font.family}"
            Toast.show(self, toast, "warning" if warnings else "success")
        record_path = self._export_records(write_result=result)
        if record_path:
            self.log_view.appendPlainText(f"完整记录已导出：{record_path}")
            # 写回审计报告随记录文档落盘（record_writer 已生成
            # writeback/audit.txt 时在记录目录内；此处兜底独立路径）
            audit_path = self.state.project.out_dir / "writeback" / "audit.txt"
            if audit_path.is_file():
                self.log_view.appendPlainText(
                    f"写回审计报告：{audit_path}")

    def reveal_output(self):
        out = str(self.state.project.out_dir)
        if os.path.exists(out):
            if os.name == "nt":
                os.startfile(out)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", out])

    def _staged_executable(self):
        """汉化副本 exe 的绝对路径；无法定位时返回 None。

        汉化副本与原游戏布局一致，exe 相对位置取自 fingerprint，
        拼到 out_dir 上即为发布后的可执行文件。
        """
        project = self.state.project
        if project is None:
            return None
        fingerprint = getattr(project, "_fingerprint", None)
        if not callable(fingerprint):
            return None
        try:
            info = fingerprint()
        except Exception:  # noqa: BLE001 定位不到 exe 就不亮起按钮
            return None
        exe = getattr(info, "executable", None)
        if exe is None:
            return None
        try:
            return project.out_dir / exe.relative_to(project.game_dir)
        except ValueError:
            return None

    def launch_game(self):
        """启动已发布的汉化副本 exe（写回验证通过后按钮亮起）。"""
        exe = self._staged_executable()
        if exe is None or not exe.exists():
            Toast.show(self, "找不到汉化副本可执行文件", "warning")
            return
        try:
            # cwd 指向 exe 所在目录：Unity 游戏常见相对路径资源加载
            subprocess.Popen([str(exe)], cwd=str(exe.parent))
            Toast.show(self, f"已启动汉化副本：{exe.name}", "success")
        except OSError as exc:
            Toast.show(self, f"启动失败：{exc}", "error")

    # ── 状态刷新 ──
    def _refresh_chips(self):
        """计数刷新（全量 O(N) 查库）——统计放后台线程。

        #6 已把 3 次独立 SQL COUNT 并为单循环；#2 再进一步：整个统计
        （get_entries + 万级 JSON 解析）移出主线程。翻译中每 ≥1s 一次
        的刷新也不再卡 UI；写回按钮等状态在 _on_chips_stats 回调渲染。
        """
        if self.state.project is None:
            return
        store = self.state.project.store
        self._chips_token += 1
        self._chips_loading = True
        token = self._chips_token
        worker = Worker(self._collect_chips_stats, store)
        # 引用必须保存（局部 worker 函数返回后 wrapper 引用丢失，
        # finished 连接失效——同 review_page #2 实证）。
        self._chips_worker = worker
        worker.signals.finished.connect(
            lambda stats: self._on_chips_stats(token, stats))
        worker.signals.error.connect(
            lambda err: self._on_chips_error(token, err))
        QThreadPool.globalInstance().start(worker)

    @staticmethod
    def _collect_chips_stats(store):
        """后台线程统计：#6 单循环口径（actionable/低置信留档/已翻/失败/
        跳过 + 质量门失败原因 + 写回可用数），一次 get_entries 算完。"""
        rows = store.get_entries()
        # 待翻译 = 引擎实际会翻的条目（is_actionable_translation），与翻译引擎
        # 同源。此前用 store.count('pending') 裸计数：IL2CPP 低置信度引擎消息
        # 留档（pending/low，不可自动翻译）被计入 → 显示虚高且永不减少，
        # 「翻译已完成但待翻译不变」的真实案例（526 条引擎异常消息留档）。
        actionable = low_pending = translated = failed = skipped = 0
        reasons: dict[str, int] = {}
        write_ready = 0
        for row in rows:
            entry = TranslatePage._entry_from_row(row)
            if is_actionable_translation(entry):
                actionable += 1
            elif (row.get("status") == "pending"
                    and entry.meta.get("confidence") == "low"):
                low_pending += 1
            status = row.get("status")
            if status == "translated":
                translated += 1
            elif status == "failed":
                failed += 1
            elif status == "skipped":
                skipped += 1
            # 质量门失败原因统计并入主循环（entry.meta 已解析，
            # 省去二次全量 json.loads——万级条目每轮省一整遍 O(N) 解析）。
            # write_ready 计数同样并主循环，替代独立 _write_ready_count。
            for reason in entry.meta.get("quality_reasons", []):
                reasons[str(reason)] = reasons.get(str(reason), 0) + 1
            if is_write_ready(status, row.get("translation", ""), entry.meta):
                write_ready += 1
        return (actionable, low_pending, translated, failed, skipped,
                reasons, write_ready)

    def _on_chips_stats(self, token: int, stats: tuple) -> None:
        """后台统计完成：渲染 chips/写回状态/进度口径（主线程）。"""
        if token != self._chips_token:
            return
        self._chips_loading = False
        (actionable, low_pending, translated, failed, skipped,
         reasons, write_ready) = stats
        s = self._last_stats
        self.chip_pending.setText(f"待翻译 {actionable}")
        self.chip_pending.setToolTip(
            f"另有 {low_pending} 条低置信度条目（引擎消息/疑似噪音）留档，"
            "不参与翻译；如需处理可在文本审校中逐一精修" if low_pending else "")
        self.chip_done.setText(f"已翻译 {translated}")
        self.chip_failed.setText(f"失败 {failed}")
        self.chip_skipped.setText(f"跳过 {skipped}")
        if s is None or not self._running:
            # 未开始 / 翻译已结束：进度显示切回全量口径（TranslateStats
            # 未导入，直接传轻量代理对象——_update_progress_widgets 只用
            # total/done/failed 三个字段）。#2：翻译完成后 progress_sub
            # 残留最后一批的「剩余 X 条」导致左右计数不符——这里用
            # store 全量刷新：剩余 = actionable（待翻译，含失败可重试），
            # 失败 = failed。翻译进行中（_running）保持批粒度由
            # _on_progress 实时维护，不覆盖。写回失败等终端消息已显示
            # 时跳过——异步 chips 回调不得覆盖（_on_write_drained 用
            # _write_terminal_message 恢复，回调时序在其后）。
            if not self._write_terminal_message:
                self._update_progress_widgets(type(
                    "_StatsProxy", (), {
                        "total": actionable + translated,
                        "done": translated, "failed": failed})())
        if reasons:
            summary = " · ".join(f"{reason} {count}" for reason, count in sorted(reasons.items()))
            self._set_quality_reason(summary)
        else:
            self._set_quality_reason(None)
        # 写回可用性与核心质量门同源，翻译/写回进行中禁用；SafetyBar
        # 统一管理按钮状态与原因（§6.3：禁用时说明具体原因）。
        # 2026-08-26 写回按键失灵根因：按钮使能只看 write_ready，而
        # write_back() 点击守卫还要求 analysis_report.unblocked——两者
        # 不一致时按钮「亮着但点了没反应」（报告被必需步骤阻断却仍可
        # 点）。此处把报告阻断并入使能判定，与点击守卫同源，杜绝
        # 亮着失灵。
        report = self.state.analysis_report
        # 报告存在但被必需步骤阻断 → 按钮禁用（与 write_back 点击守卫
        # 同源，杜绝「亮着但点了没反应」）。report 为 None（测试/未分析
        # 的假项目）不阻断，保持既有行为。
        report_blocked = (report is not None and not report.unblocked)
        if self._running:
            self.write_safety.set_ready(False, "翻译进行中，写回已锁定")
        elif self._write_running:
            self.write_safety.set_ready(False, "写回进行中…")
        else:
            if write_ready > 0 and not report_blocked:
                self.write_safety.set_ready(
                    True,
                    f"{write_ready} 条已通过质量门，写回生成汉化副本并验证")
            elif report_blocked:
                blocked = [
                    step.reason for step in (report.route if report else ())
                    if step.required
                    and step.status in {"blocked", "failed"}]
                detail = blocked[0] if blocked else "分析报告尚未满足写回条件"
                self.write_safety.set_ready(False, f"写回已阻断：{detail}")
            elif failed > 0:
                self.write_safety.set_ready(
                    False, f"仍有 {failed} 条翻译失败，请先在审校页处理")
            else:
                self.write_safety.set_ready(
                    False, "还没有通过质量门的译文，先开始翻译")

    def _on_chips_error(self, token: int, err: str) -> None:
        if token != self._chips_token:
            return
        self._chips_loading = False
        self.chip_pending.setText("待翻译 —")
        self.chip_done.setText("已翻译 —")
        self.chip_failed.setText("失败 —")
        self._set_quality_reason(f"统计失败（{err[:60]}）")

    def _on_project(self, _proj):
        if self.state.project is None:
            return
        self._running = False
        self.state.translation_running = False
        self._write_terminal_message = ""
        self._worker = None
        self._last_stats = None
        self._last_review_summary = None
        self._stream_last_done = 0
        self._last_review = None
        self.start_btn.setEnabled(self._active_run is None)
        self.stop_btn.setEnabled(False)
        self.retry_btn.setEnabled(False)
        self._reset_pipeline_progress()
        self.progress_label.setText("尚未开始")
        self.progress_sub.setText(
            "在开始前，请确认设置页的 API 与游戏档案已配置")
        self.log_view.clear()
        self.activity_feed.clear()
        self._set_stream_status("等待开始", phase="idle")
        self._refresh_chips()
        self._set_primary(self.start_btn)
        self.reveal_btn.setHidden(True)
        self.play_btn.setEnabled(False)

    def _on_project_changing(self, _project):
        self.stop()
