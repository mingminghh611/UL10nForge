"""Aurora Forge UL10nForge深色设计系统：颜色 token + 全局 QSS（2026-08-13）。

设计约束（spec §4/§5/§8）：
- 石墨黑中性底（canvas #090B12）+ surface/surface_raised 两级层级分组；
  默认卡片不描边，只有输入、焦点、选中与语义状态显示清晰边框。
- 薄荷青（accent）当前主流程与主操作；天蓝（info）检测扫描；紫罗兰
  （ai）AI 判断；琥珀（warning）待确认；珊瑚红（error）错误；绿（success）通过。
- 中文正文 Microsoft YaHei UI；代码/日志 Cascadia Mono。
- QSS 不包含 transition/animation/keyframes（Qt 不支持），动效全在 Python。
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication
from hanhua.ui.design_system import TOKENS

# ── 设计 token ──────────────────────────────────────────────
BG = TOKENS.background          # canvas
PANEL = TOKENS.surface          # 主面板
CARD = TOKENS.surface           # 卡片底（层级一）
CARD_HOVER = TOKENS.surface_hover
RAISED = TOKENS.surface_raised  # 浮层与强调卡
BORDER = TOKENS.border
BORDER_STRONG = TOKENS.border_strong
ACCENT = TOKENS.accent          # 薄荷青：主流程
ACCENT_HOVER = TOKENS.primary_hover
ACCENT_PRESSED = TOKENS.primary_pressed
ACCENT_BG = TOKENS.primary_muted
ACCENT_DIM = TOKENS.accent_dim       # 半透明感主色：卡片描边强调
AI = TOKENS.ai                  # 紫罗兰=AI
AI_SECONDARY = TOKENS.ai_secondary
AI_BG = TOKENS.ai_muted
GRAD_START = TOKENS.gradient_start
GRAD_END = TOKENS.gradient_end
SIDEBAR_BG = TOKENS.sidebar_bg
GLASS_EDGE = TOKENS.glass_edge
TEXT = TOKENS.text
TEXT_SECONDARY = TOKENS.text_secondary
TEXT_DISABLED = TOKENS.text_disabled
SUCCESS = TOKENS.success
WARNING = TOKENS.warning
ERROR = TOKENS.error
INFO = TOKENS.info
STATUS_IDLE = TOKENS.status_idle
STATUS_LOCKED = TOKENS.status_locked
LOGGER_BG = TOKENS.logger_bg
RADIUS = TOKENS.radius
RADIUS_MD = TOKENS.radius_md
RADIUS_CARD = TOKENS.radius_card
RADIUS_PANEL = TOKENS.radius_panel
RADIUS_DIALOG = TOKENS.radius_dialog
PRIMARY_TEXT = "#071713"  # 主按钮上的深色文字
MONO = '"Cascadia Mono", Consolas, monospace'

_QSS = f"""
* {{
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 10pt;
    color: {TEXT};
}}
QWidget {{ background: transparent; }}
QWidget#root {{ background: {BG}; }}

/* ── 侧边栏（176px：图标 + 短标签 + 滑动选中指示器） ──────── */
QFrame#sidebar {{
    background: {SIDEBAR_BG};
    border-right: 1px solid {GLASS_EDGE};
}}
QLabel#appTitle {{ font-size: 17px; font-weight: 700; color: {TEXT}; }}
QLabel#appSub {{ color: {TEXT_DISABLED}; font-size: 9pt; }}
QListWidget#navList {{
    background: transparent;
    border: none;
    outline: none;
}}
QListWidget#navList::item {{
    padding: 10px 14px;
    margin: 2px 12px;
    border-radius: {RADIUS_MD}px;
    color: {TEXT_SECONDARY};
    font-size: 10pt;
}}
QListWidget#navList::item:hover {{ background: {CARD_HOVER}; color: {TEXT}; }}
QListWidget#navList::item:selected {{
    background: {ACCENT_BG};
    color: {ACCENT};
    font-weight: 600;
}}
QListWidget#navList::item:disabled {{ color: {TEXT_DISABLED}; }}
/* 导航指示条：选中项左缘 3px 薄荷青条，200ms 滑动 */
QFrame#navIndicator {{
    background: {ACCENT};
    border-radius: 2px;
    border: none;
}}

