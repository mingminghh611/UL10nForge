"""v2 资源提取：UnityPy 解析 .assets / AssetBundle。
TextAsset 整文本 + MonoBehaviour 序列化原始字节字符串扫描（typetree 不可用时兜底）。"""
from __future__ import annotations

import dataclasses
import struct
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote

from hanhua.core.engine_strings import (CORE_MENU_SOURCE_TERMS,
                                         display_evidence_tier,
                                         has_display_text_evidence,
                                        is_code_action_binding,
                                        is_engine_string,
                                        is_engine_string_core,
                                        is_interaction_prompt,
                                        is_physical_binding_identifier)
from hanhua.core.extractor import ParsedFile, looks_like_noise_file
from hanhua.core.formats import json_format
from hanhua.core.models import STATUS_SKIPPED, TextEntry
from hanhua.core.placeholders import (DISPLAY_WORDS, is_credit_like,
                                      is_hard_structural, is_key_style_identifier,
                                      is_vn_command_line, should_skip,
                                      _HAS_LETTER, _LOG_TEMPLATE_TAIL,
                                      _QUALIFIED, _IDENTIFIER, _WORD_CASE)
from hanhua.core.scanner import (_has_unity_bundle_magic, _is_runtime_file,
                                 _walk_files)
from hanhua.core.unity.class_registry import disposition as _class_disposition
from hanhua.core.unity import structural_fields
from hanhua.core.tmp_tags import (is_pure_tags, is_tag_composed,
                                  referenced_names)
import re as _re

_METHOD_NAME = _re.compile(r"^(?:get|set)_[A-Za-z_][A-Za-z0-9_]*$")
# InputSystem action 路径（Section/Action，每段 1-2 个标识符词）：
# Player/Move、Menu/dPadHoriz、Debug/Warp 0、Forward/Back Tilt。
# 翻译后 InputSystem 按原名查找 action 失败 → 键盘/手柄按键全部无反应
# （真实语料：ivor 323 条、doubleshake 48 条被误标 display 放行）。
# 仅二进制 rawstr 路径使用（_structural_reason），文本文件行扫描不经此规则，
# 因此 "fridge open/close" 类句子不受影响。
_INPUT_ACTION_PATH = _re.compile(
    r"^[A-Za-z][A-Za-z0-9]*(?: [A-Za-z0-9]+)?/"
    r"[A-Za-z][A-Za-z0-9_]*(?: [A-Za-z0-9]+)?$")
_QUALIFIED_TYPE = _re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_+`]*\.)+[A-Z_][A-Za-z0-9_+`]*$")
# 类型引用：`Namespace.Type, Assembly`（如 Fungus.Flowchart, Fungus）。
# 程序集部分不设白名单——`A.B, C` 形态（点连标识符 + 逗号分隔）本身
# 就是 .NET 类型引用信号，显示文本几乎不出现（真实语料：
# level1 str 数组 Fungus.Flowchart, Fungus 曾被误判 natural_language，
# 译成「真菌.流程图」写回，破坏类型引用）。版本/公钥段可选。
_ASSEMBLY_REFERENCE = _re.compile(
    r"^(?:"
    r"[A-Za-z_][A-Za-z0-9_+`]*(?:\.[A-Za-z_][A-Za-z0-9_+`]*)+,\s*"
    r"[A-Za-z_][A-Za-z0-9_.-]*"
    r"|[A-Za-z_][A-Za-z0-9_+`]*,\s*Assembly-[A-Za-z0-9_.-]+"
    r")"
    r"(?:,\s*Version=[^,\s]+(?:,\s*Culture=[^,\s]+,\s*"
    r"PublicKeyToken=[^,\s]+)?)?$",
    _re.I,
)
_LIFECYCLE_METHODS = frozenset({
    "Awake", "Start", "Update", "FixedUpdate", "LateUpdate",
    "OnEnable", "OnDisable", "OnDestroy", "Reset",
})
# 代码驱动 UI 方法名（2026-08-15 minato 实证「no translation found
# for 音频」）：level0 obj 3311 是 [Minato(对象名), audio(子对象名),
# TMPro.TMP_Text(类型引用), SetText(方法名)]——audio 被白名单词规则
# 放行翻译成「音频」，写回后游戏按对象名查找失败。SetText 此前在
# 引擎串过滤中（不贡献 code 信号），导致 direct_code_signal_count
# 只计到 1（TMPro 类型引用），is_code_heavy 判定不足。这些方法名
# 说明该对象文本由代码运行时设置——对象内其余单词是名字/引用，
# 不是静态显示文本（静态按钮对象的 Save/Load 不含这些方法，仍按
# has_ui_evidence 正常放行，不误伤）。
_CODE_DRIVEN_METHODS = frozenset({
    "SetText", "SetActive", "SetActiveGameObject", "SendMessage",
    "SetTextMeshProText", "set_text",
})
_UNITY_CONTROL_STATE_NAMES = frozenset({
    "normal", "highlighted", "pressed", "selected", "disabled",
})
# UGUI Button 组件 m_AnimationTriggers 的视觉状态动画触发器字段（归一化
# 后：m_DisabledTrigger/mDisabledTrigger/disabledTrigger → disabledtrigger）：
# 值是动画状态名（Normal/Highlighted/Pressed/Selected/Disabled/Enabled），
# 反射/状态机按名引用，翻译必断按钮动画状态切换。与 _UNITY_CONTROL_STATE_
# NAMES 既有语义对齐（F38 排除），typetree 路径同样结构跳过——2026-08-31
# 用户实证：Button 对象的 m_DisabledTrigger="Disabled" 被显示证据链误放行，
# 本地模型译「残疾人士」→ 审核幻觉 PASS → AgentMemory active 跨游戏污染
# （同一触发器字段还有 m_EnabledTrigger）。
_TYPETREE_ANIMATION_TRIGGER_FIELDS = frozenset({
    "normaltrigger", "highlightedtrigger", "pressedtrigger",
    "selectedtrigger", "disabledtrigger", "enabledtrigger",
    # SuperTextMesh 文本动画状态字段（doubleshake 实证 2026-09-02：
    # drawAnimName/undrawAnimName='Appear'/'stamp' 是文字进出场动画名——
    # 运行时按名查找，翻译断 SuperTextMesh 动画 → 文本不显示/不消失）。
    # 字段名级登记为结构（与 UGUI 动画触发器同语义：动画状态引用键）。
    "drawanimname", "undrawanimname", "animname",
})
_INPUT_BINDING_NAMES = frozenset({
    "move", "wasd", "fire", "look", "dpad",
    "right click", "square button", "x button", "y button",
    "square/x/y button",
})
# Unity InputSystem 默认模板 action 名——仅当对象含 action map 名
# （InputSystem 对象）时降级为输入绑定，普通游戏里 SELECT 按钮文本不受影响
_INPUTSYSTEM_MAP_NAMES = frozenset({"gameactions"})
_INPUTSYSTEM_ACTION_NAMES = frozenset({
    "select", "cancel", "submit", "click", "point", "scroll",
    "navigate", "move", "look",
})
_TIMELINE_TRACK = _re.compile(
    r"^(?:Activation|Animation|Audio|Control|Group|Marker|Playable|Signal|Cinemachine) "
    r"Track(?:\s*\(\d+\))?$",
    _re.I,
)
# Input System 序列化绑定路径：<Keyboard>/z、<Mouse>/position、<Gamepad>/leftStick。
# 只出现在 InputActionAsset/InputActionMap 配置对象中，是「对象是输入配置」的强信号。
_INPUT_BINDING_PATH = _re.compile(
    r"^<[A-Za-z0-9_.]+>/(?:[A-Za-z0-9_./-]+)?$")
# Input System interactions 触发方式串（Press(behavior=2) 等）——同上，输入配置强信号。
_INPUTSYSTEM_INTERACTION = _re.compile(
    r"^(?:press|hold|tap|slowtap|multitap|doubletap|"
    r"pressandrelease|pressdelay|presspoint)\s*\(.*\)$",
    _re.I,
)
# 引擎配置对象程序集信号：MonoBehaviour 序列化含 m_AssemblyTypeName 程序集限定名，
# 若其中出现 Input System / Timeline 程序集，对象内的名字串都是引擎配置而非显示文本。
_INPUTSYSTEM_ASSEMBLY_SIGNALS = (
    "UnityEngine.InputSystem", "Unity.InputSystem")
_TIMELINE_ASSEMBLY_SIGNALS = (
    "UnityEngine.Timeline", "UnityEngine.Playables", "Unity.Timeline")

# 文本运行时镜像/中间字段（SuperTextMesh 实证 doubleshake 2026-09-02）：
# drawText/preParsedText/hyphenedText 是 _text 的处理后缓存（动画预解析/
# 连字符重排），与 _text 同值或仅形态差异，运行时重算——写回重复翻译 4
# 份同值无意义且改缓存字段有风险。字段名级登记为结构（非显示）跳过，
# 只让权威 _text（归一化 text）走白名单。
_TEXT_MIRROR_FIELDS = frozenset({
    "drawtext", "hyphenedtext", "preparsedtext", "parsedtext",
    "undrawname", "rawname",
})
# Timeline 对象常见轨道/标记 displayName（不带编号的裸词形式）
_TIMELINE_MARKER_NAMES = frozenset({"markers", "track"})

# Unity Localization 结构标记：出现这些串的对象是 Localization 表/共享数据对象
# （StringTable / SharedTableData）。其中标识符形态的字符串是表键（Key），绝不翻译。
_LOCALIZATION_MARKERS = ("UnityEngine.Localization", "Unity.Localization", "DistributedUIDGenerator")

# UnityEvent 事件绑定对象信号：MonoBehaviour 序列化内嵌 UnityEvent 持久化
# 回调字段（m_PersistentCalls/m_Target/m_MethodName）——方法名/目标名是
# 反射按名绑定键，翻译必断绑（知识库 writeback_case「替换 prefab/资源后
# UnityEvent 事件绑定断裂按钮无反应」转规则：点击回调链断裂 = 按键没反应）。
# 注意：这些**字段名**只存在于 typetree/字段路径，rawstr 字节层只序列化
# 值不序列化字段名（give-me-strength obj508 实证：UnityEvent 回调的值是
# PPtr+方法名+mode，m_Target/m_MethodName 字段名不进入 raw 字节）。
_UNITYEVENT_SIGNALS = frozenset({
    "m_PersistentCalls", "persistentCalls", "m_Listener",
    "m_Target", "m_MethodName", "m_Arguments",
})
# 程序集限定名「UnityEngine.Object, UnityEngine」/「UnityEngine.EventSystems.
# UnityEvent, UnityEngine.UI」是 UnityEvent 回调持久化里 m_Target 的类型
# 引用——**rawstr 值**里唯一能证明「对象内含事件绑定结构」的信号（字段名
# 不进 raw 字节，give-me-strength obj508 实证：Play 按钮回调链含此串
# 2 次 = 两个回调各一个 m_Target 类型引用）。对象字符串池含此信号
# （同值 count≥2 更能证明是多个 m_Target 而非普通文本）→ 对象内方法名/
# 目标名按反射绑定键处置。普通按钮文本对象（'Save'+类型引用单次，
# a-catfiends obj1319）不触发。
_UNITYEVENT_TARGET_TYPES = frozenset({
    "UnityEngine.Object, UnityEngine",
    "UnityEngine.EventSystems.UnityEvent, UnityEngine.UI",
    "UnityEngine.Events.UnityEvent, UnityEngine.CoreModule",
    "UnityEngine.EventSystems.EventTrigger, UnityEngine.UI",
})

# UnityEvent 持久化回调 / 自定义输入动作映射的**绑定元数据字段**（casefold 名，
# 不带 m_ 前缀——与 _normalized_field_name 输出对齐）：值是按名绑定键，绝不
# 可能是玩家显示文本——
#   m_TargetAssemblyTypeName：'GameMaster, Assembly-CSharp'（目标脚本程序集
#     限定类名，Dobraminhos 实证 642 条被 typetree_display_evidence 放行进
#     池）；m_ObjectArgumentAssemblyTypeName 同理（方法参数类型引用）。
#   m_ActionName/m_ActionEvents：'PlayerActionsXbox/Move'（输入动作映射路径，
#     Dobraminhos 实证 389 条）——'PlayerInUI/New action' 还是编辑器默认名。
# 字段名是确定性结构证据（值=反射按名查找的键，翻译断事件绑定 → 按钮无
# 反应/输入失灵，知识库案例转规则），值形态（'X, Y' 恰似短语）不足以推翻。
# typetree 路径逐字段可见，命中即整子树跳过（含 m_Calls 数组下的全部叶子）。
_EVENT_BINDING_FIELDS = frozenset({
    "persistentcalls", "listener", "callstate",
    "target", "methodname", "arguments",
    "targetassemblytypename", "objectargumentassemblytypename",
    "objecttypename",
    "actionevents", "actionname", "actionid",
    # Cinemachine 相机混合容器（hrana 实证 2026-09-02：obj m_Name='ZoomBlends'
    # 的 m_CustomBlends[].m_From/m_To 值 'Cinemachine vcam2 Main'/'**ANY
    # CAMERA**' 是相机名引用——CinemachineBrain 按名混合相机，翻译断镜头
    # 过渡）。容器字段名归一化命中即整枝跳过。用归一化名（customblends）
    # 而非 token 拆分——_TYPETREE_STRUCTURAL_FIELDS 是按拆 token 匹配，
    # 单合并 token 命中不了。
    "customblends",
    # 网络/连接配置字段（forgeverse 实证 2026-09-02）：offlineScene/
    # onlineScene（'Assets/Scenes/xxx.unity' 场景加载路径，已被路径形态
    # 拦）与 networkAddress（'localhost' 主机地址，纯文本形态漏网）——
    # 网络地址/主机名是运行时连接查找键，翻译断联机。字段名级登记。
    "networkaddress", "ipaddress", "hostname", "serveraddress",
    "offlinescene", "onlinescene",
    # 词表过滤字段（forgeverse 实证 2026-09-02）：Swears 脏话过滤词表
    # （'anal anus arse…' 聊天过滤用）——翻译破坏过滤词匹配（玩家可发
    # 脏话/或正常词被误杀）。开发词表非显示文本。
    "swears", "profanity", "bannedwords", "filterwords",
    "blacklist", "blockedwords",
    # FMOD Platform 平台继承键（give-me-strength 实证 2026-09-02）：
    # parentIdentifier='default'（平台继承查找键）——值恰是普通词形态
    # 漏网；identifier 值 32hex 已被 GUID 拦。字段名级登记。
    "parentidentifier",
    # TMP 字体资产元数据字段（give-me-strength 实证 2026-09-02）：m_FamilyName/
    # m_StyleName 值（'ITC Clearface Std'/'Regular'）是字体族/字重元数据
    # （asset 含 '1.1.0'+'SDF' 同池，typetree 路径下 raw tmp_asset 判定
    # 不适用）——<font> 按名引用，翻译断字体。真显示文本无此字段名。
    "familyname", "stylename", "sourcefontfileguid",
})

# InputManager 轴名（Unity 旧输入系统 Input.GetAxis 查找键）：Standalone
# InputModule 的 Horizontal/Vertical/Submit/Cancel、相机 Orbit 轴的
# Mouse X/Mouse Y、Fire1/2/3、Jump 等。轴名是运行时按名查找键，翻译必
# 断输入绑定（give-me-strength 实证：每 level 的 StandaloneInputModule
# 配置对象 Horizontal/Vertical/Submit/Cancel 全被 word_list_object 放行
# 翻译 → 键盘/手柄菜单导航失灵；CinemachineOrbitalTransposer 的
# Mouse X 被 single_visible_string 放行 → 相机轨道轴断裂）。与
# _INPUTSYSTEM_ACTION_NAMES（InputSystem action 名）区分：本集是
# 老 InputManager 轴名。
_INPUT_AXIS_NAMES = frozenset({
    "horizontal", "vertical", "submit", "cancel", "fire1", "fire2", "fire3",
    "jump", "mouse x", "mouse y", "mouse scrollwheel",
    "mouse scroll x", "mouse scroll y",
})
# 无歧义轴名（独立出现必是轴名，不可能是显示文本）：带「Mouse+轴后缀」
# 的相机/输入轴名。Cinemachine 相机轨道对象（give-me-strength obj513
# 实证：单串 'Mouse X' 孤立出现）由本规则拦截——'Submit'/'Cancel' 等
# 同时是常见按钮文本，不放本集（靠对象级 ≥2 轴名信号），防误杀。
_UNAMBIGUOUS_AXIS_NAMES = frozenset({
    "mouse x", "mouse y", "mouse scroll x", "mouse scroll y",
})
# InputManager 轴配置对象信号：串池含 ≥2 个不同轴名（StandaloneInputModule
# 有 Horizontal+Vertical+Submit+Cancel 四轴）→ 对象是输入轴配置，轴名是
# 查找键。单轴名对象（'Mouse X' 孤立）由 Cinemachine 类信号/轴名直接跳过
# 覆盖。
_INPUT_AXIS_OBJECT_MIN = 2

# Cinemachine 相机类前缀：CinemachineOrbitalTransposer/CinemachineFreeLook
# 等的轨道轴名（Mouse X/Mouse Y/Orbit X）是相机控制查找键，翻译断相机
# 轨道（give-me-strength obj513 实证）。
_CINEMACHINE_CLASS_PREFIX = "Cinemachine."

# 署名/credit 形态：作者名 + 作品平台 ID/URL（pixiv/twitter/artstation 等
# 平台名 + 数字 ID 或用户名，或括号包裹）。「林まか (pixiv: 10768714)」
# （doog 实证）是作者署名+作品引用——翻译/半翻损坏引用信息，识别层跳过。
# F49（ned-flanders 实证）：'x' 单独作 Twitter 代称匹配过宽——'Cam X
# Sensitivity'（相机 X 轴灵敏度）等 UI 文本被误杀。仅显式 'x (twitter)'
# 组合形态算署名，单独 'x' 不匹配（'x: @handle' 形态由 @ 后缀兜底）。
_SIGNATURE_CREDIT_RE = _re.compile(
    r"\(?(?:\b(?:pixiv|twitter|facebook|instagram|"
    r"artstation|deviantart|newgrounds|sketchfab|youtube|furaffinity|"
    r"booth|fantia)\b|x\s*\(twitter\))\s*[:：]?\s*@?[\w.-]{2,}",
    _re.I,
)

ASSET_SUFFIXES = {".assets", ".ab", ".unity3d", ".bundle", ".pak"}
_BUNDLE_SUFFIXES = frozenset(ASSET_SUFFIXES - {".assets"})
_LEVEL_SCENE = _re.compile(r"^level\d+$")
# 老式布局（Unity ≤4.x）：游戏根目录的 mainData 是无后缀序列化场景索引，
# 含全部场景文本（hotel-paradise 识别不全的根因，见 ISSUES #192）。
# levelN 仅在「同目录存在 mainData」（老式布局证据）时才收——根目录裸
# level1 可能是游戏自有数据文件，拒绝（见 rejects_level_scene_outside_data_tree）。
_LEGACY_SCENE = _re.compile(r"^mainData$")

_LOCALIZATION_TABLE_BUNDLE = _re.compile(
    r"^localization-string-tables-(?P<locale>.+?)_assets_all\.bundle$", _re.I)
_ENGLISH_LOCALE = _re.compile(r"(?:^|[^a-z])english\s*\(en\)(?:[^a-z]|$)", _re.I)


def _string_table_logical_identity(tree: dict, locale: str) -> str | None:
    """Return a locale-independent identity for one StringTable tree."""
    shared = tree.get("m_SharedData")
    if isinstance(shared, dict):
        file_id = shared.get("m_FileID")
        path_id = shared.get("m_PathID")
        if (isinstance(file_id, int) and not isinstance(file_id, bool)
                and isinstance(path_id, int) and not isinstance(path_id, bool)
                and path_id != 0):
            return f"shared:{file_id}:{path_id}"
    name = tree.get("m_Name")
    if not isinstance(name, str) or not name.strip():
        return None
    base = name.strip()
    locale_variants = {
        locale.casefold(), locale.casefold().replace("-", "_"),
        locale.casefold().replace("_", "-"),
    }
    language = locale.casefold().replace("_", "-").split("-", 1)[0]
    locale_variants.add(language)
    for suffix in sorted(locale_variants, key=len, reverse=True):
        for separator in ("_", "-", " "):
            marker = separator + suffix
            if base.casefold().endswith(marker):
                return "name:" + base[:-len(marker)].casefold()
    return "name:" + base.casefold()


def _localization_bundle_probe(
        path: Path) -> tuple[frozenset[str], str] | None:
    """Read stable StringTable identities and one locale without mutation."""
    if not path.is_file():
        return None
    from UnityPy import Environment
    env = Environment()
    env.path = str(path.parent)
    locales: set[str] = set()
    identities: set[str] = set()
    try:
        env.load([str(path)])
        for obj in env.objects:
            tname = getattr(getattr(obj, "type", None), "name", "")
            if tname not in ("MonoBehaviour", "ScriptableObject"):
                # 非脚本类型不触发失败缓存，直接读
                try:
                    tree = obj.read_typetree()
                except Exception:  # noqa: BLE001
                    continue
            else:
                # 脚本类型：先检查失败缓存，再做纯 Python 预检
                # （node 缺失时无法预检，退回直接读——探测语境无 generator）
                cls_sig = _mono_class_sig(obj)
                if cls_sig and cls_sig in _FAILED_CLASS_CACHE:
                    continue
                tree = None
                if _quick_typetree_check(obj):
                    try:
                        tree = obj.read_typetree()
                    except Exception:  # noqa: BLE001
                        continue
                else:
                    node = getattr(
                        getattr(obj, "serialized_type", None), "node", None)
                    if node is None:
                        # 无法预检（无 node 无 generator）：直接读，失败计入
                        # 缓存的仅限确实解析失败的类
                        try:
                            tree = obj.read_typetree()
                        except Exception:  # noqa: BLE001
                            continue
                    else:
                        # 预检失败：越界对象，boost 读必 5-7s 病态慢，跳过
                        if cls_sig:
                            _FAILED_CLASS_CACHE.add(cls_sig)
                        continue
                if tree is None:
                    continue
            if not isinstance(tree, dict) or not _is_string_table_tree(tree):
                continue
            locale = (tree.get("m_LocaleId") or {}).get("m_Code")
            if isinstance(locale, str) and locale.strip():
                locale = locale.strip()
                identity = _string_table_logical_identity(tree, locale)
                if identity:
                    locales.add(locale)
                    identities.add(identity)
    except Exception:  # noqa: BLE001
        return None
    finally:
        from hanhua.core.unity.writer import _dispose_environment
        _dispose_environment(env)
    if len(locales) != 1 or not identities:
        return None
    return frozenset(identities), next(iter(locales))


def _localization_bundle_locale(path: Path) -> str | None:
    """Read a unique StringTable locale without mutating the source bundle."""
    probe = _localization_bundle_probe(path)
    return probe[1] if probe else None


def _is_english_locale(locale: str) -> bool:
    return locale.casefold().replace("_", "-").split("-", 1)[0] == "en"


def _is_localization_bundle_probe_candidate(path: Path) -> bool:
    return bool(
        _LOCALIZATION_TABLE_BUNDLE.match(path.name)
        or path.suffix.casefold() in _BUNDLE_SUFFIXES
        or _has_unity_bundle_magic(path)
    )


def _prefer_source_locale_bundles(paths: list[Path]) -> list[Path]:
    """多语言 StringTable 并存时只选择英文源表，其余资源原样保留。"""
    probes = {
        path: _localization_bundle_probe(path)
        for path in paths
        if _is_localization_bundle_probe_candidate(path)
    }
    groups: dict[frozenset[str], list[tuple[Path, str]]] = {}
    for path, probe in probes.items():
        if probe is not None:
            identity, locale = probe
            groups.setdefault(identity, []).append((path, locale))
    excluded: set[Path] = set()
    for group in groups.values():
        if any(_is_english_locale(locale) for _, locale in group):
            excluded.update(
                path for path, locale in group
                if not _is_english_locale(locale))

    remaining = [path for path in paths if path not in excluded]
    localization = [
        path for path in remaining
        if _LOCALIZATION_TABLE_BUNDLE.match(path.name)]
    tree_locales = {
        path: (probes[path][1] if probes[path]
               else _localization_bundle_locale(path))
        for path in localization}
    known_tree_locales = [locale for locale in tree_locales.values() if locale]
    if known_tree_locales:
        english = [path for path, locale in tree_locales.items()
                   if locale and _is_english_locale(locale)]
    else:
        english = [p for p in localization if _ENGLISH_LOCALE.search(p.name)]
    if not english:
        # No verified English source: retain every locale rather than guessing.
        return remaining
    localization_set = set(localization)
    english_set = set(english)
    return [p for p in remaining
            if (p not in localization_set or p in english_set
                or (known_tree_locales and tree_locales.get(p) is None))]


def _asset_file_name(obj) -> str:
    asset_file = getattr(obj, "assets_file", None)
    return str(getattr(asset_file, "name", "") or "")


def _object_identity(obj) -> tuple[str, int]:
    """返回可跨 UnityPy 环境重建的 SerializedFile 名称 + Path ID。"""
    return _asset_file_name(obj), int(obj.path_id)


def _is_string_table_tree(tree: dict) -> bool:
    locale_node = tree.get("m_LocaleId")
    locale = locale_node.get("m_Code") if isinstance(locale_node, dict) else None
    rows = tree.get("m_TableData")
    if not isinstance(locale, str) or not isinstance(rows, list):
        return False
    return all(
        isinstance(row, dict) and row.get("m_Id") is not None
        and isinstance(row.get("m_Localized"), str)
        for row in rows
    )


