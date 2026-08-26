"""「翻译」页（#11）：集成轻量翻译应用——本地/API 模型即时翻译。

独立于批量翻译流程：原文 → 译文单轮对话，提示词可自由编辑（默认
按当前游戏档案生成游戏本地化角色提示词，见 prompts.build_system_prompt），
历史记录保存在 app_dir/quick_translate_history.json（最近 50 条）。
长文本按行分块（每块 ≤2000 字符）逐块翻译，结果保持原文换行结构。
"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QSplitter,
                               QVBoxLayout, QWidget)

from hanhua.core.local_model import LocalModelError, discover_model
from hanhua.core.prompts import build_system_prompt
from hanhua.core.translator import (create_client, merge_translation_references,
                                    strip_prompt_echo, translate_interactive)
from hanhua.core.glossary import GlossaryStore
from hanhua.ui.app_state import AppState
from hanhua.ui.design_system import TOKENS
from hanhua.ui.widgets import PageHeader, Toast, Worker

_HISTORY_FILENAME = "quick_translate_history.json"
_HISTORY_LIMIT = 50          # 落盘上限
_HISTORY_SHOWN = 20          # 下拉展示条数
_BLOCK_CHARS = 2000          # 长文本单块字符上限（行不拆分）


def _is_symbol_only(text: str) -> bool:
    """是否为纯符号/非文字内容（无字母、无 CJK、无西文带调字母等）。

    2026-08-20 用户实证：输入 {}【】这类不可翻译符号时，小模型会把
    整段提示词当输出回显塞满译文栏。翻译前拦截——这类输入本就无
    语义可译。允许少量空白分隔（strip 已去首尾，行内空白不计为字符）。
    """
    import unicodedata
    for ch in text:
        if ch.isspace():
            continue
        cat = unicodedata.category(ch)
        # 字母（L*）与数字（N*）视为有语义可翻译内容
        if cat[0] in ("L", "N"):
            return False
    return bool(text.strip())


def _is_only_punctuation(text: str) -> bool:
    """是否纯标点/符号残渣（AI 翻译剥原文回显后只剩 '.' 等无义标点）。"""
    if not text:
        return False
    import unicodedata
    for ch in text:
        if ch.isspace():
            continue
        cat = unicodedata.category(ch)
        if cat[0] in ("L", "N"):
            return False
    return True


def _load_glossary_pairs(app_dir) -> list[tuple[str, str]]:
    """加载术语库 active 词对（翻译意图信号 + 术语译名约束）。

    失败/库不存在 → 空列表（不阻断翻译）。
    """
    if not app_dir:
        return []
    try:
        g = GlossaryStore(Path(app_dir) / "glossary.db")
        g.init_schema()
        pairs = [
            (row["term"], row["translation"])
            for row in g.list_all()
            if row.get("status", "active") == "active"]
        g.close()
        return pairs
    except Exception:  # noqa: BLE001 术语库不可用不阻断翻译
        return []


class TranslateToolPage(QWidget):
    """轻量翻译应用页：模型信息 + 可编辑提示词 + 原文/译文 + 历史。"""

    def __init__(self, state: AppState, window):
        super().__init__()
        self.state = state
        self.window = window
        self._worker: Worker | None = None
        self._running = False
        self._last_warn_ts = 0.0        # 失败提示防连点刷屏（同页 2 秒只弹一条）
        self._active_local_model = ""   # 本次翻译的本地模型名（历史记录用）
        self._history: list[dict] = []
        self._history_path = Path(state.app_dir) / _HISTORY_FILENAME
        self._load_history()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 18)
        lay.setSpacing(12)

        header = PageHeader(
            "翻译", "本地模型即时翻译：粘贴文本、调整提示词、一键翻译")
        self.settings_btn = QPushButton("模型设置 →")
        self.settings_btn.setMinimumHeight(TOKENS.control_height)
        self.settings_btn.clicked.connect(
            lambda: self.window.navigate("settings"))
        header.set_actions([self.settings_btn])
        lay.addWidget(header)

        # ── 模型信息 + 历史 ──
        info_row = QHBoxLayout()
        info_row.setSpacing(10)
        self.model_label = QLabel("")
        self.model_label.setProperty("class", "subtitle")
        info_row.addWidget(self.model_label)
        info_row.addStretch(1)
        self.history_combo = QComboBox()
        self.history_combo.setMinimumWidth(260)
        self.history_combo.setPlaceholderText("历史翻译…")
        self.history_combo.setMinimumHeight(TOKENS.control_height)
        self.history_combo.activated.connect(self._restore_history)
        info_row.addWidget(self.history_combo)
        lay.addLayout(info_row)

        # ── 提示词（可编辑；默认游戏本地化角色） ──
        lay.addWidget(self._section_label("提示词（可编辑，自定义翻译要求）"))
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setObjectName("promptEdit")
        self.prompt_edit.setFixedHeight(96)
        self.prompt_edit.setPlainText(self._default_prompt())
        self.prompt_edit.textChanged.connect(self._prompt_changed_hint)
        lay.addWidget(self.prompt_edit)
        prompt_row = QHBoxLayout()
        prompt_row.setSpacing(8)
        self.reset_prompt_btn = QPushButton("使用当前游戏档案提示词")
        self.reset_prompt_btn.setMinimumHeight(TOKENS.control_height)
        self.reset_prompt_btn.setToolTip(
            "按当前游戏档案 + 术语库/知识库/经验记忆词对重新生成")
        self.reset_prompt_btn.clicked.connect(self._reset_prompt)
        prompt_row.addWidget(self.reset_prompt_btn)
        prompt_row.addStretch(1)
        lay.addLayout(prompt_row)

        # ── 原文 / 译文 ──
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        src_panel = QWidget()
        src_lay = QVBoxLayout(src_panel)
        src_lay.setContentsMargins(0, 0, 0, 0)
        src_lay.setSpacing(6)
        src_lay.addWidget(self._section_label("原文"))
        self.src_edit = QPlainTextEdit()
        self.src_edit.setObjectName("srcEdit")
        self.src_edit.setPlaceholderText("粘贴要翻译的文本（支持多行）…")
        src_lay.addWidget(self.src_edit, 1)
        splitter.addWidget(src_panel)
        dst_panel = QWidget()
        dst_lay = QVBoxLayout(dst_panel)
        dst_lay.setContentsMargins(0, 0, 0, 0)
        dst_lay.setSpacing(6)
        dst_lay.addWidget(self._section_label("译文"))
        self.dst_edit = QPlainTextEdit()
        self.dst_edit.setObjectName("dstEdit")
        self.dst_edit.setReadOnly(True)
        self.dst_edit.setPlaceholderText("翻译结果将显示在这里…")
        dst_lay.addWidget(self.dst_edit, 1)
        splitter.addWidget(dst_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([480, 480])
        lay.addWidget(splitter, 1)

        # ── 操作行 ──
        ops = QHBoxLayout()
        ops.setSpacing(8)
        self.translate_btn = QPushButton("翻译")
        self.translate_btn.setProperty("primary", True)
        self.translate_btn.setMinimumHeight(TOKENS.control_height + 8)
        self.translate_btn.setAccessibleName("开始翻译")
        self.translate_btn.clicked.connect(self._translate)
        ops.addWidget(self.translate_btn)
        self.clear_btn = QPushButton("清空")
        self.clear_btn.setMinimumHeight(TOKENS.control_height)
        self.clear_btn.clicked.connect(self._clear_all)
        ops.addWidget(self.clear_btn)
        self.copy_btn = QPushButton("复制译文")
        self.copy_btn.setMinimumHeight(TOKENS.control_height)
        self.copy_btn.clicked.connect(self._copy_dst)
        ops.addWidget(self.copy_btn)
        self.status_label = QLabel("")
        self.status_label.setProperty("class", "subtitle")
        ops.addWidget(self.status_label, 1)
        lay.addLayout(ops)

        self._refresh_model_label()
        self.state.settingsChanged.connect(self._refresh_model_label)
        self._refresh_history_combo()

    @staticmethod
    def _section_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("detailSection")
        return label

    # ── 提示词 ───────────────────────────────────────────────
    def _default_prompt(self) -> str:
        """精简纯翻译提示词（2026-08-15 用户要求：工具页就是纯翻译
        小工具，不注入术语库/知识库/经验记忆——默认提示词与批量翻译
        的精简角色同款（build_system_prompt 2026-08-14 精简版），
        长度固定不随库膨胀，不再出现「提示词上万 token 超 ctx」）。
        提示词仍可自由编辑（用户自定义要求）。"""
        return build_system_prompt(self.state.profile, "")

    def _reset_prompt(self):
        self.prompt_edit.setPlainText(self._default_prompt())
        Toast.show(self, "已按当前游戏档案重新生成提示词", "success")

    def _prompt_changed_hint(self):
        """用户手动编辑提示词后：静默——提示词本就允许自由编辑。"""
        return

    # ── 模型信息 ─────────────────────────────────────────────
    def _refresh_model_label(self):
        api = self.state.api
        if api.mode == "local":
            try:
                # 本地模型路径可为空：settings 不存路径，按 models/ 目录自动发现
                model = discover_model(api.local_model_path,
                                       self.state.resource_dir)
                self.model_label.setText(f"本地模型：{model.stem}")
            except LocalModelError:
                self.model_label.setText(
                    "本地模型：未找到（点击右上角「模型设置」）")
        elif api.base_url and api.api_key and api.model:
            self.model_label.setText(f"API 模型：{api.model}")
        else:
            self.model_label.setText("模型未配置（点击右上角「模型设置」）")

    # ── 翻译执行（后台 worker，长文本分块） ──────────────────
    def _translate(self):
        if self._running:
            return
        text = self.src_edit.toPlainText().strip()
        if not text:
            self._warn("请输入要翻译的文本")
            return
        # 2026-08-20 用户实证：输入纯符号/格式串（{}【】等无字母与 CJK
        # 的内容）时，小模型把整段提示词当输出回显，译文栏塞满提示词。
        # 翻译前直接拦截——这类输入本就无语义可译，不调模型、不浪费
        # 资源，直接告知用户而非吐出回显。
        if _is_symbol_only(text):
            self.dst_edit.setPlainText("")
            self.status_label.setText(
                "内容为纯符号/非文字，无法翻译（请输入含字母或文字的文本）")
            Toast.show(self, "内容为纯符号/非文字，无法翻译", "warning")
            return
        api = self.state.api
        if api.mode == "api" and not (api.base_url and api.api_key
                                      and api.model):
            self._warn("请先在设置中配置 API 模型")
            return
        if api.mode == "local":
            try:
                # 与 LocalModelManager 同一发现逻辑：路径可为空，
                # 从 models/ 目录自动发现（模型已启动场景不应误报未配置）
                model = discover_model(api.local_model_path,
                                       self.state.resource_dir)
            except LocalModelError as exc:
                self._warn(f"未找到本地模型：{exc}")
                return
            self._active_local_model = model.stem
        else:
            self._active_local_model = ""
        system = self.prompt_edit.toPlainText().strip() \
            or self._default_prompt()
        blocks = self._split_blocks(text)
        self._running = True
        self.translate_btn.setEnabled(False)
        self.translate_btn.setText(
            f"翻译中…（0/{len(blocks)} 段）")
        self.status_label.setText(
            "正在翻译…" + ("（首次本地模型启动约 30-120 秒）"
                        if api.mode == "local" else ""))
        worker = Worker(self._run_blocks, api, system, blocks,
                        self.state.local_model,
                        self.state.app_dir)
        # 引用必须保存：worker 局部变量会丢 wrapper（同各页 _worker 模式）
        self._worker = worker
        worker.signals.finished.connect(
            lambda out: self._on_done(out, blocks))
        worker.signals.error.connect(self._on_error)
        QThreadPool.globalInstance().start(worker)

    def _warn(self, message: str):
        """失败提示：2 秒内同页只弹一条（连点不叠加多条消息）。"""
        now = time.monotonic()
        if now - self._last_warn_ts < 2.0:
            return
        self._last_warn_ts = now
        Toast.show(self, message, "warning")

    @staticmethod
    def _run_blocks(api, system: str, blocks: list[str],
                    local_model, app_dir):
        """后台线程：本地模式先确保服务运行，然后逐块翻译。

        2026-08-26 根治「模型回显提示词」：根因是本地 1.8B 模型的
        LocalOpenAIClient.chat 会把 system 提示词**合并进 user 消息**
        （Hy-MT2 无 system-prompt 契约），而本页默认提示词是长角色提示词
        （build_system_prompt）——模型把整段提示词当 user 输入回显塞满
        译文栏（用户多次实证 wada/setting/out of the loop 均回显整段
        提示词）。因此：

        - 本地模式：**一律不走 system**——直接走 translate_interactive
          共享多级降级链（纯中文短指令，无长提示词，1.8B 稳定产译文）。
          自定义提示词对本地 1.8B 是回显之源，故本地模式下忽略 system。
        - API 模式：模型支持独立 system 消息（Anthropic/OpenAI），才
          透传自定义提示词；输出经 strip_prompt_echo 清洗提示词/原文回显，
          回显整段提示词时剥空交降级链兜底。
        """
        if api.mode == "local":
            runtime = local_model.ensure_running(api)
            api = replace(api, base_url=runtime.endpoint,
                          api_key=runtime.api_key, model=runtime.model)
        client = create_client(api)
        refs = merge_translation_references(_load_glossary_pairs(app_dir))
        parts = []
        for block in blocks:
            if api.mode == "api" and system and system.strip():
                # API 模式支持独立 system：自定义提示词直译，回显清洗
                text, _usage = client.chat(
                    system, [{"role": "user", "content": block}])
                cleaned = strip_prompt_echo(text, system, block)
                if not cleaned.strip() or _is_only_punctuation(cleaned):
                    cleaned = translate_interactive(client, block, "zh-CN",
                                                    refs)
                parts.append(cleaned)
            else:
                # 本地模式（及未填提示词的 API）一律走共享降级链
                parts.append(
                    translate_interactive(client, block, "zh-CN", refs))
        return parts

    @staticmethod
    def _split_blocks(text: str) -> list[str]:
        """长文本按行分块（≤_BLOCK_CHARS，行不拆分，保留换行结构）。"""
        if len(text) <= _BLOCK_CHARS:
            return [text]
        blocks: list[str] = []
        current: list[str] = []
        size = 0
        for line in text.split("\n"):
            if size + len(line) + 1 > _BLOCK_CHARS and current:
                blocks.append("\n".join(current))
                current, size = [], 0
            current.append(line)
            size += len(line) + 1
        if current:
            blocks.append("\n".join(current))
        return blocks

    def _on_done(self, parts: list[str], blocks: list[str]):
        self._running = False
        self.translate_btn.setEnabled(True)
        self.translate_btn.setText("翻译")
        # 2026-08-26 用户要求「绝不能空输出」：translate_interactive 已
        # 保证每段非空（无法翻译时返回原文兜底）；此处再兜底一次——任何
        # 空段用对应原文段填充，译文区绝不空白。
        parts = [p if str(p).strip() else src
                 for p, src in zip(parts, blocks)]
        joined = "\n".join(parts)
        self.dst_edit.setPlainText(joined)
        self.status_label.setText(f"完成 · {len(parts)} 段")
        api = self.state.api
        model = ((self._active_local_model
                  or Path(api.local_model_path).stem)
                 if api.mode == "local" else api.model or "")
        self._append_history(
            self.src_edit.toPlainText().strip(),
            joined,
            model,
            self.prompt_edit.toPlainText().strip())
        self._refresh_history_combo()

    def _on_error(self, err: str):
        self._running = False
        self.translate_btn.setEnabled(True)
        self.translate_btn.setText("翻译")
        self.status_label.setText("翻译失败")
        Toast.show(self, f"翻译失败：{err}", "error")

    # ── 历史（落盘 json，最近 50 条） ────────────────────────
    def _load_history(self):
        try:
            if not self._history_path.is_file():
                return
            data = json.loads(self._history_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._history = [d for d in data
                                 if isinstance(d, dict)][:_HISTORY_LIMIT]
        except (OSError, ValueError):
            self._history = []

    def _append_history(self, src: str, dst: str, model: str, prompt: str):
        record = {
            "ts": time.strftime("%Y-%m-%d %H:%M"),
            "src": src[:2000], "dst": dst[:4000],
            "model": model, "prompt": prompt[:2000],
        }
        self._history.insert(0, record)
        del self._history[_HISTORY_LIMIT:]
        try:
            self._history_path.write_text(
                json.dumps(self._history, ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            pass

    def _refresh_history_combo(self):
        self.history_combo.clear()
        for record in self._history[:_HISTORY_SHOWN]:
            first_line = record.get("src", "").strip().splitlines()
            summary = (first_line[0] if first_line else "")[:24]
            label = f"{record.get('ts', '')} · {summary}"
            self.history_combo.addItem(label, record)
        self.history_combo.setEnabled(
            self.history_combo.count() > 0)

    def _restore_history(self, index: int):
        record = self.history_combo.itemData(index)
        if not isinstance(record, dict):
            return
        self.src_edit.setPlainText(record.get("src", ""))
        self.dst_edit.setPlainText(record.get("dst", ""))
        if record.get("prompt"):
            self.prompt_edit.setPlainText(record["prompt"])
        self.status_label.setText(f"已载入历史 · {record.get('ts', '')}")

    # ── 小操作 ───────────────────────────────────────────────
    def _clear_all(self):
        self.src_edit.clear()
        self.dst_edit.clear()
        self.status_label.setText("")

    def _copy_dst(self):
        from PySide6.QtWidgets import QApplication
        text = self.dst_edit.toPlainText()
        if not text:
            Toast.show(self, "译文为空", "warning")
            return
        QApplication.clipboard().setText(text)
        Toast.show(self, "译文已复制", "success")
