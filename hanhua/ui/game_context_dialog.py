# -*- coding: utf-8 -*-
"""游戏语境「查看游戏介绍」对话框（设计文档 §11/§24，2026-08-21）。

用户看到的是易读的游戏介绍，模型使用的是结构化 Game Context——二者
同一份数据（§11）。此对话框展示用户可见视角：游戏名/类型/背景/简介/
主要角色/专有名词/语言风格/翻译注意事项。不展示 Token/推理深度/置信度
等系统内部信息（§24 明确禁止）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from hanhua.ui.design_system import TOKENS


class GameContextDialog(QDialog):
    """展示 Game Context 的用户可读「游戏介绍」。"""

    def __init__(self, ctx: dict, parent=None):
        super().__init__(parent)
        self.ctx = ctx or {}
        self.setWindowTitle("游戏介绍")
        self.setMinimumSize(520, 560)
        self.setModal(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(14)

        title = QLabel("游戏介绍")
        title.setProperty("class", "pageTitle")
        title.setAlignment(Qt.AlignCenter)
        outer.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(6, 6, 6, 6)
        body_lay.setSpacing(10)

        rows = self._build_rows()
        if not rows:
            # 2026-08-31 空上下文兜底：识别失败/结果全「未知」时对话框
            # 呈现提示文案，不再是一片黑色空窗（用户实证「查看游戏介绍
            # 是黑色」的根因之一）。
            empty = QLabel(
                "游戏语境尚未建立或识别结果为空。\n"
                "请点击「开始识别」分析游戏背景；\n"
                "识别完成后此处会显示游戏介绍。")
            empty.setWordWrap(True)
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet("color:#8a8a8a;")
            body_lay.addWidget(empty)
        for label, value in rows:
            if not value:
                continue
            block = QVBoxLayout()
            block.setSpacing(2)
            cap = QLabel(label)
            cap.setProperty("class", "subtitle")
            text = QLabel(value)
            text.setWordWrap(True)
            text.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            block.addWidget(cap)
            block.addWidget(text)
            body_lay.addLayout(block)
        body_lay.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.setMinimumHeight(TOKENS.control_height)
        close_btn.setProperty("primary", True)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        btn_row.addStretch(1)
        outer.addLayout(btn_row)

    def _build_rows(self) -> list[tuple[str, str]]:
        """Game Context → 易读介绍行（§11 用户视角）。"""
        c = self.ctx
        game_name = str(c.get("game_name") or "").strip()
        genre = str(c.get("genre") or "").strip()
        setting = str(c.get("setting") or "").strip()
        summary = str(c.get("summary") or "").strip()
        style = str(c.get("style") or "").strip()
        chars = c.get("characters") or []
        terms = c.get("terms") or []
        notes = c.get("translation_notes") or []

        rows: list[tuple[str, str]] = []
        title = "《" + game_name + "》" if game_name and game_name != "未知" else ""
        if genre and genre != "未知" or setting and setting != "未知":
            meta = " / ".join(
                p for p in (genre, setting)
                if p and p != "未知")
            if meta:
                title = f"{title}　{meta}".strip() if title else meta
        if title:
            rows.append(("游戏", title))
        if summary and summary != "未知":
            rows.append(("简介", summary))
        if chars:
            rows.append(("主要角色", "；".join(str(x) for x in chars)))
        if terms:
            rows.append(("专有名词", "；".join(str(x) for x in terms)))
        if style and style != "未知":
            rows.append(("语言风格", style))
        if notes:
            rows.append(("翻译注意事项", "；".join(str(x) for x in notes)))
        return rows
