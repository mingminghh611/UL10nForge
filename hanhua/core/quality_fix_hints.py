# -*- coding: utf-8 -*-
"""机械质量门 reason code → 中文修正指引（单一来源）。

2026-09-06 P5 从 reviewer.py 迁出为独立模块：审校页质量门区域需要把
quality_reasons 的英文码翻译成用户能读懂的中文（此前直接显示
「未通过：placeholder_mismatch、rich_text_mismatch」，用户不知道要
改什么）。reviewer 的重译反馈与审校页的展示共用同一张映射表，
防止两处文案漂移。
"""
from __future__ import annotations

# 机械失败原因 → 修正指引（2026-08-15 minato 实证：4B 判 PASS 但
# 机械门 failed 的条目强制重译时，反馈只有干巴巴的原因列表——模型
# 不知道具体修什么，重译输出再被同一机械门拒 → BLOCKED 留人工）
# 2026-08-22 补全：覆盖 quality.py 全部 failure reason——此前缺
# key_name_mistranslated 等 10 项时落到通用「请按原文语义重译」，
# 模型不知道具体修什么，重译再被同一门拒（「翻译没问题却被阻断」
# 的直接根因之一：反馈盲修 → 多轮不收敛 → BLOCKED）
QUALITY_FIX_HINTS: tuple[tuple[str, str], ...] = (
    ("newline_mismatch", "保持与原文完全一致的换行行数与结构"),
    ("line_content_mismatch", "保持与原文一致的行内容分布（不合并不拆行）"),
    ("placeholder_mismatch", "完整保留原文全部占位符（{0}、%s 等），不得增删"),
    ("rich_text_mismatch", "完整保留原文全部富文本标签（<b>…</b> 等）"),
    ("numeric_mismatch", "译文必须包含原文的全部数字且数值不变"),
    ("untranslated_text", "必须译成中文，不得保留大段原文英文"),
    ("target_script_mismatch", "只输出简体中文译文，不混入其他文字"),
    ("explanatory_prefix",
     "直接输出译文本身，不要任何「译文：」等前缀或解释说明"),
    ("markdown_wrapper", "不要用 markdown 代码块/列表标记包裹译文"),
    ("key_name_mistranslated",
     "物理键名（Shift/Ctrl/RMB/Esc/Space 等）与按键别名必须原样保留英文，"
     "不得译成中文"),
    ("glossary_mismatch", "严格遵守术语表词对，按术语表译文用词"),
    ("consistency_mismatch",
     "同一原文在同一语境必须给同一译文（与批内其他条目一致）"),
    ("builtin_ui_mismatch",
     "引擎/系统内置 UI 文案按该引擎官方中文译法输出"),
    ("input_token_mismatch",
     "输入标记/协议 token（如 {input} 等模板占位）必须原样保留"),
    ("action_word_residue",
     "动作动词必须译成中文，不得残留原文英文动词"),
    ("empty_translation", "必须输出非空译文"),
    ("illegal_control", "不得输出控制字符（除原文已有的换行/制表符）"),
    ("direction_mismatch",
     "输入绑定语境的方向词（left/right/up/down 等）必须译出对应方向字"
     "（左/右/上/下）"),
)

_HINT_MAP = dict(QUALITY_FIX_HINTS)


def quality_fix_hint(reason: str) -> str:
    """单个 reason code → 中文指引；未知码原样返回（宁原样勿臆造）。"""
    return _HINT_MAP.get(str(reason), str(reason))