/* ── 按钮（输入 10px 圆角；hover/press/focus 不改变尺寸） ─── */
QPushButton {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    padding: 6px 16px;
    min-height: {TOKENS.control_height}px;
    color: {TEXT};
}}
QPushButton:hover {{ background: {CARD_HOVER}; border-color: {BORDER_STRONG}; }}
QPushButton:pressed {{ background: {RAISED}; }}
QPushButton:disabled {{ color: {TEXT_DISABLED}; background: {PANEL}; border-color: {BORDER}; }}
QPushButton:focus {{ border: {TOKENS.focus_width}px solid {ACCENT}; }}

/* 主按钮：薄荷青纯色 + 深色文字 */
QPushButton[primary="true"] {{
    background: {ACCENT};
    border: none;
    color: {PRIMARY_TEXT};
    font-weight: 700;
    min-height: {TOKENS.primary_height}px;
}}
QPushButton[primary="true"]:hover {{ background: {ACCENT_HOVER}; }}
QPushButton[primary="true"]:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton[primary="true"]:disabled {{ background: #27403B; color: {TEXT_DISABLED}; }}

/* 危险按钮：透明底 + 珊瑚红 */
QPushButton[danger="true"] {{
    background: transparent;
    border: 1px solid {ERROR};
    color: {ERROR};
}}
QPushButton[danger="true"]:hover {{ background: rgba(255, 114, 133, 0.12); }}
QPushButton[danger="true"]:disabled {{ color: {TEXT_DISABLED}; border-color: {BORDER}; }}

/* 幽灵按钮：仅 hover 反馈 */
QPushButton[ghost="true"] {{
    background: transparent;
    border: 1px solid transparent;
    color: {TEXT_SECONDARY};
}}
QPushButton[ghost="true"]:hover {{ background: {CARD}; color: {TEXT}; }}

/* ── 筛选胶囊（§8 FilterChip：可键盘操作的筛选状态） ───────── */
QPushButton[filterChip="true"] {{
    background: transparent;
    border: 1px solid {BORDER};
    border-radius: 16px;
    padding: 4px 14px;
    min-height: 28px;
    color: {TEXT_SECONDARY};
    font-size: 9pt;
}}
QPushButton[filterChip="true"]:hover {{
    border-color: {BORDER_STRONG};
    color: {TEXT};
}}
QPushButton[filterChip="true"]:checked {{
    background: {ACCENT_BG};
    border-color: {ACCENT};
    color: {ACCENT};
    font-weight: 600;
}}
QPushButton[filterChip="true"][chipKind="risk"]:checked {{
    background: rgba(255, 114, 133, 0.14);
    border-color: {ERROR};
    color: {ERROR};
}}
QPushButton[filterChip="true"][chipKind="ai"]:checked {{
    background: {AI_BG};
    border-color: {AI};
    color: {AI};
}}

/* ── 输入控件 ───────────────────────────────────────────── */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    padding: 5px 10px;
    selection-background-color: {ACCENT};
    selection-color: {PRIMARY_TEXT};
}}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ min-height: {TOKENS.control_height}px; }}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
    background: {CARD_HOVER};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: {TOKENS.focus_width}px solid {ACCENT};
}}
QLineEdit:disabled, QComboBox:disabled {{ color: {TEXT_DISABLED}; }}
QComboBox::drop-down {{ border: none; width: 30px; }}
QComboBox QAbstractItemView {{
    background: {RAISED};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    outline: none;
    selection-background-color: {ACCENT_BG};
    selection-color: {TEXT};
}}
QComboBox QAbstractItemView::item {{ min-height: 30px; padding: 4px 10px; }}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    background: transparent;
    border: none;
    width: 18px;
}}

