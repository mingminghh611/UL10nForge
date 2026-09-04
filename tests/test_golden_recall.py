"""m_Name 对象标识名测量口径回归测试（0.38.0）。

scripts/_golden_recall.py 的 _is_object_identity_name 把「旧库过提取、
当前提取器按 _mono_object_name_span 正确跳过」的对象名从召回分母剔除
（drova 143 条实证）。本测试锁定判定边界：

- 必须豁免：GNode( 前缀、AI_Set/ABI_ 能力键、Pascal_Pascal 键、
  形态键+数字后缀（Unity 同名去重 ' N'）、Seq: 前缀、含空格的
  下划线标识符开头的对象名——且来源是 MonoBehaviour 常驻载体；
- 必须不豁免：真 UI 词（'Combat Music'/'Settings'）、尾段小写句词、
  无下划线的 'Level 1'、DLL/il2cpp 来源的任何形态（无 m_Name）。

这组边界是「宁漏勿坏」在测量端的体现：豁免只解释旧库过提取，
绝不掩盖真缺口（把真 UI 文本误豁免 = 哑信号回归）。
"""
from scripts._golden_recall import _is_object_identity_name

# MonoBehaviour 常驻载体（rawstr 路径 m_Name 保护生效的 file_id 形态）
_ASSET_FID = "resources.assets"
_LEVEL_FID = "level0"
_SHARED_FID = "sharedassets1.assets"
_BUNDLE_FID = "ui/icons.bundle"


def test_identity_shapes_from_mono_carriers():
    # GNode( 前缀（drova AI 节点名，含空格短语）
    assert _is_object_identity_name("GNode(Global Attack)", _ASSET_FID)
    assert _is_object_identity_name("GNode(Global Attack Cooldown)", _LEVEL_FID)
    # AI_Set / ABI_ 能力键（无空格与带空格两种）
    assert _is_object_identity_name("AI_SetCombatMusic", _SHARED_FID)
    assert _is_object_identity_name("AI_Set Combat Music", _ASSET_FID)
    assert _is_object_identity_name("ABI_Regeneration _InstantHeal", _ASSET_FID)
    # Pascal_Pascal 任务键 + 下划线连接的多段
    assert _is_object_identity_name("SubQuest_FOA_HelpFrom NPC_Hunter", _ASSET_FID)
    # Unity 同名对象自动去重 ' N' 后缀（键段含下划线）
    assert _is_object_identity_name("StatusEffect_StickyWeb 1", _SHARED_FID)
    assert _is_object_identity_name("Misc_MysteryNote 1", _ASSET_FID)
    # Seq: 行为序列节点名
    assert _is_object_identity_name("Seq:Medium Circle", _ASSET_FID)
    # Pascal_Pascal 键形态
    assert _is_object_identity_name("Pascal_Pascal", _LEVEL_FID)
    # 键风格单 token（ui_newGame）与 bundle 载体
    assert _is_object_identity_name("ui_newGame", _BUNDLE_FID)


def test_non_identity_ui_text_stays_in_denominator():
    # 真 UI 词组：首段无下划线 → 不豁免（drova 'Combat Music' 反例）
    assert not _is_object_identity_name("Combat Music", _ASSET_FID)
    assert not _is_object_identity_name("Hello World", _ASSET_FID)
    # 尾段是小写句词 → 不豁免（drova 'Quest_1 completed' 反例）
    assert not _is_object_identity_name("Quest_1 completed", _ASSET_FID)
    # 尾段无大写无下划线 → 不豁免
    assert not _is_object_identity_name("main_settings menu", _LEVEL_FID)
    # 无下划线的 '数字后缀'（真关卡名）→ 不豁免
    assert not _is_object_identity_name("Level 1", _SHARED_FID)
    # 单词式显示文本 → 不豁免
    assert not _is_object_identity_name("Settings", _ASSET_FID)
    assert not _is_object_identity_name("CONGRATULATIONS", _LEVEL_FID)
    # 空串 / 纯空白
    assert not _is_object_identity_name("", _ASSET_FID)
    assert not _is_object_identity_name("   ", _ASSET_FID)


def test_carrier_gate_dll_and_il2cpp_never_exempt():
    # DLL / il2cpp 来源没有 m_Name 跨度——同形态串也不豁免
    #（豁免只对 MonoBehaviour 常驻载体成立，防测量端误豁免代码串）
    assert not _is_object_identity_name("GNode(Global Attack)",
                                        "Assembly-CSharp.dll")
    assert not _is_object_identity_name("AI_SetCombatMusic",
                                        "Managed/Mono.Security.dll")
    assert not _is_object_identity_name("ui_newGame", "global-metadata.dat")
    assert not _is_object_identity_name("Pascal_Pascal", "text.csv")
    # 空 file_id
    assert not _is_object_identity_name("GNode(Global Attack)", "")