def _localization_entries_from_tree(file_id: str, obj_path_id: int,
                                    tree: dict, asset_file_name: str = "") -> list[TextEntry]:
    """从 Unity Localization StringTable 类型树中只提取显示值。"""
    locale = (tree.get("m_LocaleId") or {}).get("m_Code")
    rows = tree.get("m_TableData")
    if not locale or not isinstance(rows, list):
        return []
    entries: list[TextEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        entry_id = row.get("m_Id")
        value = row.get("m_Localized")
        if entry_id is None or not isinstance(value, str) or not value.strip():
            continue
        if is_hard_structural(value):
            # I2 复数模板（{0:p:mine|mines}）等结构值：模型必失败回显且
            # 翻译会破坏 plural 语法（minato 真实样本）
            continue
        prefix = f"asset#{asset_file_name}#{obj_path_id}" if asset_file_name else f"asset#{obj_path_id}"
        meta = {
            "kind": "localization",
            "obj": obj_path_id,
            "entry_id": entry_id,
            "locale": locale,
            "table": tree.get("m_Name", ""),
            "confidence": "high",
            "role": "display",
            "disposition": "translate",
            "reason": "localization_table_value",
        }
        if asset_file_name:
            meta["asset_file"] = asset_file_name
        entries.append(TextEntry(
            file_id=file_id,
            key_path=f"{prefix}/loc/{entry_id}",
            original=value,
            meta=meta,
        ))
    return entries


# 显示字段白名单登记制（识别 L7）：每字段带出处分组——新增字段必须
# 登记来源（指南 §3.2「所有 SerializedFile 对象的字符串字段」或游戏
# 实证锚点），防止无依据滥加。表单通用名（text/label）是双刃剑：误
# 放行键名会淹没真实文本；出处让每次新增可审计（0.14.1 证据分层）。
@dataclasses.dataclass(frozen=True)
class _DisplayField:
    name: str       # casefold 字段名（m_ 前缀由 _normalized_field_name 剥离）
    group: str      # 出处分组：ui / dialogue / locale / misc


_TYPETREE_DISPLAY_FIELD_ROWS: tuple[_DisplayField, ...] = (
    # ui：常见 UI 标签/提示字段（指南 §3.2）
    _DisplayField("text", "ui"), _DisplayField("label", "ui"),
    _DisplayField("title", "ui"), _DisplayField("description", "ui"),
    _DisplayField("displayname", "ui"),
    _DisplayField("tooltip", "ui"), _DisplayField("hint", "ui"),
    _DisplayField("prompt", "ui"), _DisplayField("placeholder", "ui"),
    _DisplayField("heading", "ui"), _DisplayField("header", "ui"),
    _DisplayField("footer", "ui"),
    # dialogue：对话/字幕/选项字段（指南 §3.2；Fungus/对话系统实证）
    _DisplayField("dialogue", "dialogue"), _DisplayField("line", "dialogue"),
    _DisplayField("lines", "dialogue"), _DisplayField("subtitle", "dialogue"),
    _DisplayField("message", "dialogue"), _DisplayField("messages", "dialogue"),
    _DisplayField("content", "dialogue"), _DisplayField("caption", "dialogue"),
    _DisplayField("question", "dialogue"), _DisplayField("answer", "dialogue"),
    _DisplayField("choice", "dialogue"), _DisplayField("choices", "dialogue"),
    _DisplayField("dialoguetext", "dialogue"),
    _DisplayField("questiontext", "dialogue"),
    # 对话插件序列化字段（Fungus Say.storyText = 对话台词本体；
    # Fungus MenuDialog/DialogueSystem Menu Text = 菜单选项显示文本；
    # SetSayDialog 的台词文本——typetree 白名单路径此前漏收，
    # raw scan 靠句子形态兜底，确定性字段证据优先）
    _DisplayField("storytext", "dialogue"),
    _DisplayField("saytext", "dialogue"),
    _DisplayField("menutext", "dialogue"),
    _DisplayField("optiontext", "dialogue"),
    _DisplayField("buttontext", "dialogue"),
    # locale：本地化表字段（指南 §3.2；Localization 表实证）
    _DisplayField("singular", "locale"), _DisplayField("plural", "locale"),
    _DisplayField("format", "locale"), _DisplayField("template", "locale"),
    _DisplayField("prefix", "locale"), _DisplayField("suffix", "locale"),
    # misc：叙事/提示杂项（指南 §3.2）
    _DisplayField("objective", "misc"), _DisplayField("lore", "misc"),
    _DisplayField("bio", "misc"), _DisplayField("error", "misc"),
    _DisplayField("body", "misc"), _DisplayField("details", "misc"),
    _DisplayField("summary", "misc"), _DisplayField("greeting", "misc"),
    _DisplayField("farewell", "misc"), _DisplayField("notice", "misc"),
    _DisplayField("warning", "misc"), _DisplayField("help", "misc"),
)
# 派生 frozenset 保持既有接口（大小写归一后成员判定）。"name" 有意
# 排除——m_Name 是每个对象的标识名（inspector 标签/查找键），翻译会
# 淹没真实文本。
_TYPETREE_DISPLAY_FIELDS = frozenset(
    f.name for f in _TYPETREE_DISPLAY_FIELD_ROWS)
_TYPETREE_STRUCTURAL_FIELDS = frozenset(
    {"key", "keys", "id", "method", "binding", "path", "property", "code"})
# Unity 惯例不可变字段（M1 单一源：structural_fields.IMMUTABLE_FIELD_NAMES_FOLDED，casefold 化以
# 拦截 m_name/M_Name 等变体；裸 name 字段不受影响）。写回闸门同样拦截，
# 扫描端先拦避免 UI 展示不可写条目（review 实证）。
# TextAsset 数据文件判定：行内 ≥3 字母单词（fp_level_* 数据行实证）
_WORD_TOKEN = _re.compile(r"[A-Za-z]{3,}")
_TYPETREE_IMMUTABLE_FIELD_NAMES = (
    structural_fields.IMMUTABLE_FIELD_NAMES_FOLDED)

# 每对象候选条目上限：VisualTreeAsset 等深层结构可能含数千叶子，
# 防止「低置信证据层」膨胀数据库（识别 ≠ 全入库）。
_MAX_CANDIDATES_PER_OBJECT = 200


# 同对象平行语言后缀字段（fake-it 实证 2026-09-07，B21）：本地化对象
# 同层携带多语言字段——ContentEn/ContentFr/ContentDe、TitleEn/TitleFr、
# m_Text_EN/m_Text_FR 等。游戏只按当前语言读其一（默认英文）：
# - En（或无后缀权威字段）：进池翻译；
# - 其他语言后缀（Fr/De/It/Es/Ja/Ko/Zh/Ru/Pt/Cn…）：翻译无人读取，
#   浪费翻译量，且非英语源进审核后 4B 频繁输出「原文为法语，译文误译
#   成中文」类幻觉阻断（fake-it 120+ 条 ContentFr 被阻断实证）——
#   提取层整字段跳过（skipped 留档，reason=parallel_lang_field）。
_PARALLEL_LANG_SUFFIXES = frozenset({
    "fr", "de", "it", "es", "ja", "jp", "ko", "zh", "cn", "ru", "pt",
    "pl", "nl", "sv", "da", "no", "fi", "cs", "tr", "ar", "hi", "th",
    "id", "uk", "hu", "ro", "el", "bg", "he", "vi", "en",
})
# 字段名 → (基名, 语言后缀) 拆分：ContentEn → ("content", "en")；
# m_Text_FR → ("text", "fr")；不含后缀返回 None。三种形态都认：
# 尾部 PascalCase 后缀（ContentEn）、下划线（Content_EN/Content_Eng/
# m_Text_FR）、m_/_ 前缀组合。
_FIELD_LANG_RE = _re.compile(
    r"^(m_|_)?(?P<base>[A-Za-z]+?)[ _]*_(?P<lang>[A-Za-z]{2,3})$",
    _re.I)
# PascalCase 尾缀：基名任意大小写开头 + 2~3 字母大写开头后缀，
# ContentEn/ContentFr/ContentEng/ContentJPN/TitleFr。普通 Pascal 词
# （LevelName/AudioSource）不会命中——尾部大写起始词段要么超过 3
# 字符，要么拆出的「语言码」不在白名单/歧义黑名单。
# 歧义黑名单：id/no 在语言白名单里（印尼/挪威语）但作为字段后缀
# 几乎总是 Id=标识/No=编号，宁可漏判。
_FIELD_LANG_CAMEL_RE = _re.compile(
    r"^(m_|_)?(?P<base>[A-Za-z][A-Za-z0-9]*?)(?P<lang>[A-Z][A-Za-z]{1,2})$")
_FIELD_LANG_AMBIGUOUS = frozenset({"id", "no"})


def _parallel_lang_field_info(key: str) -> tuple[str, str] | None:
    """字段名是「基名+语言码」形态时返回 (归一化基名, 小写语言码)。

    只认**基名是已知显示字段/常见显示词**的后缀拆分——防止 Source/
    Level 之类普通字段被误判（SourceFr 的 fr 拆出来 'sourcefr' 基名
    不在 known_bases，返回 None）。语言码白名单（_PARALLEL_LANG_
    SUFFIXES）外的后缀不判；3 字母码（ENG/JPN/CHN/FRA/GER/ESP/RUS/
    ITA/PTB）归一到前 2 位再查白名单。
    """
    name = str(key)
    lang = None
    base_raw = None
    m = _FIELD_LANG_RE.match(name)
    if m:
        base_raw, lang = m.group("base"), m.group("lang")
    else:
        # PascalCase 尾缀（ContentEn/TitleFr）：后缀必须全大写字母组
        # （En/Fr/De），且拆出后基名须非空
        m2 = _FIELD_LANG_CAMEL_RE.match(name)
        if m2:
            base_raw, lang = m2.group("base"), m2.group("lang")
    if lang is None or base_raw is None:
        return None
    lang = lang.casefold()
    if lang in _FIELD_LANG_AMBIGUOUS:
        return None
    if len(lang) == 3:
        long_map = {"eng": "en", "jpn": "ja", "kor": "ko", "chn": "zh",
                    "fra": "fr", "ger": "de", "esp": "es", "rus": "ru",
                    "ita": "it", "ptb": "pt"}
        lang = long_map.get(lang)
        if lang is None:
            return None
    if lang not in _PARALLEL_LANG_SUFFIXES:
        return None
    base = _normalized_field_name(base_raw)
    known_bases = _TYPETREE_DISPLAY_FIELDS | {
        "text", "label", "title", "content", "description", "message",
        "name", "string", "value", "dialog", "body", "caption",
        "subtitle", "question", "answer", "choice", "prompt", "hint",
        "tooltip", "header", "footer", "story", "line"}
    if base not in known_bases:
        return None
    return base, lang


def _normalized_field_name(value: object) -> str:
    name = str(value)
    low = name.casefold()
    if low.startswith("m_"):
        return low[2:]
    # NGUI/旧序列化 camelCase m 前缀（mText/mCaption/mLabel，NGUI UILabel
    # 私有序列化字段实证）：mText 归一化后是 "mtext" 不在白名单——strip
    # 前导 m（仅当原字段名为 m+大写字母形态，method/matrix 等普通词不受
    # 影响）。NGUI 游戏整类字段因此漏提取，此为通用修复。
    if len(name) > 1 and name[0] == "m" and name[1].isupper():
        return low[1:]
    # 序列化私有字段下划线前缀（_text/_displayedText/_name，SuperTextMesh
    # 等自定义组件私有字段实证 2026-09-02）：_text 语义 = text 显示字段。
    # 只剥**单条前导下划线**（__text 双下划线是命名重整/特殊标记不剥），
    # 归一到既有白名单（text/name/label…）判定。
    if low.startswith("_") and not low.startswith("__"):
        return low[1:]
    return low


def _field_name_tokens(value: object) -> frozenset[str]:
    name = str(value)
    if name[:2].casefold() == "m_":
        name = name[2:]
    separated = _re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return frozenset(
        token for token in _re.split(r"[^A-Za-z0-9]+", separated.casefold())
        if token)


def _encode_field_path(field_path: list[str | int]) -> str:
    """Encode path segments reversibly while retaining key/index types."""
    return "/".join(
        f"i:{segment}" if isinstance(segment, int)
        else f"k:{quote(segment, safe='')}"
        for segment in field_path)


def _decode_field_path(locator: str) -> list[str | int]:
    decoded: list[str | int] = []
    for segment in locator.split("/") if locator else []:
        if segment.startswith("i:"):
            decoded.append(int(segment[2:]))
        elif segment.startswith("k:"):
            decoded.append(unquote(segment[2:]))
        else:
            raise ValueError(f"invalid field path segment: {segment}")
    return decoded


_TYPE_DESCRIPTOR = _re.compile(
    r"^[A-Za-z_]\w* [A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+ [A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")


def _looks_like_type_descriptor(text: str) -> bool:
    """Unity Localization/序列化的类型描述字符串：``TypeName Namespace Assembly``。

    resonance-of-the-ocean 实测：SmartFormat 配置对象里
    "Parser UnityEngine.Localization.SmartFormat.Core.Parsing Unity.Localization"
    是类型引用（游戏按名反射加载），当文本翻译后 save_typetree 直接抛
    ValueError（Referenced type not found）。形态：恰好 3 段——段 1 标识符
    （类名）、段 2 点分命名空间、段 3 点分程序集。真实游戏文本（"Open the
    File. Read docs." 等）因段间标点/段内含点位置不符而被排除，误伤极低。
    """
    return bool(_TYPE_DESCRIPTOR.match(text.strip()))


def _typetree_string_entries(
        file_id: str, obj_path_id: int, tree: dict,
        asset_file_name: str = "",
        skipped: dict[str, int] | None = None,
        script_class: str = ""
) -> tuple[list[TextEntry], list[TextEntry]]:
    """全叶子字符串分类：返回 (display 条目, 低置信候选条目)。

    display 层（可翻译）：
    - 白名单字段名（text/label/title/…）→ high；
    - 句子形态 / 显示证据（含对象级值特征）→ medium。
    候选层（不可翻译，仅作证据留档）：
    - 其余非键风格字符串 → status=skipped, role="candidate", confidence=low。
      写回与质量门禁（is_actionable_translation 要求 role=display 且
      confidence≠low）天然排除——「过滤不是删除」（指南 §2.4）。
    键风格标识符（should_skip）不产生条目——它们在各处已是键。

    script_class：对象脚本类名（m_Script PPtr 解析）透传入 meta——写回端
    logic_audit._config_class_of 兜底依赖它（B15：rawstr 路径一直写入，
    typetree 路径此前缺失，配置类对象写回无兜底）。
    """
    display: list[TextEntry] = []
    candidates: list[TextEntry] = []
    prefix = (f"asset#{asset_file_name}#{obj_path_id}"
              if asset_file_name else f"asset#{obj_path_id}")
    leaves: list[tuple[list[str | int], str, str, bool, bool]] = []

    def visit(value, path: list[str | int], structural: bool = False) -> None:
        if isinstance(value, dict):
            # B21 平行语言后缀字段：先摸清本层「字段 → 语言码」分布与
            # 无后缀权威字段。En（或无后缀同基名字段）在场时，其他语言
            # 后缀列（Fr/De/Ja…）整列跳过——游戏默认读英文列，翻其他列
            # 无人读取且非英语源进审核会被 4B 指「原文为法语」误阻断
            # （fake-it 120+ 条 ContentFr 实证）；En 不在场时不动（游戏
            # 可能本身就是该语言）。
            lang_of: dict[str, str] = {}
            base_has_en: set[str] = set()
            for key in value:
                info = _parallel_lang_field_info(key)
                if info:
                    lang_of[key] = info[1]
                    if info[1] == "en":
                        base_has_en.add(info[0])
            plain_bases = {_normalized_field_name(k) for k in value}
            for key, child in value.items():
                normalized = _normalized_field_name(key)
                # 事件绑定元数据字段（UnityEvent 回调 m_Calls 下的
                # m_TargetAssemblyTypeName/m_ActionEvents…，见文件头
                # _EVENT_BINDING_FIELDS 注释）：值是反射按名绑定键/类型
                # 引用/输入动作路径，绝不显示。结构证据直达字段名——
                # 命中字段名即该子树整枝跳过（不依赖值形态猜测）。
                event_branch = structural or bool(
                    normalized in _EVENT_BINDING_FIELDS)
                # 文本运行时镜像字段（drawText/preParsedText 等缓存，见文件头
                # _TEXT_MIRROR_FIELDS）——只让权威 _text 进白名单，镜像跳过防
                # 同值多份重复翻译写回。
                mirror_branch = bool(normalized in _TEXT_MIRROR_FIELDS)
                # m_Name 等 Unity 惯例对象标识字段（Inspector 标题/Find 查找
                # 键/引用/地址）：翻译破坏对象查找与回写（immutable_field_
                # protected 会拦截）——即使对象含值证据也不得升格 display
                # （doubleshake 实证）。casefold 拦截 m_name/M_Name 变体；
                # 裸 name 字段（对话角色名等）不受影响。
                parallel_info = _parallel_lang_field_info(key)
                parallel_skip = bool(
                    parallel_info and parallel_info[1] != "en"
                    and (parallel_info[0] in base_has_en
                         or parallel_info[0] in plain_bases))
                blocked = structural or event_branch or mirror_branch or bool(
                    _field_name_tokens(key) & _TYPETREE_STRUCTURAL_FIELDS) \
                    or key.casefold() in _TYPETREE_IMMUTABLE_FIELD_NAMES \
                    or _normalized_field_name(key) \
                    in _TYPETREE_ANIMATION_TRIGGER_FIELDS
                child_path = [*path, key]
                if isinstance(child, str) and child.strip():
                    leaves.append((child_path, child, normalized, blocked,
                                   parallel_skip))
                else:
                    visit(child, child_path,
                          blocked or parallel_skip)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, [*path, index], structural)

    visit(tree, [])

    # 对象级值特征：任一叶子是显示证据（句子/白名单字段/显示证据形态）
    # → 其余非键字符串也升 display（与 raw scan 的 obj_has_values 一致）
    has_value_evidence = any(
        not blocked and not parallel_skip and (
            normalized in _TYPETREE_DISPLAY_FIELDS
            or _has_sentence_shape(text.strip())
            or has_display_text_evidence(text)
        )
        for _, text, normalized, blocked, parallel_skip in leaves)

    def append(kind: str, path: list[str | int], text: str, reason: str,
               confidence: str, status: str, role: str,
               extra_meta: dict | None = None) -> None:
        meta = {
            "kind": kind, "obj": obj_path_id, "field_path": path,
            "confidence": confidence, "role": role,
            "disposition": "translate" if role == "display" else "structural",
            "reason": reason,
            "obj_has_values": has_value_evidence,
        }
        if script_class:
            # B15：写回端 logic_audit._config_class_of 兜底的证据源
            meta["script_class"] = script_class
        if extra_meta:
            meta.update(extra_meta)
        if asset_file_name:
            meta["asset_file"] = asset_file_name
        target = display if role == "display" else candidates
        target.append(TextEntry(
            file_id=file_id,
            key_path=f"{prefix}/field/{_encode_field_path(path)}",
            original=text, status=status, meta=meta))

    # R5 留档：键/标识符/结构值/类型引用不再静默 continue（限量样本 +
    # skipped_count 承载真实总数），typetree 候选层同理。
    prefilter_counts: dict[str, int] = {}
    for path, text, normalized, blocked, parallel_skip in leaves:
        if blocked:
            continue
        if parallel_skip:
            # B21：非英文平行语言列跳过——skipped 留档（非静默），
            # 与 prefilter 同款「样本 + skipped_count」语义。
            key = "parallel_lang_field"
            count = prefilter_counts[key] = \
                prefilter_counts.get(key, 0) + 1
            if count <= _PREFILTER_SAMPLE_LIMIT:
                append("typetree_prefilter", path, text,
                       "parallel_lang_field", "low", STATUS_SKIPPED,
                       "candidate",
                       {"prefilter": "parallel_lang_field",
                        "skipped_count": count})
            continue
        stripped = text.strip()
        if normalized in _TYPETREE_DISPLAY_FIELDS:
            append("typetree", path, text, "typetree_display_field",
                   "high", "pending", "display")
        elif is_tag_composed(text):
            # TMP 标签组合串：正文是可译内容（typetree 非白名单字段也
            # 放行，如 <color=red>Warning!</color> 在描述字段里）
            extra = {"tmp_tag_refs": sorted(referenced_names(text))} \
                if referenced_names(text) else None
            append("typetree", path, text, "tmp_tag_composed",
                   "medium", "pending", "display", extra)
        elif (should_skip(text) or is_hard_structural(text)
              or _looks_like_type_descriptor(text)):
            prefilter = ("key_identifier" if should_skip(text)
                         else "hard_structural" if is_hard_structural(text)
                         else "type_descriptor")
            # 计数键带 prefilter_ 前缀 = 样本 meta 的 reason（回写同形）
            key = f"prefilter_{prefilter}"
            count = prefilter_counts[key] = \
                prefilter_counts.get(key, 0) + 1
            if count <= _PREFILTER_SAMPLE_LIMIT:
                append("typetree_prefilter", path, text,
                       f"prefilter_{prefilter}", "low", STATUS_SKIPPED,
                       "candidate", {"prefilter": prefilter,
                                     "skipped_count": count})
        elif (_has_sentence_shape(stripped) or has_display_text_evidence(text)
              or has_value_evidence):
            append("typetree", path, text, "typetree_display_evidence",
                   "medium", "pending", "display")
        elif len(candidates) < _MAX_CANDIDATES_PER_OBJECT:
            append("typetree_candidate", path, text, "typetree_candidate",
                   "low", STATUS_SKIPPED, "candidate")
        elif skipped is not None:
            # 识别 C5：候选层 200 上限不再静默截断——超限叶子无条目也无
            # 聚合计数时，整类截断在报告里不可见（与 R5「样本+skipped_
            # count」语义不一致）。按 reason 聚合计数留档，报告可见
            # 「该对象候选超限 N」。
            skipped["typetree_candidate_truncated"] = \
                skipped.get("typetree_candidate_truncated", 0) + 1
    _finalize_skipped_counts(display, prefilter_counts)
    _finalize_skipped_counts(candidates, prefilter_counts)
    return display, candidates


# ── I2 Localization 语言源提取 ─────────────────────────────────────────────
# I2 Localization 是使用率最高的 Unity 本地化插件之一（官方手册：Terms
# 存于 LanguageSource，序列化结构 = LanguageSourceAsset 的 mSource/mData
# → mTerms[].Term（键）+ mTerms[].Languages[]（各语言译文，源语言首个
# 非空）+ mTerms[].TermType（0=Text，其余为字体/纹理/音频等资产引用）。
# 语言源 = 整游戏的文本全集，确定性提取（写回走 typetree 字段路径补丁）。
_I2_SOURCE_FIELD_NAMES = ("mSource", "mData", "m_Source", "m_Data")


def _i2_source_data(tree: dict) -> dict | None:
    """取 I2 LanguageSourceData 容器（mSource/mData 嵌套或平铺）。"""
    for key in _I2_SOURCE_FIELD_NAMES:
        data = tree.get(key)
        if isinstance(data, dict) and isinstance(data.get("mTerms"), list):
            return data
    if isinstance(tree.get("mTerms"), list):
        return tree
    return None


def _is_i2_language_source_tree(tree: dict) -> bool:
    data = _i2_source_data(tree)
    if data is None:
        return False
    terms = data.get("mTerms") or []
    return bool(terms) and all(
        isinstance(t, dict) and t.get("Term") is not None
        and isinstance(t.get("Languages"), list)
        for t in terms[:5])


def _i2_english_language_index(data: dict) -> int | None:
    """I2 语言源的英文索引（用户指令：多语言游戏语言优先翻译英文）。

    mLanguages[].Name 含 English/en 即英文；找不到返回 None（调用方
    回退首个非空语言值）。
    """
    langs = data.get("mLanguages")
    if not isinstance(langs, list):
        return None
    for idx, lang in enumerate(langs):
        if not isinstance(lang, dict):
            continue
        name = str(lang.get("Name", "") or "").casefold()
        if "english" in name or name == "en":
            return idx
    return None


