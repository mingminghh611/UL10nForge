"""通用控件：页面抬头、状态徽章/轨道、数据舱、空态、Toast、后台 Worker。"""
from __future__ import annotations

from PySide6.QtCore import (QEasingCurve, QObject, QPropertyAnimation,
                            QRunnable, Qt, QTimer, Signal)
from PySide6.QtWidgets import (QFrame, QGraphicsOpacityEffect, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem,
                               QPushButton, QSizePolicy, QVBoxLayout,
                               QWidget)

from hanhua.ui import theme
from hanhua.ui.design_system import TOKENS, motion_enabled
from hanhua.ui.icons import LineIcon

STATUS_TEXT = {
    "pending": "待翻译",
    "translated": "已翻译",
    "failed": "失败",
    "skipped": "跳过",
    "idle": "空闲",
    "running": "运行中",
    "succeeded": "通过",
    "warning": "待确认",
    "locked": "锁定",
    "blocked": "受限",
    # #47（2026-08-14）：审校页状态列透出审核态（不再是机械态受限）——
    # 已重译（重译收敛待人工确认）/已通过（APPROVED 系）/待审核（未收敛）
    "retranslated": "已重译",
    "approved": "已通过",
    "needs_review": "待审核",
}
STATUS_COLOR = {
    "pending": theme.TEXT_DISABLED,
    "translated": theme.SUCCESS,
    "failed": theme.ERROR,
    "skipped": theme.STATUS_IDLE,
    "locked": theme.STATUS_LOCKED,
    "idle": theme.STATUS_IDLE,
    "running": theme.ACCENT,
    "succeeded": theme.SUCCESS,
    "warning": theme.WARNING,
    "blocked": theme.WARNING,
    "retranslated": theme.ACCENT,
    "approved": theme.SUCCESS,
    "needs_review": theme.WARNING,
}


