"""写回逻辑层审计：写回前敏感形态检查 + 写回中扩容记录 + 写回后重开逻辑验证。

背景（用户报告 2026-08-11）：已写回游戏存在功能损坏——按键无响应、游戏
卡住、逻辑性问题大量存在。写回验证现状是「字节自证」：_verify_saved_bundle
比较的 expected 来自补丁后的数据本身，只能证明「序列化往返无损」，发现不了
补丁过程引入的结构破坏：
- rawstr 字节路径 _patch_serialized_string 对变长译文直接扩容插入，后续
  字段整体后移；若对象内字符串边界被破坏（长度头错乱），游戏加载该对象
  失败 → 按钮无反应/卡住。
- disposition=translate 的条目中存在逻辑敏感形态（camelCase/snake_case/
  短代码词等标识符，fieldtrigger 实证）——游戏按原名查找这些值，翻译必断链。

本模块补四块（系统性通用规则，不针对单游戏）：
1. 写回前：待写回条目的逻辑敏感形态审计——只报告不阻断（back/retry 等
   真实按钮文本大量命中短词形态，阻断会误伤）。
2. 写回中：rawstr 扩容记录 + 同原文互斥一致性 + 补丁后译文长度头自证。
3. 写回后：重开容器逻辑验证——改动对象字符串序列数量与内容一致（边界
   未被破坏）+ 对象可解析性健康。
4. 写回前（补丁循环内）：反向语义审计——对每个待翻译条目按「对象角色 +
   形态」判定逻辑键身份；确定性逻辑键自动回退译文（保留原文，不写补丁），
   疑似键报告进审计段。知识库 writeback_case「UnityEvent 绑定断裂」「显示
   文本当逻辑键」两案例在此实现为可执行规则。
"""
from __future__ import annotations

import re

from hanhua.core.unity import structural_fields
from collections.abc import Callable