def _i2_localization_entries_from_tree(
        file_id: str, obj_path_id: int, tree: dict,
        asset_file_name: str = "") -> list[TextEntry]:
    """I2 语言源条目：每 Term 的源语言值 = 游戏显示文本。

    键（Term）是查找键绝不翻译；TermType≠0 的术语是资产引用
    （字体/纹理/音频路径名）跳过。值走标准 typetree 字段路径，
    写回端 _set_typetree_value_at_path 直接可用。
    """
    data = _i2_source_data(tree)
    if data is None:
        return []
    source_key = next(
        (key for key in _I2_SOURCE_FIELD_NAMES
         if isinstance(tree.get(key), dict) and tree.get(key) is data),
        None)
    english_index = _i2_english_language_index(data)
    terms = data.get("mTerms") or []
    entries: list[TextEntry] = []
    prefix = (f"asset#{asset_file_name}#{obj_path_id}"
              if asset_file_name else f"asset#{obj_path_id}")
    for i, term in enumerate(terms):
        if not isinstance(term, dict):
            continue
        key = term.get("Term")
        languages = term.get("Languages")
        term_type = term.get("TermType")
        # TermType 0=Text；其余（Font/Texture/AudioClip/GameObject/
        # Sprite/Video/Object）是资产引用路径，翻译断引用
        if term_type is not None and int(term_type) != 0:
            continue
        if key is None or not isinstance(languages, list):
            continue
        j = None
        if english_index is not None and english_index < len(languages):
            value = languages[english_index]
            if isinstance(value, str) and value.strip():
                j = english_index
        if j is None:
            j = next((idx for idx, v in enumerate(languages)
                      if isinstance(v, str) and v.strip()), None)
        if j is None:
            continue
        value = languages[j]
        if is_hard_structural(value):
            # I2 复数模板等结构值（同 Localization 表处理）
            continue
        field_path = ([source_key, "mTerms", i, "Languages", j]
                      if source_key else ["mTerms", i, "Languages", j])
        meta = {
            "kind": "typetree",
            "obj": obj_path_id,
            "field_path": field_path,
            "confidence": "high",
            "role": "display",
            "disposition": "translate",
            "reason": "i2_language_source",
            "i2_term": key,
            "i2_lang_index": j,
        }
        if asset_file_name:
            meta["asset_file"] = asset_file_name
        entries.append(TextEntry(
            file_id=file_id,
            key_path=f"{prefix}/field/{_encode_field_path(field_path)}",
            original=value,
            meta=meta,
        ))
    return entries


def find_asset_files(
        game_dir: str | Path, *, data_dir: str | Path | None = None,
        exclude_roots: Iterable[str | Path] = ()) -> list[Path]:
    """发现 Unity 二进制资源，应用与文本扫描一致的运行时排除。

    识别依据是内容探测（UnityFS 魔数 / SerializedFile 头部自洽 / WebFile 魔数），
    不是扩展名：
    - 任意后缀/无后缀文件只要命中即收（无后缀 level 场景、.dat 伪装资源、
      .bytes 数据文件此前整类漏检，指南 §3.3）；
    - 注意 Addressables catalog.bin 不是 SerializedFile（BinaryStorageBuffer，
      kMagic 0x0DE38942），头部大端自洽检查会拒绝它，由 Addressables 管线
      的字节级 CRC 替换处理（catalog 无文本，无需解析）；
    - .bytes/.dat/.bin 伪装文件只在探测确认 Unity 容器时收（纯文本 .bytes
      由文本扫描负责，避免 UnityPy 解析失败）。
    """
    from hanhua.core.scanner import (_BINARY_SUFFIXES, probe_file_kind)
    game_dir = Path(game_dir)
    explicit_data_dir = Path(data_dir) if data_dir is not None else None
    all_files = [p for p in _walk_files(game_dir, exclude_roots=exclude_roots)
                 if not _is_runtime_file(p, game_dir)]
    # 老式布局证据：mainData 所在目录（根目录裸 levelN 需与之同目录才收）
    legacy_data_dirs = {p.parent for p in all_files
                        if not p.suffix and _LEGACY_SCENE.fullmatch(p.name)}
    _UNITY_KINDS = frozenset({"unity", "serialized", "webfile",
                              # 工具移植任务 1（2026-08-16）：UnityCN
                              # 加密 bundle 纳入发现——提取时自动解密
                              "unitycn_encrypted"})
    found: list[Path] = []
    for p in all_files:
        suffix = p.suffix.lower()
        relative_parent_parts = p.relative_to(game_dir).parts[:-1]
        is_level_scene = (
            _LEVEL_SCENE.fullmatch(p.name)
            and (
                (explicit_data_dir is not None
                 and p.is_relative_to(explicit_data_dir))
                or any(part.endswith("_Data") for part in relative_parent_parts)
            )
        )
        is_legacy_main = (
            not p.suffix and _LEGACY_SCENE.fullmatch(p.name))
        is_legacy_level = (
            not p.suffix and _LEVEL_SCENE.fullmatch(p.name)
            and p.parent in legacy_data_dirs)
        if is_level_scene or is_legacy_main or is_legacy_level:
            found.append(p)
            continue
        if suffix in ASSET_SUFFIXES and suffix != ".bytes":
            found.append(p)
            continue
        if suffix in _BINARY_SUFFIXES:
            continue
        kind = probe_file_kind(p)
        if kind in _UNITY_KINDS:
            if suffix == ".bytes" and kind == "unity":
                found.append(p)
            elif suffix != ".bytes":
                found.append(p)
            continue
    return _prefer_source_locale_bundles(sorted(found))


_MAX_RAW_STRING_BYTES = 4096

# CJK 表意文字区（2 字符显示词豁免用）：统一表意 + 扩展 A + 兼容区。
# 假名/谚文不含——纯假名 2 字符串与噪声形态区分度低，保持拒绝。
_CJK_IDEO_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
)


def _is_cjk_display_pair(text: str) -> bool:
    """恰好 2 字符且全部是 CJK 表意文字（'同意'/'返回'/'設定' 类 UI 词）。

    min_len 按字符数计，但 CJK 每字 3 字节——2 字符 CJK 串 6 字节，与
    3 字符 ASCII 同量级，按字符数拒绝会整类漏掉真 UI 文本（electric-
    trains obj1558 '同意' 实锤：typetree 失败文件 raw 扫描是唯一通道，
    合法长度头被 min_len=3 拒绝 → 整对象零条目）。混合 ASCII 的 2 字符
    串（'ok'/'a汉'）仍拒。65 游戏普查：2 字符 CJK 对齐串 134 处/23 词
    全部是真显示文本，二进制噪声不会撞出「合法长度头 + 全表意字符 +
    零填充」形态。
    """
    if len(text) != 2:
        return False
    return all(any(lo <= ord(char) <= hi for lo, hi in _CJK_IDEO_RANGES)
               for char in text)


def scan_strings(raw: bytes, min_len: int = 3,
                 max_len: int = _MAX_RAW_STRING_BYTES) -> list[tuple[int, str]]:
    """对齐扫描 Unity 序列化字符串（int32 长度头 + UTF-8）。返回 [(字节偏移, 文本)]。"""
    out: list[tuple[int, str]] = []
    for i in range(0, len(raw) - 3, 4):
        length = int.from_bytes(raw[i:i + 4], "little")
        data_offset = i + 4
        data_end = data_offset + length
        aligned_end = data_end + (-data_end % 4)
        if not (0 < length <= max_len and aligned_end <= len(raw)):
            continue
        if any(raw[data_end:aligned_end]):
            continue
        chunk = raw[data_offset:data_end]
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if "\x00" in text:
            continue
        stripped = text.strip()
        if len(stripped) < min_len and not _is_cjk_display_pair(stripped):
            continue
        if not all(char.isprintable() or char in "\n\r\t" for char in text):
            continue
        out.append((data_offset, text))
    return out


_SHORT_LENGTH_HEADER = _re.compile(
    rb"(?=(?:[\x03-\xff][\x00-\x0f]|\x00[\x01-\x10])\x00\x00)")


def _scan_unaligned_display_strings(
        raw: bytes, occupied_offsets: set[int], min_len: int = 3,
        max_len: int = _MAX_RAW_STRING_BYTES) -> list[tuple[int, str]]:
    """Recover strongly display-like ASCII strings whose length header is shifted.

    The fallback searches only valid little-endian length headers up to the raw
    string safety bound,
    rather than walking every byte as a candidate.  This keeps large Unity objects
    cheap to scan and prevents arbitrary printable runs from becoming write targets.
    """
    out: list[tuple[int, str]] = []
    for match in _SHORT_LENGTH_HEADER.finditer(raw):
        header_offset = match.start()
        data_offset = header_offset + 4
        if data_offset in occupied_offsets or header_offset % 4 == 0:
            continue
        length = int.from_bytes(raw[header_offset:data_offset], "little")
        data_end = data_offset + length
        aligned_end = data_end + (-data_end % 4)
        if not (min_len <= length <= max_len and aligned_end <= len(raw)):
            continue
        if any(raw[data_end:aligned_end]):
            continue
        try:
            text = raw[data_offset:data_end].decode("utf-8")
        except UnicodeDecodeError:
            continue
        if ("\x00" in text
                or not all(char.isprintable() or char in "\n\r\t" for char in text)
                or not has_display_text_evidence(text)):
            continue
        out.append((data_offset, text))
    return out


_is_engine_string = is_engine_string   # 兼容别名（公共层 engine_strings.py）


def _structural_reason(text: str) -> str | None:
    """返回可确定解释的 raw 结构角色；None 表示仍需对象级判断。"""
    stripped = text.strip()
    if _INPUT_ACTION_PATH.match(stripped):
        return "input_action_path"
    if _INPUT_BINDING_PATH.match(stripped):
        return "input_binding"
    if is_code_action_binding(stripped):
        return "code_action_binding"
    if is_physical_binding_identifier(stripped):
        return "input_binding"
    if stripped.casefold() in _INPUT_BINDING_NAMES:
        return "input_binding"
    if _METHOD_NAME.match(stripped):
        return "method_name"
    if _QUALIFIED_TYPE.fullmatch(stripped) or _ASSEMBLY_REFERENCE.fullmatch(stripped):
        return "type_reference"
    if stripped == "New Text":
        return "default_placeholder"
    if _TIMELINE_TRACK.match(stripped):
        return "timeline_track"
    # 署名/credit 形态（doog 实证「林まか (pixiv: 10768714)」被当显示文本
    # 放行后模型改动大小写/半翻——作者署名+作品 ID 是引用信息不是翻译
    # 内容，翻译反而损坏署名信息）→ 结构跳过。
    if _SIGNATURE_CREDIT_RE.search(stripped):
        return "signature_credit"
    return None


def _has_sentence_shape(text: str) -> bool:
    # R3 统一阈值：句子档 = 句末标点或 ≥3 词（display_evidence_tier）。
    # 旧的「≥10 字符含空格即句子」把 'Player Idle'/'White Flash'/'Grass
    # Shader'（2 词引擎配置名）误判为句子放行；2 词短语归 phrase 档，
    # 由对象级证据（组件对象/UI 控件/白名单）决定放行与否。
    return display_evidence_tier(text.strip()) == "sentence"


# 游戏脚本『配置管理器』类判定（F53b，Dobraminhos 实证 2026-09-02）：
# 类名形如 XManager/XControl/XController（X 含 Audio/Sound/Game/UI/
# Music/Scene 等配置域词）的 MonoBehaviour 单例——序列化字段几乎都是
# 运行时按键（音频触发名/状态名/场景流键）。此类对象内的 TitleCase 词
# 不得被 F39 word_list_object 当 UI 目录放行（见 is_word_list_object）。
# 判定用类名 token：须**同时**含配置域词（audio/game/sound/ui/…）与
# 收尾段（manager/control/controller/master/state）。'MenuMaster'/'GameMaster'
# 同样收尾 master；引擎类名（GameObject/Transform/AudioListener）收尾不是
# 这些段、或被 _CODE_QUALIFIED 形态排除，不误杀。单收尾词（'Manager'）
# 单独不足以成立——'PlayerManager' 可能是 UI 名字面板组件；域词 + 收尾
# 双条件是『游戏总控/音频/UI 管理器』的确定性形态。
_MANAGER_DOMAIN = frozenset({
    "audio", "sound", "sfx", "music", "game", "ui", "menu", "scene",
    "level", "player", "enemy", "input", "save", "settings", "option",
    "screen", "map", "world", "state", "flow", "control", "volume",
    # 场景背景/视差层管理器（fish 实证 2026-09-02：SeamlessBackgroundController
    # 序列化 Sky/Parallax/Trees/Grass/TreesFar/Near = 背景渲染层按名引用键，
    # 翻译断无缝滚动背景切换）——'background'/'parallax' 收尾 Controller/
    # Manager 才是层管理器，UI 背景选择按钮（'Background' 无 Controller 收尾）
    # 不命中。
    "background", "parallax",
})
_MANAGER_SUFFIX = ("manager", "control", "controller", "master", "state")


def _manager_domain_tokens(script_class: str) -> frozenset[str]:
    """类名 → 配置域词集合：GameManager → {game}；PlayerActionsController
    → {player}（actions 不在域词表）；UIController → {ui}。拆词用标准
    camelCase 边界（含首部全大写缩写 UI/AI 等：upper→upper→lower 在最后一个
    大写前断开），不用单纯 lower→upper——纯缩写前缀整段粘连会丢域词。"""
    import re as _re_tok
    parts = _re_tok.sub(
        r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])",
        " ", script_class).casefold()
    return frozenset(t for t in parts.split() if t in _MANAGER_DOMAIN)


def _is_manager_script_class(script_class: str) -> bool:
    """类名是否『配置管理器』（X 含域词 + Manager/Control/Master 收尾）。"""
    if not script_class or script_class != script_class.strip():
        return False
    # 命名空间限定名剥到最后一段（FMODUnity.X 是引擎配置，另有 class_
    # registry；本判定只处理游戏脚本裸类名）
    simple = script_class.rsplit(".", 1)[-1]
    if not simple or len(simple) > 48:
        return False
    import re as _re_mgr
    if not _re_mgr.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", simple):
        return False
    lower = simple.casefold()
    if not lower.endswith(_MANAGER_SUFFIX):
        return False
    return bool(_manager_domain_tokens(simple))


# R5：预过滤留档样本上限——每对象每原因最多保留 N 条样本条目（防止
# 键列表对象/引擎串密集对象条目爆炸），完整计数由首条样本的
# skipped_count 承载，报告按 reason 聚合可得真实总数。
_PREFILTER_SAMPLE_LIMIT = 10


def _skipped_sample_entry(file_id: str, key_path: str, text: str, *,
                          kind: str, reason: str, count: int,
                          extra_meta: dict | None = None) -> TextEntry | None:
    """静默跳过限量样本（识别 L1：mono/il2cpp/text 提取器统一留档）。

    rawstr/typetree 路径 R5 已用 _prefilter_entry 留档；mono/il2cpp
    此前只有计数（skipped_reasons 聚合）——被跳过的具体内容不可见，
    用户无法区分「确为该跳」与「该翻未翻」。本工具为计数叠加限量
    样本条目（status=skipped，role=structural，skipped_count 承载
    真实总数）：内容可审计、报告聚合可得真数。超过样本上限返回
    None（防条目爆炸——样本数 ≠ 总数）。count 是累计值，提取函数
    末尾由 _finalize_skipped_counts 统一回写为该单元最终计数。"""
    if count > _PREFILTER_SAMPLE_LIMIT:
        return None
    meta = {
        "kind": kind, "confidence": "low", "role": "structural",
        "disposition": "structural", "reason": reason,
        "skipped_count": count,
    }
    if extra_meta:
        meta.update(extra_meta)
    return TextEntry(file_id=file_id, key_path=key_path, original=text,
                     status=STATUS_SKIPPED, meta=meta)


def _prefilter_entry(file_id: str, obj_path_id: int, idx: int, offset: int,
                     text: str, prefilter: str, count: int,
                     asset_file_name: str = "") -> TextEntry:
    """预过滤留档条目（审计 R5）：被引擎串/键标识符/高频串过滤的字符串
    不再静默丢弃，产生限量样本（status=skipped，role=structural）供审计。

    count = 该对象内同 prefilter 原因的累计跳过数；提取函数末尾统一
    回写为最终计数（_finalize_skipped_counts），报告按单元取 max 聚合
    即真实总数（样本数不等于总数）。
    """
    prefix = (f"asset#{asset_file_name}#{obj_path_id}"
              if asset_file_name else f"asset#{obj_path_id}")
    meta = {
        "kind": "rawstr", "obj": obj_path_id, "offset": offset,
        "confidence": "low", "role": "structural",
        "disposition": "structural",
        "reason": f"prefilter_{prefilter}",
        "prefilter": prefilter,
        "skipped_count": count,
    }
    if asset_file_name:
        meta["asset_file"] = asset_file_name
    return TextEntry(file_id=file_id, key_path=f"{prefix}/str/{idx}",
                     original=text, status=STATUS_SKIPPED, meta=meta)


def _finalize_skipped_counts(entries: list[TextEntry],
                             *count_sources: dict[str, int]) -> None:
    """样本计数回写（聚合语义修正）：限量样本的 skipped_count 是累计
    计数（1..10），报告聚合需真实总数——提取函数末尾用最终计数统一
    回写（同 reason 的样本最终值相同），消费端按 (file_id, reason, obj)
    去重取 max 即真数。样本条目标识 = meta 含 skipped_count（真实行无
    此键）；count_sources 按样本 meta 的 reason 键查最终值（typetree/
    rawstr 的 prefilter 计数键带 prefilter_ 前缀，与样本 reason 同形）。"""
    for e in entries:
        if "skipped_count" not in e.meta:
            continue
        reason = e.meta.get("reason") or ""
        for src in count_sources:
            final = src.get(reason)
            if isinstance(final, int) and final > 0:
                e.meta["skipped_count"] = final
                break


def _is_scriptable_object_shape(raw: bytes) -> bool:
    """MonoBehaviour 头部 m_GameObject 引用为空 → ScriptableObject 形态。

    资源配置对象（Timeline 剪辑/TrackAsset/InputActionAsset 等）没有
    m_GameObject（头部 12 字节全零）；场景组件对象（UI 脚本/对话组件等）带
    m_GameObject 引用（非零）。2019.3 及老版 PPtr 为 4+4 字节，新版本 4+8——
    空引用两种布局前 12 字节都全零，组件两种布局都非零（fileID=0 时 pathID
    落在第 4 或第 8 字节起）。
    """
    return len(raw) >= 12 and raw[:12] == b"\x00" * 12


# 识别 L8：高频串阈值相对化——硬编码 40 对小游戏不可达（该跳未跳、
# doubleshake 实证大游戏噪音串全跳）。相对阈值 = max(绝对下限,
# min(旧绝对阈值 40, 总出现次数 × 比例))：小游戏 ≥15 次即判高频
# （修复该跳未跳），大游戏封顶 40（保持升级前判定，噪音串全跳行为
# 不回归——全面复盘审查钉死：>20k 规模相对阈值放大有未验证回归面）。
_HIGH_FREQ_ABS_MIN = 15     # 绝对下限（对象重复/小游戏也适用）
_HIGH_FREQ_RATIO = 0.002    # 占总出现次数比例（小游戏相对收紧）
_HIGH_FREQ_CAP = 40         # 旧硬编码阈值（大游戏封顶，不改变既有判定）

# 识别 L6：确定性脚本类名（PPtr m_Script 解析）——包内脚本（DLL 编译）
# 的 MonoScript 对象在同类文件里，FileID=0 可解析；内建类型（FileID≠0）
# 解析不到，靠串池信号兜底。类名是确定性证据，优先于串池猜类。
_INPUT_SYSTEM_SCRIPT_CLASSES = frozenset({
    "InputActionAsset", "InputActionMap", "PlayerInput",
    "InputActionReference", "InputControlScheme",
})
_TIMELINE_SCRIPT_CLASSES = frozenset({
    "TimelineAsset", "PlayableDirector",
})


def _mono_class_sig(obj) -> str:
    """MonoBehaviour/ScriptableObject 的 (assembly, class_name) 签名，用于失败类缓存。"""
    try:
        mb = obj.parse_monobehaviour_head()
        script = mb.m_Script.deref_parse_as_object()
        ns = script.m_Namespace or ""
        cls = script.m_ClassName or ""
        asm = script.m_AssemblyName or ""
        return f"{asm}|{ns}.{cls}" if ns else f"{asm}|{cls}"
    except Exception:  # noqa: BLE001
        return ""


def _script_class_from_head(obj) -> str:
    """头部解析兜底的脚本类名（命名空间限定）。

    give-me-strength 音频消失实证（2026-08-29）：FMODUnity.Settings
    （m_Banks 的 bank 名被译 → 全游戏静音）的 typetree 读取失败
    （EOFError），且其 m_Script PPtr（FileID=1）跨文件指向
    globalgamemanagers.assets 的 MonoScript——_script_class_of 的
    两个前提（typetree 成功 + FileID=0）都不成立，类名证据拿不到
    → class_registry 判不了 → 走启发式链误判显示文本。

    parse_monobehaviour_head 只读头部固定布局（m_GameObject/m_Enabled/
    m_Script），不触碰 typetree 越界路径——typetree 失败对象也能读。
    单独加载资源文件时外部引用未挂载，deref 抛 FileNotFoundError；
    按需补载 externals（同目录 .assets 文件）再 deref（实测 380/380
    成功）。返回命名空间限定名（FMODUnity.Settings），class_registry
    按命名空间前缀判定 FMOD 家族。

    解析失败返回 ""（不改变既有判定，串池信号兜底）。
    """
    try:
        mb = obj.parse_monobehaviour_head()
        script = mb.m_Script.deref_parse_as_object()
    except Exception:  # noqa: BLE001
        assets_file = getattr(obj, "assets_file", None)
        if assets_file is None:
            return ""
        environment = getattr(assets_file, "environment", None)
        if environment is None:
            return ""
        # 资源文件所在目录：reader.stream.name 是加载时的绝对路径
        # （env.path 是 cwd 不是加载目录，不可用）
        from pathlib import Path as _P
        try:
            stream = getattr(
                getattr(assets_file, "reader", None), "stream", None)
            base_dir = _P(str(getattr(stream, "name", ""))).parent
            if not str(base_dir) or base_dir == _P("."):
                return ""
        except Exception:  # noqa: BLE001
            return ""
        externals = getattr(assets_file, "externals", None) or []
        paths = []
        for ext in externals:
            name = str(getattr(ext, "path", "") or "").replace(
                chr(92), "/").split("/")[-1]
            if not name:
                continue
            candidate = base_dir / name
            if candidate.is_file():
                paths.append(str(candidate))
        if not paths:
            return ""
        try:
            environment.load(paths)
        except Exception:  # noqa: BLE001
            return ""
        # 补载后重试 deref（外部 MonoScript 已挂载）
        try:
            mb = obj.parse_monobehaviour_head()
            script = mb.m_Script.deref_parse_as_object()
        except Exception:  # noqa: BLE001
            return ""
    ns = str(getattr(script, "m_Namespace", "") or "")
    cls = str(getattr(script, "m_ClassName", "") or "")
    if not cls:
        return ""
    return f"{ns}.{cls}" if ns else cls


def _quick_typetree_check(obj, gen=None) -> bool:
    """纯 Python read_value 快速预检，返回 True=成功（可继续 boost），False=失败（跳过 boost）。

    核心逻辑：
    - 已知失败类在 _FAILED_CLASS_CACHE 跳过（最大节省：ObjectState 31 实例 × 7s）
    - 纯 Python read_value 在越界时 0.00s 失败（vs boost 5-7s）
    - 成功读：纯 Python 约 0.003s（vs boost 0.001s），可接受
    """
    node = getattr(obj, "serialized_type", None)
    node = getattr(node, "node", None) if node is not None else None
    if node is None and gen is not None:
        try:
            mb = obj.parse_monobehaviour_head()
            script = mb.m_Script.deref_parse_as_object()
            fullname = (f"{script.m_Namespace}.{script.m_ClassName}"
                        if script.m_Namespace else script.m_ClassName)
            node = gen.get_nodes_up(script.m_AssemblyName, fullname)
        except Exception:  # noqa: BLE001
            return False
    if node is not None:
        try:
            data = obj.get_raw_data()
            from UnityPy.streams.EndianBinaryReader import EndianBinaryReader
            reader = EndianBinaryReader(data, endian=obj.reader.endian)
            from UnityPy.helpers.TypeTreeHelper import (
                TypeTreeConfig, read_value as tt_read_value)
            tt_read_value(node, reader, TypeTreeConfig(True, obj.assets_file, False))
            return True
        except Exception:  # noqa: BLE001
            return False
    return False  # 无 node 且无法构建 → 视为失败


# 全局失败类缓存（同类对象首例失败后跳过后续 read_typetree）
_FAILED_CLASS_CACHE: set[str] = set()


def _script_class_of(tree: dict, obj) -> str:
    """确定性脚本类名（识别 L6）：typetree m_Script PPtr（FileID=0）
    指向同文件 MonoScript → m_Name。解析失败返回 ""（串池信号兜底，
    不因解析失败改变既有判定）。"""
    pptr = tree.get("m_Script")
    if not isinstance(pptr, dict) or pptr.get("m_FileID") != 0:
        return ""
    assets_file = getattr(obj, "assets_file", None)
    objects = getattr(assets_file, "objects", None)
    if not isinstance(objects, dict):
        return ""
    mono = objects.get(pptr.get("m_PathID"))
    if mono is None:
        return ""
    if str(getattr(getattr(mono, "type", None), "name", "")) != "MonoScript":
        return ""
    try:
        st = mono.read_typetree()
    except Exception:  # noqa: BLE001
        return ""
    name = st.get("m_Name") if isinstance(st, dict) else None
    return str(name) if isinstance(name, str) else ""


def _high_freq_threshold(freq: dict[str, int]) -> int:
    """高频串相对阈值（识别 L8）：基于全文件 raw 串出现总次数缩放，
    封顶 _HIGH_FREQ_CAP——大游戏（total>20k 时相对值超 40）保持升级前
    判定，小游戏相对收紧。"""
    total = sum(freq.values())
    return max(_HIGH_FREQ_ABS_MIN,
               min(_HIGH_FREQ_CAP, int(total * _HIGH_FREQ_RATIO)))


