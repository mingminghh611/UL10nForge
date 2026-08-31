"""v2 Mono 程序集提取：dnfile 读取 .NET #US 字符串堆（C# 字符串字面量）。"""
from __future__ import annotations
from pathlib import Path
import re
from typing import Callable

from hanhua.core.extractor import ParsedFile, looks_like_noise_file
from hanhua.core.models import STATUS_SKIPPED, TextEntry
from hanhua.core.unity.extractor import (
    _finalize_skipped_counts, _skipped_sample_entry)
from hanhua.core.paths import (UnsafeRelativePathError, ensure_trusted_root,
                               resolve_relative_under)
from hanhua.core.placeholders import is_code_identifier, is_hard_structural
from hanhua.core.engine_strings import (is_engine_string as _is_engine_string,
                                        is_engine_string_core,
                                        is_engine_string_gated,
                                        is_strong_interaction_prompt)
from hanhua.core.tooling.player_layout import (
    PlayerLayoutError,
    discover_application_assemblies,
)

# 代码拼接 UI 文本证据：4+ 字符全大写词（BEST/LEFT/DRIFT），诊断日志通常无
_UI_UPPERCASE_WORD = re.compile(r"(?<![A-Za-z0-9_])[A-Z]{4,}(?![A-Za-z0-9_])")
# 内部诊断/日志/错误消息（a-catfiends 实证：ProBuilder/Poly2Tri 等编辑器
# 工具的测试日志/断言/错误消息被 uppercase_ui 放行进池——' FAILED: '、
# '[FLIP] - subedge done'、'CNOT had non-bool arg' 是代码诊断文本，玩家
# 不可见，翻译无意义且会改动符号（'--' 被模型改成 '–'）。形态特征：
# 开发状态词（PASSED/FAILED/EXTEND/INTERNAL/DEBUG/TEST/SCAN）、内部类型
# 名（TailCallRequest/YieldRequest/CNOT）、方括号调试前缀（[BUG:/[FLIP]/
# [FIXME）、错误消息句式（Error parsing/not correct/Improper ... JSON）。
# 日志专用词（全大写日志格式 ' FAILED: '，驼峰 'Failed to X' 可能是
# 错误对话框 UI 文本，不拦截）：PASSED/FAILED/EXTEND
_DEV_STATUS_WORD = re.compile(
    r"^[\s_=—-]*(?:PASSED|FAILED|EXTEND)\b")
# 通用词（INTERNAL/DEBUG 等在普通句子中可能驼峰出现，如 'Internal format {0}'，
# 要求全大写形态才判诊断）：INTERNAL/DEBUG/TEST/SCAN/ERROR
_DEV_STATUS_WORD_UPPER = re.compile(
    r"^[\s_=—-]*(?:INTERNAL|DEBUG|TEST|SCAN|ERROR)\b")
_DEBUG_BRACKET_PREFIX = re.compile(
    r"(?i)^\[(?:BUG[:\]]|FIXME[:\]]|FLIP[:\]]|SCAN[:\]])")
_INTERNAL_TYPE_NAME = re.compile(
    r"(?i)\b(?:TailCallRequest|YieldRequest|CNOT)\b")
_ERROR_MSG_PATTERN = re.compile(
    r"(?i)(?:Error parsing|is not correct|Improper .*JSON|JSON format is not)")
_DEBUG_SENTINEL = re.compile(r"INTERNAL!")

# ── F33（2026-08-16，78-hour-rain 实证）句子形态显示文本 ──────────────
# 未证明流入 UI setter 的小写/混合大小写句子型 #US 串（叙事文本、教程
# 提示、状态提示、设置标签）被整类跳过——'Darkness has a voice' 系列 9 条
# 叙事 + 'Left-click to throw held traps' + 'Volume: ' 等 17 条真实 UI
# 文本全落 unverified_user_string 跳过桶（哑信号）。调试打印串
# （'doorbreakHealth: '、'Monster spawned at ('、'setting teeth angle
# to '）有变量拼接痕迹，用形态剔除——拼接痕迹 = 代码文本，宁漏勿坏。
# 驼峰/下划线词+冒号结尾 = 变量打印标签（'doorbreakHealth: '）——
# 词内中间位置含大写或数字（'Volume: ' 'Sensitivity: ' TitleCase
# 首字母大写其余小写是 UI 标签，负向前瞻豁免）
_CAMEL_OR_SYMBOL_COLON = re.compile(
    r"^\s*(?![A-Z]?[a-z]+:)[A-Za-z0-9]+:\s*$")
# 尾部拼接痕迹：`( `、` to `、` in `、` with `、` at `、` of `、` index `
# 等介词/量词结尾 = Concat 拼接片段（'Monster spawned at ('、'setting
# teeth angle to '、'spawning unique at spot index '）。2026-08-24
# come-back 实证：真实对话以完成义小品词结尾（'ill let you in'、
# 'nice ill let you in' 的 in 是 let in 的完成义小品词，不是悬空介词）——
# 从词表剔除 in/out/up/down/away/back 等完成义小品词，保留真正的悬空
# 介词/量词（to/with/at/of/for/from/index/length/value...），真拼接片段
# （'setting teeth angle to '）仍拒，对话不再误漏。
_DEBUG_CONCAT_TAIL = re.compile(
    r"(?i)(?:\(\s*$|\b(?:to|with|at|of|for|from|index|length|value|"
    r"state|section|spot|floor|count)\s*$)")
# 调试句子词（小写形态的日志/错误动词/系统名——UI 中极罕见）。
# F33 第二版扩充（test_v2 契约实证）：'Internal diagnostic message'、
# 'Internal format {0}'（未消费格式串）、'Press E state observed'
# （日志观测句）、'Assertion failed...Aborting'（断言失败）均为开发
# 文本——diagnostic/state observed/internal format/assertion/aborting
# 是铁调试特征；'Debug:'/'Trace:' 前缀是日志格式
_DEBUG_SENTENCE_WORD = re.compile(
    r"(?i)\b(?:could not|couldn't|failed|spawning|spawned|rerolling|"
    r"updating|placing|setting up|not found|does not work|max reached|"
    r"lightning|thunder|heard|struck|playerprefs|changing menu|"
    r"loading section|diagnostic|state observed|internal format|"
    r"assertion|aborting|assert)\b")
# 日志前缀（'Debug: Press E state observed'、'Trace. Failed to...'）
_DEBUG_LOG_PREFIX = re.compile(r"^(?:debug|trace)[\.\: ]")
# 句子混入驼峰标识符（'Press inventoryManager to open'——代码拼接
# 痕迹；UI 自然句无驼峰 token）
_DEBUG_CAMEL_MIX = re.compile(r"[a-z][A-Z][a-z]")
# 拼接片段：以空白/符号开头 = 上一片段尾部（' loot spots available'、
# ' seconds until next strike.'、'*****changing menu to None'）
_DEBUG_LEADING_JUNK = re.compile(r"^[^\w]+\s")
# 代码格式占位符（'bool2({0}, {1})' 引擎调试模板）——真实 UI/对话句
# 不用 {N} 裸占位符，命中即代码文本
_FORMAT_PLACEHOLDER = re.compile(r"\{[0-9]+\}")

# 引擎/编辑器诊断句式（2026-08-24 come-back 实证）：动词开头的错误消息
# 不被 _is_mono_diagnostic_string 拦截（它只拦开发词开头的句子）。这些
# 是玩家不可见的引擎/输入系统诊断（'There is already a virtual axis
# named'、'Invalid quality option'），翻译无意义。真实 UI/对话不含
# 'virtual axis'/'quality option'/'not supported' 等开发措辞。
_ENGINE_DIAGNOSTIC_PATTERN = re.compile(
    r"(?i)\b(?:virtual axis|virtual button|quality option|not supported|"
    r"not implemented|only supported|can only be called|is not possible)"
    r"\b")


def _is_sentence_display_text(s: str) -> bool:
    """句子形态显示文本证据（未证明 #US 串的最后一层放行）。

    满足全部条件的未证明串按显示文本进池（由质量门/审核兜底）：
    长度 ≥8、以字母/数字开头（排除拼接片段）、≥2 词（含空格/换行）、
    无驼峰冒号变量标签、无拼接尾部、无调试句子词。

    2026-08-24 come-back 实证放宽（识别遗漏修复，宁严勿漏）：
    - 短对话短语（4-15 字符含空格，如 'why not'/'take 1'/'nice ill let
      you in'）是玩家可见对话/提示，不是代码——此前 len<8 整类漏掉。
      真诊断短句（'not found'/'max reached'/'to '）被 _DEBUG_SENTENCE_WORD/
      _DEBUG_CONCAT_TAIL 提前拦截，不因放宽漏入。
    - 圆括号开头的完整句子（'（translation: ...）' 对话内自嘲注解）是
      显示台词，不是拼接碎片（拼接碎片以 `(` 结尾被 _DEBUG_CONCAT_TAIL
      拦截，且无空格）。
    - 代码格式模板（'bool2({0}, {1})' 含 {N} 占位符）是引擎/算法调试
      消息，翻译无意义——{N} 占位符是强代码形态，直接拒（真实 UI 句
      不用 {0} 裸占位符）。
    """
    if len(s) < 4 or not re.search(r"[A-Za-z]", s):
        return False
    if _FORMAT_PLACEHOLDER.search(s):
        return False
    # 短「词: 」标签/拼接片段（'day: ' 'Score: '）是标签/打印前缀，非对话
    # ——短对话（'why not'/'take 1'）不以冒号结尾。真正 UI 标签达到 8 字符
    # （'Volume: ' 'smooth: '）才按标签放行（len 放宽不波及短冒号标签）。
    if len(s) < 8 and s.strip().endswith(":"):
        return False
    # 短短语（4-15 字符）必须含空格/换行，且不得是单 token——单 token
    # 短词（'Oh'/'No'/'ok'）歧义大（可能是枚举名/引擎键），宁漏勿坏，
    # 留给 unverified 桶 + 审核兜底。
    if not (s[0].isalpha() or s[0].isdigit() or s[0] == "("):
        return False
    if " " not in s and "\n" not in s:
        return False
    if _CAMEL_OR_SYMBOL_COLON.search(s):
        return False
    if _DEBUG_CONCAT_TAIL.search(s):
        return False
    if _DEBUG_SENTENCE_WORD.search(s):
        return False
    if _DEBUG_LOG_PREFIX.match(s):
        return False
    if _DEBUG_CAMEL_MIX.search(s):
        return False
    return True


