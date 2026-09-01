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
    # UI 设置定义 JSON（project-arrhythmia ui-setting-def 实证 2026-09-01）：
    # values/function_call 是机器引用（枚举/对象名/函数调用指令），翻译写坏
    # 设置读写；name/ui_desc 是真显示文本保留（在 REAL_TEXT 里锁）。
    ("ArcadeHealthMod/0/values", "menu", "ui-setting values(小写枚举)"),
    ("ArcadeHealthMod/0/values", "nostalgia", "ui-setting values(小写枚举)"),
    ("ArcadeHealthMod/0/values", "down", "ui-setting values(小写枚举)"),
    ("ArcadeHealthMod/0/function_call", "vote::false", "ui-setting function_call(函数指令)"),
    ("ArcadeHealthMod/0/function_call", "vote::true", "ui-setting function_call(函数指令)"),
    ("Modifiers/0/values/1", "fil", "ui-setting values(数组下标)"),
    ("Modifiers/0/values/2", "left", "ui-setting values(数组下标)"),
]

REAL_TEXT = [
    ("Texts/3_HEX_UNITS/Text", "3-hex Units", "UI 词典显示文本"),
    ("Texts/DR_SHORT/Text", "DR", "UI 词典显示文本(值全大写也保留)"),
    ("Texts/AP_DESCR/Text", "{APS} used for any Actions in combat", "UI 词典带占位符"),
    ("Languages/EN/Text", "English", "语言名"),
    ("Languages/ZH_CH/Text", "简体中文", "语言名(中文)"),
    ("Languages/ZH_TW_C/FileName", "zh-TW-C", "语言文件名字段"),
    # ui-setting-def 显示文本（project-arrhythmia 实证）：name 是设置项显示
    # 名（'Default'/'Practice'/'1 Hit'/'Hated it'），ui_desc 是设置说明句。
    # 它们与上面的 values/function_call 同处一个顶层块，绝不能误杀。
    ("ArcadeHealthMod/0/name", "Default", "ui-setting name 显示名"),
    ("ArcadeHealthMod/0/ui_desc", "All effects", "ui-setting ui_desc 说明"),
    ("ArcadeHealthMod/0/ui_desc", "Camera will jiggle during gameplay",
     "ui-setting ui_desc 完整说明句"),
    ("ArcadeHealthMod/0/ui_desc", "Only needed effects, shake and vignette",
     "ui-setting ui_desc 带逗号说明句"),
    ("Languages/EN/FileName", "English", "语言文件名字段"),
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
    "ArcadeHealthMod": [{"name": "Default", "values": ["menu", "nostalgia"],
                         "function_call": ["vote::false"], "ui_desc": "All effects"}],
    "Modifiers": [{"name": "Off", "values": ["fil", "left"],
                   "function_call": ["vote::true"], "ui_desc": "Only needed effects"}],
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


# ── Spine 骨骼动画 JSON（soul-delivery/zero-deaths/monsters-of-new-spark
# 实证 2026-09-01）──────────────────────────────────────────────
# 顶层键 skeleton/bones/slots/ik/skins/animations 是 Spine 运行库数据：
# 值全是骨骼名/插槽名/附件名/皮肤名/动画名查表引用，翻译写坏骨骼动画
# 加载。文件级判定（skeleton 键 + 全键 ⊆ _SPINE_TOP_KEYS）整文件跳过。

_SPINE_SAMPLE = json.dumps({
    "skeleton": {"hash": "abc", "spine": "3.8.99", "x": 0, "y": 0,
                 "width": 1024, "height": 1024},
    "bones": [{"name": "root"}, {"name": "bone", "parent": "root"},
              {"name": "Foot RL", "parent": "root"}],
    "slots": [{"name": "Tail", "bone": "root", "attachment": "Tail"},
              {"name": "Body", "bone": "root", "attachment": "Body"}],
    "ik": [{"name": "Foot RL", "order": 1, "bones": ["bone"],
            "target": "Foot RL"}],
    "skins": {"default": {"Tail": {"Tail": {"name": "Tail"}}}},
    "events": [{"name": "footstep", "time": 0}],
    "animations": {"idle": {"bones": {}}},
}, ensure_ascii=False)

_SPINE_VARIANT_NO_IK = json.dumps({
    "skeleton": {"hash": "abc"},
    "bones": [{"name": "root"}],
    "slots": [{"name": "Head", "bone": "root"}],
    "skins": {"default": {}},
    "animations": {"walk": {}},
}, ensure_ascii=False)

_SPINE_VARIANT_TRANSFORM = json.dumps({
    "skeleton": {"hash": "abc"},
    "bones": [{"name": "root"}],
    "slots": [{"name": "Head", "bone": "root"}],
    "ik": [{"name": "Hand", "order": 2, "bones": ["bone4"],
            "target": "Hand"}],
    "transform": [{"name": "Hand", "order": 2, "bones": ["bone4"],
                   "target": "Hand", "rotation": -180}],
    "skins": {"default": {}},
    "animations": {"idle": {}},
}, ensure_ascii=False)


def test_spine_document_empty():
    """Spine 动画 JSON 整文件不产生条目（文件级判定在条目级之前）。"""
    entries = json_format.extract_json_text(_SPINE_SAMPLE, "1000.json")
    assert entries == [], "Spine 动画 JSON 不得进池"

    e2 = json_format.extract_json_text(_SPINE_VARIANT_NO_IK, "Felix.json")
    assert e2 == [], "低版本 Spine（无 ik/events）同样跳过"

    e3 = json_format.extract_json_text(_SPINE_VARIANT_TRANSFORM, "2000.json")
    assert e3 == [], "含 transform 约束的 Spine 同样跳过"


def test_spine_detect_variants():
    """_is_spine_document 覆盖全键子集变体（缺键/多键）。"""
    import json as _j
    for text in (_SPINE_SAMPLE, _SPINE_VARIANT_NO_IK, _SPINE_VARIANT_TRANSFORM):
        data = _j.loads(text)
        assert json_format._is_spine_document(data) is True


def test_spine_not_false_positive():
    """非 Spine 文件（字典/设置/显示文本）不得被 Spine 判定误杀。"""
    import json as _j
    real = _j.loads(json.dumps({
        "Texts": {"NEW_GAME": {"Text": "New Game"}},
        "Settings": {"MusicGroups": ["MENU"]},
    }))
    assert json_format._is_spine_document(real) is False
    # 顶层只有部分 Spine 键、缺 skeleton → 非 Spine（防半截 JSON 误伤）
    partial = _j.loads(json.dumps({"bones": [{"name": "root"}]}))
    assert json_format._is_spine_document(partial) is False
    # 空 dict / 非 dict 根
    assert json_format._is_spine_document({}) is False
    assert json_format._is_spine_document([]) is False


def test_extractor_spine_skipped_counter():
    """extractor._textasset_entries 对 Spine JSON 整文件跳过并留档。"""
    from hanhua.core.unity import extractor as ex
    skipped: dict = {}
    entries = ex._textasset_entries(
        "asset#data.unity3d#923", 923,
        _SPINE_SAMPLE.encode("utf-8"), "data.unity3d", skipped)
    assert entries == []
    assert skipped.get("textasset_json_spine", 0) == 1


# ── PAChat 终端脚本 JSON（project-arrhythmia 实证 2026-09-01）─────────────
# {settings, branches} 顶层是游戏内 PAChat 终端脚本（启动/登录/教程/对话/
# 结算全在这）。文本值绝大多数是机器引用：分支名（initial_branch/name=
# 入口跳转标识）、element settings 数组配置（loop:N/alignment:*/width:0.5/
# bg-color:text-color/font-style:bold）、data 命令 token（wait::2/branch::
# login/replaceline::6::…/setbg::E0E0E0/loadscene:Main Menu）。真显示文本
# 只占少部分且与命令 token 在同一个 data 数组里逐条混杂——条目级过滤
# 只能拦命令前缀，全文件机器引用主导 → 文件级跳过整文件（宁漏勿坏，
# 防译坏分支跳转/命令解析）。

_PACHAT_SAMPLE = json.dumps({
    "settings": {"initial_branch": "login"},
    "branches": [
        {"name": "copyright", "settings": {"clear_screen": "true"},
         "elements": [{"type": "text", "data": ["Copyright (C) 2052 Vitamin Games LLC."]},
                      {"type": "event", "data": ["wait::2", "branch::login"]}]},
        {"name": "login", "settings": {"clear_screen": "true"},
         "elements": [{"type": "event", "data": ["setbg::E0E0E0", "settext::212121"]},
                      {"type": "text", "settings": ["loop:3"], "data": [" "]},
                      {"type": "text", "settings": ["alignment:center"],
                       "data": ["PA Mainframe Interface"]},
                      {"type": "text",
                       "data": ["| Login:                   |", "wait::0.5"]}]},
    ],
}, ensure_ascii=False)

# 全键子集防误伤：含 branches 但顶层不止 settings/branches（如嵌在真游戏
# 文件里的子结构）→ 非 PAChat
_PACHAT_PARTIAL = json.dumps({
    "settings": {"initial_branch": "login"},
    "branches": [],
    "extra": {"a": 1},
}, ensure_ascii=False)


def test_pachat_document_empty():
    """PAChat 终端脚本 JSON 整文件不产生条目（文件级判定在条目级之前）。"""
    entries = json_format.extract_json_text(_PACHAT_SAMPLE, "level_1.json")
    assert entries == [], "PAChat 脚本不得进池（分支名/命令 token 是机器引用）"


def test_pachat_detect():
    """_is_pachat_document 覆盖全键子集变体（缺/多键）。"""
    import json as _j
    assert json_format._is_pachat_document(_j.loads(_PACHAT_SAMPLE)) is True
    assert json_format._is_pachat_document(_j.loads(_PACHAT_PARTIAL)) is False
    assert json_format._is_pachat_document({}) is False
    assert json_format._is_pachat_document([]) is False
    assert json_format._is_pachat_document(
        _j.loads(json.dumps({"branches": []}))) is True


def test_extractor_pachat_skipped_counter():
    """extractor._textasset_entries 对 PAChat JSON 整文件跳过并留档。"""
    from hanhua.core.unity import extractor as ex
    skipped: dict = {}
    entries = ex._textasset_entries(
        "asset#resources.assets#1", 1,
        _PACHAT_SAMPLE.encode("utf-8"), "resources.assets", skipped)
    assert entries == []
    assert skipped.get("textasset_json_pachat", 0) == 1


def test_pachat_not_false_positive():
    """非 PAChat 文件（字典/显示文本 JSON）不得被 PAChat 判定误杀。"""
    import json as _j
    real = _j.loads(json.dumps({
        "Texts": {"NEW_GAME": {"Text": "New Game"}},
        "Settings": {"MusicGroups": ["MENU"]},
    }))
    assert json_format._is_pachat_document(real) is False
    # Spine 是 {skeleton,...} 顶层，不冲突
    assert json_format._is_pachat_document(
        _j.loads(_SPINE_SAMPLE)) is False
