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
from hanhua.core.models import TextEntry
from hanhua.core.unity.extractor import (
    _is_json_data_area,
    _should_downgrade_pending,
)

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