def _is_exclamation_ui_word(s: str) -> bool:
    """单 token 感叹/疑问词 UI 文本（'Trapped!' 'Untrapped!' 实证）。

    lilys-day-off 实证对话反应词（'What?' 'Hahaha!'）也是真实文本。
    形态：长度 3..24、首字符字母且非全小写单词（TitleCase/全大写）、
    以 ! 或 ? 结尾、无空白/下划线/连字符。调试哨兵（INTERNAL!/CNOT!）
    已被 _DEBUG_SENTINEL/_INTERNAL_TYPE_NAME 提前剔除。

    2026-08-24 come-back 实证：语气/情绪反应词常连写重复字母 + 多个
    感叹号（'allllmooooost!!!' 'ALLLLLLMOOOOOOOOSTTT!!!!!!!'
    'CELEBRATE!!!!'）——全是小写或大写、尾部感叹号连写、无空白。这些
    是屏幕上显示的情绪反应（玩家可见），非代码标识符（标识符不含
    !/?）。放宽：剥离尾部 !/? 连写后剩余体为纯字母即可（首字母不强制
    大写——'allllmooooost!!!' 是故意小写拉长语气）。多感叹号不视为
    非字母字符（此前 s[:-1].isalpha() 要求恰好一个 ! 导致连写误漏）。
    """
    body = s.rstrip("!?")
    if not body or len(body) < 2 or len(body) > 22:
        return False
    if not (s.endswith("!") or s.endswith("?")):
        return False
    if not body.isalpha():
        return False
    return True


# 物理按键名（alisa-demo 实证）：输入绑定配置里用作「按键名标签」的字
# 面量（InputBinding key / PlayerPrefs 绑定键 / 显示在重绑 UI 的按钮名）。
# 它们会被验证为 UI 文本（流入重绑 UI 的 set_text），但**翻译后绑定失效**
# ——按键名是引擎解析值，必须保留原文（与引擎串同一硬结构语义，但
# 数据流证明已让它绕过 is_engine_string 门）。纯字面量集合，不误伤
# 真实对话（'Esc' 在对话里是语气词时不在表内/不匹配模式）。
_INPUT_KEY_NAME_LABELS = frozenset({
    # 手柄 JS_ButtonN（Unity 遗留输入）
    *(f"JS_Button{i}" for i in range(20)),
    # 轴名称（Unity 遗留输入 Axis 名；含 '1st Axis ±'/'2ndAxis±' 两种形态）
    "1stAxis", "2ndAxis", "3rdAxis", "4thAxis", "5thAxis", "6thAxis",
    "7thAxis", "8thAxis", "9thAxis", "10thAxis", "11thAxis", "12thAxis",
    *(f"{n} Axis" for n in ("1st", "2nd", "3rd", "4th", "5th", "6th",
                            "7th", "8th", "9th", "10th", "11th", "12th")),
    "X Axis", "Y Axis", "Mouse X", "Mouse Y", "Horizontal", "Vertical",
    # 键盘键名（KeyCode 名）
    "Arrow Up", "Arrow Down", "Arrow Left", "Arrow Right",
    "Up Arrow", "Down Arrow", "Left Arrow", "Right Arrow",
    "Tab", "Space", "Escape", "Esc", "Return", "Enter", "Backspace",
    "Delete", "Insert", "Home", "End", "Page Up", "Page Down", "Pause",
    "CapsLock", "NumLock", "ScrollLock", "PrintScreen", "Left Shift",
    "Right Shift", "Left Ctrl", "Right Ctrl", "Left Alt", "Right Alt",
    "LeftControl", "RightControl", "LeftShift", "RightShift",
    "LeftAlt", "RightAlt", "LeftWindows", "RightWindows", "Menu",
    "Numpad0", "Numpad1", "Numpad2", "Numpad3", "Numpad4", "Numpad5",
    "Numpad6", "Numpad7", "Numpad8", "Numpad9",
    "Keypad Enter", "Keypad Divide", "Keypad Times",
    "Keypad Minus", "Keypad Plus", "Keypad Period", "Keypad 0",
    "Keypad 1", "Keypad 2", "Keypad 3", "Keypad 4", "Keypad 5",
    "Keypad 6", "Keypad 7", "Keypad 8", "Keypad 9", "LB", "RB", "LT",
    "RT", "Left Bumper", "Right Bumper", "Left Trigger",
    "Right Trigger", "Left Stick", "Right Stick", "Left Stick Button",
    "Right Stick Button", "D-Pad Up", "D-Pad Down", "D-Pad Left",
    "D-Pad Right", "D-Pad", "A Button", "B Button", "X Button",
    "Y Button", "Square", "Triangle", "Circle", "Cross",
})
# casefold 版本（成员判定统一用大小写折叠；避免漏掉 'Escape'→'escape' 等）
_INPUT_KEY_NAME_LABELS_CASEFOLD = frozenset(
    x.casefold() for x in _INPUT_KEY_NAME_LABELS)


def _is_input_key_name_label(s: str) -> bool:
    """字面量按键名标签（精确匹配 + 轴/方向键复合形态）。"""
    core = s.strip()
    if core.casefold() in _INPUT_KEY_NAME_LABELS_CASEFOLD:
        return True
    low = core.casefold()
    if re.fullmatch(r"js_button[0-9]{1,2}", low):
        return True
    if re.fullmatch(r"(?:1st|2nd|3rd|4th|5th|6th|7th|8th|9th|10th|11th|12th)"
                    r"(?:axis)?[ \t]*[+\-]?", low):
        return True
    if re.fullmatch(r"(?:1st|2nd|3rd|4th|5th|6th|7th|8th|9th|10th|11th|12th)"
                    r"[ \t]+axis[ \t]*[+\-]?", low):
        return True
    if re.fullmatch(r"(?:left|right)[ \t]+(?:shift|ctrl|control|alt)", low):
        return True
    if re.fullmatch(r"arrow[ \t]+(?:up|down|left|right)", low):
        return True
    if re.fullmatch(r"(?:up|down|left|right)[ \t]+arrow", low):
        return True
    if re.fullmatch(r"(?:keypad|numpad)[ \t]*[0-9]|(?:keypad|numpad)[ \t]+enter|"
                    r"(?:keypad|numpad)[ \t]+(?:divide|times|minus|plus|period)",
                    low):
        return True
    return False


def _safe_resolve_discovery_path(game_root: Path, candidate: Path) -> Path | None:
    """Resolve a discovered path after rejecting any reparse point in its chain."""
    try:
        relative = candidate.absolute().relative_to(game_root)
        return resolve_relative_under(game_root, relative)
    except (OSError, ValueError):
        return None


def find_dll_files(game_dir: str | Path) -> list[Path]:
    """Discover application assemblies using the shared player-layout rules."""
    try:
        game_dir = ensure_trusted_root(game_dir)
    except UnsafeRelativePathError:
        return []
    found: dict[str, Path] = {}
    pending = [game_dir]
    while pending:
        current = pending.pop()
        try:
            assemblies = discover_application_assemblies(game_dir, current)
        except PlayerLayoutError:
            return []
        for assembly in assemblies:
            found[str(assembly).casefold()] = assembly
        try:
            children = sorted(current.iterdir(), key=lambda path: (
                path.name.casefold(), path.name))
        except OSError:
            continue
        for child in reversed(children):
            candidate = _safe_resolve_discovery_path(game_dir, child)
            if candidate is not None and candidate.is_dir():
                pending.append(candidate)
    return sorted(found.values(), key=lambda path: (
        path.relative_to(game_dir).as_posix().casefold(),
        path.relative_to(game_dir).as_posix(),
    ))


def _walk_us_heap(data: bytes) -> list[tuple[int, bytes]]:
    """遍历 #US 堆 → [(字节偏移, 原始 UTF-16 字节)]。偏移 0 为占位。"""
    return [(data_offset, raw)
            for _, data_offset, raw in _walk_us_heap_records(data)]