/* ── 表格 ────────────────────────────────────────────────── */
QTableView, QTableWidget {{
    background: {LOGGER_BG};
    alternate-background-color: {CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CARD}px;
    gridline-color: transparent;
    selection-background-color: {ACCENT_BG};
    selection-color: {TEXT};
}}
QTableView::item:hover, QTableWidget::item:hover {{ background: {CARD_HOVER}; }}
QTableView:focus {{ border: {TOKENS.focus_width}px solid {ACCENT}; }}
QHeaderView::section {{
    background: {PANEL};
    color: {TEXT_SECONDARY};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 7px 10px;
    font-size: 9pt;
}}
QTableCornerButton::section {{ background: {PANEL}; border: none; }}

/* ── 滚动条 ─────────────────────────────────────────────── */
QScrollBar:vertical {{
    background: transparent; width: 8px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER_STRONG}; border-radius: 4px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {INFO}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: transparent; height: 8px; margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER_STRONG}; border-radius: 4px; min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{ background: {INFO}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ── 标签页 ─────────────────────────────────────────────── */
QTabWidget::pane {{ border: none; background: transparent; top: 0; }}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_SECONDARY};
    padding: 9px 24px;
    border: none;
    font-size: 10pt;
}}
QTabBar::tab:selected {{
    color: {TEXT};
    border-bottom: 2px solid {ACCENT};
    font-weight: 600;
}}
QTabBar::tab:hover:!selected {{ color: {TEXT}; }}

/* ── 进度条（完成段 mint→sky 渐变） ─────────────────────── */
QProgressBar {{
    background: {CARD};
    border: none;
    border-radius: 4px;
    min-height: 8px;
    max-height: 8px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {GRAD_START}, stop:1 {GRAD_END});
    border-radius: 4px;
}}
/* 扫描进度条（#14）：需要容纳百分比文本，故单独加高；
   文本用深色（落在渐变 chunk 上）/轨道内浅色双态保证对比度 */
QProgressBar#scanBar {{
    min-height: 18px;
    max-height: 18px;
    border-radius: 6px;
    font-size: 9pt;
    font-weight: 600;
    color: {TEXT_SECONDARY};
    background: {LOGGER_BG};
    border: 1px solid {BORDER};
}}
QProgressBar#scanBar::chunk {{
    border-radius: 5px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {GRAD_START}, stop:1 {GRAD_END});
    width: 4px;
}}

/* ── 任务状态轨道（五步状态轨，贯穿四页的产品记忆点） ─────── */
QFrame#statusNode {{
    background: {PANEL};
    border: none;
    border-top: 2px solid transparent;
    border-radius: {RADIUS_CARD}px;
}}
QFrame#statusNode[status="running"] {{
    border-top-color: {ACCENT};
    background: {ACCENT_BG};
}}
QFrame#statusNode[status="succeeded"] {{ border-top-color: {SUCCESS}; }}
QFrame#statusNode[status="failed"] {{ border-top-color: {ERROR}; }}
QFrame#statusNode[status="warning"] {{ border-top-color: {WARNING}; }}
QFrame#brandRail {{ background: {BORDER}; border: none; border-radius: 2px; }}
QFrame#brandRail[progress="true"] {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {GRAD_START}, stop:1 {GRAD_END});
}}
QLabel#statusNodeDot {{
    min-width: 8px; max-width: 8px;
    min-height: 8px; max-height: 8px;
    border-radius: 4px;
    background: {STATUS_IDLE};
}}
QLabel#statusNodeDot[status="running"] {{ background: {ACCENT}; }}
QLabel#statusNodeDot[status="succeeded"] {{ background: {SUCCESS}; }}
QLabel#statusNodeDot[status="failed"] {{ background: {ERROR}; }}
QLabel#statusNodeDot[status="warning"] {{ background: {WARNING}; }}
QLabel#statusNodeDot[status="locked"] {{ background: {STATUS_LOCKED}; }}
QLabel#statusNodeTitle {{ font-weight: 600; font-size: 9.5pt; }}
QLabel#statusNodeDetail {{ color: {TEXT_SECONDARY}; font-size: 8.5pt; }}
QLabel#statusNodeMetrics {{ color: {TEXT_DISABLED}; font-size: 8pt; }}