class PageHeader(QWidget):
    """统一页面抬头：左侧标题 + 任务说明，右侧动作插槽。

    只处理布局，不连接业务信号；动作按钮由页面自行连接。
    """

    def __init__(self, title: str, subtitle: str, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        left = QVBoxLayout()
        left.setSpacing(2)
        self.title_label = QLabel(title)
        self.title_label.setProperty("class", "pageTitle")
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setProperty("class", "subtitle")
        # 2026-08-22 修复文字拥挤/截半：副标题强制换行 + 最小高度，
        # 窄窗口下不再被固定行高压成一条缝（PageHeader 高度 64→自适应，
        # min 64 保底，说明长时自动撑高）
        self.subtitle_label.setWordWrap(True)
        left.addWidget(self.title_label)
        left.addWidget(self.subtitle_label)
        lay.addLayout(left)
        lay.addStretch(1)
        self.setMinimumHeight(64)
        self.actions_box = QHBoxLayout()
        self.actions_box.setSpacing(10)
        lay.addLayout(self.actions_box)
        self.primary_slot: QPushButton | None = None

    def set_actions(self, buttons: list[QPushButton]) -> None:
        """右侧动作：第一个按钮标记为主按钮，其余为次按钮。"""
        for index, button in enumerate(buttons):
            if index == 0:
                button.setProperty("primary", True)
                self.primary_slot = button
            button.setMinimumHeight(TOKENS.control_height)
            self.actions_box.addWidget(button)


class StatusBadge(QWidget):
    """状态徽章：6px 语义色圆点 + 文字，由 QSS 动态属性驱动。

    状态：idle / running / succeeded / warning / failed / locked /
    pending / translated / skipped / blocked。
    """

    def __init__(self, status: str = "pending", parent=None):
        super().__init__(parent)
        self._status = status
        self.setFixedHeight(22)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.dot = QLabel()
        self.dot.setObjectName("statusNodeDot")
        self.text_label = QLabel()
        self.text_label.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: 8.5pt;")
        lay.addWidget(self.dot)
        lay.addWidget(self.text_label)
        self._apply_status()

    def setStatus(self, status: str):
        self._status = status
        self._apply_status()

    def status(self) -> str:
        return self._status

    def _apply_status(self):
        self.setProperty("status", self._status)
        self.dot.setProperty("status", self._status)
        for widget in (self, self.dot):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.text_label.setText(STATUS_TEXT.get(self._status, self._status))


class StatusNode(QFrame):
    """状态轨道上的一个节点：状态灯 + 标题 + 细节 + 指标。"""

    def __init__(self, step_id: str, title: str, icon_name: str,
                 parent=None):
        super().__init__(parent)
        self.step_id = step_id
        self.setObjectName("statusNode")
        self.setProperty("status", "idle")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(7)
        self.dot = QLabel()
        self.dot.setObjectName("statusNodeDot")
        self.title_label = QLabel(title)
        self.title_label.setObjectName("statusNodeTitle")
        head.addWidget(self.dot)
        head.addWidget(self.title_label)
        head.addStretch(1)
        self.detail_label = QLabel("等待")
        self.detail_label.setObjectName("statusNodeDetail")
        self.metrics_label = QLabel("")
        self.metrics_label.setObjectName("statusNodeMetrics")
        lay.addLayout(head)
        lay.addWidget(self.detail_label)
        lay.addWidget(self.metrics_label)
        self._pulse_anim = None

    def start_pulse(self):
        """运行态：状态灯 900ms 呼吸（0.35 ↔ 1.0，循环）。

        减少动效（HANHUA_REDUCED_MOTION）时不启动循环脉冲，
        保持静态高亮（spec §7：仅保留瞬时状态变化）。
        """
        if self._pulse_anim is not None:
            return
        if not motion_enabled():
            return
        effect = QGraphicsOpacityEffect(self.dot)
        self.dot.setGraphicsEffect(effect)
        self._pulse_anim = QPropertyAnimation(effect, b"opacity", self.dot)
        self._pulse_anim.setDuration(900)
        self._pulse_anim.setStartValue(0.35)
        self._pulse_anim.setEndValue(1.0)
        self._pulse_anim.setEasingCurve(QEasingCurve.InOutSine)
        self._pulse_anim.setLoopCount(-1)
        self._pulse_anim.start()

    def stop_pulse(self):
        """离开运行态：停止呼吸并还原状态灯（不留残影）。"""
        if self._pulse_anim is None:
            return
        self._pulse_anim.stop()
        self._pulse_anim.deleteLater()
        self._pulse_anim = None
        self.dot.setGraphicsEffect(None)


class StatusRail(QWidget):
    """横向状态轨道：节点 + 细连接线，贯穿四页的产品记忆点。

    2026-08-20（#15）布局重做：原实现 rail 与 node 以 stretch 1:3 交替
    加入布局——五格宽度随内容与窗口宽度抖动、互不相等。现改为等宽网格
    思路：每个节点占据一个等分格（addWidget(node, 1)），连接线以绝对
    定位横穿两节点之间的间隙中点，不参与拉伸分配——五格恒等宽、间隙
    连接线视觉对齐。"""

    def __init__(self, nodes: list[tuple[str, str, str]], parent=None):
        super().__init__(parent)
        self.nodes: list[StatusNode] = []
        self._rails: list[QFrame] = []
        self.setMouseTracking(True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(24)
        for index, (step_id, title, icon_name) in enumerate(nodes):
            node = StatusNode(step_id, title, icon_name)
            # 每个节点等分（stretch 1），内部 head 布局吃掉多余宽度
            node.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            lay.addWidget(node, 1)
            self.nodes.append(node)
            if index:
                rail = QFrame(self)
                rail.setObjectName("brandRail")
                rail.setFixedHeight(2)
                # 置于两节点之间的间隙：x 由 resizeEvent 按几何重算
                rail.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                rail.lower()
                self._rails.append(rail)

    def resizeEvent(self, event):
        """间隙连接线定位：每条线横跨前一节点右缘到后一节点左缘，
        垂直居中于标题行（节点内边距 10 + 标题行高一半 ≈ 18px）。"""
        super().resizeEvent(event)
        for rail, node in zip(self._rails, self.nodes[1:]):
            prev = self.nodes[self.nodes.index(node) - 1]
            x1 = prev.geometry().right() + 1
            x2 = node.geometry().left() - 1
            if x2 > x1:
                rail.setGeometry(x1, 18, x2 - x1, 2)
                rail.show()
            else:
                rail.hide()

    def set_node_state(self, step_id: str, status: str, detail: str,
                       metrics: str = ""):
        """更新节点状态；status ∈ idle/running/succeeded/failed/warning。"""
        node = next((n for n in self.nodes if n.step_id == step_id), None)
        if node is None:
            return
        node.setProperty("status", status)
        node.dot.setProperty("status", status)
        for widget in (node, node.dot):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        node.detail_label.setText(detail)
        node.metrics_label.setText(metrics)
        # 运行态呼吸动画只作用于状态灯，绝不触碰表格/日志/滚动条
        if status == "running":
            node.start_pulse()
        else:
            node.stop_pulse()


class MetricStrip(QFrame):
    """数据舱：panel 底 + 左侧 2px 语义色条 + 数值/标签。"""

    def __init__(self, label: str, value: str = "—",
                 accent: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("metricStrip")
        if accent:
            self.setProperty("accent", accent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(0)
        self.value_label = QLabel(value)
        self.value_label.setObjectName("metricStripValue")
        self.label = QLabel(label)
        self.label.setObjectName("metricStripLabel")
        lay.addWidget(self.value_label)
        lay.addWidget(self.label)

    def setValue(self, value) -> None:
        self.value_label.setText(str(value))


class EmptyState(QWidget):
    """空态：居中图标 + 标题 + 提示，页面无数据时的引导。"""

    def __init__(self, icon_name: str, title: str, hint: str, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        icon_row = QHBoxLayout()
        icon_row.addStretch(1)
        icon_row.addWidget(LineIcon(icon_name, 42))
        icon_row.addStretch(1)
        self.title = QLabel(title)
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setStyleSheet(
            f"font-size: 14px; font-weight: 600; color: {theme.TEXT};")
        self.hint = QLabel(hint)
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setProperty("class", "subtitle")
        lay.addStretch(1)
        lay.addLayout(icon_row)
        lay.addWidget(self.title)
        lay.addWidget(self.hint)
        lay.addStretch(1)


class StatCard(QFrame):
    """大数字 + 标签的统计卡片。"""

    def __init__(self, label: str, value: int = 0, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(2)
        self.value_label = QLabel(str(value))
        self.value_label.setProperty("class", "statValue")
        self.label = QLabel(label)
        self.label.setProperty("class", "statLabel")
        lay.addWidget(self.value_label)
        lay.addWidget(self.label)

    def setValue(self, value):
        self.value_label.setText(str(value))


class MetricChip(QFrame):
    def __init__(self, label: str, value: str = "—", parent=None):
        super().__init__(parent)
        self.setObjectName("metricChip")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 7, 12, 7)
        lay.setSpacing(7)
        caption = QLabel(label)
        caption.setProperty("class", "metricLabel")
        self.value_label = QLabel(value)
        self.value_label.setProperty("class", "metricValue")
        lay.addWidget(caption)
        lay.addWidget(self.value_label)

    def setValue(self, value) -> None:
        self.value_label.setText(str(value))


class CapabilityBadge(QLabel):
    def __init__(self, text: str = "等待", status: str = "pending", parent=None):
        super().__init__(text, parent)
        self.setObjectName("capabilityBadge")
        self.setAlignment(Qt.AlignCenter)
        self.setStatus(status)

    def setStatus(self, status: str) -> None:
        self.setProperty("status", status)
        self.style().unpolish(self)
        self.style().polish(self)


class PipelineStepCard(QFrame):
    def __init__(self, step_id: str, title: str, icon_name: str, parent=None):
        super().__init__(parent)
        self.step_id = step_id
        self.setObjectName("pipelineStep")
        self.setProperty("status", "pending")
        self.setMinimumHeight(112)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 13, 14, 12)
        lay.setSpacing(7)
        head = QHBoxLayout()
        self.icon = LineIcon(icon_name, 26)
        self.title_label = QLabel(title)
        self.title_label.setProperty("class", "stepTitle")
        self.status_badge = CapabilityBadge("等待", "pending")
        head.addWidget(self.icon)
        head.addWidget(self.title_label)
        head.addStretch(1)
        head.addWidget(self.status_badge)
        self.detail_label = QLabel("等待项目分析")
        self.detail_label.setProperty("class", "stepDetail")
        self.detail_label.setWordWrap(True)
        self.metrics_label = QLabel("置信度 — · 缓存 — · 耗时 —")
        self.metrics_label.setProperty("class", "stepMetrics")
        lay.addLayout(head)
        lay.addWidget(self.detail_label)
        lay.addWidget(self.metrics_label)

    def setState(self, status: str, detail: str, confidence: str = "—",
                 cache_hit: bool | None = None, elapsed_ms: int = 0) -> None:
        labels = {
            "pending": "等待", "running": "运行中", "succeeded": "通过",
            "failed": "失败", "blocked": "受限", "skipped": "跳过",
        }
        self.setProperty("status", status)
        self.style().unpolish(self); self.style().polish(self)
        self.status_badge.setText(labels.get(status, status))
        self.status_badge.setStatus(status)
        self.detail_label.setText(detail)
        cache = "命中" if cache_hit is True else ("未命中" if cache_hit is False else "—")
        elapsed = f"{elapsed_ms} ms" if elapsed_ms else "—"
        self.metrics_label.setText(f"置信度 {confidence} · 缓存 {cache} · 耗时 {elapsed}")


class FilterChip(QPushButton):
    """筛选胶囊（§8）：可键盘操作的筛选状态，选中态由 QSS 动态属性驱动。

    value 为业务键；kind 可选 risk/ai 语义着色（QSS chipKind 属性）。
    """

    def __init__(self, text: str, value: str, kind: str = "",
                 parent=None):
        super().__init__(text, parent)
        self.value = value
        self.setCheckable(True)
        self.setProperty("filterChip", True)
        if kind:
            self.setProperty("chipKind", kind)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName(f"筛选：{text}")


class ActivityFeed(QListWidget):
    """活动流（§8）：消费翻译进度事件并限制可见条目数。

    append_event(status, source, translation) 追加一行；超过 max_items
    时丢弃最旧行。状态经 Qt.UserRole 保存，供样式/语义使用。
    """

    def __init__(self, max_items: int = 80, parent=None):
        super().__init__(parent)
        self.max_items = max_items
        self.setObjectName("activityFeed")
        self.setSelectionMode(QListWidget.NoSelection)
        self.setFocusPolicy(Qt.NoFocus)

    def append_event(self, status: str, source: str,
                     translation: str = ""):
        item = QListWidgetItem(f"{source}\n{translation}".rstrip())
        item.setData(Qt.UserRole, status)
        item.setToolTip(source)
        self.addItem(item)
        while self.count() > self.max_items:
            self.takeItem(0)
        self.scrollToBottom()

    def latest_text(self) -> str:
        return self.item(self.count() - 1).text() if self.count() else ""


class SafetyBar(QFrame):
    """写回安全栏（§6.3）：独立底部安全栏，明确「生成副本、验证后可启动」。

    set_ready(ready, reason) 同时驱动写回按钮 enabled、原因文字与
    status 属性（ready=薄荷绿 / blocked=琥珀 / error=珊瑚红）。
    """

    def __init__(self, button: QPushButton, parent=None):
        super().__init__(parent)
        self.setObjectName("safetyBar")
        self.setProperty("status", "blocked")
        self.button = button
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title_label = QLabel("安全写回")
        self.title_label.setObjectName("safetyTitle")
        self.reason_label = QLabel("等待项目与译文就绪")
        self.reason_label.setObjectName("safetyReason")
        self.reason_label.setWordWrap(True)
        text.addWidget(self.title_label)
        text.addWidget(self.reason_label)
        lay.addLayout(text, 1)
        lay.addWidget(button)

    def set_ready(self, ready: bool, reason: str):
        self.button.setEnabled(ready)
        self.reason_label.setText(reason)
        self.setProperty("status", "ready" if ready else "blocked")
        self.style().unpolish(self)
        self.style().polish(self)


class Toast:
    """右上角浮动通知，自动淡出。"""

    _stack: list[QFrame] = []

    @staticmethod
    def show(parent: QWidget, message: str, kind: str = "info", duration_ms: int = 3000):
        frame = QFrame(parent)
        frame.setObjectName("toast")
        if kind == "success":
            frame.setProperty("success", True)
        elif kind == "error":
            frame.setProperty("error", True)
        elif kind == "warning":
            frame.setProperty("warning", True)
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(14, 10, 14, 10)
        prefix = {"success": "成功 · ", "error": "错误 · ",
                  "warning": "警告 · "}.get(kind, "")
        label = QLabel(prefix + message)
        lay.addWidget(label)
        frame.adjustSize()
        # 右上角堆叠
        x = parent.width() - frame.width() - 24
        y = 24 + 8 * len(Toast._stack)
        frame.move(x, y)
        frame.show()
        Toast._stack.append(frame)
        effect = QGraphicsOpacityEffect(frame)
        frame.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", frame)
        anim.setDuration(180)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.start()
        frame._toast_anim = anim  # noqa: SLF001 防 GC

        def close_toast():
            fade = QPropertyAnimation(effect, b"opacity", frame)
            fade.setDuration(180)
            fade.setStartValue(1.0)
            fade.setEndValue(0.0)

            def cleanup():
                frame.deleteLater()
                if frame in Toast._stack:
                    Toast._stack.remove(frame)
                for i, other in enumerate(Toast._stack):
                    other.move(parent.width() - other.width() - 24, 24 + 8 * i)

            fade.finished.connect(cleanup)
            fade.start()

        QTimer.singleShot(duration_ms, close_toast)


class TopBar(QWidget):
    """§14 顶部条：当前项目（名称 + 语言 + 切换）。

    精简（2026-08-13）：搜索操作/Ctrl+K、通知、设置快捷入口全部移除，
    右上角只保留项目上下文；设置与页面切换统一走侧栏导航。
    """

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self._state = state
        self.setObjectName("topBar")
        self.setFixedHeight(56)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 0, 18, 0)
        lay.setSpacing(12)

        left = QHBoxLayout()
        left.setSpacing(8)
        self.project_name = QLabel("尚未打开项目")
        self.project_name.setObjectName("topBarProject")
        self.project_sub = QLabel("选择 Unity 游戏文件夹开始")
        self.project_sub.setObjectName("topBarProjectSub")
        self.switch_btn = QPushButton("切换项目")
        self.switch_btn.setAccessibleName("切换项目")
        self.switch_btn.setProperty("ghost", True)
        self.switch_btn.setMinimumHeight(TOKENS.control_height)
        self.switch_btn.setCursor(Qt.PointingHandCursor)
        self.switch_btn.setVisible(False)
        left.addWidget(self.project_name)
        left.addWidget(self.project_sub)
        left.addWidget(self.switch_btn)
        lay.addLayout(left)
        lay.addStretch(1)

        state.projectOpened.connect(self._on_project)

    def _on_project(self, project):
        self.project_name.setText(project.game_dir.name)
        profile = getattr(project, "profile", None)
        if profile is not None:
            lang = getattr(profile, "target_lang", "") or ""
            src = getattr(profile, "source_lang", "") or ""
            sub = f"Unity · {src} → {lang}"
        else:
            sub = "Unity"
        self.project_sub.setText(sub)
        self.switch_btn.setVisible(True)


class WorkerSignals(QObject):
    finished = Signal(object)
    error = Signal(str)
    progress = Signal(object)
    log = Signal(str)
    review = Signal(int, int)   # 语义审核判定进度 (done, total) 实时刷新
    review_disposition = Signal(int, int)
                                # 语义审核处置进度 (done, total)——
                                # 2026-08-20 全链路 3-3-3-1 进度条
                                # 60-90% 段：反馈重译 + 再审收敛
                                # 串行 2-30s/条，原本静默
    note = Signal(str, str)     # 活动流消息 (status, text)——worker 线程
                                # 不得直接操作 QWidget，经此信号回主线程
    review_summary = Signal(str)  # 审核完成汇总——2026-08-14 用户实证
                                # 「只有完成二字，两分钟后才出反馈」：
                                # 汇总此前只进默认折叠的日志面板，完成
                                # 时须经此信号回主线程弹 Toast/活动流
    audit = Signal(str, str)    # 写回审计进度 (status, text)——worker 线程
                                # 不得直接操作 QWidget，经此信号回主线程
                                #（写回后地毯式审计：确定性逐文件 PASS/FAIL
                                # + 模型软复核 FLAG/不可用，2026-08-26）


class Worker(QRunnable):
    """后台任务：fn 在池线程执行，结果经信号回到主线程。"""

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as e:  # noqa: BLE001
            try:
                self.signals.error.emit(str(e))
            except RuntimeError:
                # 0.51.2 翻译中途崩溃（crash B）兜底：页面在 worker 运行
                # 中丢弃 Worker 引用时（_on_project 置 _worker=None），
                # Python GC 会删掉无 parent 的 WorkerSignals QObject；
                # 此后工作线程任何 emit 都抛
                # "RuntimeError: Signal source has been deleted"。
                # 信号源已死 = 结果已无人接收，静默放弃即可——绝不能
                # 让异常从 run() 二次抛出（QThreadPool 打印后线程死亡，
                # 进程状态不确定）。emit 本身无 is_current_project 守卫
                # （守卫只在主线程接收端），这里是最后一道防线。
                pass
        else:
            try:
                self.signals.finished.emit(result)
            except RuntimeError:
                pass  # 同上：信号源已被 GC，结果无人接收，放弃