def read_us_record_at(data: bytes, offset: int) -> tuple[int, bytes] | None:
    """按 token 偏移定位读取单条 #US 记录（与 CLR 语义一致，自包含）。

    记录 = 压缩长度前缀 + UTF-16 数据 + 1 字节尾部标志；读取不依赖前后
    记录（#US 堆无需紧凑，写回后残留字节不影响）。返回 (数据区位置,
    含尾部 flag 的原始字节)；偏移非法/记录越界返回 None。
    """
    if offset < 0 or offset >= len(data):
        return None
    compressed = _read_compressed_uint(data, offset)
    if compressed is None:
        return None
    ln, prefix_size = compressed
    data_start = offset + prefix_size
    if ln <= 0 or data_start + ln > len(data):
        return None
    return data_start, bytes(data[data_start:data_start + ln])


def _read_compressed_uint(data: bytes, offset: int) -> tuple[int, int] | None:
    """Read an ECMA-335 compressed unsigned integer as (value, byte count)."""
    if offset >= len(data):
        return None
    first = data[offset]
    if first & 0x80 == 0:
        return first, 1
    if first & 0xC0 == 0x80:
        if offset + 1 >= len(data):
            return None
        value = ((first & 0x3F) << 8) | data[offset + 1]
        return (value, 2) if value >= 0x80 else None
    if first & 0xE0 == 0xC0:
        if offset + 3 >= len(data):
            return None
        value = ((first & 0x1F) << 24) | (data[offset + 1] << 16)
        value |= (data[offset + 2] << 8) | data[offset + 3]
        return (value, 4) if value >= 0x4000 else None
    return None


def _walk_us_heap_records(data: bytes) -> list[tuple[int, int, bytes]]:
    """Return (token offset, data offset, raw bytes) for each #US record.

    流式遍历假设堆紧凑（编译器产物成立）。写回后记录变短会在尾部残留
    旧字节，残留区会被误解析为非法记录——此时步进 1 继续（鲁棒模式），
    不丢弃残留区之后的真实记录；原始紧凑堆行为不变。垃圾短记录会被
    提取侧字符串级过滤淘汰，写回侧只按 offset 单记录定位，不受影响。
    """
    out: list[tuple[int, int, bytes]] = []
    i = 1
    while i < len(data):
        token_offset = i
        compressed = _read_compressed_uint(data, i)
        if compressed is None:
            i += 1
            continue
        ln, prefix_size = compressed
        if ln <= 0 or i + prefix_size + ln > len(data):
            i += 1
            continue
        i += prefix_size
        out.append((token_offset, i, bytes(data[i:i + ln])))
        i += ln
    return out


_UI_SETTER_TYPES = frozenset({
    "TMPro.TMP_Text", "TMPro.TextMeshPro", "TMPro.TextMeshProUGUI",
    "TMPro.TMP_InputField", "UnityEngine.UI.Text", "UnityEngine.UI.InputField",
    "UnityEngine.TextMesh", "UnityEngine.UIElements.TextElement",
    "UnityEngine.UIElements.Label", "UnityEngine.UIElements.TextField",
})
# 日志 sink（F33 配套负面证据）：字面量流入这些 API = 确定性非 UI
# （开发日志/控制台输出，玩家不可见）——句子形态启发式也不得放行
_LOG_SINKS = frozenset({
    ("System.Console", "WriteLine"),
    ("System.Console", "Write"),
    ("UnityEngine.Debug", "Log"),
    ("UnityEngine.Debug", "LogError"),
    ("UnityEngine.Debug", "LogWarning"),
    ("UnityEngine.Debug", "LogException"),
    # alisa-demo 实证：MonoBehaviour.print = 旧版 Debug.Log（开发控制台）。
    # ~120 条调试消息（'Video has ended.'/'Scene Loaded'/'Stage 8 starts'/
    # 'I did it correctly'）此前未识别为日志 → F33 句子启发式放行
    # （多余识别根因）。print 是静态方法，MemberRef 为
    # UnityEngine.MonoBehaviour::print。
    ("UnityEngine.MonoBehaviour", "print"),
})
# IMGUI OnGUI 显示调用（XUnity 框架清单 + 语料挖掘实证：GUI.Label 9 游戏
# 205 次调用/GUI.Button 9 游戏 220 次——OnGUI 每帧渲染，字符串参数即
# 屏幕文本）。文本参数恒为最后一个参数（静态调用，栈顶即文本）。
_UI_DISPLAY_CALLS = frozenset({
    ("UnityEngine.GUI", "Label"), ("UnityEngine.GUI", "Box"),
    ("UnityEngine.GUI", "Button"), ("UnityEngine.GUI", "TextField"),
    ("UnityEngine.GUI", "TextArea"), ("UnityEngine.GUI", "Window"),
    ("UnityEngine.GUI", "PasswordField"), ("UnityEngine.GUI", "Tooltip"),
    ("UnityEngine.GUILayout", "Label"), ("UnityEngine.GUILayout", "Box"),
    ("UnityEngine.GUILayout", "Button"),
    ("UnityEngine.GUILayout", "TextField"),
    ("UnityEngine.GUILayout", "TextArea"),
    ("UnityEngine.GUILayout", "Window"),
    ("UnityEngine.GUILayout", "PasswordField"),
    ("UnityEngine.GUILayout", "Tooltip"),
})
# StringBuilder 拼接链（语料挖掘实证：Append 41 游戏 1792 次调用/
# AppendLine 16 游戏/AppendFormat 12 游戏——sb.Append("HP: ").Append(hp)
# 是拼接显示文本的第三形态，此前未建模全灭）
_STRINGBUILDER_TYPE = "System.Text.StringBuilder"
_STRINGBUILDER_CTOR_NAME = ".ctor"
_STRINGBUILDER_APPEND_NAMES = frozenset({"Append", "AppendLine"})
_STRINGBUILDER_FORMAT_NAMES = frozenset({"AppendFormat"})
_STRINGBUILDER_TOSTRING_NAME = "ToString"
# 结构 sink（证明链的镜像用法）：字符串参数是运行时按名查找键的引擎
# API——流入这些调用的字面量被**证明为结构**（确定性跳过，取代形态
# 猜测）。语料挖掘实证：Animator.SetTrigger 58 游戏/Shader.PropertyToID
# 43/GameObject.Find 44/set_name 49 游戏高频消费字符串。
# 两组：
# - 栈顶组：字符串参数恒为栈顶（单字符串参数或字符串为末参的重载；
#   多参重载如 LoadScene(name, mode) 的 top 是枚举 → 自然错过，安全）；
# - 首参组：字符串是第一个参数（name, value 形态）——arity 定长弹栈，
#   名字在 stack[-arity]。
_STRUCTURAL_SINKS = frozenset({
    ("UnityEngine.GameObject", "Find"),
    ("UnityEngine.GameObject", "FindGameObjectWithTag"),
    ("UnityEngine.Transform", "Find"),
    ("UnityEngine.Object", "set_name"),
    ("UnityEngine.Animator", "SetTrigger"),
    ("UnityEngine.Animator", "ResetTrigger"),
    ("UnityEngine.Animator", "Play"),
    ("UnityEngine.Shader", "PropertyToID"),
    ("UnityEngine.Shader", "Find"),
    ("UnityEngine.Material", "EnableKeyword"),
    ("UnityEngine.Material", "DisableKeyword"),
    ("UnityEngine.Material", "HasProperty"),
    ("UnityEngine.SceneManagement.SceneManager", "LoadScene"),
    ("UnityEngine.Input", "GetAxis"),
    ("UnityEngine.Input", "GetAxisRaw"),
    ("UnityEngine.Input", "GetButton"),
    ("UnityEngine.Input", "GetButtonDown"),
    ("UnityEngine.Input", "GetButtonUp"),
    ("UnityEngine.MonoBehaviour", "Invoke"),
    ("UnityEngine.Component", "SendMessage"),
    ("UnityEngine.Component", "CompareTag"),
    ("UnityEngine.GameObject", "SendMessage"),
    ("UnityEngine.GameObject", "BroadcastMessage"),
    ("UnityEngine.LayerMask", "NameToLayer"),
    # alisa-demo 实证：AnimatorStateInfo.IsName(string) = 运行时按动画状态
    # 名查找（Animator 状态机哈希），~15 条（'Dead Talk'/'Jump In'/
    # 'Run Away'/'Bath Enter'）此前未识别 → F33 句子启发式放行。字符串
    # 参数恒为栈顶（struct 接收者在其下方），走栈顶组。
    ("UnityEngine.AnimatorStateInfo", "IsName"),
    # alisa-demo 实证：LayerMask.GetMask(params string[]) = 按图层名
    # 查找（'Block'/'Default'/'EnemyLayer'/'Ignore Raycast'），翻译破坏
    # 物理碰撞层。params 数组重载 arity=None → 走保守清栈路径（不产生
    # 证明），但 GetMask 的字符串参数是**确定性结构键**，作为独立
    # 栈顶 sink 跳过。
    ("UnityEngine.LayerMask", "GetMask"),
})
# 首参组：(type, method) → 参数总数（名字在 stack[-arity] 位置）
_STRUCTURAL_NAME_SINKS = {
    ("UnityEngine.Material", "SetFloat"): 2,
    ("UnityEngine.Material", "SetInt"): 2,
    ("UnityEngine.Material", "SetColor"): 2,
    ("UnityEngine.Material", "SetVector"): 2,
    ("UnityEngine.Material", "SetTexture"): 2,
    ("UnityEngine.Material", "SetMatrix"): 2,
    ("UnityEngine.Animator", "SetBool"): 2,
    ("UnityEngine.Animator", "SetFloat"): 2,
    ("UnityEngine.Animator", "SetInteger"): 2,
    ("UnityEngine.MonoBehaviour", "InvokeRepeating"): 3,
    # alisa-demo 实证：PlayerPrefs.GetString(key) 按持久化键名查找（输入
    # 绑定/设置键），翻译破坏按键配置。GetString(实例方法，键在 stack[-2])。
    # GetInt/GetFloat/HasKey/DeleteKey/SetInt/SetFloat 同 arity 形态；
    # SetString 首参=键 与 GetString 对称，同 arity 覆盖。
    ("UnityEngine.PlayerPrefs", "GetString"): 1,
    ("UnityEngine.PlayerPrefs", "GetInt"): 1,
    ("UnityEngine.PlayerPrefs", "GetFloat"): 1,
    ("UnityEngine.PlayerPrefs", "HasKey"): 1,
    ("UnityEngine.PlayerPrefs", "DeleteKey"): 1,
    ("UnityEngine.PlayerPrefs", "SetString"): 2,
    ("UnityEngine.PlayerPrefs", "SetInt"): 2,
    ("UnityEngine.PlayerPrefs", "SetFloat"): 2,
}
_IL_OPERAND_1 = frozenset({
    *range(0x0E, 0x14), 0x1F, *range(0x2B, 0x38), 0xDE,
})
_IL_OPERAND_4 = frozenset({
    0x20, 0x22, 0x27, 0x28, 0x29, *range(0x38, 0x45),
    0x6F, *range(0x70, 0x76), 0x79, *range(0x7B, 0x82),
    0x8C, 0x8D, 0x8F, 0xA3, 0xA4, 0xA5, 0xC2, 0xC6, 0xD0, 0xDD,
})
_IL_OPERAND_8 = frozenset({0x21, 0x23})
_IL_NO_OPERAND = frozenset({
    *range(0x00, 0x0E), *range(0x14, 0x1F), 0x25, 0x26, 0x2A,
    *range(0x46, 0x6F), 0x76, 0x7A, *range(0x82, 0x8C), 0x8E,
    *range(0x90, 0xA3), *range(0xB3, 0xBB), 0xC3,
    *range(0xD1, 0xDD), 0xDF, 0xE0,
})
_IL_FE_OPERANDS = {
    0x06: 4, 0x07: 4, 0x09: 2, 0x0A: 2, 0x0B: 2, 0x0C: 2,
    0x0D: 2, 0x0E: 2, 0x12: 1, 0x15: 4, 0x16: 4, 0x19: 1, 0x1C: 4,
}
_IL_FE_NO_OPERAND = frozenset({
    *range(0x00, 0x06), 0x0F, 0x11, 0x13, 0x14, 0x17, 0x18,
    0x1A, 0x1D, 0x1E,
})
_IL_CONTROL_FLOW_BOUNDARIES = frozenset({
    0x2A, *range(0x2B, 0x46), 0x7A, 0xDC, 0xDD, 0xDE,
})