def _mono_object_name_span(raw: bytes) -> tuple[int, int] | None:
    """MonoBehaviour/ScriptableObject 的 m_Name 字符串跨度（长度头+内容）。

    m_Name 是对象标识名（Inspector 标签/Find 查找键），翻译必断链
    （Rendezvous 2026-08-18 实证：场景对象名被 rawstr 路径翻译后，
    游戏代码按原名查找 → 过场流程空指针崩溃）。typetree 路径已有
    m_Name 排除（_IMMUTABLE_FIELD_NAMES），rawstr 路径此前缺失——
    此处按 MonoBehaviour 固定布局定位 m_Name 并排除。

    2026-08-26 布局缺陷修复（写回按键失灵根因之一）：原实现只认新版
    PPtr（4+8 字节）布局——m_GameObject(4+8) + m_Enabled(4) +
    m_Script(4+8) = 28，m_Name 长度头在 @28。老 Unity（<2019.3）PPtr
    为 4+4 字节：m_GameObject(4+4) + m_Enabled(4) + m_Script(4+4) = 20，
    m_Name 长度头在 @20。老布局下 @28 读到的是 m_Name 内容区，n 大概率
    非法 → 返回 None → m_Name 未被排除 → 对象名被当文本翻译 → 断链。
    现对两种布局都尝试定位（新布局优先，头部校验失败再试老布局），
    任一命中即返回该跨度。提取器与写回侧兜底共享本函数，一处修复
    双层防线同时生效。
    """
    if raw is None or len(raw) < 24:
        return None
    # 两种布局的 m_Name 长度头偏移：新 PPtr(4+8)=28，老 PPtr(4+4)=20。
    # 每种布局的头部校验字段偏移不同，分别尝试。
    for name_off, go_off, en_off, sc_off in (
        (28, 0, 12, 16),   # 新版 PPtr：m_GameObject@0(4+8) m_Enabled@12 m_Script@16(4+8)
        (20, 0, 8, 12),    # 老版 PPtr：m_GameObject@0(4+4) m_Enabled@8 m_Script@12(4+4)
    ):
        span = _try_mono_name_at(raw, name_off, go_off, en_off, sc_off)
        if span is not None:
            return span
    # 注：ScriptableObject 的 m_Name 在 @0，但「@0 起是合法长度头+可打印
    # 内容」与「rawstr 对象从 @0 起的显示串」无法区分（_with_len 构造的
    # 对象即此形态）——@0 检测会误伤真实显示文本，故不做。MonoBehaviour
    # 两种布局（新/老 PPtr）的头部校验已覆盖对象名保护主战场。
    return None


def _try_mono_name_at(raw: bytes, name_off: int, go_off: int,
                      en_off: int, sc_off: int) -> tuple[int, int] | None:
    """在指定布局偏移下定位 MonoBehaviour m_Name 跨度。

    头部校验：m_GameObject PPtr(fileID 0..2, pathID≥0) + m_Enabled(0/1)
    + m_Script PPtr(fileID 0..2)——防止把「payload 从 @0 开始」的
    假对象（如测试构造的 raw 池）误判 m_Name 位置。校验通过后读
    m_Name 长度头（@name_off）与内容（@name_off+4），内容须为可打印
    UTF-8 才认定是名字。
    """
    try:
        go_fid = struct.unpack_from("<i", raw, go_off)[0]
        go_pid = struct.unpack_from("<q", raw, go_off + 4)[0]
        enabled = struct.unpack_from("<i", raw, en_off)[0]
        sc_fid = struct.unpack_from("<i", raw, sc_off)[0]
        sc_pid = struct.unpack_from("<q", raw, sc_off + 4)[0]
    except (struct.error, IndexError):
        return None
    if go_fid not in (0, 1, 2) or go_pid < 0 or enabled not in (0, 1):
        return None
    if sc_fid not in (0, 1, 2) or sc_pid < 0:
        return None
    try:
        n = struct.unpack_from("<i", raw, name_off)[0]
    except (struct.error, IndexError):
        return None
    if not (0 < n < 500) or name_off + 4 + n > len(raw):
        return None
    try:
        name = raw[name_off + 4:name_off + 4 + n].decode("utf-8")
        if not all(c.isprintable() or c in " \t" for c in name):
            return None
    except Exception:  # noqa: BLE001 非 UTF-8 内容不是名字
        return None
    return (name_off, name_off + 4 + n)


def _raw_string_entries(file_id: str, obj_path_id: int, raw: bytes,
                        freq: dict[str, int], asset_file_name: str = "",
                        freq_threshold: int | None = None,
                        script_class: str = "") -> list[TextEntry]:
    """MonoBehaviour 原始字节扫描 + 智能过滤。

    关键规则（多层防线，防止把键名当文本翻译）：
    1) 同对象重复字符串（I2/字典结构键值对）：第一次出现是「键」，最后一次是「值」。
    2) 键风格标识符（ui_newGame / MENU_PLAY / en）：should_skip 直接剔除。
    3) 对象级键列表判定：对象内键风格标识符占绝大多数（≥85% 且 ≥3 个），或含
       Unity Localization 结构标记 → 该对象是 SharedTableData 等键存储结构，
       其中全部标识符形态字符串都是键。
    4) 单词式写法（Bold / WASD / Move / Fire）**条件放行**：单词式字符串只有
       在「值特征对象」中才是显示文本（Localization 表值 CREDITOS / SETTINGS）；
       在无值特征的配置/代码型对象（InputActionAsset、UI 样式等）里是
       绑定名/枚举名/引擎名——游戏按原名查找，翻译必然破坏功能（输入失效）。
       值特征 = 对象含 Localization 标记，或含句子形态字符串（标点结尾或长句）。
    5) 预过滤（引擎串/键风格标识符/全游戏高频串）不静默丢弃（R5）：产生
       限量样本条目（status=skipped，带 reason）供审计——用户能区分
       「日志/键」与「该翻未翻」，且对象级统计可告警（消灭哑信号）。
    """
    aligned = scan_strings(raw)
    recovered = _scan_unaligned_display_strings(
        raw, {offset for offset, _ in aligned})
    scanned_with_mode = sorted(
        [(offset, text, "aligned") for offset, text in aligned]
        + [(offset, text, "unaligned") for offset, text in recovered],
        key=lambda item: item[0],
    )
    if freq_threshold is None:
        freq_threshold = _high_freq_threshold(freq)
    scanned = [(idx, offset, s) for idx, (offset, s, _) in enumerate(scanned_with_mode)]
    scan_modes = {offset: mode for offset, _, mode in scanned_with_mode}
    # 每个字符串在对象内的出现次数
    counts: dict[str, int] = {}
    for _, _, s in scanned:
        counts[s] = counts.get(s, 0) + 1
    # 标记串（UnityEngine.Localization 等程序集名）本身会被引擎过滤剔除，
    # 因此标记检测必须在完整扫描列表上做，而不是过滤后的 non_engine。
    has_marker = any(m in s for s in (s for _, _, s in scanned) for m in _LOCALIZATION_MARKERS)
    # 每个字符串已出现次数（用于判断是否最后一次）
    seen: dict[str, int] = {}
    entries: list[TextEntry] = []
    non_engine: list[str] = []
    prefilter_counts: dict[str, int] = {}
    # Rendezvous 2026-08-18 实证：rawstr 路径翻译 m_Name（对象标识名）
    # → 游戏按原名查找断链 → 过场空指针崩溃。typetree 路径经
    # _IMMUTABLE_FIELD_NAMES 排除 m_Name，rawstr 路径在此按固定布局
    # 定位 m_Name 跨度并跳过（对象标识名绝不进入翻译池）。
    name_span = _mono_object_name_span(raw)
    for idx, offset, s in scanned:
        if (name_span is not None
                and name_span[0] <= offset <= name_span[1]):
            continue
        seen[s] = seen.get(s, 0) + 1
        is_last = seen[s] == counts[s]
        structural_reason = _structural_reason(s)
        interaction_prompt = (
            structural_reason is None and is_interaction_prompt(s)
        )
        strong_display_evidence = (
            interaction_prompt
            or (structural_reason is None and has_display_text_evidence(s))
        )
        # R5 预过滤留档：引擎串/键风格标识符/全游戏高频串不再静默 continue。
        # 产生限量样本条目（status=skipped，带 reason）供审计与报告聚合
        # ——用户能区分「日志/键（该跳）」与「该翻未翻（误跳）」（审计 R5：
        # the-supper 893 条 unverified 零告警的机制根源是静默丢弃）。
        # 注意：should_skip/freq 串仍进 non_engine（与原语义一致——它们是
        # 对象字符串池成员，贡献对象级值证据；只有引擎串不进池）。
        if _is_engine_string(s) and structural_reason is None:
            prefilter = "engine_string"
        else:
            non_engine.append(s)
            if should_skip(s) and structural_reason is None:
                prefilter = "key_identifier"
            elif (freq.get(s, 0) >= freq_threshold
                  and not strong_display_evidence):
                prefilter = "high_frequency"
            else:
                prefilter = None
        if prefilter is not None:
            # 计数键带 prefilter_ 前缀 = 样本 meta 的 reason（回写同形）
            key = f"prefilter_{prefilter}"
            count = prefilter_counts[key] = \
                prefilter_counts.get(key, 0) + 1
            if count <= _PREFILTER_SAMPLE_LIMIT:
                entries.append(_prefilter_entry(
                    file_id, obj_path_id, idx, offset, s,
                    prefilter, count, asset_file_name))
            continue
        # 非键风格显示文本每次出现都 pending。原“首键末值”规则（首次=键、
        # 末次=值）只对 I2/Localization 字典（has_marker）有意义——其键是
        # 标识符，已被 should_skip 剔除；普通 UI 对象里相同文本多处出现
        # （同一按钮在多个面板 / 多状态 Text）若只留末条可译，游戏里就只有
        # 一种 UI 状态被汉化（deadbeat 暂停菜单 Pause 按钮 ×3 实证）。
        if interaction_prompt or (
                structural_reason is None
                and (not has_marker or is_last)):
            status = "pending"
        else:
            status = "skipped"
        prefix = f"asset#{asset_file_name}#{obj_path_id}" if asset_file_name else f"asset#{obj_path_id}"
        meta = {"kind": "rawstr", "obj": obj_path_id, "offset": offset,
                "scan_mode": scan_modes[offset]}
        if structural_reason:
            meta["structural_reason"] = structural_reason
        if script_class:
            meta["script_class"] = script_class
        if asset_file_name:
            meta["asset_file"] = asset_file_name
        entries.append(TextEntry(
            file_id=file_id, key_path=f"{prefix}/str/{idx}",
            original=s, status=status,
            meta=meta))

    # 值特征：含 Localization 标记，或含句子形态字符串（标点结尾 / 较长含空格句）
    has_value_evidence = has_marker or any(
        _has_sentence_shape(s) and _structural_reason(s) is None
        for s in non_engine)

    # InputSystem 对象信号：确定性脚本类名（识别 L6：PPtr m_Script 解析，
    # morfosigame/deadbeat 输入配置对象的类名证据优先于串池猜类）或
    # action map 名（GameActions 等）/绑定路径（<Keyboard>/z）/interactions
    # 串（Press(behavior=2)）/InputSystem 程序集串 → 该对象是输入配置，
    # 对象内 action 名等全是运行时按名查找的键。翻译必然破坏按键交互
    # （morfosigame 实证：默认模板 map 名 'Normal' 不在名单里，
    # Proceed/SkipCutscene 动作名全被翻译 → 点击对话/F 跳过全部无反应）。
    is_input_system_object = (
        script_class in _INPUT_SYSTEM_SCRIPT_CLASSES
        or any(
            s.strip().casefold() in _INPUTSYSTEM_MAP_NAMES
            or bool(_INPUT_BINDING_PATH.match(s.strip()))
            or bool(_INPUTSYSTEM_INTERACTION.match(s.strip()))
            for _, _, s in scanned)
        or any(
            sig in s for s in (s for _, _, s in scanned)
            for sig in _INPUTSYSTEM_ASSEMBLY_SIGNALS))

    # Timeline 对象信号：确定性脚本类名（识别 L6）或轨道名（Animation
    # Track (1) 带编号形式）/Markers 标记/Timeline/Playables 程序集串 →
    # 轨道名/剪辑名/动画状态名按名查找，翻译破坏演出（morfosigame 实证：
    # 'Animation Track (1)' 被拆成 '动画轨道'+' (1)'，字符串计数 2→4
    # 结构错乱，Timeline 反序列化失败）。
    is_timeline_object = (
        script_class in _TIMELINE_SCRIPT_CLASSES
        or any(
            bool(_TIMELINE_TRACK.match(s.strip()))
            or s.strip().casefold() in _TIMELINE_MARKER_NAMES
            for _, _, s in scanned)
        or any(
            sig in s for s in (s for _, _, s in scanned)
            for sig in _TIMELINE_ASSEMBLY_SIGNALS))

    # UnityEvent 事件绑定对象信号：对象字符串池含持久化回调字段
    # （m_PersistentCalls/m_Target/m_MethodName）→ 对象是事件绑定配置，
    # 其中方法名/目标名是反射按名绑定键（知识库案例「UnityEvent 事件
    # 绑定断裂按钮无反应」转规则）。判定在完整扫描列表上做（引擎串被
    # 过滤不影响信号——与 InputSystem/Timeline 信号同模式）。
    # 2026-08-29 补盲（give-me-strength 实证）：UnityEvent 字段名只存在
    # 于 typetree/字段路径，rawstr 字节层只序列化值——Play 按钮回调链
    # （m_Target 类型引用 + m_MethodName=Play）的字段名不进 raw 字节，
    # 字段信号检测不到。rawstr 值里唯一的事件绑定结构证据是 m_Target 的
    # 程序集限定名（UnityEngine.Object, UnityEngine）——序列化对象内
    # 同值出现 ≥2 次 = 多个回调的目标类型引用（obj508 实证：两个回调各
    # 一个，count=2）。计数条件防普通按钮文本对象（a-catfiends obj1319
    # 'Save'+单次类型引用，count=1）误触发。
    _unityevent_target_count = sum(
        s.strip() in _UNITYEVENT_TARGET_TYPES for _, _, s in scanned)
    is_unityevent_object = (
        any(sig in s for s in (s for _, _, s in scanned)
            for sig in _UNITYEVENT_SIGNALS)
        or _unityevent_target_count >= 2)

    # InputManager 轴配置对象信号（give-me-strength 实证 2026-08-29）：
    # StandaloneInputModule 序列化含 Horizontal/Vertical/Submit/Cancel
    # 四轴名——轴名是 Input.GetAxis 运行时查找键，翻译必断输入绑定
    # （菜单键盘导航失灵）。串池含 ≥2 个不同轴名 → 轴配置对象，轴名
    # 全部跳过。Cinemachine 相机类信号（script_class 前缀 Cinemachine.）
    # 或孤立轴名（Mouse X 单串）由 _INPUT_AXIS_NAMES 直接拦截（见分类链）。
    axis_names_in_pool = {
        s.strip().casefold() for _, _, s in scanned
        if s.strip().casefold() in _INPUT_AXIS_NAMES}
    is_input_axis_object = len(axis_names_in_pool) >= _INPUT_AXIS_OBJECT_MIN
    is_cinemachine_object = script_class.startswith(_CINEMACHINE_CLASS_PREFIX)

    # 共享资源小配置对象：非场景文件（level*）里无句子形态、≤2 个不同短词串的对象
    # （Timeline 剪辑 displayName 'Timothy'、'White Flash'、动画状态 'Player Idle' 等）。
    # 场景（level）里的同形对象是对话说话者名（TIMOTHY），按现有规则正常翻译——
    # 文件位置 + 内容形态双条件区分（morfosigame 实证：sharedassets4 116 字节
    # 'Timothy' 对象是 AnimationPlayableAsset 剪辑名，level 对话对象含句子）。
    is_shared_resource = not (
        asset_file_name and Path(asset_file_name).name.casefold().startswith("level"))
    small_words = {s.strip() for s in non_engine if s.strip()}
    # UI 控件配置对象信号：串池含 UI 控件词缀（Button/Label 等）——对象是
    # UI 元素配置（Corgi Engine 按钮实证 the-supper obj 1643：对象名
    # 'NewGameButton' + 按钮文本 'New Game'）。控件词缀是显式形态证据，
    # 优先于「小配置对象=引擎键」的对象级猜测（证据分层，审计 R1）：
    # 该对象里的普通词串是按钮/标签显示文本，必须放行翻译。
    # 词缀只取最强形态（Button/Label）——menu/screen/panel 等歧义词（可能
    # 是场景对象名/引擎资源名）不纳入，防过宽。
    ui_control_signal = any(
        s.strip().casefold().endswith(("button", "btn", "label"))
        for _, _, s in scanned)
    # 小配置对象形态判定（无豁免的原始条件）：ScriptableObject 形态 +
    # 共享资源 + ≤2 个非句子短词。豁免（ui_control_signal/ui_word_signal）
    # 只在本形态内生效——绑定名对象（down/left/right，InputActionAsset
    # 非 ScriptableObject 头部）里白名单词仍是键，不得豁免。
    is_small_config_shape = (
        _is_scriptable_object_shape(raw)
        and is_shared_resource
        and not has_marker
        and 1 <= len(small_words) <= 2
        and all(
            not _has_sentence_shape(s) and len(s) <= 16
            for s in small_words))
    # 白名单显示词证据：小配置形态对象中任一词在显式显示词白名单
    # （Pause/Menu/Save/Load/Language/Off/Talk 等 UI 界面词）——该对象
    # 是 UI 配置（Corgi Engine UIMenu 面板实证 the-supper obj 1755
    # 'Pause'+'Menu'），其词串是界面文本。白名单词是显式显示词证据
    # （形态性），不得被「小配置=引擎键」的猜测性规则推翻（证据分层）；
    # 引擎配置名（'Timothy'/'Player Idle'/'White Flash'）不在白名单，
    # 不受影响。
    ui_word_signal = is_small_config_shape and any(
        s.strip().casefold() in DISPLAY_WORDS for s in small_words)
    # 引擎配置对象（无豁免的小配置形态）：Timeline 剪辑名/动画状态名
    # 不含控件词缀且不在白名单，仍按配置跳过。
    is_small_config_object = (
        is_small_config_shape
        and not ui_control_signal
        and not ui_word_signal)

    # 对象级键列表判定：键列表对象中的标识符全部降级为 skipped（写回也据此跳过）。
    # 单词式写法（CREDITOS / Settings）是显示值不算键风格标识符——避免西语等
    # 全单词 UI 表被误判为键列表。
    idents = [s for s in non_engine
              if _IDENTIFIER.match(s) and not _WORD_CASE.match(s)]
    is_key_list = (len(idents) >= 3 and len(idents) / max(1, len(non_engine)) >= 0.85) or \
        has_marker
    direct_code_signal_count = sum(
        _structural_reason(s) in ("method_name", "type_reference")
        or s.strip() in _CODE_DRIVEN_METHODS
        for _, _, s in scanned
    )
    lifecycle_signal_count = sum(
        s.strip() in _LIFECYCLE_METHODS for _, _, s in scanned)
    is_code_heavy = (direct_code_signal_count >= 2 or
                     (direct_code_signal_count >= 1 and lifecycle_signal_count >= 1))
    core_menu_terms = {
        s.strip().casefold() for s in non_engine
        if s.strip().casefold() in CORE_MENU_SOURCE_TERMS
    }
    is_core_menu_collection = len(core_menu_terms) >= 2
    control_states = {
        s.strip().casefold() for _, _, s in scanned
        if s.strip().casefold() in _UNITY_CONTROL_STATE_NAMES
    }
    is_core_menu_control = len(control_states) >= 3
    is_single_visible = len(scanned) == 1 and len(entries) == 1
    # 词表/字典对象判定（happy-cat-tavern 实证 2026-08-12）：打字游戏
    # 单词库对象——字符串几乎全部是单 token 单词且数量大（level1#1311
    # 1700 条 100% 单词）。此类对象中白名单常见词（play/time/gold…）
    # 被 direct_code_signal/ui_control_signal 误放行进池翻译，写回后
    # 玩家无法按英文打字（打字玩法破坏）。大型全单词数组是确定性词表
    # 结构证据，优先于形态性猜测（证据分层）；正常 UI 对象含句式/描述
    # 文本且条目数少（设置菜单 <50 条），不触发。
    _stripped_pool = [s.strip() for s in non_engine if s.strip()]
    is_word_table = (
        len(_stripped_pool) >= 50
        and sum(1 for s in _stripped_pool if _WORD_TOKEN_RE.match(s))
        / len(_stripped_pool) >= 0.95
    )
    # TMP 资产对象判定（headache 实证 2026-08-12）：TextMeshPro 字体/
    # 精灵资产序列化对象——m_AssetVersion 值 '1.1.0' + 字体名含独立
    # token 'sdf'（'BaiJamjuree-Medium SDF'）或精灵资产名 'sprite
    # asset'（'Default Sprite Asset'）。资产名是 <font>/<sprite
    # name=...> 按名引用键（Winkle/Smiley/Bai Jamjuree Medium），
    # 翻译断引用——写回后 Sprite 变体/表情/字体全部丢失。资产对象
    # 字符串是资产元数据（名字+GUID+版本），非可译 UI 文本，对象级
    # 判定整体跳过。'1.1.0' 词边界防普通文本 "v1.1.0" 误伤。
    # 检测用完整 scanned 池（含引擎串）：资产名本身被引擎串过滤拦截
    # （不进 non_engine），但同对象其余串（精灵名 Smiley/Wink、布局
    # 参数 Character/Line Spacing）进池——资产名是判定证据必须可见。
    _pool_lower = " ".join(s.strip().casefold() for _, _, s in scanned)
    # TMP 资产对象探测放宽（B11，handshakes 实证 2026-09-02）：资产对象
    # 也可能只带 m_Version='1.1.0' + 字体字段（m_FamilyName/m_StyleName/
    # m_SourceFontFileGUID）而无 'sdf'/'sprite asset' 字样——typetree
    # 字段名（familystyle/…）是确定性资产结构证据。raw scan 的串池探测
    # 覆盖不到字段名（只看到值 Medium/rainyhearts），故放宽到「对象含
    # 字体资产字段名串」（m_StyleName 值 Medium + 邻域 m_Name 字体名）。
    # 判定仍要求 m_Version '1.1.0' 值在场（m_FamilyName 字体族名 + 资产
    # 版本号 = 字体资产形态）。
    _asset_field_sig = (
        "m_familyname" in _pool_lower or "m_stylename" in _pool_lower
        or "m_sourcefontfileguid" in _pool_lower
        or "m_fontassets" in _pool_lower)
    is_tmp_asset_object = (
        _re.search(r"\b1\.1\.0\b", _pool_lower) is not None
        and (_re.search(r"\bsdf\b", _pool_lower) is not None
             or "sprite asset" in _pool_lower
             or _asset_field_sig)
    )
    # 识别 L9：确定性脚本类注册表（class_registry）。config 类（TMP
    # 字体/精灵资产、InputSystem/Timeline 配置）对象内字符串是按名
    # 查找键/资产元数据——确定性类名证据优先于串池信号猜测
    # （is_tmp_asset_object 等形态猜测的确定性版本），整体跳过。
    # 未登记类名不判定（走既有启发式），由提取器收集进报告待登记队列。
    from hanhua.core.unity.class_registry import disposition
    class_disposition = disposition(script_class)
    is_config_class = class_disposition == "config"
    # F39（attack-on-wendigo 实证）：命名列表对象信号——TitleCase 单词式
    # 词 ≥3 且无代码/输入/Timeline/事件/配置/键列表信号 → 对象是武器/
    # 物品/地点目录（商店/掉落/库存 UI 文本，'Pistol'/'Magnum'/'Rifle'
    # 等整批被标识符规则跳过）。单词式写法（_WORD_CASE）是显示文本
    # 形态（is_key_style_identifier 的 CREDITOS/Settings 同证据）；
    # 粒子名（SnowParticle 驼峰）、类型引用对象（code_heavy）、
    # InputAction/Timeline 对象（信号先拦）不受影响。
    titlecase_words = [s.strip() for s in non_engine
                       if _WORD_CASE.match(s.strip())]
    # F39 占比约束（test_v2 InputAction 场景实证）：命名列表对象几乎
    # 全是单词式词（武器列表 5/7）；InputAction 绑定对象（'Player'/
    # 'Move'/'Dpad' + down/left/right 小写绑定名）TitleCase 占比低
    # （3/6）——大小写混合是输入配置形态，不触发。含输入绑定词
    # （WASD/move/fire/look 等）的对象是 InputAction 特征，不触发
    # （test_raw_string_entries_word_identifiers 契约锚点）。
    is_word_list_object = (
        len(titlecase_words) >= 3
        and len(titlecase_words) / max(1, len(non_engine)) >= 0.6
        and not any(s.strip().casefold() in _INPUT_BINDING_NAMES
                    for s in non_engine)
        and not is_code_heavy and not is_input_system_object
        and not is_timeline_object and not is_unityevent_object
        # 2026-08-29（give-me-strength 实证）：StandaloneInputModule 的
        # 轴名 Horizontal/Vertical/Submit/Cancel 全是 _WORD_CASE TitleCase
        # 词（占比 4/4 ≥0.6）→ 此前被本规则放行翻译成「水平/垂直/提交/取消」，
        # Input.GetAxis 查找键断裂 → 菜单键盘导航失灵。轴配置对象信号
        # （≥2 不同轴名）优先于「命名列表=显示文本」的形态猜测。
        and not is_input_axis_object
        and not is_small_config_object and not is_key_list
        and not is_word_table and not is_tmp_asset_object
        and not is_config_class
        # F53b（Dobraminhos 实证 2026-09-02）：AudioManager 单例对象
        # （脚本类名 = 确定性『音频管理器』语义）内同文件重复 25 个
        # TitleCase 词——'Lobby'/'Boss'/'Preto'/'Ataque' 全是 PlayOneShot/
        # AudioSource 按名触发的音频键（同对象内 Boss_Final/Inimigo_*
        # 下划线键 + 'Normal' 状态名是同类证据），不是商店/物品目录。
        # 音频键是运行时按名查找，翻译断音效触发。UI 文本（菜单/设置
        # 按钮）在 TMP_Text m_Text 字段经 typetree 单独提取，不受影响。
        # 类名按 token 拆：Audio/Game/Sound/Music 等『配置管理器』语义段
        # + Manager/Control/Controller 收尾段——游戏脚本类常叫 GameManager
        # /SoundManager，引擎类不叫这个名字。
        and not _is_manager_script_class(script_class))
    # F38（adapt-prologue 实证）：对象最终会被释放（非代码/输入/
    # Timeline/事件绑定/共享配置/键列表/词表/资产对象）时，其
    # prefilter_high_frequency 样本是**游戏 UI 词**而非引擎高频——
    # 生物卡片对象（'Clam 01' pending 同侪）里 'Health'/'Food'/
    # 'Resource' 全被高频跳过（哑信号）。组件配置对象（code_heavy/
    # InputSystem/Timeline/共享配置/键列表）保持跳过——'Play' 音效
    # 事件名 294 对象实证：升级会断 FMOD/动画引用。注意 is_core_menu_
    # control（UI 控件状态 Normal/Pressed ≥3）是 UI 对象信号，不排除。
    obj_will_release = not (
        is_code_heavy or is_input_system_object or is_timeline_object
        or is_unityevent_object or is_small_config_object
        or is_input_axis_object or is_cinemachine_object
        or is_key_list or is_word_table or is_tmp_asset_object
        or is_config_class or is_single_visible)
    for entry in entries:
        # R5：预过滤留档条目（prefilter_*）的 reason/role 已由
        # _prefilter_entry 定稿，不再走分类链（否则会被
        # duplicate_key_position 等后处理覆盖）。
        if entry.meta.get("prefilter"):
            # F38：高频样本在对象释放时升级为显示文本（非升级的
            # 引擎/键样本保持 skipped，审计语义不变）。UGUI 状态名
            # （Normal/Highlighted/Pressed/Selected/Disabled）即使
            # 在 UI 对象里也是控件状态串（翻「正常」写回状态错乱），
            # 保持跳过（'Enabled'/'Disabled' 双义——天赋卡片「已启用/
            # 已禁用」标签是真 UI，不排除）
            if (entry.meta["prefilter"] == "high_frequency"
                    and obj_will_release
                    and entry.original.strip().casefold()
                    not in _UNITY_CONTROL_STATE_NAMES):
                entry.status = "pending"
                entry.meta.update({
                    "reason": "single_visible_string",
                    "confidence": "medium",
                    "role": "display",
                    "disposition": "translate",
                    "f38_released": True,
                })
                entry.meta.pop("prefilter", None)
                entry.meta.pop("skipped_count", None)
                continue
            continue
        reason = entry.meta.pop("structural_reason", None)
        stripped = entry.original.strip()
        # 控件状态词硬拦截（2026-08-31 用户实证「Disabled 残疾人士」）：
        # Normal/Highlighted/Pressed/Selected/Disabled 是 Unity 视觉状态
        # 动画名（按钮 m_AnimationTriggers 状态值），即使孤立单串在 UI
        # 对象里也是控件状态串（翻「正常/残疾」写回状态错乱）。typetree
        # 路径已按 m_*Trigger 字段名结构跳过；rawstr 路径无字段名，按值
        # 兜底拦截。'Enabled'/'Disabled' 双义——真 UI 标签（天赋卡片
        # 「已启用/已禁用」）由 BUILTIN_UI_REFERENCES 权威译名 + 确定性
        # 直填处理，不是这里放行进池的原因。
        if stripped.casefold() in _UNITY_CONTROL_STATE_NAMES:
            entry.status = STATUS_SKIPPED
            entry.meta["obj_is_key_list"] = True
            reason = "unity_control_state"
            confidence, role = "low", "structural"
        elif is_word_table:
            # 词表对象条目：整体跳过（含白名单词——词表词翻译破坏
            # 打字玩法；白名单显示词证据只在真实 UI 组件对象生效）
            entry.status = STATUS_SKIPPED
            entry.meta["obj_is_key_list"] = True
            reason = "word_table_object"
            confidence, role = "low", "structural"
        elif is_tmp_asset_object:
            # TMP 字体/精灵资产序列化对象：资产名是 <font>/<sprite>
            # 引用键（翻译断引用→Sprite 变体/表情丢失），对象整体跳过
            entry.status = STATUS_SKIPPED
            reason = "tmp_asset_object"
            confidence, role = "low", "structural"
        elif is_config_class:
            # 确定性脚本类注册表 config 类（识别 L9）：TMP 资产/
            # InputSystem/Timeline 配置——对象内字符串是按名查找键，
            # 类名证据优先于串池信号猜测，整体跳过。L6 已覆盖的类
            # 保持既有 reason 词汇（审计连续性），注册表新类用
            # script_class_config。
            entry.status = STATUS_SKIPPED
            entry.meta["obj_is_key_list"] = True
            entry.meta["script_class"] = script_class
            reason = (
                "input_system_object"
                if script_class in _INPUT_SYSTEM_SCRIPT_CLASSES
                else "timeline_object"
                if script_class in _TIMELINE_SCRIPT_CLASSES
                else "script_class_config")
            confidence, role = "low", "structural"
        elif reason:
            entry.status = STATUS_SKIPPED
            if reason == "input_binding":
                entry.meta["obj_is_key_list"] = True
            confidence, role = "low", "structural"
        elif _is_script_code_line(stripped):
            # 单行代码（Lua 命令块/类型全名/函数签名链）：翻译即破坏功能。
            # 0.25.0 地毯式实证：a-catfiends 的 runblock/setcharacter/local
            # choice/elseif 行与 "System.Boolean, mscorlib, ..." 类型全名、
            # InvertVector2(...) 签名链被句子形状规则误放行进池、模型回显
            # 或乱译、质量门拦截成失败——代码文本不进池（硬结构规则）。
            entry.status = STATUS_SKIPPED
            entry.meta["obj_is_key_list"] = True
            reason = "code_line"
            confidence, role = "low", "structural"
        elif not any(ch.isalpha() for ch in stripped):
            # 纯符号串（{0} : {1} 等格式占位/分隔符/图标字符）：无字母 =
            # 无语言内容可翻，模型常回显或乱改（实证 {0} : {1} 失败）。
            entry.status = STATUS_SKIPPED
            reason = "symbols_only"
            confidence, role = "low", "structural"
        elif entry.status == STATUS_SKIPPED:
            reason = "duplicate_key_position"
            confidence, role = "low", "structural"
        elif is_pure_tags(stripped):
            # 纯 TMP 标签序列（<size=30><align=center>，无正文字母）：
            # 标签是排版标记不是语言内容，翻译必破坏标签结构（TMP 标签
            # 语法层，覆盖全部 TMP 游戏）
            entry.status = STATUS_SKIPPED
            reason = "tmp_pure_tags"
            confidence, role = "low", "structural"
        elif is_tag_composed(stripped):
            # TMP 标签组合串（<color=red>Warning!</color> / <b>hi</b>）：
            # 标签是排版标记、正文是可译内容——即使正文短小无空格也放行
            # （形态规则会把 <b>hi</b> 误判为标识符/HTML 结构降级）
            reason = "tmp_tag_composed"
            confidence, role = "medium", "display"
            refs = referenced_names(stripped)
            if refs:
                entry.meta["tmp_tag_refs"] = sorted(refs)
        elif is_interaction_prompt(stripped):
            entry.status = "pending"
            reason = "interaction_prompt"
            confidence, role = "high", "display"
        elif stripped in _LIFECYCLE_METHODS:
            entry.status = STATUS_SKIPPED
            reason = "lifecycle_method"
            confidence, role = "low", "structural"
        elif is_input_system_object or is_timeline_object or is_unityevent_object or is_small_config_object:
            # 引擎配置对象（输入/时间线/UnityEvent 事件绑定/动画配置）：
            # 其中的短词串是运行时按名查找的键（动作名/轨道名/方法名/
            # 状态名），翻译破坏功能（UnityEvent 绑定断裂 → 按钮无反应，
            # 知识库案例转规则）；强显示证据串理论上不出现，保守放行防
            # 误伤。注意不能用 _has_sentence_shape（≥10 字符含空格即真，
            # 'Arrow Keys' 10 字符会被误判为句子）。
            if has_display_text_evidence(stripped):
                reason = "natural_language"
                confidence, role = "medium", "display"
            else:
                entry.status = STATUS_SKIPPED
                entry.meta["obj_is_key_list"] = True
                reason = (
                    "input_system_object" if is_input_system_object
                    else "timeline_object" if is_timeline_object
                    else "unityevent_object" if is_unityevent_object
                    else "shared_resource_config_object")
                confidence, role = "low", "structural"
        elif is_input_axis_object or is_cinemachine_object:
            # InputManager 轴配置对象 / Cinemachine 相机对象（give-me-
            # strength 实证 2026-08-29）：轴名（Horizontal/Vertical/Submit/
            # Cancel/Mouse X）是 Input.GetAxis 运行时查找键，翻译必断输入
            # 绑定（菜单键盘导航失灵、相机轨道轴断裂）。对象级整体跳过。
            entry.status = STATUS_SKIPPED
            entry.meta["obj_is_key_list"] = True
            entry.meta["obj_is_input_axis"] = True
            reason = ("input_axis_object" if is_input_axis_object
                      else "cinemachine_object")
            confidence, role = "low", "structural"
        elif stripped.casefold() in _UNAMBIGUOUS_AXIS_NAMES:
            # 无歧义轴名（Mouse X/Mouse Y 等带 Mouse 前缀的输入轴）：即使
            # 孤立单串（Cinemachine 相机轨道对象 give-me-strength obj513
            # 实证）也是 Input.GetAxis 查找键——翻译断相机轨道轴。Cinemachine
            # 类对象已由上一分支整体跳过；此处兜底非 Cinemachine 类的孤立
            # 轴名对象（未登记类/typetree 失败时 script_class 为空）。
            entry.status = STATUS_SKIPPED
            entry.meta["obj_is_key_list"] = True
            entry.meta["obj_is_input_axis"] = True
            reason = "unambiguous_axis_name"
            confidence, role = "low", "structural"
        elif (is_single_visible
              and Path(asset_file_name).name.casefold() == "resources.assets"
              and _IDENTIFIER.match(stripped)):
            if stripped.casefold() in DISPLAY_WORDS:
                # 显示词白名单优先于资源猜测：a-catfiends-impending-relapse
                # 实证（0.25.0 地毯式）：Fungus 对话按钮 Continue/Save/Load/
                # Restart/Submit/Cancel 在 resources.assets 单串对象里被
                # 资源标识符规则整组跳过——白名单是显式显示词证据（形态性），
                # 不得被「单串即资源键」的猜测性规则推翻（证据分层）。
                entry.status = "pending"
                reason = "single_visible_string"
                confidence, role = "high", "display"
            else:
                entry.status = STATUS_SKIPPED
                reason = "resource_identifier_without_display_evidence"
                confidence, role = "low", "structural"
        elif is_single_visible and _LIST_MARKER.match(stripped):
            # 列表项标记（'- a'、'• x'）：符号+极短 token，无可译语义
            # 内容——78-hour-rain 实证：'- a' 被模型回显恒败
            # target_script_mismatch（2 条阻塞）。与资源标识符猜测不同，
            # 这是确定性形态（符号+≤3 字符），不依赖对象语境。
            entry.status = STATUS_SKIPPED
            reason = "list_marker"
            confidence, role = "low", "structural"
        elif is_single_visible:
            # 孤立纯小写长词（≥10 字符）：触发器/字段名形态（fieldtrigger
            # 12 字符实证——MonoBehaviour rawstr 数组里孤立的代码词被
            # 无条件放行后模型回显恒败）。对象内无其他显示证据可参照，
            # 长纯小写词无空格无分隔符是代码标识符形态；真实显示文本
            # 的孤立长词（staircase/hallway 等场景词）短于此阈值。
            # F41（bottle-cracks 实证）：显示词白名单豁免——'fullscreen'
            # 是设置项文本（该翻「全屏」），白名单优先于长词形态猜测。
            if (stripped.islower() and stripped.isalpha()
                    and len(stripped) >= 10
                    and stripped.casefold() not in DISPLAY_WORDS):
                entry.status = STATUS_SKIPPED
                reason = "isolated_lowercode_word"
                confidence, role = "low", "structural"
            else:
                entry.status = "pending"
                reason = "single_visible_string"
                confidence, role = "high", "display"
        elif is_key_list and _IDENTIFIER.match(stripped):
            entry.status = STATUS_SKIPPED
            entry.meta["obj_is_key_list"] = True
            reason = "localization_key_list"
            confidence, role = "low", "structural"
        elif is_code_heavy and stripped in _LIFECYCLE_METHODS:
            entry.status = STATUS_SKIPPED
            reason = "lifecycle_method"
            confidence, role = "low", "structural"
        elif is_code_heavy:
            # 白名单显示词（Play/Instructions 等按钮文本）仅在对象有 UI 证据
            # （交互提示/控件状态）时放行——hotel-paradise 真实按钮对象含
            # Normal/Highlighted/Pressed 状态；纯 code 对象（无 UI 证据）中的
            # 单词仍跳过（防代码常量误放行）。core_menu_terms 不能作为证据——
            # 那是被检查词自身，用它会循环放行菜单词。
            has_ui_evidence = bool(
                len(control_states) >= 3 or interaction_prompt)
            # 控件状态名（Normal/Highlighted/Pressed/Selected/Disabled）是
            # Unity VisualState 引擎文本，即使在本按钮对象中也不翻译
            # （hotel-paradise 真实误伤：按钮对象的 Normal 被错误放行）
            in_control_state = stripped.casefold() in control_states
            # 对象名共享词（2026-08-15 多游戏实证：写回后按键 UI 失灵、
            # 游戏卡住无法推进——按钮对象 m_Name="Start" 与 m_text=
            # "Start" 同值，rawstr 无字段身份，两处一起被翻译写回，
            # 对象名被改 → 代码 Find("Start")/事件按名引用断裂）。
            # 代码对象 + UI 证据 + 白名单词 + 同值重复（≥2 处）=
            # 名字+文本共享词结构——全组跳过保留原文（宁漏勿坏；
            # 此类词几乎都有本地化表副本可翻）。单次出现不跳
            # （hotel-paradise 静态按钮文本场景）。
            # 2026-08-26 冲突缺口修复（写回按键失灵根因之一）：F44 让
            # 非白名单按钮词（西语 'Jugar' 等 _WORD_CASE 单词式）可译，
            # 但 shared_with_name 只认 DISPLAY_WORDS（英语词表）——非英语
            # 按钮词与对象名同值重复时保护失效，对象名被改断链。把
            # _WORD_CASE 形态词并入共享词判定（与 F44 的按钮文本证据
            # 同源），非英语按钮词同样受对象名保护。
            shared_with_name = (
                has_ui_evidence and not in_control_state
                and (stripped.casefold() in DISPLAY_WORDS
                     or _WORD_CASE.match(stripped))
                and counts.get(stripped, 1) >= 2)
            if (_has_sentence_shape(stripped)
                    or (has_ui_evidence and not in_control_state
                        and (stripped.casefold() in DISPLAY_WORDS
                             # F44（dinofurie 实证）：按钮对象（控制状态
                             # ≥3 = 真实 UI 控件）里的单词式词也是按钮
                             # 文本——'Jugar'（西语"玩"）不在白名单
                             # （白名单是英语词表），_WORD_CASE 单词式
                             # 形态是显示文本证据
                             or _WORD_CASE.match(stripped))
                        and not shared_with_name)):
                reason = ("natural_language_in_code_object"
                          if _has_sentence_shape(stripped)
                          else "code_heavy_display_word")
                confidence, role = "medium", "display"
            else:
                entry.status = STATUS_SKIPPED
                reason = ("object_name_shared_word" if shared_with_name
                          else "code_heavy_identifier")
                confidence, role = "low", "structural"
        elif ((is_core_menu_collection or is_core_menu_control)
              and stripped.casefold() in CORE_MENU_SOURCE_TERMS):
            entry.status = "pending"
            reason = (
                "core_menu_collection" if is_core_menu_collection
                else "core_menu_control")
            confidence, role = "high", "display"
        elif _has_sentence_shape(stripped):
            reason = "natural_language"
            confidence, role = "medium", "display"
        elif has_value_evidence:
            reason = "object_has_display_evidence"
            confidence, role = "medium", "display"
        elif _IDENTIFIER.match(stripped):
            if (is_word_list_object and _WORD_CASE.match(stripped)
                    and stripped.casefold() not in _UNITY_CONTROL_STATE_NAMES):
                # F39：命名列表对象中的单词式词 = 显示文本
                # （武器/物品/地点目录）；UGUI 状态名（Normal 等）
                # 即使占比达标也是控件状态串，保持跳过（F38 同语义）
                reason = "word_list_object"
                confidence, role = "medium", "display"
            elif (stripped.casefold() in DISPLAY_WORDS
                    and stripped.casefold() not in control_states
                    and (direct_code_signal_count >= 1
                         or ui_control_signal
                         or ui_word_signal)
                    # 对象名共享词保护（2026-08-15 多游戏实证：按钮对象
                    # m_Name 与 m_text 同值重复——同值 ≥2 处全组跳过
                    # 保留原文，防写回改对象名致按钮失灵/游戏卡住；
                    # 单次出现照常翻译）
                    and counts.get(stripped, 1) < 2):
                # 显示词白名单仅在「真实组件对象」中优先于通用标识符规则
                # （0.25.0 地毯式实证：a-catfiends 的 Save/Load/Rewind 按钮
                # 在「单显示词+类型引用」对象（MonoBehaviour 组件实例）里被
                # 通用标识符规则误杀——组件含 type_reference 信号 = 组件实例
                # 证据，其白名单词是按钮文本；无组件信号的纯字符串对象
                # （绑定名 down/left/right）中白名单词仍是键，维持跳过）。
                # UI 控件词缀信号（ui_control_signal，the-supper 实证
                # QuitButton 对象里的 'Quit'）与白名单显示词信号
                # （ui_word_signal，UIMenu 面板 'Pause'+'Menu' 实证）与
                # 组件信号同等权重——对象名含 Button/Label 或对象词串
                # 是白名单 UI 词即 UI 元素配置，白名单词是按钮/标签文本。
                reason = "display_phrase"
                confidence, role = "medium", "display"
            else:
                entry.status = STATUS_SKIPPED
                entry.meta["obj_is_key_list"] = True
                reason = ("object_name_shared_word"
                          if (stripped.casefold() in DISPLAY_WORDS
                              and counts.get(stripped, 1) >= 2)
                          else "identifier_without_display_evidence")
                confidence, role = "low", "structural"
        else:
            reason = "display_phrase"
            confidence, role = "medium", "display"
        entry.meta.update({
            "confidence": confidence,
            "role": role,
            "disposition": "translate" if role == "display" else "structural",
            "reason": reason,
            "obj_is_code_heavy": is_code_heavy,
            "obj_is_unityevent": is_unityevent_object,
            "obj_is_input_axis": (
                is_input_axis_object or is_cinemachine_object
                or stripped.casefold() in _UNAMBIGUOUS_AXIS_NAMES),
        })
    for e in entries:
        e.meta["obj_has_values"] = has_value_evidence
    _finalize_skipped_counts(entries, prefilter_counts)
    return entries


