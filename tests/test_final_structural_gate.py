# -*- coding: utf-8 -*-
"""结构性文本『翻译前最终终检』回归测试（识别 B 节兜底）。

背景：提取器（rawstr/typetree/mono/il2cpp/textasset/json…）虽然各自分类，
但历史上一度出现某条路径把 Key/ID/路径/变量名/资源名误标 role=display、
disposition=translate 放行进翻译队列，导致翻译后游戏按键失效/UI 消失/
音频丢失（0.37.x 多游戏回归）。

机制：is_actionable_translation 是全部队列入口（翻译页待翻译池 / runner
run_scope / 概览计数）的**单一权威判定**——任何条目进队列前必须通过它。
此处给该判定追加『原文结构终检』（should_skip），对原文内容做确定性结构
拦截，无论提取层为何判定。真显示文本（句子/TMP 组合串/按钮词/白名单词）
不受影响。

本测试固化：
1. 结构文本即使被提取层误标为可译，也绝不能被 is_actionable 放行进队列
   （覆盖全部队列入口）。
2. 真显示文本不受终检误伤（宁漏勿坏的反面：不得因兜底误杀正常翻译）。
"""
from __future__ import annotations

import pytest

from hanhua.core.models import (_final_structural_backstop,
                                is_actionable_translation)
from hanhua.core.placeholders import DISPLAY_WORDS, should_skip


def _mk(original, *, status="pending", role="display", disposition="translate",
        confidence="medium", kind="rawstr", **extra_meta):
    """构造一个被提取层【误标为可译】的条目（最严苛前提）：role=display、
    disposition=translate、confidence=medium/high——即便这样，结构终检也必须拦。"""
    from hanhua.core.models import TextEntry
    meta = {"role": role, "disposition": disposition,
            "confidence": confidence, "kind": kind, **extra_meta}
    return TextEntry(file_id="f", key_path="k", original=original, status=status, meta=meta)


# ── 契约 1：无歧义结构文本在任何队列前提（最宽放行 meta）下都被终检拦截 ──
HARD_STRUCTURAL_CASES = [
    # 路径 / URL / 资源名
    "C:/Program Files/Mono", "Assets/Scenes/Main.unity",
    "https://support.apple.com/en-us/example", "v2.0",
    "UnityEngine.InputSystem.PlayerInput",
    "BaiJamjuree-Medium SDF",
    # GUID / 哈希 / 版本号 / 纯数字
    "GUID:cef3ca5fc32178c449992c58120ccded",
    "System.Boolean, mscorlib, Version=2.0.0.0, Culture=neutral",
    "12345", "0.4.3", "cef3ca5fc32178c449992c58120ccded",
    # 代码 / 程序结构
    "{0} : {1}", "xChDC-Gs%OmaMl+g",
    # 启动脚本 / 引擎配置参数
    "-screen-fullscreen 0", "--platform=Windows",
    # 确定性引擎串（即使提取层标 display 也是引擎内部串）
    "default sprite asset",
]


@pytest.mark.parametrize(
    "text", HARD_STRUCTURAL_CASES, ids=lambda t: t[:24])
def test_structural_never_actionable_even_when_mislabeled(text):
    """最严苛前提：即使提取层把无歧义结构文本标成 display/translate/high，
    is_actionable_translation 也不得放行进队列。"""
    for confidence in ("medium", "high"):
        e = _mk(text, confidence=confidence)
        assert _final_structural_backstop(e), f"终检漏放行结构文本: {text!r}"
        assert not is_actionable_translation(e), (
            f"结构文本被放行进队列: {text!r} (confidence={confidence})")
        e2 = _mk(text, status="failed", confidence=confidence)
        assert not is_actionable_translation(e2), (
            f"failed 结构文本仍被放行进队列: {text!r}")