/* ── 数据舱（MetricTile：数值 + 标签 + 语义色值） ─────────── */
QFrame#metricTile {{
    background: {PANEL};
    border: none;
    border-left: 3px solid {BORDER_STRONG};
    border-radius: {RADIUS_MD}px;
}}
QFrame#metricTile[accent="success"] {{ border-left-color: {SUCCESS}; }}
QFrame#metricTile[accent="warning"] {{ border-left-color: {WARNING}; }}
QFrame#metricTile[accent="error"] {{ border-left-color: {ERROR}; }}
QFrame#metricTile[accent="info"] {{ border-left-color: {INFO}; }}
QFrame#metricTile[accent="ai"] {{ border-left-color: {AI}; }}
QLabel#metricTileValue {{ font-size: 17px; font-weight: 700; }}
QLabel#metricTileLabel {{ color: {TEXT_SECONDARY}; font-size: 8.5pt; }}
/* 旧名兼容（translate 页既有 MetricStrip） */
QFrame#metricStrip {{
    background: {PANEL};
    border: none;
    border-left: 3px solid {BORDER_STRONG};
    border-radius: {RADIUS_MD}px;
}}
QFrame#metricStrip[accent="success"] {{ border-left-color: {SUCCESS}; }}
QFrame#metricStrip[accent="warning"] {{ border-left-color: {WARNING}; }}
QFrame#metricStrip[accent="error"] {{ border-left-color: {ERROR}; }}
QFrame#metricStrip[accent="info"] {{ border-left-color: {INFO}; }}
QLabel#metricStripValue {{ font-size: 17px; font-weight: 700; }}
QLabel#metricStripLabel {{ color: {TEXT_SECONDARY}; font-size: 8.5pt; }}

/* ── 概览页英雄区（§6.1） ────────────────────────────────── */
QFrame#heroCard {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 {RAISED}, stop:1 {PANEL});
    border: none;
    border-radius: {RADIUS_CARD}px;
}}
QFrame#heroCard[state="scanning"] {{ border: 1px solid {INFO}; }}
QLabel#heroTitle {{ font-size: 24px; font-weight: 700; }}
QLabel#heroSub {{ color: {TEXT_SECONDARY}; font-size: 10pt; }}
QFrame#dataStrip {{
    background: {PANEL};
    border: none;
    border-radius: {RADIUS_CARD}px;
}}
QLabel#dataStripValue {{ font-size: 20px; font-weight: 700; }}
QLabel#dataStripLabel {{ color: {TEXT_SECONDARY}; font-size: 8.5pt; }}

/* ── 活动流（§8 ActivityFeed：实时事件流，限制可见条目数） ── */
QListWidget#activityFeed {{
    background: {LOGGER_BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CARD}px;
    outline: none;
    font-family: {MONO};
    font-size: 9pt;
}}
QListWidget#activityFeed::item {{
    padding: 4px 10px;
    border: none;
    border-bottom: 1px solid {PANEL};
    color: {TEXT_SECONDARY};
}}
QListWidget#activityFeed::item:selected {{ background: transparent; color: {TEXT}; }}
QLabel[class="feedStatus"] {{ font-weight: 600; }}

/* ── 实时处理流状态文本（2026-08-21 Task #4：阶段语义配色） ── */
/* stream_status 随当前阶段着色：idle 灰 / running 薄荷青 / succeeded
   绿 / warning 琥珀 / error 珊瑚红。QSS 读 [phase="..."] 动态属性，
   _set_stream_status 改属性 + repolish 切色（无动画） */
QLabel#streamStatus {{
    font-size: 9.5pt;
    font-weight: 700;
    border-radius: {RADIUS}px;
    padding: 2px 8px;
}}
QLabel#streamStatus[phase="idle"] {{ color: {TEXT_SECONDARY}; }}
QLabel#streamStatus[phase="running"] {{ color: {ACCENT}; }}
QLabel#streamStatus[phase="succeeded"] {{ color: {SUCCESS}; }}
QLabel#streamStatus[phase="warning"] {{ color: {WARNING}; }}
QLabel#streamStatus[phase="error"] {{ color: {ERROR}; }}

/* 质量门失败原因（2026-08-21 Task #4：失败时有语义色 + 浅色胶囊底，
   无失败时回归 subtitle 灰）。class=reasonStatus 由代码动态切换 */