# ── KV 词典 TextAsset（多语言词典集合，electric-trains 实证） ──
# electric-trains 内置 19 张同结构词典（意/匈/德/西/乌/俄/日/韩/中/
# 葡/英，全部名为 dictionary/dictionary old/dictionary_old/dictionary
# veryold）——旧管线 19 张全提取，同一批 ~356 个键翻译 19 遍（键也被
# 译坏：missions= 被改成 任务=，运行时按键查找断裂）。
# 两条通用规则：
# 1. 组内只提取第一张表（游戏默认源语言表），其余 skipped 留档
#    （reason=textasset_locale_table_<脚本>，报告可见）；
# 2. KV 行只提取值（键保真——键是运行时查找键）。
_KV_LINE = _re.compile(
    r"^(?P<key>[^=:\t\r\n]+?)\s*[:=]\s*(?P<value>.*)$")
_KV_DICT_MIN_LINES = 5
_KV_DICT_MIN_RATIO = 0.8
_DICT_NAME_VARIANT = _re.compile(r"[\s_](?:very)?old$")
_CJK_CHAR = _re.compile(r"[一-鿿]")
_KANA_CHAR = _re.compile(r"[぀-ヿ]")
_HANGUL_CHAR = _re.compile(r"[가-힯]")
_CYRILLIC_CHAR = _re.compile(r"[Ѐ-ӿ]")


def _looks_like_kv_dictionary_text(script: bytes) -> bool:
    """KV 词典字节探测（非 UTF-8/二进制内容不构成词典）。"""
    try:
        text = script.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return False
    return _looks_like_kv_dictionary(text)


def _looks_like_kv_dictionary(text: str) -> bool:
    """key=value 行占比 ≥80% 且 ≥5 行，且键含字母（词典键是标识符；
    数据行 '0:12:-1:none' 的数字键不是词典——fp_level_* 实证，
    该形态由 alpha-density 数据过滤负责）。值必须非空——UI 标签行
    'ДОСТУПНЫЕ ОЧКИ:'（冒号结尾）匹配 key: 形态但值是空的，不是
    词典行（electric-trains 实证，误判会整表值丢失）。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < _KV_DICT_MIN_LINES:
        return False
    kv_lines = []
    for ln in lines:
        m = _KV_LINE.match(ln)
        # 词典键是单词（无空白）——'Уважаемый игрок: текст' 类句子的
        # 冒号不构成词典行（键含空格，electric-trains Rustore 通知实证）
        if (m is not None and m.group("value").strip()
                and not any(ch.isspace() for ch in m.group("key"))):
            kv_lines.append(m)
    if len(kv_lines) / len(lines) < _KV_DICT_MIN_RATIO:
        return False
    letter_keys = sum(
        1 for m in kv_lines
        if any(ch.isalpha() for ch in m.group("key")))
    return letter_keys / len(kv_lines) >= _KV_DICT_MIN_RATIO


def _dictionary_base_name(name: str) -> str:
    """'dictionary' / 'dictionary old' / 'dictionary_old' /
    'dictionary veryold' → 同一逻辑组 'dictionary'。"""
    return _DICT_NAME_VARIANT.sub("", name.casefold())


def _dictionary_language(values: list[str]) -> str:
    """词典值脚本 → zh/ja/ko/ru/latin（组内非源表 skip 留档用）。"""
    samples = [v for v in values[:50] if v]
    if not samples:
        return "latin"
    joined = "".join(samples)
    total = max(1, len(joined))
    if len(_CJK_CHAR.findall(joined)) / total >= 0.3:
        return "zh"
    if len(_KANA_CHAR.findall(joined)) / total >= 0.2:
        return "ja"
    if len(_HANGUL_CHAR.findall(joined)) / total >= 0.3:
        return "ko"
    if len(_CYRILLIC_CHAR.findall(joined)) / total >= 0.3:
        return "ru"
    return "latin"


# 英文功能词（拉丁表英文识别用）：意/德/西/匈等语言的句子不含这些词，
# 英文表的值普遍命中——用户指令「多语言游戏语言优先翻译英文」
# （2026-08-16），与 _prefer_source_locale_bundles 的英文源表先例一致。
_ENGLISH_FUNCTION_WORDS = frozenset({
    # ≥3 字母高频英文功能词，且罗曼语系罕见——意/西/德/匈语句子不命中
    # （意大利语 'e'=和、'in'=在 与英文功能词同形，1-2 字母词不收录）
    "the", "you", "your", "are", "and", "not", "with", "this", "that",
    "for", "have", "has", "will", "can", "was", "but", "when", "from",
    "they", "were", "been", "would", "should", "our", "them", "their",
    "than", "what", "which", "who", "how", "why", "because", "about",
    "into", "over", "under", "after", "before", "through", "also",
    "very", "much", "more", "most",
})
_WORD_TOKEN = _re.compile(r"[a-z]+")


def _english_score(values: list[str]) -> float:
    """值中英文功能词命中率（拉丁表英文判定分）。"""
    samples = [v.casefold() for v in values[:80] if v]
    if not samples:
        return 0.0
    hits = sum(
        1 for v in samples
        if _ENGLISH_FUNCTION_WORDS & set(_WORD_TOKEN.findall(v)))
    return hits / len(samples)


_ENGLISH_SCORE_MIN = 0.15  # 组内英文表命中率下限（低于则无英文表）


def _textasset_kv_entries(
        file_id: str, obj_path_id: int, text: str,
        asset_file_name: str = "",
        skipped: dict[str, int] | None = None) -> list[TextEntry]:
    """KV 词典条目：只提取值（键保真——键是运行时按键查找键）。

    写回走 textasset_format=kv 分支（apply_format_text），键与行结构
    保留，只替换值部分。
    """
    prefix = (f"asset#{asset_file_name}#{obj_path_id}"
              if asset_file_name else f"asset#{obj_path_id}")
    entries: list[TextEntry] = []

    def _skip(morph: str) -> None:
        if skipped is not None:
            skipped[morph] = skipped.get(morph, 0) + 1

    for i, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        m = _KV_LINE.match(stripped)
        if m is None:
            _skip("textasset_kv_nonkv_line")
            continue
        key = m.group("key").strip()
        value = m.group("value").strip()
        if not value:
            _skip("textasset_kv_empty")
            continue
        if should_skip(value) or is_hard_structural(value):
            _skip("textasset_kv_structural")
            continue
        meta = {
            "kind": "textasset", "obj": obj_path_id,
            "textasset_format": "kv",
            "inner_path": f"kv/{quote(key, safe='')}/{i}",
            "line": i, "kv_key": key,
            "confidence": "high", "role": "display",
            "disposition": "translate",
            "reason": "textasset_kv_value",
        }
        if asset_file_name:
            meta["asset_file"] = asset_file_name
        entries.append(TextEntry(
            file_id=file_id,
            key_path=f"{prefix}/line/{i}",
            original=value, meta=meta))
    return entries


# 词库型 TextAsset 判定（0.26 地毯式实证：force-reboot 脏话检测黑名单）。
# 单词行 = 无空格纯 ASCII 词（字母/数字/常见词内符号，≤40 字符）。
_LEXICON_WORD_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9'_.-]{0,39}$")
# 列表项标记（F37，78-hour-rain 实证）：符号+≤3 字符短 token
# （'- a' 被模型回显恒败 target_script_mismatch）——确定性形态跳过
_LIST_MARKER = _re.compile(r"^[-•*–—]\s+[A-Za-z0-9]{1,3}$")
# 词表对象单词 token（happy-cat-tavern 实证 2026-08-12：打字游戏单词库
# 条目形态——纯字母单词，无空格/符号/数字）。
_WORD_TOKEN_RE = _re.compile(r"^[A-Za-z]+$")
_LEXICON_MIN_LINES = 30        # 少于 30 行不做词库判定（防误伤短名单/词典）
_LEXICON_MIN_RATIO = 0.90      # 单词行占比阈值（对话/字幕句行必含空格）


def _is_lexicon_word(s: str) -> bool:
    return bool(s and _LEXICON_WORD_RE.match(s))


# 消息脚本命令 token（fromivan 实证 2026-09-01）：'RECEIVED_MSG|Hey, kiddo!'
# 逐行对话脚本的 '|' 左列是引擎指令（DELAY/TYPING/RECEIVED_MSG/SENT_MSG/
# WAIT_FOR_PRESS/OPEN_IF/OPEN…）。形态 = 全大写字母 + 可选下划线连词
# （命令名），翻译写坏对话时序。右列是玩家可见对话内容。
# 只用全大写形态（不用 TitleCase——'PlayerName'/'Settings' 等普通词会
# 误命中，防真实词典 CSV 被误判消息脚本）。
_MSG_SCRIPT_COMMAND = _re.compile(r"^[A-Z]{2,}(?:_[A-Z]{2,})*$")


def _is_msg_script(text: str) -> bool:
    """消息脚本判定：'|' 分隔行 ≥2 行，且「命令列全大写」占比 ≥80%
    （左右列都可判定，命令 token 形态无论行位置都命中——fromivan 实测
    全语料左列 8 种命令 100% 命中，词典表/列表 CSV 无此信号）。
    右列含对话内容（小写单词）不参与判定，整文件仍判消息脚本走 line
    拆分——命令+内容单行处理，写回按行号重建命令前缀。
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    pipe_lines = [ln for ln in lines if "|" in ln]
    if len(pipe_lines) < 2:
        return False
    hits = 0
    for ln in pipe_lines:
        for part in ln.split("|"):
            part = part.strip()
            if part and _MSG_SCRIPT_COMMAND.match(part):
                hits += 1
                break
    return hits / len(pipe_lines) >= 0.8