def _simple_string_format_parameter_count(row) -> int | None:
    """Return arity only for String.Format overloads whose first arg is string."""
    signature = getattr(getattr(row, "Signature", None), "value", None)
    if not isinstance(signature, bytes) or len(signature) < 4:
        return None
    index = 1
    if signature[0] & 0x10:  # GENERIC: generic arity precedes parameter count.
        generic = _read_compressed_uint(signature, index)
        if generic is None:
            return None
        _, size = generic
        index += size
    parameter = _read_compressed_uint(signature, index)
    if parameter is None:
        return None
    parameter_count, size = parameter
    index += size
    # Supported String.Format overloads return string and take the format
    # string as their first parameter. Provider-first overloads are rejected.
    if (parameter_count <= 0 or index + 1 >= len(signature)
            or signature[index] != 0x0E
            or signature[index + 1] != 0x0E):
        return None
    return parameter_count


def _simple_concat_parameter_count(row) -> int | None:
    """Return arity for String.Concat overloads (2-4 params).

    params 数组 / IEnumerable 重载（参数数 1）返回 None——调用点保守清栈
    （无法按定长弹出，宁漏勿错）。
    """
    signature = getattr(getattr(row, "Signature", None), "value", None)
    if not isinstance(signature, bytes) or len(signature) < 4:
        return None
    index = 1
    if signature[0] & 0x10:  # GENERIC：泛型数在参数数之前
        generic = _read_compressed_uint(signature, index)
        if generic is None:
            return None
        _, size = generic
        index += size
    parameter = _read_compressed_uint(signature, index)
    if parameter is None:
        return None
    param_count, _ = parameter
    return param_count if 2 <= param_count <= 4 else None


def _string_source(element) -> tuple[frozenset, frozenset]:
    """栈元素 → (可验证 #US token 集合, 流经的 arg 参数索引集合)。

    src = ldstr 直接字面量；frag = Concat/Format 合并出的拼接片段——
    其 token 集合里的每个字面量都已被证明是同一字符串表达式的一部分
    （动态文本成分：`"Level " + level` 里的 `"Level "` 正是此形态）。
    arg 索引随片段传递：wrapper(s){ text.text = "Prefix " + s; } 时，
    参数 s 流经 Concat 进入 setter → 消费点把该参数标记 gained，包装
    链传播（调用点传字面量 → 验证）不因拼接断链。
    """
    if isinstance(element, tuple):
        if element[0] == "frag":
            return element[1], element[2]
        if element[0] == "src":
            return frozenset((element[1],)), frozenset()
        if element[0] == "arg":
            return frozenset(), frozenset((element[1],))
    return frozenset(), frozenset()


def _method_il(pe, rva: int) -> bytes | None:
    try:
        header = pe.get_data(rva, 12)
    except Exception:  # noqa: BLE001
        return None
    if not header:
        return None
    if header[0] & 3 == 2:
        header_size, code_size = 1, header[0] >> 2
    elif header[0] & 3 == 3 and len(header) >= 12:
        header_size = ((int.from_bytes(header[:2], "little") >> 12) & 0xF) * 4
        code_size = int.from_bytes(header[4:8], "little")
        if header_size < 12:
            return None
    else:
        return None
    try:
        body = pe.get_data(rva, header_size + code_size)
    except Exception:  # noqa: BLE001
        return None
    if len(body) < header_size + code_size:
        return None
    return body[header_size:header_size + code_size]


def _decode_il(code: bytes) -> list[tuple[int, int | None]] | None:
    instructions: list[tuple[int, int | None]] = []
    offset = 0
    while offset < len(code):
        opcode = code[offset]
        offset += 1
        operand_size = 0
        if opcode == 0xFE:
            if offset >= len(code):
                return None
            second = code[offset]
            offset += 1
            if second in _IL_FE_OPERANDS:
                operand_size = _IL_FE_OPERANDS[second]
            elif second not in _IL_FE_NO_OPERAND:
                return None
            opcode = 0xFE00 | second
        elif opcode == 0x45:
            if offset + 4 > len(code):
                return None
            count = int.from_bytes(code[offset:offset + 4], "little")
            operand_size = 4 + count * 4
        elif opcode in _IL_OPERAND_1:
            operand_size = 1
        elif opcode in _IL_OPERAND_4:
            operand_size = 4
        elif opcode in _IL_OPERAND_8:
            operand_size = 8
        elif opcode not in _IL_NO_OPERAND:
            return None
        if offset + operand_size > len(code):
            return None
        operand = (int.from_bytes(code[offset:offset + operand_size], "little")
                   if operand_size in (1, 2, 4, 8) else None)
        instructions.append((opcode, operand))
        offset += operand_size
    return instructions


_SIMPLE_ELEMENT_TYPES = frozenset({
    0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A,
    0x0B, 0x0C, 0x0D, 0x0E, 0x13, 0x16, 0x18, 0x19, 0x1B,
})
_FORMATTED_UI_MAX_INSTRUCTIONS = 24
_IL_FORMAT_VALUE_LOADS = frozenset({
    *range(0x02, 0x0A), 0x0E, 0x0F, 0x11, 0x12,
    *range(0x14, 0x20), 0x7E, 0x7F,
})


def _skip_compressed_uint(data: bytes, offset: int) -> int | None:
    """Skip one ECMA-335 compressed unsigned integer, returning the new offset."""
    encoded = _read_compressed_uint(data, offset)
    return offset + encoded[1] if encoded is not None else None