QLabel[class="reasonStatus"] {{
    color: {TEXT};
    background: rgba(245,184,75,0.10);
    border-left: 3px solid {WARNING};
    border-radius: {RADIUS}px;
    padding: 4px 10px;
    font-size: 9.5pt;
}}
QLabel[class="reasonIdle"] {{
    color: {TEXT_SECONDARY};
    font-size: 10pt;
}}

/* ── 写回安全栏（§6.3：独立底部安全栏） ───────────────────── */
QFrame#safetyBar {{
    background: {PANEL};
    border: none;
    border-radius: {RADIUS_CARD}px;
}}
QFrame#safetyBar[status="ready"] {{ border-left: 3px solid {SUCCESS}; }}
QFrame#safetyBar[status="blocked"] {{ border-left: 3px solid {WARNING}; }}
QFrame#safetyBar[status="error"] {{ border-left: 3px solid {ERROR}; }}
QLabel#safetyTitle {{ font-weight: 650; font-size: 10pt; }}
QLabel#safetyReason {{ color: {TEXT_SECONDARY}; font-size: 9pt; }}

/* ── 其它 ───────────────────────────────────────────────── */
QToolTip {{
    background: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 10px;
}}
QMenu {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 6px;
}}
QMenu::item {{ padding: 7px 26px; border-radius: 6px; }}
QMenu::item:selected {{ background: {CARD_HOVER}; }}
QMenu::item:disabled {{ color: {TEXT_DISABLED}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 5px 8px; }}
QCheckBox {{ spacing: 8px; color: {TEXT_SECONDARY}; }}
QCheckBox::indicator {{
    width: 15px; height: 15px;
    border-radius: 4px;
    border: 1px solid {BORDER_STRONG};
    background: {CARD};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}
QCheckBox:disabled {{ color: {TEXT_DISABLED}; }}
QRadioButton {{ spacing: 8px; color: {TEXT_SECONDARY}; }}
QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 8px;
    border: 1px solid {BORDER_STRONG};
    background: {CARD};
}}
QRadioButton::indicator:checked {{
    border: 4px solid {ACCENT};
    background: {CARD};
}}
QRadioButton:disabled {{ color: {TEXT_DISABLED}; }}
QStatusBar {{
    background: {PANEL}; color: {TEXT_SECONDARY};
    border-top: 1px solid {BORDER};
}}
QStatusBar::item {{ border: none; }}
/* 语义卡（§8 SemanticCard）：带可选语义边/光晕的基础容器 */
QFrame#card {{
    background: {PANEL};
    border: none;
    border-radius: {RADIUS_CARD}px;
}}
QFrame#card[accent="success"] {{ border-left: 3px solid {SUCCESS}; }}
QFrame#card[accent="warning"] {{ border-left: 3px solid {WARNING}; }}
QFrame#card[accent="error"] {{ border-left: 3px solid {ERROR}; }}
QFrame#card[accent="info"] {{ border-left: 3px solid {INFO}; }}
QFrame#card[accent="ai"] {{ border-left: 3px solid {AI}; }}
QLabel[class="metricLabel"] {{ color: {TEXT_SECONDARY}; font-size: 9pt; }}
QLabel[class="metricValue"] {{ color: {TEXT}; font-weight: 600; }}
QFrame#pipelineStep {{
    background: {PANEL};
    border: none;
    border-top: 2px solid {BORDER};
    border-radius: {RADIUS_CARD}px;
}}
QFrame#pipelineStep[status="succeeded"] {{ border-top-color: {SUCCESS}; }}
QFrame#pipelineStep[status="failed"] {{ border-top-color: {ERROR}; }}
QFrame#pipelineStep[status="blocked"] {{ border-top-color: {WARNING}; }}
QFrame#pipelineStep[status="running"] {{ border-top-color: {ACCENT}; }}
QLabel#capabilityBadge {{
    min-height: 22px; padding: 1px 8px; border-radius: 11px;
    color: {TEXT_SECONDARY}; background: {CARD};
}}
QLabel#capabilityBadge[status="succeeded"] {{ color: {SUCCESS}; background: #12312B; }}
QLabel#capabilityBadge[status="failed"] {{ color: {ERROR}; background: #3A2024; }}
QLabel#capabilityBadge[status="blocked"] {{ color: {WARNING}; background: #3A3020; }}
QLabel#capabilityBadge[status="running"] {{ color: {ACCENT}; background: {ACCENT_BG}; }}
QLabel[class="stepTitle"] {{ font-weight: 650; }}
QLabel[class="stepDetail"], QLabel[class="stepMetrics"] {{ color: {TEXT_SECONDARY}; font-size: 8.5pt; }}
QFrame#dropZone {{
    border: 2px dashed {BORDER_STRONG};
    border-radius: {RADIUS_CARD}px;
    background: {PANEL};
}}
QFrame#dropZone[state="drag-active"] {{
    border-color: {ACCENT};
    background: {ACCENT_BG};
}}
QFrame#dropZone[state="scanning"] {{
    border-style: solid;
    border-color: {INFO};
}}
QFrame#dropZone[state="ready"] {{
    border-style: solid;
    border-color: {SUCCESS};
}}
QFrame#dropZone[state="blocked"] {{
    border-style: solid;
    border-color: {WARNING};
}}
/* #17 档案卡重做：与英雄区同族的渐变底 + ACCENT 左边条 + 描边，
   视觉权重高于普通面板，避免被忽视 */