def _msg_dialogue_content(line: str) -> str:
    """消息脚本行的对话内容（'|' 后首个非命令列），无则返回 ''。

    'RECEIVED_MSG|Hey, kiddo!' → 'Hey, kiddo!'；'DELAY|1'（右列纯数字）
    /'OPEN_IF|INDEPENDENT|FRIEND|Set4-Friend-N'（右列全命令/标识符）→ ''。
    命令列（全大写）跳过；含数字/段内连字符下划线的标识符列跳过（分支
    参数名，翻译破坏流程）；真对话 = 含空格句子 / 句末标点 / 纯字母词。
    句末标点（'.'）不视为标识符符号（'Please tell me there was some
    improvement.' 是真句子）。
    """
    for part in line.split("|"):
        part = part.strip()
        if not part or _MSG_SCRIPT_COMMAND.match(part):
            continue
        if any(ch.isdigit() for ch in part):
            continue
        core = part.rstrip(".!?…")
        if not core:
            continue
        if any(ch in "-_." for ch in core):
            continue
        if any(ch.isalpha() for ch in core):
            if " " in core or part[-1:] in ".!?…" or all(
                    ch.isalpha() for ch in core):
                return part
    return ""


def _is_msg_script_line(line: str) -> bool:
    """单行消息脚本判定：任一 '|' 分隔列命中命令 token 形态。"""
    for part in line.split("|"):
        part = part.strip()
        if part and _MSG_SCRIPT_COMMAND.match(part):
            return True
    return False


def _textasset_entries(file_id: str, obj_path_id: int, raw: bytes,
                       asset_file_name: str = "",
                       skipped: dict[str, int] | None = None,
                       csv_overwrite_source: bool = False) -> list[TextEntry]:
    """TextAsset 内容：嵌套格式探测（JSON → XML → YAML → CSV），否则按行拆分。

    结构化条目 meta 带 "textasset_format"（写回用 apply_format_text 整体重建，
    m_Script 是可变长 byte[]，不受容量限制）与 "inner_path"（裸格式路径）。

    文件级源码检测（a-catfiends-impending-relapse 实证：resources.assets#69
    整文件是 inspect.lua 脚本库，被按行拆成 264 条进池、模型翻译代码被
    质量门拦截成 264 条失败）：代码特征行占比 ≥30% 且 ≥8 行 → 整文件
    按代码处理不产生条目（代码文本翻译即破坏功能，属于硬结构规则）。
    """
    if _looks_like_script_source(raw):
        return []
    # Spine 图集文件（.atlas，soul-delivery/monsters-of-new-spark 实证
    # 2026-09-01）：1000.png\nsize: 2048,1024\nformat: RGBA8888\nfilter:
    # Linear,Linear\nrepeat: none 开头 + Defulat/Body 式皮肤路径行——值是
    # 纹理名/皮肤路径/uv 数值，全机器引用。yaml 分支会把这些按行号进池，
    # 翻译写坏精灵加载。头部三键签名整文件跳过（皮肤路径行还可能是任意
    # '文件夹/子名' 形态，只按行前缀拦会漏）。
    _HEAD = raw[:256]
    if _HEAD.count(b"size: ") >= 1 and b"format: RGBA" in _HEAD and b"filter: " in _HEAD:
        skipped["textasset_spine_atlas"] = skipped.get("textasset_spine_atlas", 0) + 1 if skipped else 1
        return []
    prefix = (f"asset#{asset_file_name}#{obj_path_id}"
              if asset_file_name else f"asset#{obj_path_id}")
    base_meta = {
        "obj": obj_path_id,
        "confidence": "medium",
        "role": "display",
        "disposition": "translate",
        "reason": "textasset_display_text",
    }
    if asset_file_name:
        base_meta["asset_file"] = asset_file_name
    import json as _json
    # 二进制 TextAsset 过滤（嵌套探测之前，省去大文件 decode）：
    # 非可打印字节（\x00-\x1f 除 \t\r\n）占比 >5% → 音频/网格/压缩等
    # 二进制内容（调查实证：electric-trains fp_level_*、2.1G 字符的
    # project-arrhythmia 巨型 TextAsset 中混有二进制），不做条目。
    def _skip(morph: str) -> None:
        if skipped is not None:
            skipped[morph] = skipped.get(morph, 0) + 1

    # B16（snowday 对话表漏提根因 2026-09-05）：UTF-16 TextAsset 整文件
    # 被 textasset_binary 吞掉——UTF-16LE/BE 每隔一字节一个 \x00，NUL
    # 占比 ~0.5 > 0.05 阈值，下面的二进制过滤在 decode 之前先命中。
    # 实证：DialogueStructure（resources.assets#46，UTF-8 BOM + UTF-16LE
    # 双 BOM 头 b'\xef\xbb\xbf\xff\xfe'），134 条西班牙语对话全部漏提。
    # 修复：二进制过滤**之前**探测 UTF-16 BOM（含 UTF-8 BOM 前缀的双
    # BOM 形态），命中则剥 BOM 按 UTF-16 解码进既有 JSON/XML/YAML/CSV
    # 格式链；解码失败/替换字符过多保持既有安全跳过（宁漏勿坏——写回
    # 侧 _patch_textasset 对称支持，BOM 形态留档 meta 供审计）。
    utf16_encoding = ""
    head = raw[:5]
    if head.startswith(b"\xef\xbb\xbf\xff\xfe"):
        utf16_encoding = "utf-16-le-bom8"   # 双 BOM：UTF-8 BOM + UTF-16LE
    elif head.startswith(b"\xef\xbb\xbf\xfe\xff"):
        utf16_encoding = "utf-16-be-bom8"   # 双 BOM：UTF-8 BOM + UTF-16BE
    elif head.startswith(b"\xff\xfe\x00\x00") or head.startswith(b"\x00\x00\xfe\xff"):
        pass                                # UTF-32 BOM：Unity 不产，走二进制
    elif head.startswith(b"\xff\xfe"):
        utf16_encoding = "utf-16-le"
    elif head.startswith(b"\xfe\xff"):
        utf16_encoding = "utf-16-be"
    if utf16_encoding:
        payload = raw
        if utf16_encoding.endswith("bom8"):
            payload = raw[3:]               # 剥 UTF-8 BOM 前缀再解码
        try:
            text = payload.decode("utf-16")  # BOM 由解码器自动识别移除
        except (UnicodeDecodeError, UnicodeError):
            _skip("textasset_utf16_decode_failed")
            return []
        replacement_chars = sum(1 for ch in text if ch == "�")
        if replacement_chars > len(text) * 0.02:
            # 替换字符过多 → 不是真 UTF-16 文本（BOM 巧合），按二进制跳过
            _skip("textasset_binary")
            return []
        base_meta["textasset_encoding"] = utf16_encoding
        _skip("textasset_utf16_detected")   # 形态计数留档（报告可见）
    elif raw and sum(1 for b in raw if b < 0x20 and b not in (0x09, 0x0a, 0x0d)) / len(raw) > 0.05:
        _skip("textasset_binary")
        return []
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            # F3：非 UTF-8 文本（GBK/Latin-1 等编码误判）。errors="replace"
            # 会把非法字节变成 U+FFFD，提取出的条目是 mojibake，翻译写回
            # 必然损坏原始字节——整文件不产生条目（过滤不是删除，写回侧
            # 同样 strict 拒绝，闭环安全）。
            _skip("textasset_decode_failed")
            return []
    # 双重 BOM 处理（a-catfiends obj70 实证）：UnityPy 读出的 str 已含
    # U+FEFF，调用方 encode("utf-8-sig") 又加一个 → decode 只移除一个，
    # 残留 BOM 卡住 JSON 分支（startswith("{") False），类型注册表 JSON
    # 被按行拆分、代码标识符进池。lstrip 前先彻底剥掉 BOM。
    if text.startswith("﻿"):
        text = text[len("﻿"):]
    stripped = text.lstrip()
    def _stamp(out: list, fmt: str) -> list:
        # 结构化格式（JSON/XML/YAML/CSV）提取的条目统一过单行代码判定：
        # .NET 类型注册表（registerTypes 数组）里的类型全名经 JSON 提取后
        # 无引号、会被句子形状规则放行（a-catfiends obj71 实证 8 条失败）。
        # 真实对话 JSON（字典/字幕）不受影响（模式为确定性代码特征）。
        # YAML 例外（Rendezvous 实证 2026-08-17）：yaml 写回按行号整行
        # 重建，过滤掉任何一行（表头/结构行被 _is_script_code_line 命中，
        # 如 ' ,IND,ENG,...' 逗号分隔大写列名）→ 重建丢行 → 游戏解析
        # 越界黑屏。yaml 条目必须全部保留（宁漏勿坏）；json/csv 按
        # key/row 写回，缺条目只意味着该行不写，无结构破坏。
        kept = [e for e in out
                if fmt == "yaml" or not _is_script_code_line(e.original)]
        if len(kept) != len(out):
            _skip(f"structured_code_line_{fmt}")
        out = kept
        for e in out:
            inner = e.key_path
            e.key_path = f"{prefix}/{fmt}/{inner}"
            e.meta = {**base_meta, **e.meta,
                      "textasset_format": fmt, "inner_path": inner}
        return out

    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = _json.loads(stripped)
        except Exception:  # noqa: BLE001
            data = None
        if data is not None:
            if _is_spine_document(data):
                # Spine 骨骼动画 JSON（soul-delivery/zero-deaths/
                # monsters-of-new-spark 实证）：skeleton/bones/slots/ik/
                # skins/animations 全键查表引用，整文件跳过并留档。
                _skip("textasset_json_spine")
                return []
            if _is_pachat_document(data):
                # PAChat 终端脚本 JSON（project-arrhythmia 实证 2026-09-01）：
                # {settings, branches} 顶层，分支名/命令 token/settings 配置
                # 是机器引用，真显示文本逐条混杂只占少部分 → 整文件跳过
                # （宁漏勿坏，防译坏分支跳转/命令解析）。判定与 Spine 同款。
                _skip("textasset_json_pachat")
                return []
            if _is_perftest_document(data):
                # Unity PerformanceTestRunInfo JSON（minato 实证 2026-09-02）：
                # Player/Editor 渲染与构建配置 + Dependencies 包名列表——
                # 机器元数据，整文件跳过（RenderThreadingMode Split/Version
                # 6000.3.11f1/Branch 6000.3/staging 曾被当显示文本进池）。
                _skip("textasset_json_perftest")
                return []
            return _stamp(json_format.extract_json_text(stripped, file_id), "json")
    if stripped.startswith("<") and ">" in stripped:
        from hanhua.core.formats.xml_format import extract_xml_text
        try:
            out = extract_xml_text(stripped, file_id)
        except Exception:  # noqa: BLE001
            out = None  # 非良构 XML（如 < 开头的纯文本），落到按行拆分
        if out is not None:
            # 良构 XML 即使零条目也返回结构化结果（不落到按行拆分）：
            # operation-ops PlayerStats 实证 2026-09-01——单行 XML 文档
            # 的叶子（name=Player/hp=3/speed=8…）全部被机器值过滤合法跳过
            # 后 out 为空，若不返回会落到 line 拆分把整段 XML 当一个显示
            # 文本条目进池，翻译即毁掉整个存档 XML。
            return _stamp(out, "xml")
    from hanhua.core.formats.csv_format import extract_csv_text, looks_like_csv_text
    from hanhua.core.formats.yaml_format import extract_yaml_text, looks_like_yaml_text
    # CSV 判定优先（Rendezvous 实证 2026-08-17）：多语言词典表的对话行
    # 含冒号（'SeaWall_D1,Arum: Apa kau...'）命中 YAML kv 模式 → 误判
    # yaml → 表头行被过滤 → 重建丢行 → 游戏 CSVParser 越界黑屏。CSV 是
    # 行列宽度一致的表结构，判定更确定，必须排在 yaml 之前。
    # 例外：消息脚本（fromivan 实证 2026-09-01：'RECEIVED_MSG|Hey, kiddo!'
    # 逐行对话脚本）也是「每行 2 列」的一致宽度表，会被 looks_like_csv_text
    # 命中——但 '|' 左列是引擎命令（DELAY/TYPING/RECEIVED_MSG/…），翻译
    # 写坏对话时序。整文件命令列全大写占比判定 → 走 line 拆分（命令+内容
    # 单行处理，写回按行号重建命令前缀）。
    if looks_like_csv_text(text) and not _is_msg_script(text):
        out, _ = extract_csv_text(text, file_id, overwrite_source=csv_overwrite_source)
        return _stamp(out, "csv")
    if looks_like_yaml_text(text):
        return _stamp(extract_yaml_text(text, file_id), "yaml")
    if _looks_like_kv_dictionary(text):
        # KV 词典（多语言词典/配置表）：只提取值（键保真——键是运行时
        # 按键查找键，整行翻译会改键断查找，electric-trains 实证
        # missions= 被译成 任务=）
        return _textasset_kv_entries(file_id, obj_path_id, text,
                                     asset_file_name, skipped)
    # 数据文件过滤：字母密度 <50% 的行占多数 → 关卡/配置数字表
    # （fp_level_* 的 "0:12:-1:none" 行 36% 字母实证），不做条目。
    # 真文本（字典/字幕）行字母密度高（missions=Missioni ≈88%），不误伤。
    all_lines = text.splitlines()
    if all_lines:
        alpha = sum(
            1 for ln in all_lines
            if sum(c.isalpha() for c in ln) / max(1, len(ln)) >= 0.5)
        if alpha / len(all_lines) < 0.5:
            _skip("textasset_low_alpha_density")
            return []
        # 数字密集行补充过滤（electric-trains 实证 2026-09-02）：fp_level_*
        # 列车调度表与 mission_*_targets 关卡目标表——行是「数字冒号段:资源名」
        # 调度结构（'2:-1:-1:FreeTrain_v14_hopper'），无玩家可见文本，却被当
        # textasset_display_text 逐行进池（135+ 条/文件，模型把 FreeTrain_…
        # 模型名音译/乱译写回后列车加载失败）。单行判定：**数字占比 ≥15%**
        # 或 **≥3 段数字冒号分隔**（'0:29:-1:Name' 数字比例 12-14% 但冒号
        # 结构确定是配置）→ 配置行。真字幕/对话（'Hello, how are you today?'/
        # 'Level 1 complete!'）无冒号段，不命中。整文件配置行占比过半 →
        # 数字/调度表，不做条目（fp_level 实测 98%、mission_15_targets 5/5）。
        digit_lines = sum(
            1 for ln in all_lines
            if sum(c.isdigit() for c in ln) / max(1, len(ln)) >= 0.15
            or (sum(c.isdigit() for c in ln) >= 3
                and ln.count(":") >= 2))
        if digit_lines / len(all_lines) >= 0.5:
            _skip("textasset_digit_dense_data")
            return []
    # 词库型 TextAsset（0.26 地毯式实证：force-reboot data.unity3d#obj268
    # 是脏话检测黑名单——1100+ 行全英文短词，被当显示文本 974 条全翻译
    # 写回，游戏过滤逻辑失效）：单词行（无空格纯词）占比 ≥90% 且 ≥30 行
    # 的纯词表是比对数据（黑名单/词典/名单），非显示文本——对话/字幕必
    # 有句子结构（空格/标点），占比远低于阈值；短名单（<30 行）不判定防
    # 误伤，且正常短名单（missions=Missioni 等含 = 的行）不匹配单词行。
    if (_LEXICON_MIN_LINES
            and len(all_lines) >= _LEXICON_MIN_LINES):
        lexicon_lines = sum(
            1 for ln in all_lines if _is_lexicon_word(ln.strip()))
        if lexicon_lines / len(all_lines) >= _LEXICON_MIN_RATIO:
            _skip("textasset_lexicon")
            return []
    lines: list[TextEntry] = []
    # 消息脚本文件判定（fromivan 实证 2026-09-01）：整文件 '|' 左列命令
    # token 占比 ≥80% → 是逐行消息脚本（非 CSV 表）。行级处理：命令+
    # 内容单行进池，命令前缀原样保留；无内容的命令行（DELAY|1）跳过。
    msg_script_file = _is_msg_script(text)
    for i, line in enumerate(all_lines):
        content = line.strip()
        if not content:
            continue
        # 消息脚本行：'RECEIVED_MSG|Hey, kiddo!'——命令列（RECEIVED_MSG/
        # DELAY/TYPING…）是引擎解析指令（翻译写坏对话时序），'|' 后的内容
        # 才是玩家可见对话。整行进池会让模型把命令一起翻译——只在右列有
        # 真内容时进池，保留原行（含命令前缀），写回按行号重建。
        if msg_script_file and "|" in content and _is_msg_script_line(content):
            _rhs = _msg_dialogue_content(content)
            if not _rhs:
                _skip("textasset_msg_command")
                continue
            lines.append(TextEntry(
                file_id=file_id, key_path=f"{prefix}/line/{i}",
                original=content,
                meta={**base_meta, "kind": "textasset", "line": i,
                      "msg_script": True}))
            continue
        # C4 识别侧留档：行级跳过的每一类都记 skipped_count（引擎串/键
        # 标识符/代码行），不再静默 continue——「纯文本行跳过、判定规律
        # 未定位到代码层」（222am 实证）的排查入口：报告按 reason 聚合
        # 即得各类真实总数，哑识别可见化。
        if _is_engine_string(content):
            _skip("textasset_engine_string")
            continue
        if should_skip(content):
            _skip("textasset_key_identifier")
            continue
        if is_vn_command_line(content):
            _skip("textasset_vn_command")
            continue
        # 整文件未达代码阈值（<8 行或占比不足）时的行级代码兜底：
        # 短 Lua 块/单行调用仍按代码跳过（_is_script_code_line 强特征）
        if _is_script_code_line(content):
            _skip("textasset_code_line")
            continue
        lines.append(TextEntry(
            file_id=file_id, key_path=f"{prefix}/line/{i}",
            original=content,
            meta={**base_meta, "kind": "textasset", "line": i}))
    return lines


# ── ink 对话脚本（Inkle 引擎） ──
# Rendezvous 实证：ink JSON（{"inkVersion":19,...}）含对话行与流程结构
# 混合——"done"/"end" 是流程控制词（翻译破坏对话流程）、"->" 键的值是
# divert 跳转目标（翻译断跳转）、#f/#n/#c 标签键的值是流程元数据。
# 只有 "^" 前缀对话行与纯文本值（choice 文本/变量行）是显示文本。
# ink 流程控制词全集（编译产物特殊字符串值，翻译破坏对话流程）：
# done/end = 流程结束；out = 对话块出口标记（Rendezvous 实证 2026-08-17：
# 各块固定位置 'out' 被当对话行译成「出去」）。ink 玩家选项文本带 *
# 前缀（"* 出去"），纯 "out" 值必是流程结构。
_INK_CONTROL_WORDS = frozenset({"done", "end", "out"})
_CYRILLIC_MIN = 0x0400

# ink 编译产物裸流程 token（无 "^" 前缀、list 内独立字符串值，翻译破坏对话
# 流程——project-arrhythmia 实证 2026-09-01：10 个 ink 对象每个被 291 条裸
# 流程串泄漏成「待翻译」）。全集 68 token 由 .scratch/ink_tokens_gen.py 对
# D:\游戏 全语料 6296 条裸串自动聚类生成，非手写。分类：
#   ev/du/pop/nop/str//str 等 = 字节码操作；done/end/out = 流程结束/出口；
#   GetVar/ChangeVar + _id/_locX/_delay 等 = 变量读写指令与临时变量名；
#   spawnActor/setEmotion/setStance/setEyes/setMaterial/setGlitch/setChoiceTitle
#   /blurUI/swapToUI/jumpToUI/triggerUI/triggerVar/unpauseTimeline/loadScene/
#   wait/ChantSpell/MoveToPlace/StartShop/... = 自定义 handler 名（root[2]
#   键与 flow token 完全一致——handler 调用以裸字符串作为字节码操作数）。
# 任何缺失的流程词若以裸形态出现会走下方 fail-open（未知单 token 保留），
# 不会静默吞掉真对话。
_INK_BARE_FLOW_TOKENS = frozenset({
    # 字节码栈操作（编译产物）
    "ev", "/ev", "str", "/str", "du", "pop", "nop", "out",
    # 流程控制（Rendezvous 实证）
    "done", "end",
    # 变量读写指令
    "GetVar", "GetVarArray", "ChangeVar", "ChangeVarArray",
    # 临时/局部变量名（handler 参数占位）
    "_id", "_locX", "_locY", "_delay", "_lifetime", "_amount", "_open",
    "_emotion", "_stance", "_overlay", "_material", "_title", "_value",
    # 游戏自定义 handler 调用名（与 root[2] 键一致）
    "spawnActor", "hideActor", "showActor", "setStance", "delayStance",
    "setEyes", "unsetEyes", "setEmotion", "addOverlay", "delOverlay",
    "tmpOverlay", "setMaterial", "setGlitch", "swapToUI", "jumpToUI",
    "triggerUI", "triggerVar", "unpauseTimeline", "setChoiceTitle",
    "blurUI", "unblurUI", "loadScene", "wait",
    "ChantSpell", "MoveToPlace", "StartShop", "ShopStatus", "OpenTeleportMenu",
    "FunctionCaller", "EventInvoke", "GoToDiscord", "GoToSteam", "SetFeedback",
    "UnlockFixingCatapult", "FetchItemFromKimoInventory", "FaceToKimo",
    "GetOtherVarBool", "IsFinCompleted", "IsMeetWispy", "IsMeetWispyAndTalya",
    "IsNewSpellAvailable", "CheckArchive", "CheckCharm", "CheckCollectedArchive",
    "CheckCurrentArchive", "CheckTrophy", "GiveFin", "ObtainPureWater",
    "current_archive", "multiplayer",
})