def _method_signature_string_params(sig: bytes) -> list[bool] | None:
    """解析方法签名 → 每个参数是否为 string（不含 receiver）；无法解析返回 None。

    仅识别常用 ELEMENT_TYPE；ARRAY/FNPTR/GENERICINST 等复杂编码保守返回 None，
    使该 helper 不参与传递验证（不会误放行，只是少验证一类）。

    首字节是调用约定：0x00 DEFAULT 是 C# 编译器最常见的约定，必须放行
    （此前与 0xFF 一并拒绝，默认约定方法签名全部解析失败 → 包装链
    传播对 ~17% 方法全灭，是证明率灾难性低下的根源之一，hickory 实证
    签名首字节分布 0x00×120/0x10×11/0x20×565）。0xFF（native/unmanaged
    约定，非标准 MethodDefSig 布局）仍拒绝。
    """

    def skip_type(i: int) -> int | None:
        if i >= len(sig):
            return None
        element = sig[i]
        if element in _SIMPLE_ELEMENT_TYPES:
            return i + 1
        if element in (0x0F, 0x10, 0x1C):  # PTR / BYREF / SZARRAY：后跟一个类型
            return skip_type(i + 1)
        if element in (0x11, 0x12):  # VALUETYPE / CLASS：后跟 typeDefOrRef token
            return _skip_compressed_uint(sig, i + 1)
        if element in (0x1E, 0x1F):  # CMOD_REQD / CMOD_OPT：token + 类型
            after = _skip_compressed_uint(sig, i + 1)
            return skip_type(after) if after is not None else None
        return None

    if not sig or sig[0] == 0xFF:
        return None
    index = 1
    if sig[0] & 0x10:  # GENERIC：泛型数在参数数之前
        generic = _read_compressed_uint(sig, index)
        if generic is None:
            return None
        _, size = generic
        index += size
    count = _read_compressed_uint(sig, index)
    if count is None:
        return None
    param_count, size = count
    index += size
    return_type = skip_type(index)
    if return_type is None:
        return None
    index = return_type
    out: list[bool] = []
    for _ in range(param_count):
        if index >= len(sig):
            return None
        if sig[index] == 0x41:  # SENTINEL：vararg 分隔，其后仍可能是参数
            index += 1
            if index >= len(sig):
                return None
        out.append(sig[index] == 0x0E)
        next_type = skip_type(index)
        if next_type is None:
            return None
        index = next_type
    return out


def _methoddef_identity_map(pe) -> dict[int, tuple[str, str]]:
    """MethodDef 索引（1-based）→ 声明身份 (TypeNamespace.TypeName, Name)。

    dnfile 的 TypeDef.MethodList 是 MDTableIndex 列表，其 row_index
    为 MethodDef 表的 1-based 索引（实证 FirstPersonController.Awake
    → row_index 1）。身份必须含方法名：MemberRef 身份是 (类型, 方法)
    二元组，闭包集合按此形态比较。
    """
    try:
        typedefs = pe.net.mdtables.TypeDef.rows
    except AttributeError:
        return {}
    out: dict[int, tuple[str, str]] = {}
    for td in typedefs:
        method_list = getattr(td, "MethodList", None)
        if not isinstance(method_list, (list, tuple)):
            continue
        ns = str(getattr(td, "TypeNamespace", "") or "")
        name = str(getattr(td, "TypeName", "") or "")
        type_full = f"{ns}.{name}" if ns else name
        for ref in method_list:
            row = getattr(ref, "row", None)
            m_idx = int(getattr(ref, "row_index", 0) or 0)
            if row is None or m_idx <= 0:
                continue
            out[m_idx] = (
                type_full,
                str(getattr(row, "Name", "") or ""),
            )
    return out


def _member_identity_map(pe) -> dict[int, tuple[str, str]]:
    """MemberRef token → (TypeNamespace.TypeName, Name)。"""
    try:
        rows = pe.net.mdtables.MemberRef.rows
    except AttributeError:
        return {}
    out: dict[int, tuple[str, str]] = {}
    for index, row in enumerate(rows, 1):
        declaring = getattr(getattr(row, "Class", None), "row", None)
        full_type = ".".join(filter(None, (
            str(getattr(declaring, "TypeNamespace", "") or ""),
            str(getattr(declaring, "TypeName", "") or ""),
        )))
        out[0x0A000000 | index] = (
            full_type, str(getattr(row, "Name", "") or ""))
    return out


def _cross_assembly_ui_sinks(pes) -> frozenset:
    """多程序集联合闭包：可达 UI setter 的方法身份集合（跨 DLL 链）。

    逐程序集分析时跨 DLL 调用（Fungus.Say 等 MemberRef）在链尾断掉——
    游戏字符串经 Fungus 方法 → Fungus.dll 内 set_text 的链路因此不可证
    （a-catfiends 实证：堆含空格 1924 条仅 1 条被证明）。本函数在所有
    发现的程序集上跑不动点闭包：(type, name) 身份加入 sink 集合 ⇔
    方法体调用了 sink（种子 = _UI_SETTER_TYPES 的 setter），跨程序集
    调用天然经 MemberRef 身份解析。返回的集合喂给逐程序集证明器的
    cross_sinks 参数——调用这些身份的 MemberRef 等同调用 setter。
    """
    sink_identities: set[tuple[str, str]] = {
        (full_type, name)
        for full_type in _UI_SETTER_TYPES
        for name in ("set_text", "SetText")}
    idents = [_methoddef_identity_map(pe) for pe in pes]
    members = [_member_identity_map(pe) for pe in pes]
    # 方法体解码缓存（闭包迭代最多 8 轮，重复解码浪费大）
    il_cache: dict[tuple[int, int], list | None] = {}
    for _round in range(8):
        grew = False
        for pe, ident_map, member_map in zip(pes, idents, members):
            try:
                method_rows = pe.net.mdtables.MethodDef.rows
            except AttributeError:
                continue
            for index, row in enumerate(method_rows, 1):
                identity = ident_map.get(index)
                if identity is None or identity in sink_identities:
                    continue
                rva = int(getattr(row, "Rva", 0) or 0)
                if not rva:
                    continue
                cache_key = (id(pe), rva)
                if cache_key not in il_cache:
                    code = _method_il(pe, rva)
                    il_cache[cache_key] = (_decode_il(code)
                                           if code is not None else None)
                instructions = il_cache[cache_key]
                if instructions is None:
                    continue
                for opcode, operand in instructions:
                    if opcode not in (0x28, 0x6F):
                        continue
                    if operand in member_map and member_map[operand] in sink_identities:
                        sink_identities.add(identity)
                        grew = True
                        break
        if not grew:
            break
    return frozenset(sink_identities)


