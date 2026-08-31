# -*- coding: utf-8 -*-
"""JSON TextAsset 数据区过滤测试（8 More Lives 实证 2026-08-31）。

背景：游戏数据文件（平衡表/全局设置/技能/装备字典）经 JSON 分支提取后，
数据区叶子值（hex 颜色/数值公式/资源引用枚举/数组下标枚举）全部漏网进
pending 池，翻译成中文会破坏游戏内部引用（颜色查表/STR*0.2 属性公式/
音效动画逻辑名 STRIKE/FIST）。ARCTIC/STRIKE 等词在数据区与显示区同时
出现，必须按 inner_path 结构拦截（structure-based），不能按值拦截。

本测试锁定 _is_json_data_area 的结构判定 + _should_downgrade_pending
的 json 特判接入，防止回归。
"""
import json

from hanhua.core.models import TextEntry, STATUS_PENDING, STATUS_SKIPPED
from hanhua.core.unity.extractor import (
    _is_json_data_area,
    _should_downgrade_pending,
)
from hanhua.core.formats import json_format

DATA_AREA = [
    # (inner_path, value, 说明)
    ("GameColors/SHIELD_RED/Hex", "#7d1923", "hex 颜色"),
    ("GameColors/GAMBEZON_0/Hex", "#7d1923", "hex 颜色"),
    ("Perks/CALM_MIND/ExtraSecondaryStats/MORALE", "INT*0.15", "数值公式"),
    ("WeaponStatuses/SWORD_BLOCK/ExtraSecondaryStats/W_BLOCK/0",
     "STR*0.2", "数值公式(数组下标)"),
    ("GlobalBiomsDistribution/0/0", "ARCTIC", "数组下标枚举"),
    ("GlobalBiomsDistribution/5/1", "TAYGA", "数组下标枚举"),
    ("Settings/MusicGroups/0", "MENU", "数组下标枚举"),
    ("GlobalGameSettings/WaterBioms/0", "OCEAN", "数组下标枚举"),
    ("AttackAbilitiesDatas/CLAW_FIST_STRIKE/VisualLogic", "STRIKE", "资源引用枚举"),
    ("AttackAbilitiesDatas/CLAW_FIST_STRIKE/VisualEffect", "STRIKE", "资源引用枚举"),
    ("AttackAbilitiesDatas/CLAW_FIST_STRIKE/SoundEffect", "STRIKE", "资源引用枚举"),
    ("AttackAbilitiesDatas/CLAW_FIST_STRIKE/Icon", "FIST", "资源引用枚举"),
    ("AttackAbilitiesDatas/CLAW_FIST_STRIKE/Tags/0", "MAIN", "数组下标枚举"),
    ("AmmoAbilitiesDatas/DEFAULT_SHORT_BOW_ARROW/VisualLogic", "ARROW", "资源引用枚举"),
    ("UnitPresets/RAT_THUG_MELEE/Name", "BANDIT", "资源引用枚举(Name)"),
    ("UnitPresets/RAT_THUG_MELEE/CombatAI", "SMART", "资源引用枚举(CombatAI)"),
    ("Armors/PADDED_JACKET/Layer", "UNDERARMOR", "资源引用枚举(Layer)"),
    ("GlobalBioms/DEFAULT/MovementTag", "LAND", "资源引用枚举(MovementTag)"),
    ("Settings/BiomToMusicGroup/ARCTIC", "NORDIC", "嵌套全大写(枚举键)"),
    ("GlobalGameSettings/BiomeFallbacks/LAKE", "RIVER", "嵌套全大写(回退映射)"),
    ("GlobalGameSettings/SoundFallbacks/CLAW", "STRIKE", "嵌套全大写(音效回退)"),
    ("GlobalCellFeatures/STONE/HireCollection/CROWD", "STONE", "嵌套全大写(招募人群)"),
    ("BattleGameSettings/DeafultBattleBioms/OCEAN", "WATER", "嵌套全大写(战场生态)"),
    ("UnitSettings/StancesFallback/PISTOL_2H", "PISTOL", "嵌套全大写(姿态回退)"),
    # 资源引用叶子 + 非全大写标识符值（2H 混合大小写/main 小写）
    ("Weapons/GODENDAG/VisualStance", "2H", "引用叶子混合大写值"),
    ("LocalItemStatuses/LONGAXE_DOUBLE_GRIP/VisualStance", "2H", "引用叶子混合大写值"),
    # 音频配置 JSON：目标游戏对象名（小写值也是引用）
    ("Items/16/Set_To_Gameobject", "UI", "音频目标对象名(全大写)"),
    ("Items/1/Set_To_Gameobject", "main", "音频目标对象名(小写)"),
    ("Items/9/Set_To_Gameobject", "positional", "音频目标对象名(小写)"),
]

REAL_TEXT = [
    ("Texts/3_HEX_UNITS/Text", "3-hex Units", "UI 词典显示文本"),
    ("Texts/DR_SHORT/Text", "DR", "UI 词典显示文本(值全大写也保留)"),
    ("Texts/AP_DESCR/Text", "{APS} used for any Actions in combat", "UI 词典带占位符"),
    ("Languages/EN/Text", "English", "语言名"),
    ("Languages/ZH_CH/Text", "简体中文", "语言名(中文)"),
    ("Languages/ZH_TW_C/FileName", "zh-TW-C", "语言文件名字段"),
    ("STR", "Сила", "顶层键俄语 UI"),
    ("NEW_GAME", "Новая игра", "顶层键俄语 UI"),
    ("MORALE_STATE_0", "Готовность", "顶层键俄语 UI"),
    ("Names/ALEXANDER/Text", "Alexander", "人名显示"),
    ("CityNames/BERLIN/Text", "Berlin", "城市名显示"),
    ("Names/HEINRICH/Text", "Heinrich", "人名显示"),
]