QFrame#profileCard {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 {RAISED}, stop:1 {PANEL});
    border: 1px solid {ACCENT_DIM};
    border-left: 4px solid {ACCENT};
    border-radius: {RADIUS_CARD}px;
}}
QFrame[class="sectionRule"] {{
    background: {BORDER};
    min-height: 1px;
    max-height: 1px;
    border: none;
}}
QLabel[class="pageTitle"] {{ font-size: 18px; font-weight: 600; }}
QLabel[class="title"] {{ font-size: 26px; font-weight: 700; }}
QLabel[class="subtitle"] {{ color: {TEXT_SECONDARY}; font-size: 10pt; }}
QLabel[class="statValue"] {{ font-size: 25px; font-weight: 700; }}
QLabel[class="statLabel"] {{ color: {TEXT_SECONDARY}; font-size: 9pt; }}
QPlainTextEdit#logView {{
    background: {LOGGER_BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CARD}px;
    font-family: {MONO};
    font-size: 9.5pt;
    color: #c3cbd8;
}}
QFrame#toast {{
    background: {RAISED};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_MD}px;
    border-left: 4px solid {INFO};
}}
QFrame#toast[success="true"] {{ border-left-color: {SUCCESS}; }}
QFrame#toast[error="true"] {{ border-left-color: {ERROR}; }}
QFrame#toast[warning="true"] {{ border-left-color: {WARNING}; }}

/* ── Top Bar（当前项目 · 目标语言 · 切换项目） ── */
QFrame#topBar {{
    background: {PANEL};
    border: none;
    border-bottom: 1px solid {BORDER};
}}
QLabel#topBarProject {{ font-size: 10.5pt; font-weight: 600; }}
QLabel#topBarProjectSub {{ color: {TEXT_SECONDARY}; font-size: 8.5pt; }}

/* ── 审校三栏工作区 ─────────────────────────────────────── */
QSplitter::handle {{
    background: transparent;
    width: 12px;
}}
QSplitter::handle:hover {{
    background: {ACCENT_BG};
}}
QFrame#detailPanel {{
    background: {PANEL};
    border: none;
    border-radius: {RADIUS_PANEL}px;
}}
/* 0.41.0：原文区改只读 QPlainTextEdit（QLabel 多行裁切致显示不全），
   字号/配色沿用原 QLabel 方案保持视觉不变 */