# ── 契约 2：真显示文本绝不受终检误伤 ──
DISPLAY_CASES = [
    # 按钮 / 菜单短文本（含白名单词）
    "Save", "Load", "New Game", "Settings", "Options", "Quit", "resume",
    "Fullscreen", "SFX", "Volume", "Music", "Back", "Begin", "Credits",
    "Extras", "Submit", "Shop", "Seed",
    # 短句（自然语言）
    "Press E to interact", "Welcome back, warrior!", "Game Over",
    "Board Cleared", "Main Menu", "Pan Up", "Pan Left", "Zoom out",
    # 设置项长句
    "When enabled, numbers will also count exploded mines.",
    "Hide resource tooltips", "Disable tutorial dialogs",
    "Each class brings a unique ability into the run",
    # TMP 组合串（正文可译）
    "<b>hi</b>", "{punch=3,2}* Y A W N *{w=3}{x}", "<color=red>Warning!</color>",
    # C# 转义换行格式串（HUD/多行模板，字面 \n 是结构标记不是噪音）
    "Alpha\\n\\nBravo\\nCharlie",
    "\r\nSettings\r\n\r\n{0}kg\\n£{1:0.00}\r\n",
    # 显示词白名单的标识符形态
    "fullscreen", "v-sync",
    # 键风格标识符的单 token 真显示语义（关卡/对话框名）——终检闸门不误杀
    "Level1", "Room2",
    # 2 词短语（按钮/菜单）
    "Rate the game",
]


@pytest.mark.parametrize("text", DISPLAY_CASES, ids=lambda t: t[:24])
def test_real_display_text_not_blocked(text):
    """真显示文本必须仍可进队列（终检不得误伤，宁漏勿坏的反面）。"""
    e = _mk(text)
    assert not _final_structural_backstop(e), f"终检误伤真显示文本: {text!r}"
    assert is_actionable_translation(e), f"真显示文本被误拦: {text!r}"


def test_display_words_not_blocked():
    """DISPLAY_WORDS 白名单词：终检不得新增误伤（凡 should_skip=False 的词
    必须仍可进队列）。个别 2 字母词（hi/ok/no/go/on/up）命中既有语言代码
    rule 被判键风格、由提取层 should_skip 拦——那是既有防御规则，本闸门
    不重复拦（终检职责是硬结构兜底，不做键风格判定），故不在此断言。"""
    for w in DISPLAY_WORDS:
        if should_skip(w):
            continue  # 既有键风格规则处理，非本闸门职责
        e = _mk(w)
        assert not _final_structural_backstop(e), f"终检误伤白名单词: {w!r}"
        assert is_actionable_translation(e), f"真显示词被终检误拦: {w!r}"


def test_low_confidence_structural_meta_also_blocked():
    """低置信度结构条目（提取层留档）同样不进队列。"""
    e = _mk("ui_newGame", confidence="low")
    assert not is_actionable_translation(e)


def test_status_gate_independent_of_backstop():
    """结构终检独立于 status：pending/failed/translated 一律按硬结构拒。"""
    for status in ("pending", "failed", "translated"):
        e = _mk("https://x.com/y", status=status)
        assert not is_actionable_translation(e), f"{status} 硬结构文本被放行"


def test_key_style_identifiers_delegated_to_extractors_not_blocked_by_backstop():
    """键风格标识符（ui_newGame/MENU_PLAY/en）由各提取器的 should_skip
    分层拦截，**终检闸门不重复拦**（防误伤 Level1/Room2 等真关卡/对话框名）：
    本闸门职责 = 无歧义机器结构兜底。验证：这类单 token 在终检层视为非结构
    兜底（但队列判定仍由 role/disposition 决定，正常 pending 的 display 名
    不误伤）。"""
    for text in ("ui_newGame", "MENU_PLAY", "en", "Level1", "Room2"):
        e = _mk(text)
        # 终检闸门本身不硬拦键风格标识符（避免误杀真关卡/对话框名）
        # ——是否跳过交给提取层 + role/disposition + should_skip 分层
        assert _final_structural_backstop(e) is False, \
            f"终检闸门误拦键风格标识符（应由提取层分层）: {text!r}"