def _verified_ui_user_string_tokens(pe, *, cross_sinks: frozenset = frozenset(),
                                    structural_out: set[int] | None = None,
                                    log_consumed_out: set[int] | None = None) -> set[int]:
    """Return #US token offsets proven to flow into verified UI setter calls.

    游戏常用自封装方法（SetTutorialText(text) 内部再 set_text），字面量先传给
    包装方法而非直接喂 setter——因此做传递式验证（cell-machine 真实样本）：
    1) 方法参数流入 UI setter / 已标记的包装方法 → 该参数标记为 UI 字符串参数；
    2) 任意方法内 ldstr 位于目标方法的 string 参数位 → token 验证通过；
    3) 逐轮传播直到不动点（包装链可多层）。
    未知调用 / 分支 / 未建模指令一律清空栈——Debug.Log、String.Concat 等
    非 UI 消费路径不会被误放行。
    """
    try:
        member_rows = pe.net.mdtables.MemberRef.rows
        method_rows = pe.net.mdtables.MethodDef.rows
    except AttributeError:
        return set()
    ui_setters: set[int] = set()
    string_formatters: dict[int, int] = {}
    string_concats: dict[int, int | None] = {}
    sb_ctors: set[int] = set()
    sb_appends: set[int] = set()
    sb_formats: dict[int, int | None] = {}
    sb_tostrings: set[int] = set()
    structural_sinks: set[int] = set()
    structural_name_sinks: dict[int, int] = {}
    safe_value_producers: set[int] = set()
    log_sinks: set[int] = set()
    member_identity: dict[int, tuple[str, str]] = {}
    for index, row in enumerate(member_rows, 1):
        declaring = getattr(getattr(row, "Class", None), "row", None)
        full_type = ".".join(filter(None, (
            str(getattr(declaring, "TypeNamespace", "") or ""),
            str(getattr(declaring, "TypeName", "") or ""),
        )))
        method_name = str(getattr(row, "Name", "") or "")
        member_identity[0x0A000000 | index] = (full_type, method_name)
        if (full_type, method_name) in _STRUCTURAL_SINKS:
            structural_sinks.add(0x0A000000 | index)
        elif (full_type, method_name) in _STRUCTURAL_NAME_SINKS:
            structural_name_sinks[0x0A000000 | index] = \
                _STRUCTURAL_NAME_SINKS[(full_type, method_name)]
        elif (full_type, method_name) in _LOG_SINKS:
            # 日志消费 = 确定性非 UI 负面证据（F33 配套：Console.WriteLine/
            # Debug.Log 的字面量是开发日志，句子形态启发式也不得放行）
            log_sinks.add(0x0A000000 | index)
        if full_type in _UI_SETTER_TYPES and method_name in {"set_text", "SetText"}:
            ui_setters.add(0x0A000000 | index)
        elif (full_type, method_name) in _UI_DISPLAY_CALLS:
            # IMGUI OnGUI 显示调用：字符串参数即屏幕文本
            ui_setters.add(0x0A000000 | index)
        elif full_type == "System.String" and method_name == "Format":
            parameter_count = _simple_string_format_parameter_count(row)
            if parameter_count is not None:
                string_formatters[0x0A000000 | index] = parameter_count
        elif full_type == "System.String" and method_name == "Concat":
            # 拼接（C# 的 + 编译产物）：arity 可解析时按定长弹栈合并
            # token 集合；params/IEnumerable 重载 arity=None → 调用点
            # 保守清栈（宁漏勿错）
            string_concats[0x0A000000 | index] = \
                _simple_concat_parameter_count(row)
        elif full_type == _STRINGBUILDER_TYPE:
            if method_name == _STRINGBUILDER_CTOR_NAME:
                sb_ctors.add(0x0A000000 | index)
            elif method_name in _STRINGBUILDER_APPEND_NAMES:
                sb_appends.add(0x0A000000 | index)
            elif method_name in _STRINGBUILDER_FORMAT_NAMES:
                sb_formats[0x0A000000 | index] = \
                    _simple_concat_parameter_count(row)
            elif method_name == _STRINGBUILDER_TOSTRING_NAME:
                sb_tostrings.add(0x0A000000 | index)
        elif method_name.startswith("get_") and full_type != "System.String":
            safe_value_producers.add(0x0A000000 | index)
    for index, row in enumerate(method_rows, 1):
        if str(getattr(row, "Name", "") or "").startswith("get_"):
            safe_value_producers.add(0x06000000 | index)
    if not ui_setters and not structural_sinks and not structural_name_sinks:
        # 无任何可判定 sink（UI/结构都无）时结构证明也无需运行
        return set()

    # 每个方法签名的 string 参数位置（None = 无法解析，不参与传递验证）
    string_params: dict[int, list[bool] | None] = {}
    for index, row in enumerate(method_rows, 1):
        sig = getattr(getattr(row, "Signature", None), "value", None)
        string_params[0x06000000 | index] = (
            _method_signature_string_params(sig)
            if isinstance(sig, bytes) else None)

    # 方法 token → 已证明流入 UI 文本的参数索引
    ui_string_params: dict[int, set[int]] = {}
    verified: set[int] = set()
    for _round in range(16):  # 包装链深度上限（真实游戏通常 1-2 层）
        grew = False
        for index, row in enumerate(method_rows, 1):
            method_token = 0x06000000 | index
            rva = int(getattr(row, "Rva", 0) or 0)
            code = _method_il(pe, rva) if rva else None
            instructions = _decode_il(code) if code is not None else None
            if instructions is None:
                continue
            # 栈元素：("src", us_token) / ("arg", param_idx) / "other"
            stack: list[tuple[str, int] | str] = []
            gained: set[int] = set()
            for opcode, operand in instructions:
                if opcode == 0x72:  # ldstr
                    stack.append(("src", operand & 0x00FFFFFF))
                elif opcode in (0x02, 0x03, 0x04, 0x05):  # ldarg.0-3
                    stack.append(("arg", opcode - 0x02))
                elif opcode == 0x0E:  # ldarg.s
                    stack.append(("arg", operand))
                elif opcode in (0x06, 0x07, 0x08, 0x09, 0x11, 0x14,
                                *range(0x15, 0x20), 0x20, 0x21, 0x22,
                                0x23, 0x8E):
                    # ldloc.* / ldnull / ldc.i4.* / ldc.* / ldlen → 普通值
                    stack.append("other")
                elif opcode == 0x25:  # dup
                    if stack:
                        stack.append(stack[-1])
                elif opcode in (0x0A, 0x0B, 0x0C, 0x0D, 0x10, 0x13,
                                0x26, 0x30):
                    # stloc.* / starg.s / pop / starg → 消费栈顶
                    if stack:
                        stack.pop()
                elif opcode in (0x7D, 0x80):
                    # stfld / stind.* → 消费接收者+值，清空最稳
                    stack.clear()
                elif opcode in (0x0F, 0x12):
                    # ldarga.s / ldloca.s → 引用地址，普通值
                    stack.append("other")
                elif opcode in (0x7B, 0x7C, 0x7E, 0x74, 0x75, 0x8C, 0x79,
                                0xA3):
                    # ldfld / ldflda / castclass / isinst / box / unbox.* /
                    # ldobj → 消费接收者，产出普通值
                    if stack:
                        stack.pop()
                    stack.append("other")
                elif opcode == 0x8D:  # newarr
                    # newarr → 数组元素来源暂存 ("arr", tokens)。调用点
                    # 是结构 sink（如 LayerMask.GetMask(params string[])）
                    # 时，数组的所有字面量元素都是结构键（params 数组的
                    # 元素就是被查找的名字本身）。
                    if stack:
                        stack.pop()  # 数组长度
                    stack.append(("arr", frozenset()))
                elif opcode == 0xA2:  # stelem.ref
                    # 栈序：arr, index, value → 元素并入 arr 容器。
                    # 通用数组合并：新数组元素来源并入已有 ("arr", tokens)。
                    # 多个元素通过 dup 共享同一 arr 引用（栈上相邻），
                    # 逐个 stelem 合并后保留。
                    if len(stack) >= 3:
                        value = stack[-1]
                        array, index_elem = stack[-3], stack[-2]
                        del stack[-3:]
                        if isinstance(array, tuple) \
                                and array[0] == "arr":
                            tokens, args = _string_source(value)
                            stack.append(
                                ("arr", frozenset(array[1] | tokens))
                                if tokens else array)
                        elif isinstance(index_elem, tuple) \
                                and index_elem[0] == "arr":
                            tokens, args = _string_source(value)
                            stack.append(
                                ("arr", frozenset(index_elem[1] | tokens))
                                if tokens else index_elem)
                        else:
                            stack.append("other")
                    else:
                        stack.clear()
                elif opcode == 0x73:  # newobj
                    if operand in sb_ctors:
                        # StringBuilder 实例：流式 append 链的 token 容器；
                        # 构造器带初始内容时（new StringBuilder("Init ")）
                        # 该字面量也是拼接成分
                        tokens: frozenset = frozenset()
                        args: frozenset = frozenset()
                        if stack:
                            t, a = _string_source(stack[-1])
                            if t or a:
                                tokens, args = t, a
                                stack.pop()
                        stack.append(("sb", tokens, args))
                    else:
                        stack.clear()
                elif opcode in (0x28, 0x6F):  # call / callvirt
                    if operand in structural_sinks:
                        # 结构证明（镜像用法）：栈顶字面量是运行时按名
                        # 查找键（GameObject.Find/SetTrigger 等），确定
                        # 性跳过——取代形态猜测（误翻名字断功能的
                        # 按钮失灵教训的证明版）
                        if structural_out is not None and stack:
                            tokens, _args = _string_source(stack[-1])
                            # params string[] 结构 sink（GetMask）：元素
                            # 在 ("arr", tokens) 容器里（数组元素 = 被
                            # 查找的名字），一并证明
                            structural_out |= tokens
                            if isinstance(stack[-1], tuple) \
                                    and stack[-1][0] == "arr":
                                structural_out |= stack[-1][1]
                        stack.clear()
                    elif operand in structural_name_sinks:
                        # 首参结构 sink（SetFloat(name, value) 形态）：
                        # 名字在 stack[-arity]
                        arity = structural_name_sinks[operand]
                        if structural_out is not None and len(stack) >= arity:
                            tokens, _args = _string_source(stack[-arity])
                            structural_out |= tokens
                        stack.clear()
                    elif operand in log_sinks:
                        # 日志消费负面证据：栈顶字面量流入 Console.WriteLine/
                        # Debug.Log → 收集为 mono_diagnostic（F33 配套，
                        # 防句子形态启发式放行开发日志串）
                        if log_consumed_out is not None and stack:
                            tokens, _args = _string_source(stack[-1])
                            # StringBuilder 实例被 log 消费（如
                            # print(sb.ToString()) 或 print("a"+x) 拼接后
                            # 打印）——拼接结果整体是日志文本，所有成分
                            # 都是 mono_diagnostic
                            if isinstance(stack[-1], tuple) \
                                    and stack[-1][0] == "sb":
                                log_consumed_out |= stack[-1][1]
                            log_consumed_out |= tokens
                        stack.clear()
                    elif operand in sb_tostrings:
                        # sb.ToString() → 拼接结果片段（流入 setter 时
                        # 全部 token 验证）
                        if (stack and isinstance(stack[-1], tuple)
                                and stack[-1][0] == "sb"):
                            tokens, args = stack[-1][1], stack[-1][2]
                            stack[-1] = (("frag", tokens, args)
                                         if tokens or args else "other")
                        else:
                            stack.clear()
                    elif operand in ui_setters or (
                            cross_sinks and member_identity.get(operand)
                            in cross_sinks):
                        # 本程序集 setter，或跨程序集 sink（Fungus.Say 等
                        # 方法体可达 setter 的导入方法）：等同 setter 消费
                        # 栈顶字符串来源
                        if stack:
                            top = stack[-1]
                            tokens, args = _string_source(top)
                            verified |= tokens
                            gained |= args
                        stack.clear()
                    elif operand in string_formatters:
                        # 格式串来源暂存为 ("frag", tokens, args)：只有真正
                        # 流入 setter / helper 才验证——格式化结果被丢弃
                        # （pop）时该格式串不是显示文本（回归保护）。
                        arity = string_formatters[operand]
                        if len(stack) >= arity:
                            source = stack[-arity]
                            del stack[-arity:]
                            tokens, args = _string_source(source)
                            stack.append(("frag", tokens, args)
                                         if tokens or args else "other")
                        else:
                            stack.clear()
                            stack.append("other")
                    elif operand in string_concats:
                        # 拼接片段（动态文本成分证明）：按 arity 弹出定长
                        # 参数，合并所有字符串来源的 token/arg 集合推回——
                        # 每个字面量都是同一字符串表达式的一部分，流入
                        # setter 时全部验证（`"Level " + level` 的
                        # `"Level "` 实证形态）；流经拼接的参数索引随
                        # 片段传播（包装链不断链）。非常量参数（数字/
                        # 变量）不阻断拼接。
                        arity = string_concats[operand]
                        if arity is not None and len(stack) >= arity:
                            sources = stack[-arity:]
                            del stack[-arity:]
                            tokens: set = set()
                            args: set = set()
                            for element in sources:
                                t, a = _string_source(element)
                                tokens |= t
                                args |= a
                            stack.append(("frag", frozenset(tokens),
                                          frozenset(args))
                                         if tokens or args else "other")
                        else:
                            stack.clear()
                    elif operand in sb_appends:
                        # StringBuilder.Append("HP: ")/AppendLine：合并
                        # 字符串来源 token 进 sb 实例（流式链的接收者
                        # 保留在栈上继续 append）。callvirt 栈序：
                        # 接收者在参数下方（stack[-2]）
                        if (len(stack) >= 2
                                and isinstance(stack[-2], tuple)
                                and stack[-2][0] == "sb"):
                            tokens, args = _string_source(stack[-1])
                            if tokens or args:
                                sb_elem = stack[-2]
                                stack[-2] = (
                                    "sb",
                                    frozenset(sb_elem[1] | tokens),
                                    frozenset(sb_elem[2] | args))
                            del stack[-1]  # 消费参数，sb 保留（流式）
                        else:
                            stack.clear()
                    elif operand in sb_formats:
                        # AppendFormat(string fmt, args...)：格式串与
                        # String.Format 同语义，结果并入 sb
                        arity = sb_formats[operand]
                        if (arity is not None
                                and len(stack) >= arity + 1
                                and isinstance(stack[-arity - 1], tuple)
                                and stack[-arity - 1][0] == "sb"):
                            source = stack[-arity]
                            del stack[-arity:]
                            tokens, args = _string_source(source)
                            if tokens or args:
                                sb_elem = stack[-1]
                                stack[-1] = (
                                    "sb",
                                    frozenset(sb_elem[1] | tokens),
                                    frozenset(sb_elem[2] | args))
                        else:
                            stack.clear()
                    elif operand in sb_tostrings:
                        # sb.ToString() → 拼接结果片段（流入 setter 时
                        # 全部 token 验证）
                        if (stack and isinstance(stack[-1], tuple)
                                and stack[-1][0] == "sb"):
                            tokens, args = stack[-1][1], stack[-1][2]
                            stack[-1] = (("frag", tokens, args)
                                         if tokens or args else "other")
                        else:
                            stack.clear()
                    elif ui_string_params.get(operand):
                        params = string_params.get(operand)
                        if params is not None:
                            for k, is_string in enumerate(params):
                                if not is_string:
                                    continue
                                position = len(stack) - (len(params) - k)
                                if position < 0:
                                    continue
                                element = stack[position]
                                if isinstance(element, tuple):
                                    tokens, args = _string_source(element)
                                    verified |= tokens
                                    gained |= args
                        stack.clear()
                    elif operand in safe_value_producers:  # getter
                        if stack:
                            stack.pop()
                        stack.append("other")
                    else:
                        stack.clear()
                elif opcode in _IL_CONTROL_FLOW_BOUNDARIES:
                    stack.clear()
                else:
                    # 未建模指令可能消费/产出任意栈值 → 清空（保守）
                    stack.clear()
            if gained:
                existing = ui_string_params.get(method_token)
                if existing is None:
                    ui_string_params[method_token] = set(gained)
                    grew = True
                elif not gained <= existing:
                    existing |= gained
                    grew = True
        if not grew:
            break
    return verified


