# -*- coding: utf-8 -*-
"""TextAsset 数字密集数据表过滤回归测试。

背景（electric-trains 实证 2026-09-02）：fp_level_*（列车调度表）与
mission_*_targets（关卡目标表）TextAsset——行是「数字冒号段:资源名」调度结构
（'2:-1:-1:FreeTrain_v14_hopper'），无玩家可见文本，却被当 textasset_display_text
逐行进池（135+ 条/文件），模型把 FreeTrain_… 模型名乱译写回后列车加载失败。

单行判定：数字占比 ≥15%，或 ≥3 位数字 + ≥2 个冒号（'0:29:-1:Name' 配置段结构）
→ 配置行。真字幕/对话（'Hello, how are you today?'/'Level 1 complete!'）无冒号段
不命中。整文件配置行占比过半 → 数字/调度表，整文件跳过。
"""
from __future__ import annotations

from hanhua.core.unity.extractor import _textasset_entries


def _mk(text: str) -> list:
    return _textasset_entries("f", 12165, text.encode("utf-8"), "resources.assets", {})


# 调度表（整文件应跳过 → 0 条目 + digit_dense 计数）
DATAFILE = (
    "22657:21229:21803:22557:21176:22749:1\n"
    "0:-1:-1:none\n"
    "2:1:-1:FreeTrain_v14_hopper\n"
    "1:18:22:Last:0:50\n"
    "2:-1:-1:FreeTrain_v3_hopper (1)\n"
    "1:50:1:FreeTrain_v3_hopper (1):80:100\n"
)


def test_digit_dense_schedule_file_skipped():
    skipped = {}
    ent = _textasset_entries("f", 1, DATAFILE.encode("utf-8"), "r.assets", skipped)
    assert len(ent) == 0
    # 首行纯数字(无字母)使 alpha 密度不足, 可能先触发 low_alpha_density——
    # 两条过滤任一命中整文件都跳过即可
    assert (skipped.get("textasset_digit_dense_data") == 1
            or skipped.get("textasset_low_alpha_density") == 1)


def test_colon_schedule_file_skipped():
    """mission_*_targets：'0:29:-1:Name' 数字 12-14% 但冒号结构确定是配置。"""
    mission = (
        "2:-1:-1:FreeTrain_v5_passagirskiy_retro (0)\n"
        "0:29:-1:FreeTrain_v5_passagirskiy_retro (0)\n"
        "0:33:-1:FreeTrain_v5_passagirskiy_retro (0)\n"
        "0:19:-1:FreeTrain_v5_passagirskiy_retro (0)\n"
        "0:20:-1:FreeTrain_v5_passagirskiy_retro (0)\n"
    )
    skipped = {}
    ent = _textasset_entries("f", 1, mission.encode("utf-8"), "r.assets", skipped)
    assert len(ent) == 0
    assert skipped.get("textasset_digit_dense_data") == 1


def test_real_subtitle_file_not_skipped():
    """真字幕/对话行（无冒号数字段）不受数字表过滤误伤。"""
    text = (
        "Hello, how are you today?\n"
        "Level 1 complete!\n"
        "The train will depart in 5 minutes.\n"
    )
    skipped = {}
    ent = _textasset_entries("f", 1, text.encode("utf-8"), "r.assets", skipped)
    # 这些行不是配置行：'Level 1 complete!' 无冒号、1 位数字
    assert skipped.get("textasset_digit_dense_data") is None
    assert len(ent) >= 3, "真对话/字幕不得被数据表过滤吞掉"


def test_digit_dense_does_not_break_numbered_dialogue():
    """带编号/时间的对话句不受过滤误伤（单句含少量数字冒号不达过半）。"""
    text = (
        "Objective: deliver 3 crates\n"
        "Time remaining: 5:00\n"
        "You found the hidden room!\n"
    )
    skipped = {}
    ent = _textasset_entries("f", 1, text.encode("utf-8"), "r.assets", skipped)
    assert skipped.get("textasset_digit_dense_data") is None
    assert len(ent) >= 3