def test_json_data_area_skipped():
    """数据区条目 → _is_json_data_area True（应跳过）。"""
    for inner, value, desc in DATA_AREA:
        assert _is_json_data_area(inner, value) is True, f"{desc}: {inner} = {value!r}"


def test_real_text_kept():
    """真文本条目 → _is_json_data_area False（必须保留）。"""
    for inner, value, desc in REAL_TEXT:
        assert _is_json_data_area(inner, value) is False, f"{desc}: {inner} = {value!r}"


def _pending(inner: str, value: str) -> TextEntry:
    return TextEntry(
        file_id="asset#resources.assets#2163",
        key_path=f"asset#resources.assets#2163/json/{inner}",
        original=value,
        meta={"textasset_format": "json", "inner_path": inner,
              "reason": "textasset_display_text", "kind": "textasset"},
    )


def test_downgrade_skips_json_data_area():
    """_should_downgrade_pending 经 json 特判跳过数据区条目。"""
    for inner, value, desc in DATA_AREA:
        entry = _pending(inner, value)
        assert _should_downgrade_pending(entry) is True, \
            f"{desc}: {inner} = {value!r}"


def test_downgrade_keeps_json_real_text():
    """_should_downgrade_pending 保留真文本（不被 json 特判误杀）。"""
    for inner, value, desc in REAL_TEXT:
        entry = _pending(inner, value)
        assert _should_downgrade_pending(entry) is False, \
            f"{desc}: {inner} = {value!r}"


# ── 独立 .json 文本文件路径（parse_file → json_format.extract_json_text）──
# Task #9 验证发现的缺口：资产内嵌 TextAsset 走 unity/extractor 的
# _should_downgrade_pending（line 3005），但游戏 Data/ 下的独立 .json
# 字幕/词典/设置文件走 extractor.parse_file → json_format.extract_json_text，
# 该路径此前不调用数据区闸门——hex 颜色/数值公式/枚举引用仍以 pending 进池，
# 翻译后写回破坏游戏内部引用。此组锁定独立文件路径的数据区过滤，防回归。

_STANDALONE = json_format.extract_json_text(json.dumps({
    "GameColors": {"SHIELD_RED": {"Hex": "#7d1923"}},
    "Settings": {"MusicGroups": ["MENU", "COMBAT"]},
    "GlobalBiomsDistribution": [[["ARCTIC"]], [["TAYGA"]]],
    "AttackAbilitiesDatas": {"CLAW_FIST_STRIKE": {
        "VisualLogic": "STRIKE", "SoundEffect": "STRIKE", "Icon": "FIST"}},
    "Weapons": {"GODENDAG": {"VisualStance": "2H"}},
    "Items": {"1": {"Set_To_Gameobject": "main"}},
    "Texts": {"NEW_GAME": {"Text": "New Game"},
              "AP_DESCR": {"Text": "{APS} used for any Actions"}},
    "Languages": {"EN": {"Text": "English"}},
    "Names": {"ALEXANDER": {"Text": "Alexander"}},
}, ensure_ascii=False), "ui.json")


def test_standalone_json_skips_data_area():
    """独立 .json 文件数据区叶子 → skipped（不 pending 进池）。"""
    by_path = {e.key_path: e for e in _STANDALONE}
    for inner, _, desc in DATA_AREA:
        e = by_path.get(inner)
        if e is None:
            continue
        assert e.status == STATUS_SKIPPED, \
            f"{desc}: {inner} = {e.original!r} 应 skipped"


def test_standalone_json_keeps_real_text():
    """独立 .json 文件真文本（Texts/Languages/Names 显示叶子）→ pending。"""
    by_path = {e.key_path: e for e in _STANDALONE}
    for inner, _, desc in REAL_TEXT:
        e = by_path.get(inner)
        if e is None:
            continue
        assert e.status == STATUS_PENDING, \
            f"{desc}: {inner} = {e.original!r} 应 pending"


def test_standalone_json_skipped_never_written_back():
    """skipped 数据区条目即使异常带了译文，apply_json 也拒绝写回（宁漏勿坏）。"""
    by_path = {e.key_path: e for e in _STANDALONE}
    src = json.dumps({
        "GameColors": {"SHIELD_RED": {"Hex": "#7d1923"}},
        "Texts": {"NEW_GAME": {"Text": "New Game"}},
    }, ensure_ascii=False)
    entries = json_format.extract_json_text(src, "ui.json")
    for e in entries:
        e.translation = "X"
    out = json_format.apply_json(entries, src)
    assert "#7d1923" in out, "数据区 hex 必须原样保留"
    assert "GameColors" in out.split("Texts")[0], "数据区原文不得被改写"
    assert "X" not in out.split("Texts")[0], "数据区译文不得写回"
    assert "X" in out.split("Texts")[1], "真文本区正常写回"