def _structural_for_ui_text(s: str, is_ui_text: bool) -> bool:
    """已证明 UI 文本的硬结构判定（证据分层）。

    is_ui_text=True（数据流证明流入 setter）时在剥离首尾空白的内容上判：
    首尾空白填充片段规则（placeholders._WHITESPACE_PADDED_FRAGMENT，字符串
    表拆分碎片软猜测）不得推翻确定性证明——`"HP: " + hp + " of "` 的
    `" of "` 是真实显示成分（拼接片段实证形态），被 padding 规则截杀
    即为误漏。URL/GUID/纯符号等真硬结构在内容上仍拦截。

    TMP 标签组合串（<color=red>Warning!</color>）无论是否被证明都是
    显示文本——HTML 形态规则误伤标签串，标签语法证据优先。
    """
    from hanhua.core.tmp_tags import is_tag_composed
    if is_tag_composed(s):
        return False
    return is_hard_structural(s.strip() if is_ui_text else s)


def _is_mono_diagnostic_string(s: str) -> bool:
    """DLL 内部诊断/日志/错误消息（代码文本，非 UI）。

    uppercase_ui 的放行语义是「代码拼接的 UI 文本」（BEST SCORE: 等），
    但编辑器/算法 DLL 的测试日志与错误消息也含全大写词，需在此剔除。
    保守判定：命中任一开发特征即判诊断文本（避免翻译改坏符号/产生无意义
    译文）。真实 UI 文本（ANY CAMERA/SOLO）不含这些开发标记，不受影响。
    """
    return bool(
        _DEV_STATUS_WORD.match(s)
        or _DEV_STATUS_WORD_UPPER.match(s)
        or _DEBUG_BRACKET_PREFIX.match(s)
        or _INTERNAL_TYPE_NAME.search(s)
        or _ERROR_MSG_PATTERN.search(s)
        or _DEBUG_SENTINEL.search(s))