# ink 运行时标识符块（str.../str 与 #.../#，list 内配对）：块内 "^" 值不是
# 对话。project-arrhythmia 实证 2026-09-01——str 块内是 runtime 标识符：
#   ^hal/^Busy/^Panels/^focused/^angry/^Neutral/^atan/^go-to-lucentia
#   /^later/^Demo_WIP/^energy-shell/^life-shell = 动画/姿态/情绪/面板/覆盖层
#   查找键（spawnActor/setStance/setEmotion/addOverlay 的实参，音视频状态机
#   按名查表）；str 块内 choice 文本（^rt.tonn.02.A$ What are you doing here?）
#   带 $ 前缀引用，$ 是 divert 目标标记（$ 前后都保留，翻译只动 $ 后显示
#   文本，参见 Choice Entry 引用 `.^.c-0`——玩家选它跳转的注册目标）。
# tag 块（#.../#）内是标签元数据（^actor:PM_25.01/^auto:2）与 handler 定义
# 的运行时命令模板（^Spawn ->/^Hide ->/^To -> UI/^Blur -> None——游戏
# 解析命令字符串执行时间线跳转/UI 切换，翻译必坏）。str/tag 块配对检测用
# _ink_str_tag_spans（兄弟上下文，非路径）。
_INK_BLOCK_WORDS = frozenset({"str", "#"})
_INK_BLOCK_CLOSERS = frozenset({"/str", "/#"})


def _ink_line_localized(line: str) -> bool:
    """对话行是否已本地化（CJK/西里尔主导）——语言版文件整跳过。"""
    if not line:
        return False
    cn = sum(1 for c in line if "一" <= c <= "鿿")
    jp = sum(1 for c in line if 0x3040 <= ord(c) <= 0x30ff)
    ru = sum(1 for c in line if _CYRILLIC_MIN <= ord(c) <= 0x04ff)
    letters = sum(1 for c in line if c.isalpha())
    if letters == 0:
        return False
    return (cn + jp + ru) / letters > 0.35


def _ink_str_tag_spans(seq: list) -> dict[int, str]:
    """list 内 str.../str 与 #.../# 配对的兄弟下标（ink 编译器把标识符块
    与对话行展平成同一 list，块界由配对的 'str'/'#' 与 '/str'/'/#' 标记）。
    返回 {元素下标: 'str'|'#'}。支持同 op 嵌套。project-arrhythmia 实证：
    root[0] 主流程 246 元素里 'str','^hal','/str' 三连；root[2] handler 定义
    （spawnActor/setChoiceTitle/swapToUI…）把命令模板包在 #.../# 里。"""
    members: dict[int, str] = {}
    n = len(seq)
    for i, el in enumerate(seq):
        if not isinstance(el, str):
            continue
        op = el.strip()
        if op not in _INK_BLOCK_WORDS:
            continue
        closer = "/str" if op == "str" else "/#"
        depth = 0
        for j in range(i + 1, n):
            e2 = seq[j]
            if not isinstance(e2, str):
                continue
            s2 = e2.strip()
            if s2 == op:
                depth += 1
            elif s2 == closer:
                if depth == 0:
                    for k in range(i + 1, j):
                        if isinstance(seq[k], str):
                            members[k] = op
                    break
                depth -= 1
    return members


def _ink_entries(file_id: str, obj_path_id: int, raw: bytes,
                 asset_file_name: str,
                 skipped: dict[str, int] | None = None,
                 obj_name: str = "") -> list[TextEntry]:
    """ink 对话 JSON 特判提取：只产出对话行/纯文本值，控制结构与
    语言版文件跳过留档（审计可见）。key_path 用真 JSON 路径（RFC6901），
    写回走 apply_json 原位置替换（控制结构原样保留）。"""
    import json as _json
    from hanhua.core.formats.json_format import _encode_path

    def _skip(morph: str, n: int = 1) -> None:
        if skipped is not None:
            skipped[morph] = skipped.get(morph, 0) + n

    text = raw.decode("utf-8-sig", errors="replace").lstrip("﻿")
    try:
        data = _json.loads(text)
    except Exception:  # noqa: BLE001
        # JSON 损坏 → 不产生条目（宁漏勿坏；ink 结构损坏游戏也无法运行）
        _skip("ink_json_failed")
        return []
    # 语言版判定：对话行（^ 前缀/纯文本）中 CJK/西里尔主导 → 已本地化
    dialogue_lines: list[str] = []
    out: list[TextEntry] = []
    prefix = (f"asset#{asset_file_name}#{obj_path_id}"
              if asset_file_name else f"asset#{obj_path_id}")
    base_meta = {
        "obj": obj_path_id,
        "confidence": "medium",
        "role": "display",
        "disposition": "translate",
        "reason": "ink_dialogue_line",
        "kind": "ink",
        "textasset_format": "json",
    }
    if asset_file_name:
        base_meta["asset_file"] = asset_file_name
    # 语言版对话 base 名（Chapter1_EN → Chapter1）：官方中文搬运对齐用
    # （同源编译的 ink 语言版块内行序一致）。
    # 非英文语言版（Chapter1_ITA/GER/CHN/JPN/RUS…，Rendezvous 实证 2026-08
    # -17）：游戏语言设置只有默认语言（英文）时，只有 EN 版被游戏读取——
    # 其他语言版翻译写回无人读取且浪费翻译量（ITA 版 365 条被进池实证）。
    # 按 m_Name 语言后缀整文件跳过（比内容级 CJK/西里尔判定全面——拉丁
    # 字母语言版如 ITA/GER 内容检测不可分辨）。
    import re as _re
    _ink_name_m = _re.match(r"^(.*?)_([A-Z]{2,3})$", obj_name)
    if _ink_name_m:
        base_meta["ink_base"] = _ink_name_m.group(1)
        if _ink_name_m.group(2) != "EN":
            _skip("ink_non_en_version")
            return []

    seq = 0
    block = ""

    def walk(node: Any, path: tuple, cur_block: str) -> None:
        nonlocal seq
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and k.startswith("#"):
                    continue
                if isinstance(k, str) and k == "->":
                    continue
                walk(v, path + (k,), cur_block)
        elif isinstance(node, list):
            # 兄弟上下文：str/tag 块内 ^ 值 = 运行时标识符/命令模板（非对话）
            block_members = _ink_str_tag_spans(node)
            for i, v in enumerate(node):
                walk(v, path + (i,),
                     block_members.get(i, cur_block))
        elif isinstance(node, str):
            v = node.strip()
            if not v:
                return
            if v in _INK_CONTROL_WORDS or v in _INK_BARE_FLOW_TOKENS:
                _skip("ink_flow_token")
                return
            # divert 目标（"->" 键的值）与标签元数据（#f/#n/#c 键的值）
            for seg in path:
                if (isinstance(seg, str) and seg.startswith("#")) or (
                        isinstance(seg, str) and seg == "->"):
                    _skip("ink_flow_structure")
                    return
            # 结构化数字/纯符号值（ink 计数/标记）
            if not any(c.isalpha() for c in v):
                _skip("ink_non_text")
                return
            if v.startswith("^"):
                body = v[1:]
                if not body:
                    _skip("ink_empty_caret")
                    return
                if cur_block == "#":
                    # tag 块（#.../#）内 ^ = 标签元数据/命令模板（^actor:
                    # PM_25.01/^auto:2/^Spawn ->/^To -> UI/^Blur -> None——游戏
                    # 解析命令字符串执行时间线跳转/UI 切换，翻译必坏）
                    _skip("ink_block_identifier")
                    return
                if cur_block == "str" and " " not in body and "$" not in body:
                    # str 块（str.../str）内单 token 无 $ = 运行时标识符
                    # （^hal/^angry/^Panels/^go-to-lucentia/^atan——spawnActor/
                    # setEmotion/addOverlay 实参，音视频状态机按名查表）。
                    # str 块内带空格或 $ 的是 choice 显示文本（^Choice 1/
                    # ^rt.tonn.02.A$ What are you doing here?）→ 保留。
                    _skip("ink_block_identifier")
                    return
                # 顶层 ^ 值：对话行/credits 名。单 token（^hal 无空格）在
                # 块外保留——faerie obj20 'Credits Roll' 的 ^clay/^Programming
                # /^RavenBane 都是顶层单 token 真显示文本，且含 $ 引用的
                # choice 文本（^rt.tonn.02.A$ What are you doing here?）$ 后
                # 显示文本保留（$ 是 divert 目标标记，只在 $ 前截断引用段）。
                dialogue_lines.append(v)
                out.append(TextEntry(
                    file_id=file_id, key_path=_encode_path(path),
                    original=v,
                    meta={**base_meta, "ink_text": v,
                          "inner_path": _encode_path(path),
                          "ink_block": cur_block, "ink_seq": seq}))
                seq += 1
                return
            # 裸字符串（非 ^ 前缀）
            if "$" in v:
                # $ 前缀 = 寄存器引用（$r/$r1），带 → divert 目标；译断跳转
                _skip("ink_register_ref")
                return
            if "." in v and " " not in v:
                # 点连无空格 = divert/choice 目标引用（.^.c-0、Start.0.g-0.2.
                # $r1），不是对话文本
                _skip("ink_dot_ref")
                return
            if " " in v:
                # 未知裸多词：无法归类为流程 token，fail-open 保留（宁漏勿坏）
                dialogue_lines.append(v)
                out.append(TextEntry(
                    file_id=file_id, key_path=_encode_path(path),
                    original=v,
                    meta={**base_meta, "ink_text": v,
                          "inner_path": _encode_path(path),
                          "ink_block": cur_block, "ink_seq": seq}))
                seq += 1
                return
            # 未知单 token 裸串：不在流程全集 → fail-open 保留（宁漏勿坏；
            # 未知流程词若为真对话会被保留，可观测不吞）
            dialogue_lines.append(v)
            out.append(TextEntry(
                file_id=file_id, key_path=_encode_path(path),
                original=v,
                meta={**base_meta, "ink_text": v,
                      "inner_path": _encode_path(path),
                      "ink_block": cur_block, "ink_seq": seq}))
            seq += 1

    # 对话块名 = 路径中最后出现的字母键段（Setyo_WakeUp 等）；块内行序
    # = 该块下对话行的出现序号（同源编译的 ink 语言版结构一致，块级对齐
    # 搬运官方中文用——Chapter1_CHN → Chapter1_EN，Rendezvous 实证）
    def walk_with_block(node: Any, path: tuple, cur_block: str) -> None:
        nonlocal seq
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and k.startswith("#"):
                    continue
                if isinstance(k, str) and k == "->":
                    continue
                nb = cur_block
                if (isinstance(k, str) and k not in ("root",)
                        and not k.startswith("^")
                        and not k[0].isdigit() and len(k) > 1):
                    nb = k
                    # 进入新对话块：块内行序重置（ink_seq 是块内序号，
                    # 语言版块级对齐搬运用——Chapter1_CHN → EN）
                    seq = 0
                # 递归 walk_with_block 保持块名追踪（list/dict 内嵌套的
                # 对话块）；只有字符串叶子交给 walk 提取
                walk_with_block(v, path + (k,), nb)
        elif isinstance(node, list):
            # 兄弟上下文与 walk 一致（str/tag 块内 ^ = 运行时标识符）
            block_members = _ink_str_tag_spans(node)
            for i, v in enumerate(node):
                walk_with_block(v, path + (i,),
                                block_members.get(i, cur_block))
        elif isinstance(node, str):
            walk(node, path, cur_block)

    walk_with_block(data, (), "")
    if dialogue_lines:
        localized = sum(1 for ln in dialogue_lines if _ink_line_localized(ln))
        if localized / len(dialogue_lines) > 0.5:
            # 语言版文件（CHN/JPN/RUS…）——已本地化不译（Chapter1_CHN 等）
            _skip("ink_localized", len(out))
            return []
    return out


# ── TextAsset 源码检测（整文件代码判定，0.25.0 地毯式排查实证） ──
# 源码特征行模式（Lua/JS/C# 等）：命中任一即算代码特征行。
_SCRIPT_LINE_PATTERNS = (
    # Lua：local/function/end/return 与控制流
    _re.compile(r"^\s*local\s+\w+\s*="),
    _re.compile(r"^\s*function\b"),
    _re.compile(r"^\s*end\s*$"),
    _re.compile(r"^\s*return\s+\w"),
    _re.compile(r"\b(setmetatable|rawset|rawget|pairs|ipairs|gsub|"
                r"coroutine|table\.)\s*\("),
    _re.compile(r"\b\w+\.\w+\s*=\s*function"),
    _re.compile(r"^\s*if\s+.+then\s*$"),
    _re.compile(r"^\s*for\s+.+do\s*$"),
    # Lua 注释（整行注释在 a-catfiends obj72 实证中占 11/49 行，
    # 缺此模式会导致 FungusLua 模块命中率跌破阈值漏判）
    _re.compile(r"^\s*--"),
    # FungusLua 命令（say/choose/wait/runblock/setcharacter 行首命令：
    # 对话混在 Lua 命令中，整文件判定，不逐行提取）
    _re.compile(r"^\s*(?:say|choose|wait|runblock|setcharacter)\b"),
    # JS/C#/Python：声明/作用域/导入
    _re.compile(r"^\s*(?:const|let|var|static|public|private|protected|"
                r"internal)\s+\w+"),
    _re.compile(r"^\s*(?:def|class|struct|interface|namespace|using|"
                r"import|from|require)\b"),
    _re.compile(r"^\s*(?:if|elif|else|for|while|switch|case|catch|"
                r"finally)\b"),
    _re.compile(r"\b=>\s*\{?\s*$"),
    _re.compile(r"^\s*[{}\[\]]\s*$"),          # 裸括号行
    _re.compile(r"[;\s]{1}--\s"),              # Lua 注释
    _re.compile(r"^\s*(?:function|async)\s+[\w.]+\s*\("),
)
_SCRIPT_MIN_CODE_LINES = 8       # 少于 8 行不做代码判定（防误伤短文本）
_SCRIPT_MIN_CODE_RATIO = 0.30    # 特征行占比阈值（inspect.lua 实证 45%）

# 单行级代码特征（整文件级检测的补充，0.25.0 地毯式实证）：
# Fungus 游戏的 Lua 命令块/变量行以单行形式散落在 assets 对象里
# （runblock/setcharacter/local choice/elseif/function M.start()），
# 整文件检测不覆盖（不在 TextAsset 或占比不足）；.NET 类型全名
# （"System.Boolean, mscorlib, Version=2.0.0.0, ..."）与函数签名链
# （InvertVector2(invertX=false),ScaleVector2(...)）也被句子形状规则
# 误放行。这些模式是确定性代码特征，单行命中即判代码（硬结构规则）。
_CODE_LINE_PATTERNS = (
    # Lua 语句：声明/控制流
    _re.compile(r"^\s*local\s+\w+\s*="),
    _re.compile(r"^\s*elseif\b"),
    _re.compile(r"^\s*function\s+[\w.]+\s*\("),
    _re.compile(r"^\s*if\s+.+then\s*$"),
    _re.compile(r"^\s*for\s+.+do\s*$"),
    _re.compile(r"^\s*return\s+\w"),
    _re.compile(r"^\s*--"),                      # Lua 整行注释
    _re.compile(r"\)\s*--\s"),                   # 语句后的行尾注释（不用裸 --\s
                                                 # 防口语破折号 'I -- I can't'）
    # .NET 类型全名（可选前导引号 + 程序集 + Version=；JSON 提取剥离引号
    # 后为无引号形态——a-catfiends obj71 registerTypes 实证）
    _re.compile(r'^\s*"?\s*(?:System|Unity|Mono)\.[\w.]+\s*,\s*[\w.]+\s*,\s*Version=\d'),
    # 赋值表达式（M = {} 空表声明等 Lua 模块形态）
    _re.compile(r"^\s*\w+\s*=\s*\{"),
    # 命名参数函数调用（InvertVector2(invertX=false)）
    _re.compile(r"\b\w+\(\s*[A-Za-z_]\w*=[^(),)]*\)"),
    # Lua 命令式调用（runblock(flowchart, "Intro") 含字符串参数）
    _re.compile(r"^\s*\w+\([^)]*\"[^)]*\"[^)]*\)\s*$"),
    # FungusLua 行首命令（wait(1)/say "..."/choose {...} 等：翻译整行
    # 破坏命令名，与整文件级模式同源；对话行首为 say 前缀在真实对话
    # 中不存在，0 误伤实证）
    _re.compile(r"^\s*(?:say|choose|wait|runblock|setcharacter)\b"),
)


def _is_script_code_line(text: str) -> bool:
    """单行是否确定性代码行（单行即判，不聚合行数/占比）。

    与整文件级 _looks_like_script_source 互补：整文件检测只覆盖 TextAsset
    聚合形态；rawstr 对象内的散落代码行（Fungus Lua 命令块）与引擎内部
    类型/调用签名需要行级判定。正常显示文本（含 Fungus 富文本标签
    {punch=3,2}* Y A W N *{w=3}{x}）不命中任何模式（实证 0 误伤）。
    """
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < 6:
        return False
    return any(p.search(stripped) for p in _CODE_LINE_PATTERNS)


def _looks_like_script_source(raw: bytes) -> bool:
    """TextAsset 整文件是否源码脚本（整文件跳过，不产生条目）。

    判定：非空行中命中脚本特征行的占比 ≥30% 且非空行 ≥8。
    实证锚点：a-catfiends-impending-relapse resources.assets#69
    （inspect.lua 库 264 行）命中 45%；真实对话文本 0%。

    JSON 文件例外（project-arrhythmia/dear-edmund/isolated-inhale 实证
    2026-09-01）：可解析 JSON 的缩进行（裸 { } [ ] 与 "key": value 结构行）
    命中 _SCRIPT_LINE_PATTERNS 的裸括号行/赋值模式，占比虚高触发整文件
    误杀——chat/thanks/post_level（PAChat 启动脚本）与 CharacterName_En
    （对话问答）、Socials（链接数组）都是真显示文本，被 0 条提取吞掉。
    JSON 结构行在 JSON 分支（stripped.startswith("{")）已处理，这里剔除
    它们再算占比；Lua/JS/C# 真脚本无 JSON 结构行（实证 inspect.lua 等
    命中率不受影响，见 test_textasset_script_source_file_produces_no_entries）。
    """
    if not raw:
        return False
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    stripped_head = text.lstrip().lstrip("﻿")
    # JSON 文件：整文件可解析 → 剔除 JSON 结构行（裸括号/键值对）后再判定
    is_json = bool(stripped_head[:1] in ("{", "["))
    if is_json:
        try:
            import json as _json
            _json.loads(stripped_head)
        except Exception:  # noqa: BLE001
            is_json = False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < _SCRIPT_MIN_CODE_LINES:
        return False
    if is_json:
        # JSON 缩进噪音（project-arrhythmia PAChat/CharacterName_En/Socials
        # 实证 2026-09-01）：裸括号行与 "key": value 结构行是 JSON 排版，不是
        # 代码。真实 Lua/JS/C# 脚本的键值行形态是赋值（x = ...）不带引号。
        lines = [ln for ln in lines
                 if not _re.fullmatch(r"[{}\[\]]", ln)
                 and not _re.match(r'^"[^"]+"\s*:', ln)]
        if not lines:
            return False
    hits = sum(1 for ln in lines
               if any(p.search(ln) for p in _SCRIPT_LINE_PATTERNS))
    return hits / len(lines) >= _SCRIPT_MIN_CODE_RATIO


# 原生（非 MonoBehaviour）文本承载类型：场景里的 Text/TextMesh 等组件。
# 它们的字符串走 typetree 全叶子分类（不跑 raw scan——scene 字节噪音大，
# 且这些类型的显示字段在 typetree 中完整可见）。
_NATIVE_TEXT_TYPES = frozenset({
    "Text", "TextMesh", "GUIText", "InputField",
    "VisualTreeAsset", "TextMeshPro", "TextMeshProUGUI", "TMP_Text",
})


def _should_downgrade_pending(entry: TextEntry) -> bool:
    """提取后置降级闸门（证据分层）。

    引擎串与键风格标识符照降；is_hard_structural 硬结构（JSON/URL/路径/
    GUID/纯数字…翻译会破坏功能）照降；但**确定性显示证据**条目（typetree
    UI 字段白名单 high）只被硬结构降级，署名/版权类软猜测规则不得推翻
    UI 字段证据（lilys-day-off level13 结局画廊实证：'A game by Kyuppin'
    在 m_Text 显示字段被 credit 规则降级错过）。
    """
    if entry.status != "pending" or entry.meta.get("kind") == "localization":
        return False
    fmt = entry.meta.get("textasset_format")
    inner = str(entry.meta.get("inner_path", ""))
    if fmt == "json":
        # JSON 数据区结构过滤（8 More Lives 实证 2778 条 hex 颜色/数值公式/
        # 资源引用枚举/数组下标枚举漏网进池）——结构与显示叶子保护见
        # _is_json_data_area。csv/xml 特判在上（此前的值形态/键风格/硬结构
        # 规则对 ALL-CAPS 枚举与公式是放行的——_WORD_CASE 把 'ARCTIC'
        # 当显示文本，这就是数据区全部漏网的原因）。
        return _is_json_data_area(inner, str(entry.original or ""))
    if fmt == "csv" and entry.meta.get("source_col") is not None:
        # CSV 多语言词典：source_col 是表头声明的语言列（ENG 等），列语义
        # 即"该语言的显示文本"——确定性证据。软猜测（引擎串/键风格/硬结构
        # 形态）不得推翻（Rendezvous 实证：IName_Medkit→'Med-Kit' 连字符
        # 被 hard_structural 降级、BTN_FullScreen→'FullScreen' 被引擎串
        # 降级——都是物品名/设置项显示文本，该译）。写回目标是目标语言列
        # （CHN），不覆盖源列，机器数据值写回也无破坏性。
        return False
    if fmt == "xml" and ("/value" in inner or inner.endswith("/value")):
        # xml value 节点是确定性显示文本证据（doog 的 messages/
        # message[N]/value 是游戏内显示文本）——后置闸门的**软猜测
        # 反模式**（引擎串编程命名形态 PascalCase/驼峰、key_style
        # 混合大小写、_QUALIFIED 标识符形态、credit_like 句子署名、
        # log_template 冒号结尾）不得推翻格式判定（doog 实证 33 条
        # xml value 罗马音台词 FeeNGAh/Konbanmio-n、西语 UI
        # 'Seleccione dificultad:'、英文成就句 'Get revived by…' 被
        # 误降级哑跳过）。仅形态标记明确的机器数据（URL/GUID/JSON/
        # 纯数字/输入设备/绑定路径/base64/路径/已知引擎词表）与无
        # 语言内容短串仍降级。key 节点（全大写键名 PICKUP_* 由
        # key_style 判定跳过）不豁免。
        s = entry.original.strip()
        if len(s) < 2 or not _HAS_LETTER.search(s):
            return True
        if is_engine_string_core(s):
            return True
        if not is_hard_structural(s):
            return False
        return not (
            is_credit_like(s)                       # 'Get revived by…' 含 by 被当署名
            or _QUALIFIED.match(s)                  # 'Konbanmio-n' 连字符被当程序集名
            or (len(s) >= 20 and _LOG_TEMPLATE_TAIL.search(s))  # 冒号结尾西语 UI 被当日志模板
        )
    if _is_engine_string(entry.original):
        return True
    if is_key_style_identifier(entry.original):
        return True
    if is_tag_composed(entry.original):
        # TMP 标签组合串是显示文本（正文可译、标签是排版标记）——
        # is_hard_structural 的 HTML 形态规则会误伤 <b>hi</b>/
        # <color=red>Warning!</color>（HTML_OR_BB 匹配），确定性标签
        # 语法证据优先于形态猜测
        return False
    if not is_hard_structural(entry.original):
        return False
    if (entry.meta.get("confidence") == "high"
            and entry.meta.get("reason") == "typetree_display_field"
            and is_credit_like(entry.original)):
        return False
    return True


# ── JSON TextAsset 数据区过滤（8 More Lives 实证 2026-08-31） ────────────
# 游戏数据文件（平衡表/全局设置/技能/装备字典）里，叶子字段大量是
# 机器可读值——hex 颜色、数值公式、资源引用（音效/图标/逻辑枚举）、
# 数组下标+枚举标签。这些值经 json 分支提取后全部 pending 进池，翻译
# 成中文必然破坏：颜色查表、属性公式（STR*0.2）、音效/动画/逻辑名
# （STRIKE/FIST/UNDEAD）都是游戏内部引用标识符。同时 ARCTIC/STRIKE/
# MELEE 等词在数据区（GlobalBiomsDistribution/0/0）与真文本区
# （Texts/ARCTIC/Text='Arctic'）同时出现——只能按 inner_path 结构拦截
# （structure-based），绝不能按值拦截（value-based 会误杀显示区）。