QPlainTextEdit#detailOriginal {{
    background: {RAISED};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    padding: 12px 14px;
    color: {TEXT};
    font-size: 14pt;
    font-weight: 600;
}}
/* 编辑区域获得最高对比度 */
QPlainTextEdit#detailEdit {{
    background: {RAISED};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_MD}px;
    padding: 10px 12px;
    color: {TEXT};
    font-size: 11pt;
    selection-background-color: {ACCENT_BG};
    selection-color: {PRIMARY_TEXT};
}}
QLabel#detailSection {{
    color: {TEXT_DISABLED};
    font-size: 8pt;
    font-weight: 600;
    letter-spacing: 1px;
}}
QLabel#detailContext {{
    color: {TEXT_SECONDARY};
    font-size: 9pt;
    background: {RAISED};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    padding: 8px 12px;
}}
/* P5：质量门改只读 QPlainTextEdit——多行理由/坏译文全文对照在
   区域内滚动，不再被整列布局挤压截断 */
QPlainTextEdit#detailReason {{
    color: {TEXT_SECONDARY};
    font-size: 9.5pt;
    background: {RAISED};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    padding: 8px 12px;
    selection-background-color: {ACCENT_BG};
    selection-color: {PRIMARY_TEXT};
}}
QLabel#saveFeedback {{ color: {SUCCESS}; font-size: 9pt; font-weight: 600; }}

/* ── 设置中心（左侧分类导航 + 居中表单 + 右侧状态卡） ─────── */
QListWidget#settingsNav {{
    background: {PANEL};
    border: none;
    border-radius: {RADIUS_PANEL}px;
    padding: 8px;
    outline: none;
}}
QListWidget#settingsNav::item {{
    padding: 10px 14px;
    margin: 2px 0;
    border-radius: {RADIUS}px;
    color: {TEXT_SECONDARY};
}}
QListWidget#settingsNav::item:hover {{ background: {CARD_HOVER}; color: {TEXT}; }}
QListWidget#settingsNav::item:selected {{
    background: {ACCENT_BG};
    color: {ACCENT};
}}
QFrame#statusCard {{
    background: {PANEL};
    border: none;
    border-left: 3px solid {INFO};
    border-radius: {RADIUS_CARD}px;
}}
QFrame#modelCard {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CARD}px;
}}
QFrame#modelCard:hover {{
    border: 1px solid {BORDER_STRONG};
}}
QLabel[class="cardTitle"] {{
    color: {TEXT};
    font-weight: 600;
    font-size: 13px;
}}
/* 2026-08-22 说明页可折叠卡片头：整行可点（手型光标由代码设置），
   悬停高亮提示可展开 */
QLabel[class="cardTitle"][collapsible="true"]:hover {{
    color: {ACCENT};
}}
QFrame#glossaryToolbar {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CARD}px;
}}
QFrame#glossaryBadge {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QLabel#glossaryBadgeValue {{
    color: {TEXT};
    font-weight: 600;
    font-size: 15px;
}}
QLabel#glossaryBadgeLabel {{
    color: {TEXT_SECONDARY};
    font-size: 9pt;
}}
QFrame#glossaryConflict {{
    background: {ACCENT_BG};
    border: none;
    border-left: 3px solid {WARNING};
    border-radius: {RADIUS}px;
}}
QFrame#glossaryNote {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CARD}px;
}}

/* ── 2026-08-22 设置页重做补充 ─────────────────────────── */
/* 底部状态通栏（服务/模型/显存/测试四段横排，替代右侧状态卡） */
QFrame#statusBar {{
    background: {PANEL};
    border: none;
    border-top: 1px solid {BORDER};
    border-radius: 0 0 {RADIUS_CARD}px {RADIUS_CARD}px;
}}
"""


def apply_theme(app: QApplication):
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(BG))
    palette.setColor(QPalette.Base, QColor(CARD))
    palette.setColor(QPalette.AlternateBase, QColor(CARD_HOVER))
    palette.setColor(QPalette.Text, QColor(TEXT))
    palette.setColor(QPalette.WindowText, QColor(TEXT))
    palette.setColor(QPalette.Button, QColor(CARD))
    palette.setColor(QPalette.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.HighlightedText, QColor(PRIMARY_TEXT))
    palette.setColor(QPalette.ToolTipBase, QColor(PANEL))
    palette.setColor(QPalette.ToolTipText, QColor(TEXT))
    app.setPalette(palette)
    app.setStyleSheet(_QSS)