# ── 逻辑敏感形态清单 ────────────────────────────────────────────────
# 命中这些形态的原文，即使 disposition=translate 也是「疑似逻辑字符串」。
# 顺序即优先级（先匹配先报告）。形态来源：fieldtrigger（纯小写代码词）、
# dEad（游戏 stylization 大小写）、MENU_PLAY（常量）、动画触发器
# camelCase、本地化键 snake_case 等实证形态。
LOGIC_SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 类型描述符：「Namespace.Type, Assembly」形态——第一部分必须含点号
    # （全限定名）。"Doctor, Doctor" 等对话文本无点号不命中（20 条
    # containment-breach 真实语料回测修正）。
    ("type_descriptor", re.compile(
        r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+,\s*[A-Za-z0-9_.]+$")),
    ("camel_case", re.compile(r"^[a-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*$")),
    ("snake_case", re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")),
    ("kebab_case", re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)+$")),
    ("uppercase_const", re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")),
    ("numeric_mix", re.compile(r"^[a-z]+[0-9][a-z0-9]*$")),
    ("single_char", re.compile(r"^[A-Za-z]$")),
    ("digit_leading", re.compile(r"^\d")),
    ("short_code_word", re.compile(r"^[a-z]{1,5}$")),
    # 纯小写长词（6+ 字符）——触发器/状态名/动画参数形态（fieldtrigger
    # 实证：12 字符纯小写代码词被放行翻译）。但 window/flowchart/kalkam
    # 等真实显示文本同形态（67 条真实语料回测）→ 仅 note 级供复核，
    # 不阻断。
    ("lowercode_word", re.compile(r"^[a-z]{6,}$")),
)

# 常见的「显示文本被代码当逻辑键」短词（按钮文本同时是分发键）——审计中
# 归为「需人工复核」类别，与纯显示短词区分。
LOGIC_KEYS_COMMON = frozenset({
    "ok", "yes", "no", "play", "quit", "retry", "resume", "back", "menu",
    "save", "load", "exit", "restart", "continue", "cancel", "skip", "next",
    "start", "stop", "pause", "close", "apply", "reset", "select", "delete",
})

# ── 知识库案例转规则（writeback_case 2026-08-11）────────────────────
# 案例「显示文本当逻辑键（按钮文字/物品名比较分发）」「替换 prefab/资源后
# UnityEvent 事件绑定断裂按钮无反应」→ 此处实现为可执行规则：

# UnityEvent 序列化字段路径信号：MonoBehaviour 内嵌 UnityEvent 持久化回调
# 的 m_MethodName/m_Target 等字段——方法名/目标名按反射绑定，翻译必断绑
# （按钮无反应）。字段路径信号来自 extractor 的 field 路径/字符串池内容。
UNITYEVENT_FIELD_SIGNALS = re.compile(
    r"m_(PersistentCalls|PersistentListener|MethodName|Target|TargetAssembly)"
    r"|persistentCalls|m_Listener")
# B15（snowday 按键失灵根因 2026-09-05）：输入绑定字段路径信号——
# InputActionAsset 的 m_ExpectedControlType（Button/Axis 控件类型）与
# m_Groups（XR/Joystick/Touch 控制方案组）是 Input System 按名解析的
# 机器标识，翻译即绑定解析失败 → 全部按键失灵。与 UnityEvent 同形态
# 的「字段名即结构证据」。
# M1（2026-09-05 0.39.0）单一源迁移：输入绑定字段路径 = 不可变字段清单中
# Input System 绑定相关字段的全部 casefold 变体（含无 m 前缀裸变体，
# 历史行为保留）。本体见 structural_fields.py。
INPUT_BINDING_FIELD_PATHS = frozenset(
    variant
    for leaf in structural_fields.INPUT_BINDING_FIELD_PATH_LEAVES
    for variant in ("m_" + leaf, "m" + leaf, leaf)
) - {"actionmap"}   # 历史成员不含裸 actionmap（防误拦普通字段名），保留
# UnityEvent 序列化字符串本体（方法名形态）：OnClick / OnValueChanged /
# DoSomething 等——但方法名也可能是普通单词，须与对象信号联合判定。
_UNITYEVENT_METHOD = re.compile(r"^[A-Z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*$")

# 「显示文本当逻辑键」风险词：按钮/菜单文本同时也是代码分发比较键
# （按钮文字/物品名比较分发）。按钮词本体要翻译（显示文本），风险在
# 「代码对象」里出现——代码对象（无 UI 证据的脚本/配置对象）中这些词
# 是常量比较键。由对象角色 + 词表联合判定。
LOGIC_COMPARE_WORDS = frozenset({
    "continue", "start", "options", "settings", "quit", "exit", "retry",
    "restart", "save", "load", "yes", "no", "ok", "cancel", "menu",
    "back", "next", "skip", "resume", "select", "confirm", "delete",
    "newgame", "new_game", "loadgame", "load_game", "mainmenu",
    "main_menu", "pause", "credits", "gameover", "game_over", "victory",
    "defeat", "play", "reset", "apply", "close", "begin",
})

def _config_class_of(script_class) -> bool:
    """script_class 是否为 class_registry 登记的配置类（写回侧兜底判定）。

    与识别层 disposition() 单一来源：config 类对象内的字符串是运行时
    按名查找的键（bank 名/动作名/资产名），翻译必断引用。旧库残留的
    已翻译条目据此整体回退。
    """
    if not script_class:
        return False
    from hanhua.core.unity.class_registry import disposition
    return disposition(str(script_class)) == "config"


def object_is_unityevent(obj_strings: list[str]) -> bool:
    """对象级 UnityEvent 判定：对象字符串池含事件绑定字段信号。

    知识库案例「替换 prefab/资源后 UnityEvent 事件绑定断裂按钮无反应」
    → 规则：事件绑定对象里的方法名/目标名（反射按名查找）翻译即断绑。
    返回 True 时对象内条目按逻辑键处置（自动回退）。信号常量与提取器
    共用（extractor._UNITYEVENT_SIGNALS），避免两套口径。
    """
    from hanhua.core.unity.extractor import _UNITYEVENT_SIGNALS
    return any(sig in s for s in obj_strings
               for sig in _UNITYEVENT_SIGNALS)


def logic_key_evidence(stripped: str, meta: dict,
                       obj_strings: list[str] | None = None) -> tuple[str, str] | None:
    """统一逻辑键判定（识别层与写回后反向审计共用同一规则）。

    返回 (verdict, reason)：
    - ("revert", reason)：确定性逻辑键——翻译必破坏功能，写回时自动
      回退译文（保留原文）。
    - ("report", reason)：疑似逻辑键——不能确定（可能是真实显示文本），
      写译文但报告进审计段供复核。

    meta 提供对象上下文（obj/field 路径/reason/role/obj_is_key_list），
    obj_strings 提供对象字符串池（跨条目上下文，如 UnityEvent 信号/值特征）。
    语义判定元数据单一权威键：reason（extractor 分类链写入的条目级处置
    原因）+ role（structural/display）+ obj_is_key_list（对象级键清单
    信号）——不再读已废弃的 structural_reason（extractor 在分类链
    842 行 pop 掉，读它必为空；旧值域也与分类链值域不一致）。
    """
    if not stripped:
        return None
    pattern = logic_pattern_of(stripped)
    obj_reason = meta.get("reason") or ""
    is_structural = meta.get("role") == "structural"
    # 确定性逻辑键（revert）：
    # 1. 类型描述符（Namespace.Type, Assembly）——save_typetree 依赖，
    #    翻译即 Referenced type not found。
    if pattern == "type_descriptor":
        return "revert", "type_descriptor"
    # 2. UnityEvent 事件绑定对象中的方法名/目标名——反射按名查找断绑。
    if (obj_reason or "").startswith("unityevent"):
        return "revert", "unityevent_binding"
    if obj_strings and object_is_unityevent(obj_strings):
        return "revert", "unityevent_binding"
    # 2b. 提取器写入的对象级事件绑定/输入轴信号（give-me-strength 实证
    #     2026-08-29）：meta 显式标记 obj_is_unityevent（UnityEvent 回调
    #     m_Target 类型引用 count≥2 证明对象含事件绑定）/obj_is_input_axis
    #     （InputManager/Cinemachine 轴配置）——旧库残留/识别层漏判时
    #     写回侧兜底回退，保留原文防断链（宁漏勿坏）。
    if meta.get("obj_is_unityevent"):
        return "revert", "unityevent_binding"
    if meta.get("obj_is_input_axis"):
        return "revert", "input_axis_binding"
    # 2c. FMOD Studio 事件路径（event:/Bank/Event）：RuntimeManager 运行时
    #     按路径字符串查找音频事件，翻译断路径 → 音效/音乐静默。识别层
    #     已在 engine_strings 确定性拦截；此处是写回侧兜底（旧库已翻译的
    #     event:/ 残留条目回退，防止坏译文写回）。
    if re.match(r"^event:/", stripped, re.I):
        return "revert", "fmod_event_path"
    # 2d. FMOD/输入/时间线等配置对象（识别层 class_registry 判定
    #     script_class_config 跳过）——写回侧兜底：旧库（修复前提取）
    #     残留的已翻译条目，写回时按 script_class 判定整体回退，保留
    #     原文（宁漏勿坏）。判定与 class_registry.disposition 单一
    #     来源（命名空间前缀覆盖 FMODUnity.*，专有词覆盖裸名——
    #     裸名 Settings/Platform 不命中，防误杀游戏自有同名类）。
    if _config_class_of(meta.get("script_class")):
        return "revert", "fmod_config_object"
    # 3. 代码对象（structural 跳过身份）里的逻辑比较词——代码按字符串
    #    比较分发（按钮文字/物品名比较）。structural 身份证明该对象是
    #    键清单（同对象内其他串被结构规则跳过）。obj_is_key_list 是提取
    #    器在结构跳过分支显式设置的对象级键清单信号；reason 是条目级
    #    分类——两者任一命中都说明该条目身处键环境，防识别层漏放
    #    （判成 display 但对象是键清单）的结构串在写回侧裸奔。
    low = stripped.lower()
    if ((obj_reason in ("input_binding", "code_line", "code_heavy_identifier",
                        "input_system_object", "localization_key_list")
         or meta.get("obj_is_key_list"))
            and (low in LOGIC_COMPARE_WORDS
                 or pattern in ("camel_case", "snake_case",
                                "uppercase_const", "lowercode_word"))):
        return "revert", f"logic_key_in_code_object:{pattern or low}"
    # 3b. 代码对象中放行的白名单显示词 + 对象内同值重复（≥2 处）——
    #     按钮对象 m_Name 与 m_text 同值（"Start"/"Settings"/"Exit"），
    #     rawstr 无字段身份，两处一起写回必改对象名 → 代码按名查找
    #     断裂（2026-08-15 多游戏实证：按键 UI 失灵、游戏卡住无法
    #     推进）。写回侧兜底（提取器漏判时仍安全）：全组保留原文
    #     （宁漏勿坏）。单次出现不 revert（静态按钮文本场景）。
    #     2026-08-26 冲突缺口修复：F44 让非白名单按钮词（西语 'Jugar'
    #     等 _WORD_CASE 单词式）可译，但此处只认 LOGIC_COMPARE_WORDS
    #     （英语词表）——非英语按钮词与对象名同值重复时兜底失效。
    #     把 _WORD_CASE 形态词并入（与提取器 shared_with_name 同源），
    #     非英语按钮词同样受对象名保护。
    from hanhua.core.unity.extractor import _WORD_CASE
    if (obj_reason == "code_heavy_display_word"
            and (low in LOGIC_COMPARE_WORDS or _WORD_CASE.match(stripped))
            and obj_strings is not None
            and obj_strings.count(stripped) >= 2):
        return "revert", "display_word_shared_with_object_name"
    # 疑似（report）：
    # 4. camel/snake/upper 形态本身是标识符形态（未知对象上下文时）。
    if pattern in ("camel_case", "snake_case", "uppercase_const",
                   "numeric_mix"):
        return "report", pattern
    # 5. 孤立短代码词（1-5 字符纯小写）且非常见按钮文本。
    if pattern == "short_code_word" and low not in LOGIC_KEYS_COMMON:
        return "report", "short_code_word"
    # 6. 逻辑比较词在非代码对象（按钮/菜单对象）——显示文本本体，但
    #    有被代码比较分发的风险（案例 5 的灰色地带）。
    if low in LOGIC_COMPARE_WORDS and not is_structural:
        return "report", "logic_compare_word"
    return None


def typetree_logic_key_evidence(
        meta: dict, original: str) -> tuple[str, str] | None:
    """typetree 分支的反向语义审计（W2）：按「字段路径 + 值形态」判定。

    rawstr 路径的 logic_key_evidence 在字节层无字段身份，只能靠对象
    字符串池信号；typetree 路径恰恰有精确的字段名——UnityEvent 绑定
    字段（m_MethodName/m_Target/m_PersistentCalls…）经 typetree 写入
    时反射按名绑定，翻译即断绑（按钮无反应）；这些字段不在不可变
    字段清单（m_MethodName 不在 _IMMUTABLE_FIELD_NAMES）。

    返回 (verdict, reason)：与 logic_key_evidence 同构。
    """
    from hanhua.core.unity.extractor import _UNITYEVENT_SIGNALS
    field_path = meta.get("field_path") or []
    path_names = [str(segment).casefold()
                  for segment in field_path if isinstance(segment, str)]
    # 字段路径信号：任一字段名命中 UnityEvent 绑定字段（含路径中间段——
    # m_PersistentCalls 下的 m_Calls 数组内嵌 m_MethodName，值在深层叶子）。
    if any(any(sig.casefold() in name
               for sig in _UNITYEVENT_SIGNALS)
           for name in path_names):
        return "revert", "unityevent_binding"
    # B15（snowday 按键失灵根因 2026-09-05）：输入绑定字段——与
    # UnityEvent 同形态的字段路径证据（字段名即结构证据，与值形态无关）。
    # m_ExpectedControlType（Button/Axis…）与 m_Groups（XR/Joystick/Touch…）
    # 是 Input System 按名解析的机器标识，翻译即绑定解析失败 → 按键失灵。
    # 提取端字段黑名单与 writer L2 不可变清单之外的最后兜底（老库存量
    # 条目 meta 无新字段标记时由此回退）。
    if any(name in INPUT_BINDING_FIELD_PATHS for name in path_names):
        return "revert", "input_binding_field"
    # 对象级事件绑定/输入轴信号（与 logic_key_evidence 2b 同规则）。
    if meta.get("obj_is_unityevent"):
        return "revert", "unityevent_binding"
    if meta.get("obj_is_input_axis"):
        return "revert", "input_axis_binding"
    # FMOD 事件路径（与 logic_key_evidence 2c 同规则）。
    if re.match(r"^event:/", original, re.I):
        return "revert", "fmod_event_path"
    # 配置类对象（与 logic_key_evidence 2d 同规则）。
    if _config_class_of(meta.get("script_class")):
        return "revert", "fmod_config_object"
    pattern = logic_pattern_of(original)
    # 类型描述符值（m_TargetAssemblyTypeName 等）——save_typetree 依赖，
    # 翻译即 Referenced type not found。
    if pattern == "type_descriptor":
        return "revert", "type_descriptor"
    # 键环境对象（obj_is_key_list / 代码类 reason）+ 代码形态 → 确定性回退，
    # 与 rawstr 路径 logic_key_evidence 条件 3 同一规则。
    obj_reason = str(meta.get("reason") or "")
    if (pattern in ("camel_case", "snake_case", "uppercase_const",
                    "numeric_mix", "lowercode_word")
            and (meta.get("obj_is_key_list")
                 or obj_reason in ("input_binding", "code_line",
                                   "code_heavy_identifier",
                                   "input_system_object",
                                   "localization_key_list"))):
        return "revert", f"logic_key_in_code_object:{pattern}"
    if pattern in ("camel_case", "snake_case", "uppercase_const",
                   "numeric_mix"):
        return "report", pattern
    return None


def audit_repeat_consistency(
        items: list[tuple[dict, dict]],
        on_revert: Callable[[dict, str], None] | None = None) -> list[dict]:
    """同原文互斥一致性（同一对象内同原文多处出现的处理一致性）。

    风险：Unity 对象内同原文多处出现（doog 实证 Splash ×6/SHELLS ×3），
    若一处翻译一处跳过（跳过方是结构判定=键身份）、或各处译文不一致
    （模型波动），写回后对象内「译文+原文」混排——代码按字典查原文时
    行为不一致。规则：组内任一 entry 是结构跳过（键身份）→ 全组视为
    键（翻译的改跳过）；译文不一致 → 全组跳过。返回一致性审计记录。

    on_revert：每个被回退（译文撤销回原文）的 entry 通知回调，签名
    (entry, reason)。写回方用它在逻辑审计记账（logic_reverted + 排除
    表）——回退是主动决策不是写失败，否则尾部兜底循环把回退条目误记
    rejected（闸门误阻），且原文不进运行时排除表（插件把保留原文再
    翻译 → 键断链）。
    """
    groups: dict[str, list[tuple[dict, dict]]] = {}
    for e, meta in items:
        original = str(e.get("original") or "")
        if not original:
            continue
        obj = meta.get("obj")
        groups.setdefault((obj, original), []).append((e, meta))
    records: list[dict] = []
    for (obj, original), group in groups.items():
        if len(group) < 2:
            continue
        translations = {
            str(e.get("translation") or "")
            for e, _ in group if e.get("translation") != original
        }
        structural_skip = any(
            meta.get("role") == "structural" for _, meta in group)
        pending_count = sum(
            1 for e, _ in group
            if str(e.get("translation") or "") != str(e.get("original") or ""))
        if structural_skip and pending_count:
            # 键身份优先：全组视为键，翻译条目改回跳过
            reason = ("同对象同原文存在结构跳过（键身份）——全组保留原文")
            for e, _ in group:
                if e.get("translation") != original:
                    e["translation"] = original
                    e["status"] = "skipped"
                    if on_revert:
                        on_revert(e, f"consistency_structural_skip:{reason}")
            records.append({
                "obj": obj, "original": original, "count": len(group),
                "action": "all_reverted",
                "reason": reason,
            })
        elif len(translations) > 1:
            # P5c（0.42.1 审计放宽——唯一放宽点）：译文不一致（模型波动）
            # 旧策略全组回退原文——宁漏勿坏过了头：同对象同原文的多处出现
            # 译文一致才是常态需求（按钮/标签多处显示同一词），全组回退
            # 让「6 处出现因 1 处波动全部不翻」。改为多数派统一：取出现
            # 次数最多的译文写满全组（并列取首现），只有零译文可选时才
            # 整组回退（此时维持旧 all_reverted 行为）。多数派仍受写回
            # 层既有安全检查约束（占位符/长度/分诊），此处只做统一不豁免。
            counts: dict[str, int] = {}
            for e, _ in group:
                t = str(e.get("translation") or "")
                if t and t != original:
                    counts[t] = counts.get(t, 0) + 1
            if counts:
                majority = max(counts.items(),
                               key=lambda kv: (kv[1], -list(counts).index(kv[0])))
                # 并列取首现：max 稳定取第一个达到最大计数者——Python max
                # 在 key 相等时返回先遍历到的元素，dict 保序即首现顺序
                reason = (f"同对象同原文译文不一致（模型波动）——"
                          f"多数派统一（{majority[1]}/{len(counts)} 种译文）")
                for e, _ in group:
                    if str(e.get("translation") or "") != majority[0]:
                        e["translation"] = majority[0]
                        if on_revert:
                            on_revert(e, "consistency_majority_unify:"
                                      + reason)
                records.append({
                    "obj": obj, "original": original, "count": len(group),
                    "translations": sorted(translations),
                    "action": "majority_unified",
                    "majority": majority[0],
                    "reason": reason,
                })
            else:
                # 零译文可选（全部与原文同值但 translations 集合非空——
                # 理论不可达，防御分支）→ 整组保留原文
                reason = "同对象同原文译文不一致（模型波动）——全组保留原文"
                for e, _ in group:
                    if e.get("translation") != original:
                        e["translation"] = original
                        e["status"] = "skipped"
                        if on_revert:
                            on_revert(e, "consistency_translation_variance:"
                                      + reason)
                records.append({
                    "obj": obj, "original": original, "count": len(group),
                    "translations": sorted(translations),
                    "action": "all_reverted",
                    "reason": reason,
                })
    return records


def verify_string_length_headers(raw: bytes,
                                 translations: dict[str, str]) -> list[str]:
    """补丁后译文长度头自证：每个译文在 raw 中出现处的长度头 == 译文字节数。

    知识库案例「固定容量池截短译文后字符串尾部带 NUL 导致逻辑判定失灵」
    的结构侧防线：变长译文扩容插入后，若长度头未同步（写成了旧长度），
    Unity 加载按长度头解析会读到错位数据。译文存在性 + 长度头正确 →
    字符串边界未被破坏。返回问题列表（空 = 通过）。

    注意：译文可能作为更长字符串的子串出现（doubleshake 实证：
    `<w=sassy>Quest Discovered!` 与 `Quest Discovered!` 是相邻独立字符串，
    译文 `任务被发现啦！` 在标签字符串内部也出现一次）——子串位置的
    前 4 字节是标签文本（"ssy>"）不是长度头。必须遍历**所有**出现位置，
    任一位置长度头匹配即通过；全部不匹配才判定边界破坏。
    """
    problems: list[str] = []
    for original, translation in translations.items():
        if not translation or translation == original:
            continue
        payload = translation.encode("utf-8")
        if payload not in raw:
            continue  # 存在性由 verify_logic_layer 负责
        head_ok = False
        too_early = False
        pos = raw.find(payload)
        while pos >= 0:
            length_offset = pos - 4
            if length_offset < 0:
                too_early = True
            elif int.from_bytes(raw[length_offset:pos], "little") == len(payload):
                head_ok = True
                break
            pos = raw.find(payload, pos + 1)
        if not head_ok:
            if too_early:
                problems.append(f"译文 {translation!r} 位置过前，无法核对长度头")
            else:
                problems.append(
                    f"译文 {translation!r}（原文 {original!r}）全部出现处长度头 "
                    f"≠ 实际字节 {len(payload)}（字符串边界被破坏）")
            if len(problems) >= 5:
                problems.append("…（仅列前 5 项）")
                break
    return problems


def logic_pattern_of(text: str) -> str | None:
    """命中逻辑敏感形态则返回形态名（首个命中），否则 None。"""
    stripped = text.strip()
    for name, pat in LOGIC_SENSITIVE_PATTERNS:
        if pat.fullmatch(stripped):
            return name
    return None


def audit_entries_before_writeback(entries: list[dict]) -> list[dict]:
    """写回前审计：对 disposition=translate 且译文≠原文的条目做形态检查。

    返回审计记录列表（每项：locator/original/translation/pattern/
    severity=warn/note/revert）。warn 只报告不阻断——按钮文本 back/retry
    大量命中短词形态，阻断会误伤真实显示文本；真正破坏性的是 camelCase
    等代码标识符形态（游戏按原名查找）。

    W4 联动跳过：severity 升级为 revert = 该条目在对象循环里会被
    logic_key_evidence/typetree_logic_key_evidence 确定性回退（译文→原文，
    保留原文防断链）。warn 形态 + 键环境对象（obj_is_key_list / 代码类
    reason，与判定函数同条件）时报告如实标注 revert——「warn 级审计不
    联动跳过」的缺口：报告与行为不一致（报告说风险但实际不跳）。
    报告与行为单一来源：此处仅标注，真实跳过仍由对象循环执行。"""
    audit: list[dict] = []
    for e in entries:
        original = str(e.get("original") or "")
        translation = str(e.get("translation") or "")
        if not original or not translation or translation == original:
            continue
        pattern = logic_pattern_of(original)
        if pattern is None:
            continue
        meta = {}
        raw_meta = e.get("meta")
        if isinstance(raw_meta, str) and raw_meta.strip():
            try:
                import json as _json
                meta = _json.loads(raw_meta)
            except (_json.JSONDecodeError, TypeError):
                meta = {}
        elif isinstance(raw_meta, dict):
            meta = raw_meta
        severity = "warn" if pattern in {
            "camel_case", "snake_case", "uppercase_const",
            "numeric_mix", "type_descriptor",
        } else "note"
        low = original.strip().lower()
        if pattern == "short_code_word" and low in LOGIC_KEYS_COMMON:
            severity = "note"  # 常见 UI 按钮文本，正常可译
        # W4：warn 形态 + 键环境（与 typetree_logic_key_evidence 条件 3
        # 同规则）→ 该条目写回时确定性回退，报告如实标注 revert。
        obj_reason = str(meta.get("reason") or "")
        if severity == "warn" and (
                meta.get("obj_is_key_list")
                or obj_reason in ("input_binding", "code_line",
                                  "code_heavy_identifier",
                                  "input_system_object",
                                  "localization_key_list")):
            severity = "revert"
        audit.append({
            "locator": str(e.get("key_path") or e.get("locator") or ""),
            "original": original,
            "translation": translation,
            "pattern": pattern,
            "severity": severity,
        })
    return audit


def snapshot_object_strings(raw: bytes) -> list[str]:
    """写回前快照：按 offset 顺序扫描对象的全部字符串内容序列。

    复用提取器扫描函数（与提取时同一套规则，避免两套扫描口径不一致）。
    返回按偏移排序的文本列表——重开验证时重新扫描比较数量与内容。"""
    from hanhua.core.unity.extractor import (
        _scan_unaligned_display_strings, scan_strings,
    )
    aligned = scan_strings(raw)
    unaligned = _scan_unaligned_display_strings(
        raw, {offset for offset, _ in aligned})
    combined = sorted(
        [(offset, text) for offset, text in aligned]
        + [(offset, text) for offset, text in unaligned],
        key=lambda item: item[0],
    )
    return [text for _, text in combined]


def audit_raw_expansion(entry: dict, meta: dict,
                        original: str, translation: str) -> dict | None:
    """rawstr 写回扩容记录：译文 UTF-8 字节数 > 原文 → 返回审计记录。

    扩容 = 插入字节 → 对象内后续数据整体后移。若对象内字符串边界完好
    （长度头正确），Unity 加载按长度头解析不受影响；若边界被破坏
    （长度头错乱），重开验证的字符串序列一致性会暴露。记录以报告。"""
    src_bytes = len(original.encode("utf-8"))
    dst_bytes = len(translation.encode("utf-8"))
    if dst_bytes <= src_bytes:
        return None
    return {
        "locator": str(entry.get("key_path") or ""),
        "obj": meta.get("obj"),
        "original": original,
        "translation": translation,
        "src_bytes": src_bytes,
        "dst_bytes": dst_bytes,
        "delta_bytes": dst_bytes - src_bytes,
    }


def _visible_in(raw: bytes, text: str, min_len: int = 3) -> bool:
    """探测 text 在 raw 中是否可被扫描规则识别（aligned + unaligned 双扫）。

    短译文（≤2 中文字符）字符数 < scan_strings 的 min_len=3 被过滤；
    无显示证据的译文不命中 unaligned 的 evidence 过滤——这些译文在
    重开扫描中「不可见」是预期行为，不是边界破坏。
    """
    from hanhua.core.unity.extractor import (
        _scan_unaligned_display_strings, scan_strings,
    )
    for _, t in scan_strings(raw, min_len=min_len):
        if t == text:
            return True
    occupied = {offset for offset, _ in scan_strings(raw, min_len=min_len)}
    for _, t in _scan_unaligned_display_strings(raw, occupied, min_len=min_len):
        if t == text:
            return True
    return False


def verify_logic_layer(actual_raw: bytes, expected_sequence: list[str],
                       translations: dict[str, str]) -> tuple[bool, list[str]]:
    """重开后逻辑层验证单个对象。

    expected_sequence：写回前快照的字符串内容序列（按偏移排序）。
    translations：原文 → 译文映射（写回条目的原文/译文——被翻译的字符串
    在重开扫描时内容变为译文，其余必须保持原文）。

    返回 (ok, 问题列表)。检查：
    1. 译文存在性：每条译文的 UTF-8 字节必须出现在写回后的 raw 中
       （翻译确实生效）。
    2. 译文长度头自证：出现处前 4 字节长度头 == 译文字节数（扩容插入
       后长度头未同步 → 边界破坏，知识库案例「NUL/长度头错乱」规则）。
    3. 序列对齐比较：未翻译串必须保持原文；已翻译串变为译文；「在扫描
       规则下不可见」的译文（短译文/无显示证据）豁免——它们扫不到是
       预期行为。数量多出（新字符串出现）或内容不符 → 边界被破坏。
    """
    problems: list[str] = []
    problems.extend(verify_string_length_headers(actual_raw, translations))
    if len(problems) >= 5:
        problems.append("…（仅列前 5 项）")
        return False, problems
    try:
        actual_sequence = snapshot_object_strings(actual_raw)
    except Exception as exc:  # noqa: BLE001
        return False, [f"重开扫描失败: {exc}"]
    if len(actual_sequence) > len(expected_sequence):
        return False, [
            f"字符串序列数量增多：写回前 {len(expected_sequence)} 个"
            f" → 重开后 {len(actual_sequence)} 个（边界可能被破坏）"]
    for original, translation in translations.items():
        if not translation or translation == original:
            continue
        if translation.encode("utf-8") not in actual_raw:
            problems.append(f"译文 {translation!r}（原文 {original!r}）"
                            f"未出现在写回后的对象数据中")
            if len(problems) >= 5:
                problems.append("…（仅列前 5 项）")
                return False, problems
    j = 0
    for idx, before in enumerate(expected_sequence):
        after = actual_sequence[j] if j < len(actual_sequence) else None
        if after is not None and before == after:
            j += 1
            continue
        expected_after = translations.get(before)
        if (expected_after is not None and after == expected_after):
            j += 1
            continue
        if expected_after is not None and not _visible_in(
                actual_raw, expected_after):
            continue  # 译文在扫描规则下不可见（短译文/无证据）——预期豁免
        if after is None:
            problems.append(f"第 {idx + 1} 个字符串缺失：写回前 {before!r}"
                            f" 在重开后扫描不到")
        else:
            problems.append(f"第 {idx + 1} 个字符串不符：写回前 {before!r}"
                            f" → 重开后 {after!r}")
        if len(problems) >= 5:
            problems.append("…（仅列前 5 项）")
            break
    return not problems, problems
