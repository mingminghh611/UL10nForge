# -*- coding: utf-8 -*-
"""P5（2026-09-06）：审校页质量门/上下文区域可读性回归。

用户实证（fromivan）：质量门区域 9pt QLabel 被整列布局挤压，多轮
审核意见/坏译文对照被截断；上下文区域读 scene/ui_position/text_type
三个提取器从不写入的 meta 键，现场必然显示「——」。

修复：
1. 质量门 → 只读 QPlainTextEdit（160px 最小高，内容多时区域内滚动）；
   rejected_candidate 全文对照不再截 80 字符；
2. 机械失败码经 quality_fix_hints 翻译成中文修正指引（与重译反馈
   同一张表——单一来源，防文案漂移）；
3. 上下文改读真实键：asset_file/obj/line + text_type_for + role/
   disposition + ctx_before/ctx_after（list）。
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from hanhua.core.quality_fix_hints import quality_fix_hint
from hanhua.core.reviewer import _quality_fix_hints, _QUALITY_FIX_HINTS
from hanhua.ui.pages.review_page import ReviewPage, _row_meta


class _Window:
    def navigate(self, _page):
        pass


def _row(eid, original, translation, status, meta):
    return {"file_id": "f", "key_path": eid, "original": original,
            "translation": translation, "status": status,
            "locked": False, "meta": meta}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ── 映射表单一来源 ────────────────────────────────────────────────

def test_reviewer_reuses_quality_fix_hints_module():
    """reviewer._QUALITY_FIX_HINTS 即 quality_fix_hints.QUALITY_FIX_HINTS。"""
    assert _QUALITY_FIX_HINTS[0] == ("newline_mismatch",
                                     "保持与原文完全一致的换行行数与结构")
    # 重译反馈路径仍产出中文指引
    assert "占位符" in _quality_fix_hints(["placeholder_mismatch"])


def test_quality_fix_hint_unknown_code_passthrough():
    """未知码原样返回——宁原样勿臆造。"""
    assert quality_fix_hint("totally_unknown_code") == "totally_unknown_code"


# ── 质量门文本 ────────────────────────────────────────────────────

def test_quality_text_translates_reason_codes():
    page = ReviewPage.__new__(ReviewPage)  # 不走完整 UI 构建
    row = _row("e1", "Press {0} to jump", "按 {0} 跳跃", "translated",
               {"quality_reasons": ["placeholder_mismatch",
                                    "rich_text_mismatch"],
                "quality_passed": False})
    text = page._quality_text(row, row["meta"])
    assert "占位符" in text and "富文本标签" in text
    # 英文码不再直接出现在展示里（码被翻译）
    assert "placeholder_mismatch" not in text


def test_quality_text_full_rejected_candidate_no_truncation():
    page = ReviewPage.__new__(ReviewPage)
    long_bad = "这是一个超过八十字符的被拒坏译文，" * 20
    row = _row("e2", "Hello", "", "blocked",
               {"review_blocked": True, "review_blocked_rounds": 3,
                "rejected_candidate": long_bad})
    text = page._quality_text(row, row["meta"])
    assert long_bad in text            # 全文对照，不截断
    assert "（3 轮）" in text           # 阻断轮数透出


def test_quality_text_pass_and_level():
    page = ReviewPage.__new__(ReviewPage)
    row = _row("e3", "Hello", "你好", "translated",
               {"quality_passed": True, "review_level": "PASS",
                "review_reason": "译文准确自然"})
    text = page._quality_text(row, row["meta"])
    assert "✓ 已通过质量门" in text
    assert "AI 审核：通过 · 译文准确自然" in text


# ── 上下文文本 ────────────────────────────────────────────────────

def test_context_text_reads_real_meta_keys():
    """asset_file/obj/kind/role/ctx_* 真实键驱动；不再依赖从不写入的
    scene/ui_position/text_type——fromivan 实证「上下文全是——」根因。"""
    page = ReviewPage.__new__(ReviewPage)
    meta = {"asset_file": "level0", "obj": 2050, "kind": "typetree",
            "role": "display", "disposition": "translate",
            "ctx_before": ["Previous line one", "Previous line two"],
            "ctx_after": ["Next line"]}
    text = page._context_text(meta)
    assert "文件 level0" in text and "对象 2050" in text
    assert "类型：UI 显示文本" in text    # text_type_for（typetree+role=display）
    assert "display、translate" in text
    assert "前文：Previous line one / Previous line two" in text
    assert "后文：Next line" in text


def test_context_text_empty_meta_shows_dash():
    page = ReviewPage.__new__(ReviewPage)
    # 空 meta 也会带 text_type_for 兜底「游戏文本」；彻底无定位/窗口
    # 信息时只剩类型行，不再是无信息量的「——」
    text = page._context_text({})
    assert "类型：" in text
    assert page._context_text({"kind": "us"}) == "类型：DLL 字符串"


# ── 构建层：质量门为只读编辑框 ────────────────────────────────────

def test_detail_reason_is_readonly_plaintext_edit(qapp, tmp_path):
    from hanhua.ui.app_state import AppState
    from hanhua.core.settings import SettingsStore
    settings = SettingsStore(tmp_path / "settings.json")
    settings.load()
    page = ReviewPage(AppState(tmp_path, settings), _Window())
    assert isinstance(page.detail_reason, QPlainTextEdit)
    assert page.detail_reason.isReadOnly()
    assert page.detail_reason.minimumHeight() >= 160