def extract_dll_user_strings(path: str | Path, file_id: str | None = None,
                             progress_cb: Callable | None = None, *,
                             cross_sinks: frozenset = frozenset()) -> ParsedFile:
    """提取 DLL #US 字符串 → ParsedFile。

    cross_sinks：跨程序集 UI sink 身份集合（_cross_assembly_ui_sinks
    的产物）——多 DLL 游戏由扫描管线一次性计算后传入（Fungus 等插件
    的显示方法链在逐程序集证明中不可见，联合闭包补齐跨 DLL 链）。
    """
    import dnfile
    p = Path(path)
    # UnityScript（旧 Unity JS 语言，Assembly-UnityScript*.dll）编译器生成的 IL
    # 与 C# 编译形态差异大，ui setter 验证链大面积失效（lilys-day-off 实证：
    # 825 条对话/服装/结局/选项文本全落 unverified 被跳过）。该程序集字面量
    # 几乎全是显示文本，因此对其放宽 unverified 判定。
    is_unityscript_asm = p.name.casefold().startswith("assembly-unityscript")
    # F40（bad-faith-the-game 实证）：句子形态启发式仅对游戏主程序集生效
    # ——插件/引擎 DLL（Fungus/Unity.2D.*/Unity.Mathematics）的句子型串
    # 几乎全是异常/调试/格式消息（'Invalid quality option'、'bool2({0},
    # {1})'），翻译无意义（bad-faith：Fungus 310 + Unity.2D 80 + Math 65
    # 全误放）。Unity 游戏主代码恒为 Assembly-CSharp*.dll。
    is_game_asm = (
        is_unityscript_asm
        or p.name.casefold().startswith("assembly-csharp"))
    fid = file_id or str(p).replace("\\", "/")
    pe = dnfile.dnPE(str(p))
    skipped: dict[str, int] = {}  # R5 静默跳过留档（哑识别可见化）
    try:
        us = pe.net.user_strings
        if us is None:
            return ParsedFile(
                fid, str(p), "v2_mono", [], "utf-8", "\n", {"kind": "mono"}, True)
        data = us.get_data_at_offset(0, us.sizeof())
        heap_file_offset = us.get_file_offset(0)
        entries: list[TextEntry] = []
        structural_tokens: set[int] = set()
        log_consumed: set[int] = set()
        verified_ui_tokens = _verified_ui_user_string_tokens(
            pe, cross_sinks=cross_sinks,
            structural_out=structural_tokens,
            log_consumed_out=log_consumed)
        for token_offset, offset, raw in _walk_us_heap_records(data):
            # ECMA-335 #US blobs always end with a one-byte kind flag.  The
            # flag is zero for ordinary ASCII strings and one for strings that
            # need the special-character marker; it is not UTF-16 payload.
            if raw:
                raw = raw[:-1]
            try:
                s = raw.decode("utf-16-le")
            except UnicodeDecodeError:
                # R5：解码失败静默跳过（极少见——#US 记录本身是 UTF-16）
                skipped["us_decode_failed"] = skipped.get("us_decode_failed", 0) + 1
                continue
            # 无 provenance 的 Bold/WASD/Move 等标识符按枚举名/绑定名保守排除。
            is_ui_text = token_offset in verified_ui_tokens
            # 字面量按键名标签（alisa-demo 实证）：InputBinding 键名/PlayerPrefs
            # 绑定键/重绑 UI 的按键名。虽被数据流证明为 UI 文本（mono_ui_setter），
            # 翻译后按键绑定失效——硬跳过，优先级高于显示判定。真实对话
            # （'Press Start to begin' 的 Start 是多词句）不受影响。
            input_key_label = is_ui_text and _is_input_key_name_label(s)
            # 结构证明（镜像用法）：字面量流入 GameObject.Find/SetTrigger
            # 等按名查找 API = 确定性结构键——优先于一切显示判定
            # （对象名同时被 Find 和 set_text 使用的按钮实证：宁漏勿坏）
            is_structural_proven = token_offset in structural_tokens
            if input_key_label:
                # 按键名标签虽被证明为 UI 文本，但翻译后按键绑定失效——
                # 硬跳过（宁漏勿坏）。留档计数。
                skipped["input_key_label"] = skipped.get("input_key_label", 0) + 1
                sample = _skipped_sample_entry(
                    fid, f"skip/us#{offset}", s, kind="us",
                    reason="input_key_label",
                    count=skipped["input_key_label"])
                if sample:
                    entries.append(sample)
                continue
            interaction_prompt = is_strong_interaction_prompt(s)
            # 代码拼接的 UI 文本证据：含空格 + 全大写强调词（UI 标签/教程句）。
            # driftapocalypse 真实样本：'BEST SCORE: '、'Hold LEFT or RIGHT to
            # turn\n('、'SHOW ANUNCIO'——字符串拼接未进 ui setter 验证链。
            # 诊断/日志字符串通常无全大写词，保持保守跳过。
            uppercase_ui = (
                " " in s and bool(_UI_UPPERCASE_WORD.search(s)))
            if not is_structural_proven and _structural_for_ui_text(s, is_ui_text):
                # R5/L1：结构形态（JSON/URL/路径/GUID/纯数字）静默跳过
                # 留档（计数 + 限量样本——内容可审计，样本 ≤10 条/原因）
                skipped["hard_structural"] = skipped.get("hard_structural", 0) + 1
                sample = _skipped_sample_entry(
                    fid, f"skip/us#{offset}", s, kind="us",
                    reason="hard_structural",
                    count=skipped["hard_structural"])
                if sample:
                    entries.append(sample)
                continue
            # 确定性引擎串（Shader 路径/输入绑定/哈希/枚举名等强形态，
            # is_engine_string_core 无编程命名猜测）优先于 uppercase_ui 猜测
            # ——tiiny-ragdoll 实证：'Hidden/Post FX/FXAA' 的 FX 全大写词
            # 触发 uppercase_ui 误判为 UI 文本，翻译后 Shader.Find 失败
            # → 渲染崩溃启动卡死。引擎查找键永不翻译，不论 UI 证据。
            if not is_structural_proven and is_engine_string_core(s):
                skipped["engine_core"] = skipped.get("engine_core", 0) + 1
                sample = _skipped_sample_entry(
                    fid, f"skip/us#{offset}", s, kind="us",
                    reason="engine_core", count=skipped["engine_core"])
                if sample:
                    entries.append(sample)
                continue
            if (not is_ui_text and not interaction_prompt and not uppercase_ui
                    and not is_structural_proven
                    and (is_code_identifier(s) or _is_engine_string(s)
                         or is_engine_string_gated(s))):
                # R5/L1：代码标识符/引擎串形态静默跳过留档（无 UI 证据时）
                skipped["code_identifier"] = skipped.get("code_identifier", 0) + 1
                sample = _skipped_sample_entry(
                    fid, f"skip/us#{offset}", s, kind="us",
                    reason="code_identifier",
                    count=skipped["code_identifier"])
                if sample:
                    entries.append(sample)
                continue
            # 内部诊断/日志/错误消息：即使命中 uppercase_ui 也判代码文本
            # （ProBuilder/Poly2Tri 实证：FAILED:/[FLIP]/CNOT 是开发诊断，
            # 翻译无意义且模型会改坏代码符号）。判 skipped 而非 continue：
            # 记录保留在 skipped 列表供审计（防过度拦截）。
            # 2026-08-24 come-back 实证补全：'There is already a virtual
            # axis named' / 'Invalid quality option' 是引擎诊断，但
            # _is_mono_diagnostic_string 只拦以开发词开头的句子——动词
            # 开头的错误句（'There is already...'）漏判 → 进池翻译无意义。
            # 未证明（非 is_ui_text）的句子型串若同时命中引擎诊断词
            # （axis/binding/quality option/not supported）也判诊断。
            mono_diagnostic = (
                not is_ui_text
                and (_is_mono_diagnostic_string(s)
                     or token_offset in log_consumed
                     or _ENGINE_DIAGNOSTIC_PATTERN.search(s)))
            # UnityScript 程序集：未被上方剔除的字符串全部按显示文本升级。
            # 含空格的是对话/UI/服装/结局文本；无空格语气词（'What?' 'Hahaha!'
            # 'Lily-chan!' 等对话反应词，lilys-day-off 实证 29 条）也是真实
            # 文本——纯标识符已被 is_code_identifier 剔除，此处剩余即文本。
            unityscript_display = (
                is_unityscript_asm
                and not is_ui_text and not interaction_prompt and not uppercase_ui)
            # TMP 标签组合串（<color=red>Warning!</color>）：标签语法是
            # 显示文本的强证据（诊断/日志不用 TMP 标签）——未证明时也
            # 放行（动态拼接的富文本串常不经直接 setter）
            from hanhua.core.tmp_tags import is_tag_composed
            tagged_display = (
                not is_ui_text and not mono_diagnostic
                and is_tag_composed(s))
            # F33（78-hour-rain 实证）：句子形态显示文本——未证明流入
            # setter 的小写/混合大小写叙事/教程/状态/标签句，由形态
            # 启发式放行（质量门/审核兜底），不再整类跳过。
            # F40（bad-faith 实证）：仅游戏主程序集生效——插件/引擎
            # DLL 的句子型串是异常/调试/格式消息（见 is_game_asm 注）。
            sentence_display = (
                not is_ui_text and not mono_diagnostic
                and is_game_asm
                and (_is_sentence_display_text(s)
                     or _is_exclamation_ui_word(s)))
            display_text = (
                is_ui_text or interaction_prompt or uppercase_ui
                or unityscript_display or tagged_display
                or sentence_display) and not mono_diagnostic \
                and not is_structural_proven
            entries.append(TextEntry(
                file_id=fid, key_path=f"us#{offset}",
                original=s, status="pending" if display_text else STATUS_SKIPPED,
                meta={
                    "kind": "us",
                    # 记录起始 = 压缩前缀位置（写回端定位用，与 CLR token 语义一致）
                    "record_offset": heap_file_offset + token_offset,
                    # 数据区位置（前缀之后；旧字段，写回端仅向后兼容旧项目库）
                    "heap_offset": heap_file_offset + offset,
                    "flag_offset": heap_file_offset + offset + len(raw),
                    "utf16_len": len(raw),
                    # 精确字数预算：#US 记录容量 = UTF-16 码元数 = 字符数
                    # （1 字符 = 1 码元 = 2 字节），所以「译文最多字符数」
                    # = len(raw) // 2。直接写 len(raw) 会把预算翻倍：模型按
                    # 2 倍字符输出 → 写回按码元容量截断（taxes 'I did ' 实证
                    # max_chars=12 实为 6 码元容量）。默认兜底（原文 UTF-8
                    # 字节 // 3）对 ASCII 原文同样低估，此处显式给出精确预算。
                    "max_chars": len(raw) // 2,
                    "confidence": (
                        "high" if is_ui_text or interaction_prompt
                        else "medium" if (uppercase_ui or unityscript_display
                                          or sentence_display)
                        else "low"),
                    "role": "display" if display_text else "structural",
                    "disposition": "translate" if display_text else "structural",
                    "reason": (
                        "mono_structural_sink" if is_structural_proven
                        else "mono_ui_setter" if is_ui_text
                        else "mono_diagnostic" if mono_diagnostic
                        else "interaction_prompt" if interaction_prompt
                        else "user_string_uppercase_ui" if uppercase_ui
                        else "unityscript_user_string" if unityscript_display
                        else "tmp_tag_composed" if tagged_display
                        else "user_string_sentence" if sentence_display
                        else "unverified_user_string"),
                }))
        for e in entries:
            if e.status == "pending" and _structural_for_ui_text(
                    e.original, e.meta.get("reason") == "mono_ui_setter"):
                e.status = STATUS_SKIPPED
        # 样本计数回写：限量样本的 skipped_count 是累计值，报告聚合需
        # 真实总数（消费端按 (file_id, reason, obj) 取 max）
        _finalize_skipped_counts(entries, skipped)
        noise = looks_like_noise_file(entries)
        return ParsedFile(
            fid, str(p), "v2_mono", entries, "utf-8", "\n", {"kind": "mono"},
            noise, skipped)
    finally:
        pe.close()