# 资源/渲染引用叶子：值若为 hex 颜色或全大写枚举 → 内部引用标识符。
# 含该游戏语料的实际字段（VisualLogic/VisualEffect/SoundEffect/Icon/
# Hex/Name/VisualStance/MovementTag/CombatAI/Layer/Source/Hidden…）。
_JSON_REF_LEAVES = frozenset((
    "Hex", "Color", "Icon", "VisualLogic", "VisualEffect", "SoundEffect",
    "SFX", "Sound", "Music", "Sprite", "Material", "Prefab", "Texture",
    "Animation", "Controller", "Shader", "Image", "Model", "Mesh",
    "Effect", "Particle", "Font", "Background", "Visual", "Name",
    "VisualStance", "VisualOverride", "VisualGroupOverride", "CombatAI",
    "SoundId", "MovementTag", "Source", "Layer", "Hidden", "HitName",
    "Set_To_Gameobject",  # 音频配置 JSON（dcdb50a1 实证）：目标游戏对象名
))
# 确定性显示文本叶子：其值（无论形态）是给玩家看的文本（Texts/*/Text、
# Names/*/Text 人名、Description/Tooltip/Title 等），数据区规则一律放行。
_JSON_DISPLAY_LEAVES = frozenset((
    "Text", "Description", "Tooltip", "Title", "Label", "Hint", "Tip",
    "SubText", "ButtonLabel", "Dialogue", "Line",
))
# hex 颜色 #RRGGBB
_JSON_HEX_COLOR = _re.compile(r"^#[0-9A-Fa-f]{6}$")
# 属性公式 STR*0.2 / DEX*0.15
_JSON_FORMULA = _re.compile(r"^[A-Za-z]{2,10}\*[\d.]+$")
# 全大写枚举值（≤20 字符，可含下划线/连字符）：数据区枚举标签
_JSON_ALL_CAPS = _re.compile(r"^[A-Z][A-Z0-9_\-]{0,19}$")
# 资源引用叶子 + 非 hex 非全大写值（VisualStance='2H'、Set_To_Gameobject=
# 'UI'/'main'）：引用叶子的值本身就是内部标识符（姿态/目标对象/图层），
# 词形任意（2H 混合大小写、main 小写）——值匹配引用叶子 → 跳过
_JSON_REF_LEAF = _re.compile(r"^[A-Za-z0-9_\-]{1,24}$")
# 关卡地图编辑器 JSON（project-arrhythmia 实证 2026-09-01）顶层键全集——
# 与 hanhua/core/formats/json_format.py 的 _MAP_EDITOR_TOP_KEYS 共用定义
# （防分叉）。对象是游戏内地图编辑器（玩家/关卡设计者摆放实体），字符串值
# = UUID（实体 id/p_id 互引）、base64 缩略图、对象类型名、动画曲线名、节点
# 结构名，翻译无显示语义（玩家在游戏里看到的是关卡贴字，那是 objects/*/text
# 富文本值——单独按 objects/text 路径语义由写回侧按值保留，不在此列）。
_MAP_EDITOR_TOP_KEYS = frozenset((
    "editor", "editor_prefab_spawn", "parallax_settings", "checkpoints",
    "objects", "prefab_objects", "prefabs", "themes", "markers", "events",
    "triggers",  # project-arrhythmia obj4/179/190 实证：触发器表同属编辑器数据
))
# Spine 骨骼动画 JSON 顶层键全集——与 json_format.py 的 _SPINE_TOP_KEYS
# 共用定义（防分叉）。skeleton 键是 Spine 文件专属标识，全语料普查只出现
# 在 Spine 文件；全键子集判定见 _is_spine_document。
_SPINE_TOP_KEYS = frozenset((
    "skeleton", "bones", "slots", "ik", "skins", "animations",
    "transform", "events",
))
# PAChat 终端脚本 JSON 顶层键全集——与 json_format.py 的 _PACHAT_TOP_KEYS
# 共用定义（防分叉）。{settings, branches} 是游戏内 PAChat 终端脚本
# （project-arrhythmia 实证 2026-09-01）：分支名/命令 token/settings 配置
# 是机器引用，真显示文本只占少部分且逐条混杂 → 整文件跳过（宁漏勿坏）。
_PACHAT_TOP_KEYS = frozenset(("settings", "branches"))
# Unity PerformanceTestRunInfo JSON（minato 实证 2026-09-02）：性能测试
# 运行信息——Player 渲染配置（RenderThreadingMode='Split'/AndroidBuildSystem=
# 'Gradle'）+ Editor 版本/分支（Branch='6000.3/staging'）+ Dependencies 包名
# 列表（com.unity.*@x.y.z）。键值全是机器元数据/构建产物引用，翻译即破坏
# 性能报告解析。根含 'PerformanceTestRunInfo'/'TestSuite'/'Dependencies' 键
# 即测试框架数据（非玩家内容）。与 Spine/PAChat 同款整文件跳过。
_PERFTEST_TOP_KEYS = frozenset((
    "TestSuite", "Date", "Player", "Hardware",
    "Editor", "Dependencies", "Results",
))


def _is_perftest_document(data: Any) -> bool:
    """JSON 根是否 Unity PerformanceTestRunInfo（返回 True → 整文件跳过）。"""
    return bool(isinstance(data, dict) and data
                and "TestSuite" in data
                and all(key in _PERFTEST_TOP_KEYS for key in data))


def _is_spine_document(data: Any) -> bool:
    """JSON 根是否 Spine 骨骼动画文件（返回 True → 整文件跳过）。

    skeleton/bones/slots/ik/skins/animations 全是运行库查表引用（骨骼名/
    插槽名/附件名/皮肤名/动画名），翻译写坏骨骼动画加载。判定与
    json_format.py 共用（防分叉）：根含 'skeleton' 键且全部顶层键 ⊆
    _SPINE_TOP_KEYS（变体覆盖缺 ik/events/含 transform，全语料 0 误报）。
    """
    return bool(isinstance(data, dict) and data
                and "skeleton" in data
                and all(key in _SPINE_TOP_KEYS for key in data))


def _is_pachat_document(data: Any) -> bool:
    """JSON 根是否 PAChat 终端脚本文件（返回 True → 整文件跳过）。

    project-arrhythmia 实证 2026-09-01：{settings, branches} 顶层是游戏内
    PAChat 终端脚本（系统启动/登录/教程/对话/结算全在这），元素含 type:
    text/event/buttons 与 data 数组。文本值绝大多数是机器引用：分支名
    （initial_branch/name=入口跳转标识）、settings 数组配置（loop:N/
    alignment:*/width:0.5/bg-color:text-color/font-style:bold——终端的样式
    控件配置）、data 命令 token（wait::2/branch::login/replaceline::6::…/
    setbg::E0E0E0/loadscene:Main Menu——命令名必须字节级保留）。真显示文本
    （'All rights reserved.'/'| Login: |'/'0%  [...]'/'Subject : Jane'）只
    占少部分，且与命令 token 在同一个 data 数组里逐条混杂——条目级过滤
    只能拦命令前缀（_PACHAT_CMD），全文件机器引用主导 → 文件级跳过整文件
    不产生条目（宁漏勿坏：漏译一段启动滚动文本 vs 译坏分支名/命令解析，
    后者是功能级破坏）。判定：根为 dict、含 branches、全键 ⊆ 本集。
    跨游戏泛化：任何用 {settings, branches} 的终端脚本同款拦截。
    """
    return bool(isinstance(data, dict) and data
                and "branches" in data
                and all(key in _PACHAT_TOP_KEYS for key in data))


def _is_json_data_area(inner: str, value: str) -> bool:
    """JSON TextAsset inner_path 是否数据区条目（返回 True → 跳过）。

    保护优先：Texts/Languages 顶层与确定性显示叶子（Text/Description…）
    永不跳过——人名显示（Names/*/Text）、语言名、UI 词典都在这。
    数据区判定（结构信号，非值信号）：
      1. hex 颜色值（#7d1923）
      2. 数值公式（STR*0.2）
      3. 资源引用叶子 + hex/全大写值（VisualLogic='STRIKE'/Hex='#…'）
      4. 数组下标叶子 + 全大写值（GlobalBiomsDistribution/0/0='ARCTIC'）
      5. 嵌套(≥2 段) + 全大写值（BiomeFallbacks/LAKE='RIVER'——叶子是
         枚举键而非显示字段，值是音乐组/回退/招募人群枚举）
    """
    segs = inner.split("/")
    if not segs:
        return False
    if segs[0] in ("Texts", "Languages"):
        return False
    if segs[0] in _MAP_EDITOR_TOP_KEYS:
        # 关卡地图编辑器 JSON 顶层键（project-arrhythmia 实证 2026-09-01）：
        # objects/prefabs/checkpoints/themes/markers/events...——实体摆放/
        # 曲线/节点结构名（UUID/base64 缩略图/OutSine/Face Root），无显示语义
        return True
    if _JSON_HEX_COLOR.fullmatch(value):
        return True
    if _JSON_FORMULA.fullmatch(value):
        return True
    leaf = segs[-1]
    if leaf in _JSON_DISPLAY_LEAVES:
        return False
    if leaf in _JSON_REF_LEAVES:
        # 引用叶子 + 无空格标识符值 → 内部引用（含 2H/main 等非全大写）。
        # 有空格的值（'heavy plate armor'）是描述性文本，不在此列
        if _JSON_REF_LEAF.fullmatch(value):
            return True
        if _JSON_HEX_COLOR.fullmatch(value) or _JSON_ALL_CAPS.fullmatch(value):
            return True
        return False
    if "values" in segs or "function_call" in segs:
        # UI 设置定义 JSON（project-arrhythmia ui-setting-def 实证
        # 2026-09-01）：顶层块（ArcadeHealthMod/ArcadeSpeedMod/MenuMusic…）
        # 的 list 元素是 {name, values, function_call, ui_desc}。values/
        # function_call 是机器引用（'menu'/'nostalgia'/'down'/'global'/
        # 'your'/'friends'/'fil'/'left'/'right' = 枚举/对象名/语言代码，
        # 'vote::false'/'vote::true' = 函数调用指令），翻译写坏设置读写。
        # 值可走 values 数组的子路径（Modifiers/0/values/1='fil'）——
        # 用段级判定（'values'/'function_call' 出现在路径任一位置）+ 无空格值
        # → 内部引用（含小写词形）。ui_desc/name 是真显示文本保留。
        # 结构信号：路径含机器引用容器 + 无空格值 → 内部引用。
        if " " not in value:
            return True
    if leaf.isdigit() and _JSON_ALL_CAPS.fullmatch(value):
        return True
    if len(segs) >= 2 and _JSON_ALL_CAPS.fullmatch(value):
        return True
    return False


def extract_asset_file(path: str | Path, file_id: str | None = None,
                       progress_cb: Callable | None = None, *,
                       typetree_generator: Any | None = None,
                       csv_overwrite_source: bool = False) -> ParsedFile:
    """提取一个资源文件 → ParsedFile（含文件级噪音判定）。

    容器：UnityPy 的 Environment.objects 自动递归 BundleFile/WebFile 嵌套
    （Addressables bundle 里的 bundle、UnityWebData 容器），seen_objects 去重。

    typetree_generator：UnityPy.helpers.TypeTreeGenerator 实例（Mono 游戏
    专用，从游戏 Managed DLL 生成脚本 typetree）。资产构建未带 typetree
    （BuildAssetBundleOptions.DisableWriteTypeTree / Player 构建 strip）时，
    MonoBehaviour 全部读取失败、文本只能靠 raw scan 兜底——挂上生成器后
    脚本字段可完整读取（hickory 实证：1890/1898 失败 → 1884/1898 成功，
    主菜单 Options/Quit 与对话文本全部字段级提取）。Mono 游戏 + Managed
    目录由扫描管线负责构建并传入；缺省 None 时行为与旧版一致。
    """
    from UnityPy import Environment
    p = Path(path)
    fid = file_id or str(p).replace("\\", "/")
    # 工具移植任务 1（2026-08-16）：UnityCN 加密 bundle 自动解密——
    # 文件内暴力探测 16 字符 key（UnityPy ArchiveStorageManager 全局
    # key），命中后 env.load 时内部自动解密数据块；探测失败返回
    # blocked 标记（保留原样，报告可见而非静默跳过）
    from hanhua.core.unity.unitycn_decrypt import (
        UNITY3D_SIGNATURE, brute_force_key, set_decrypt_key)
    try:
        _head = p.read_bytes()[:len(UNITY3D_SIGNATURE)]
        if _head == UNITY3D_SIGNATURE:
            _key = brute_force_key(p)
            if _key is None:
                return ParsedFile(
                    fid, str(p), "unitycn", [], "utf-8", "\n",
                    {"kind": "unitycn", "blocked": "decrypt_key_not_found"},
                    True, {"unitycn_key_missing": 1})
            set_decrypt_key(_key)
    except OSError:
        pass
    env = Environment()
    # 外部引用解析根=游戏目录（Mono 游戏 m_Script PPtr deref 需加载同
    # 目录兄弟文件；Environment() 默认 path=os.getcwd() 工具目录不可用）
    env.path = str(p.parent)
    if typetree_generator is not None:
        env.typetree_generator = typetree_generator
    entries: list[TextEntry] = []
    raw_items: list[tuple[str, int, bytes, set[str], str]] = []
    freq: dict[str, int] = {}
    deferred_candidates: list[tuple[str, int, list[TextEntry]]] = []
    seen_objects: set[tuple[str, int]] = set()
    skipped: dict[str, int] = {}  # R5 静默跳过留档（哑识别可见化）
    # 识别 L9：遇到的脚本类名全集（含未登记类）→ 报告待登记队列
    # （类注册表 class_registry 的登记审计数据源）
    script_classes: set[str] = set()
    # KV 词典分组（多语言词典集合）：组内只提取源语言表（英文优先，
    # 用户指令 2026-08-16），其余 skipped 留档——对象循环内收集，
    # 循环后统一裁决
    kv_dictionaries: dict[str, list[tuple[int, str, str, str, str, float]]] = {}
    # 识别 L7：typetree 覆盖率持续度量——每容器记录成功/失败对象数，
    # 失败靠 raw scan 兜底（Unity 6000 264/268 失败实证）但必须可量化
    typetree_ok = 0
    typetree_failed = 0
    try:
        try:
            env.load([str(p)])
        except Exception:  # noqa: BLE001
            # 无法解析的容器（未知魔数/截断/加密）→ 空结果，交给文本侧处理
            return ParsedFile(fid, str(p), "v2_asset", [], "utf-8", "\n",
                              {"kind": "asset"}, False, skipped)
        for obj in env.objects:
            object_key = _object_identity(obj)
            if object_key in seen_objects:
                continue
            seen_objects.add(object_key)
            tname = obj.type.name
            asset_name = _asset_file_name(obj)
            if tname == "TextAsset":
                try:
                    data = obj.read()
                    script = getattr(data, "m_Script", None)
                    if isinstance(script, str):
                        # 老 Unity（4.x/5.x）：TextAsset.m_Script 是 str
                        # （electric-trains 实证）。UnityPy 对二进制内容用
                        # surrogateescape 解码（\udc80 等），encode 必须用
                        # surrogateescape 还原原始字节，否则 3 个游戏
                        # （mimic-search/morfosigame/the-black-iris 实证）
                        # 的 TextAsset 全部抛 UnicodeEncodeError 被吞
                        script = script.encode(
                            "utf-8-sig", errors="surrogateescape")
                except Exception:  # noqa: BLE001
                    continue
                # KV 词典分组探测（多语言词典集合）：收集后延迟裁决
                if script and _looks_like_kv_dictionary_text(script):
                    name = str(getattr(data, "m_Name", "") or "")
                    values = [
                        m.group("value").strip()
                        for line in script.decode("utf-8-sig", errors="replace").splitlines()
                        if (m := _KV_LINE.match(line.strip())) is not None
                    ]
                    base = _dictionary_base_name(name) or str(obj.path_id)
                    kv_dictionaries.setdefault(base, []).append(
                        (int(obj.path_id), _dictionary_language(values),
                         script.decode("utf-8-sig", errors="replace"),
                         name, asset_name, _english_score(values)))
                    continue
                # ink 对话脚本（Inkle 引擎，Rendezvous 实证）：JSON 结构，
                # 内含控制词（done/end）、divert 目标（"->" 键值）、标签
                # （#f/#n/#c）与对话行（"^" 前缀）——通用 JSON 提取会把
                # 控制词/divert 目标进池翻译，破坏对话流程。特判提取：
                # 只保留对话行与纯文本值，其余留档跳过。语言版（CHN/JPN/
                # RUS 后缀或对话行 CJK/西里尔主导）已本地化 → 整文件跳过。
                # 双重 BOM 健壮（UnityPy str 已含 U+FEFF + utf-8-sig 又加
                # 一个 → EF BB BF EF BB BF）：整串 lstrip 再判 ink 头
                if script and script.lstrip(b"\xef\xbb\xbf").startswith(
                        b'{"inkVersion"'):
                    entries.extend(_ink_entries(
                        fid, obj.path_id, script or b"", asset_name, skipped,
                        str(getattr(data, "m_Name", "") or "")))
                    continue
                entries.extend(_textasset_entries(
                    fid, obj.path_id, script or b"", asset_name, skipped,
                    csv_overwrite_source))
            elif (tname in ("MonoBehaviour", "ScriptableObject")
                  or tname in _NATIVE_TEXT_TYPES):
                tree = None
                script_class = ""
                # === 根因修复：失败类快速预检（2026-08-20）===
                # UnityPyBoost.read_typetree C 扩展在 EOF 越界路径上病态耗时
                # ~5-7s/次（Rendezvous ObjectState 31 实例全如此），而纯 Python
                # read_value 同样越界但 0.00s 失败。用纯 Python 做预检，首例
                # 失败后按 (assembly, class_name) 缓存，后续同类直接跳 read_typetree。
                # typetree_failed 计数不变（保持报告语义一致）。
                cls_sig = _mono_class_sig(obj)
                if cls_sig:
                    if cls_sig in _FAILED_CLASS_CACHE:
                        typetree_failed += 1
                        skipped["typetree_failed"] = (
                            skipped.get("typetree_failed", 0) + 1)
                    else:
                        ok = _quick_typetree_check(obj, typetree_generator)
                        if not ok:
                            _FAILED_CLASS_CACHE.add(cls_sig)
                            typetree_failed += 1
                            skipped["typetree_failed"] = (
                                skipped.get("typetree_failed", 0) + 1)
                            tree = None
                if tree is None and cls_sig not in _FAILED_CLASS_CACHE:
                    try:
                        # 预检通过（纯 Python read_value 成功）才走 boost——
                        # 失败对象在缓存跳过，不会到达这里
                        tree = obj.read_typetree()
                        typetree_ok += 1
                    except Exception:  # noqa: BLE001
                        # 识别 L7：typetree 失败率留档（覆盖率指标数据源）——
                        # 失败对象靠 raw scan 兜底（Unity 6000 实证），但失败
                        # 率必须可量化：逐容器记录 + skipped 原因聚合
                        typetree_failed += 1
                        skipped["typetree_failed"] = (
                            skipped.get("typetree_failed", 0) + 1)
                if not script_class:
                    # 识别 L6 兜底（give-me-strength 音频消失实证
                    # 2026-08-29）：typetree 失败/FileID≠0 外部引用对象
                    # 拿不到类名——头部固定布局解析 m_Script PPtr（含
                    # externals 按需补载），FMODUnity.Settings 这类
                    # 配置类对象才能进 class_registry 判定
                    script_class = _script_class_from_head(obj)
                    if script_class:
                        script_classes.add(script_class)
                if isinstance(tree, dict):
                    # 识别 L6：确定性脚本类名（m_Script PPtr → MonoScript）
                    # 优先于串池信号，对象级判定直接使用
                    script_class = _script_class_of(tree, obj) or script_class
                    if script_class:
                        script_classes.add(script_class)
                    # B15（snowday 按键失灵根因 2026-09-05）：配置类对象
                    # （InputActionAsset/FMOD.Settings 等）不进 typetree
                    # 字段提取——其字符串全是机器标识（绑定键/控件类型/参数
                    # 名/总线名），字段级白名单无法穷尽（m_ExpectedControlType
                    # 'Button'→'按钮'、m_Groups 'Joystick'→'操纵杆' 均曾
                    # 漏网写回）。与 rawstr 分类链 is_config_class 分支
                    # （~1930）同规则：宁漏勿坏，对象级确定性整跳。注意
                    # 只禁 typetree 路径，对象照常回落 raw scan——rawstr
                    # 分类链按类名证据产生 per-string skipped 留档条目
                    # （legacy reason 词汇 input_system_object/
                    # timeline_object 保持，审计连续性），typetree display
                    # 泄漏面归零。
                    _skip_typetree = (
                        _class_disposition(script_class) == "config")
                    if _skip_typetree:
                        _reason = ("input_system_object"
                                   if script_class in _INPUT_SYSTEM_SCRIPT_CLASSES
                                   else "timeline_object"
                                   if script_class in _TIMELINE_SCRIPT_CLASSES
                                   else "script_class_config")
                        skipped[_reason] = skipped.get(_reason, 0) + 1
                    if not _skip_typetree:
                        if _is_string_table_tree(tree):
                            entries.extend(_localization_entries_from_tree(
                                fid, obj.path_id, tree, asset_name))
                            continue
                        if _is_i2_language_source_tree(tree):
                            # I2 Localization 语言源：整游戏文本全集，
                            # 确定性提取（键/资产引用术语跳过）
                            entries.extend(_i2_localization_entries_from_tree(
                                fid, obj.path_id, tree, asset_name))
                            continue
                        shared_rows = tree.get("m_Entries")
                        if isinstance(shared_rows, list) and any(
                                isinstance(row, dict) and "m_Key" in row for row in shared_rows):
                            continue
                        display, candidates = _typetree_string_entries(
                            fid, obj.path_id, tree, asset_name, skipped,
                            script_class=script_class)
                        entries.extend(display)
                        if display:
                            # typetree 已覆盖全部叶子，display 存在时不跑 raw scan；
                            # 候选同时入库（低置信证据层，写回自动排除）
                            entries.extend(candidates)
                            continue
                        # 无 display 条目：候选暂存，待 raw scan 后取补集
                        # （raw scan 的对象级值特征/UI 证据分类更准确）
                        if candidates:
                            deferred_candidates.append(
                                (asset_name, int(obj.path_id), candidates))
                if tname not in _NATIVE_TEXT_TYPES:
                    try:
                        raw = obj.get_raw_data()
                    except Exception:  # noqa: BLE001
                        continue
                    if raw and len(raw) < 8_000_000:
                        raw_strings = {s for _, s in scan_strings(raw)}
                        raw_items.append(
                            (asset_name, int(obj.path_id), raw, raw_strings,
                             script_class))
                        for s in raw_strings:
                            freq[s] = freq.get(s, 0) + 1
    finally:
        from hanhua.core.unity.writer import _dispose_environment
        _dispose_environment(env)
    # 识别 L8：高频串阈值按全文件规模算一次，所有对象共用（避免每对象
    # 重复 sum(freq.values())——对象上千时 O(N×M)）
    freq_threshold = _high_freq_threshold(freq)
    for asset_name, path_id, raw, _, script_class in raw_items:
        entries.extend(_raw_string_entries(
            fid, path_id, raw, freq, asset_name, freq_threshold,
            script_class))
    # 候选补集：raw scan 已发现的原文以 raw 分类为准，候选只补漏网证据
    covered_by_raw = {(name, pid): strings
                      for name, pid, _, strings, _ in raw_items}
    for asset_name, path_id, candidates in deferred_candidates:
        covered = covered_by_raw.get((asset_name, path_id), set())
        entries.extend(
            c for c in candidates if c.original not in covered)
    # KV 词典分组裁决：源语言表选择——英文表优先（用户指令 2026-08-16
    # 「多语言游戏语言优先翻译英文」，与 _prefer_source_locale_bundles
    # 的英文源表先例一致），组内无英文表时取第一张（path_id 升序 =
    # 游戏默认源语言表）；其余语言表 skipped 留档（原因带语言脚本，
    # 报告按 reason 聚合可见——electric-trains 19 张词典实证）
    for base, records in kv_dictionaries.items():
        records.sort(key=lambda r: r[0])
        source_pid = records[0][0]
        best = max(records, key=lambda r: r[5])
        if best[5] >= _ENGLISH_SCORE_MIN:
            source_pid = best[0]
        for pid, lang, text, name, rec_asset, _score in records:
            if pid == source_pid:
                # 双重 BOM 剥离（UnityPy str 已含 U+FEFF + encode
                # utf-8-sig 又加一个 → decode 只移除一个，残留 BOM
                # 会粘在首个键上 '﻿missions'——a-catfiends 实证
                # 同源问题）
                entries.extend(_textasset_kv_entries(
                    fid, pid, text.lstrip("﻿"), rec_asset, skipped))
            else:
                reason = f"textasset_locale_table_{lang}"
                skipped[reason] = skipped.get(reason, 0) + 1
                sample_value = next(
                    (m.group("value").strip()
                     for line in text.splitlines()
                     if (m := _KV_LINE.match(line.strip())) is not None
                     and m.group("value").strip()),
                    name)
                sample = _skipped_sample_entry(
                    fid, f"asset#{rec_asset}#{pid}/loc", sample_value,
                    kind="textasset", reason=reason,
                    count=skipped[reason])
                if sample:
                    entries.append(sample)
    for e in entries:
        if _should_downgrade_pending(e):
            e.status = STATUS_SKIPPED
    noise = looks_like_noise_file(entries)
    meta: dict = {"kind": "asset"}
    # 识别 L7：typetree 覆盖率入容器 meta（每容器可用率可查）——
    # 低覆盖率容器是「字段证据缺失、靠 raw 兜底」的量化信号
    if typetree_ok or typetree_failed:
        meta["typetree_coverage"] = typetree_ok / (
            typetree_ok + typetree_failed)
        meta["typetree_objects"] = typetree_ok + typetree_failed
    # 识别 L9：脚本类名全集入容器 meta（报告待登记队列数据源，
    # 未登记类名 = 类注册表的下一条登记候选）
    if script_classes:
        meta["script_classes"] = sorted(script_classes)
    return ParsedFile(fid, str(p), "v2_asset", entries, "utf-8", "\n",
                      meta, noise, skipped)
