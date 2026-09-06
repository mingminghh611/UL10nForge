from __future__ import annotations

import json
import re as _re
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from hanhua.core.formats import read_text
from hanhua.core.models import STATUS_SKIPPED, TextEntry
from hanhua.core.placeholders import (
    is_key_style_identifier,
    looks_like_key_field,
    should_skip,
)


TypedPath = tuple[str | int, ...]


@dataclass(frozen=True)
class _StringSpan:
    start: int
    end: int
    value: str


@dataclass(frozen=True)
class _Document:
    data: Any
    value_spans: dict[TypedPath, _StringSpan]


@dataclass(frozen=True)
class _Pairs:
    items: list[tuple[str, Any]]


def _preserve_pairs(pairs: list[tuple[str, Any]]) -> _Pairs:
    return _Pairs(pairs)


def _normalize(node: Any) -> Any:
    if isinstance(node, _Pairs):
        result: dict[str, Any] = {}
        for key, value in node.items:
            if key in result:
                del result[key]
            result[key] = _normalize(value)
        return result
    if isinstance(node, list):
        return [_normalize(value) for value in node]
    return node


def _mask_jsonc(text: str) -> str:
    chars = list(text)
    state = "normal"
    quote_start = -1
    comment_start = -1
    i = 0
    while i < len(chars):
        char = chars[i]
        if state == "normal":
            if char == '"':
                state = "string"
                quote_start = i
            elif char == "/" and i + 1 < len(chars) and chars[i + 1] == "/":
                state = "line_comment"
                chars[i] = chars[i + 1] = " "
                i += 1
            elif char == "/" and i + 1 < len(chars) and chars[i + 1] == "*":
                state = "block_comment"
                comment_start = i
                chars[i] = chars[i + 1] = " "
                i += 1
        elif state == "string":
            if char == "\\":
                state = "escape"
            elif char == '"':
                state = "normal"
        elif state == "escape":
            state = "string"
        elif state == "line_comment":
            if char in "\r\n":
                state = "normal"
            else:
                chars[i] = " "
        elif state == "block_comment":
            if char == "*" and i + 1 < len(chars) and chars[i + 1] == "/":
                chars[i] = chars[i + 1] = " "
                i += 1
                state = "normal"
            elif char not in "\r\n":
                chars[i] = " "
        i += 1

    if state in {"string", "escape"}:
        raise JSONDecodeError("Unterminated string", text, quote_start)
    if state == "block_comment":
        raise JSONDecodeError("Unterminated block comment", text, comment_start)

    masked = "".join(chars)
    chars = list(masked)
    state = "normal"
    i = 0
    while i < len(chars):
        char = chars[i]
        if state == "normal":
            if char == '"':
                state = "string"
            elif char == ",":
                lookahead = i + 1
                while lookahead < len(chars) and chars[lookahead].isspace():
                    lookahead += 1
                if lookahead < len(chars) and chars[lookahead] in "}]":
                    chars[i] = " "
        elif state == "string":
            if char == "\\":
                state = "escape"
            elif char == '"':
                state = "normal"
        else:
            state = "string"
        i += 1
    return "".join(chars)


def _load_data(text: str) -> tuple[Any, str]:
    extra_bom = text.find("\ufeff", 1 if text.startswith("\ufeff") else 0)
    if extra_bom != -1:
        raise JSONDecodeError("U+FEFF is only allowed at document start", text, extra_bom)
    parse_text = " " + text[1:] if text.startswith("\ufeff") else text
    # F56\uff08Rendezvous \u5b9e\u8bc1\uff09\uff1a\u7a7a/\u7eaf\u7a7a\u767d JSON \u6587\u4ef6\uff08steam_settings \u7834\u89e3
    # \u914d\u7f6e\u7684\u7a7a achievements.json\uff09\u2014\u2014json.loads('') \u629b "Expecting value"
    # \u5bfc\u81f4\u6574\u573a\u626b\u63cf\u5d29\u6e83\u3002\u7a7a\u6587\u6863 \u2192 \u7a7a\u7ed3\u679c\uff08\u4e0d\u629b\u2014\u2014\u626b\u63cf\u5bb9\u9519\uff09\u3002
    if not parse_text.strip():
        return {}, ""
    try:
        data = json.loads(parse_text, object_pairs_hook=_preserve_pairs)
        return data, parse_text
    except JSONDecodeError:
        masked = _mask_jsonc(parse_text)
        data = json.loads(masked, object_pairs_hook=_preserve_pairs)
        return data, masked


def _collect_value_tokens(source: str, parse_text: str) -> list[_StringSpan]:
    spans: list[_StringSpan] = []
    i = 0
    while i < len(parse_text):
        if parse_text[i] != '"':
            i += 1
            continue
        start = i
        i += 1
        while i < len(parse_text):
            if parse_text[i] == "\\":
                i += 2
            elif parse_text[i] == '"':
                i += 1
                break
            else:
                i += 1
        else:
            raise JSONDecodeError("Unterminated string", source, start)

        end = i
        lookahead = end
        while lookahead < len(parse_text) and parse_text[lookahead].isspace():
            lookahead += 1
        if lookahead < len(parse_text) and parse_text[lookahead] == ":":
            continue
        value = json.loads(source[start:end])
        spans.append(_StringSpan(start, end, value))
    return spans


def _remove_path_subtree(
    value_spans: dict[TypedPath, _StringSpan], prefix: TypedPath
) -> None:
    for path in list(value_spans):
        if path[: len(prefix)] == prefix:
            del value_spans[path]


def _pair_value_spans(
    node: Any,
    spans: list[_StringSpan],
    value_spans: dict[TypedPath, _StringSpan],
    path: TypedPath = (),
    position: int = 0,
) -> int:
    if isinstance(node, _Pairs):
        seen: set[str] = set()
        for key, value in node.items:
            child_path = path + (key,)
            if key in seen:
                _remove_path_subtree(value_spans, child_path)
            seen.add(key)
            position = _pair_value_spans(
                value, spans, value_spans, child_path, position
            )
    elif isinstance(node, list):
        for index, value in enumerate(node):
            position = _pair_value_spans(
                value, spans, value_spans, path + (index,), position
            )
    elif isinstance(node, str):
        if position >= len(spans) or spans[position].value != node:
            raise ValueError("JSON string value/span mismatch")
        value_spans[path] = spans[position]
        position += 1
    return position


def _load_document(text: str) -> _Document:
    occurrences, parse_text = _load_data(text)
    spans = _collect_value_tokens(text, parse_text)
    value_spans: dict[TypedPath, _StringSpan] = {}
    consumed = _pair_value_spans(occurrences, spans, value_spans)
    if consumed != len(spans):
        raise ValueError("JSON string value/span count mismatch")
    data = _normalize(occurrences)
    if any(_value_at(data, path) != span.value for path, span in value_spans.items()):
        raise ValueError("JSON effective value/span mismatch")
    return _Document(data, value_spans)


def _value_at(data: Any, path: TypedPath) -> Any:
    node = data
    try:
        for segment in path:
            node = node[segment]
    except (KeyError, IndexError, TypeError):
        return _MISSING
    return node


_MISSING = object()


def _escape_segment(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _unescape_segment(segment: str) -> str:
    result: list[str] = []
    i = 0
    while i < len(segment):
        if segment[i] != "~":
            result.append(segment[i])
            i += 1
            continue
        if i + 1 >= len(segment) or segment[i + 1] not in "01":
            raise ValueError(f"invalid RFC 6901 segment: {segment!r}")
        result.append("~" if segment[i + 1] == "0" else "/")
        i += 2
    return "".join(result)


def _encode_path(path: TypedPath) -> str:
    return "/".join(
        str(segment) if isinstance(segment, int) else _escape_segment(segment)
        for segment in path
    )


def _resolve_path(data: Any, key_path: str) -> TypedPath:
    if key_path == "" and not isinstance(data, (dict, list)):
        return ()
    node = data
    typed: list[str | int] = []
    for segment in key_path.split("/"):
        if isinstance(node, list):
            if not segment.isascii() or not segment.isdecimal():
                raise ValueError(f"invalid JSON array index: {segment!r}")
            index = int(segment)
            node = node[index]
            typed.append(index)
        elif isinstance(node, dict):
            key = _unescape_segment(segment)
            node = node[key]
            typed.append(key)
        else:
            raise ValueError(f"JSON path traverses a scalar: {key_path!r}")
    return tuple(typed)


def extract_json(path: str | Path, file_id: str | None = None) -> list[TextEntry]:
    p = Path(path)
    return extract_json_text(read_text(p), file_id or p.name)


def extract_json_text(text: str, file_id: str | None = None) -> list[TextEntry]:
    document = _load_document(text)
    if _is_spine_document(document.data):
        # Spine 骨骼动画 JSON（soul-delivery/zero-deaths/monsters-of-new-spark
        # 实证 2026-09-01）：骨骼名/插槽/附件/皮肤/动画名全是运行库查表引用，
        # 翻译即写坏骨骼动画。文件级判定（skeleton 键 + 全键子集）在条目级
        # 结构过滤之前——整文件不产生条目（写回侧同样不写，闭环）。
        return []
    if _is_pachat_document(document.data):
        # PAChat 终端脚本 JSON（project-arrhythmia 实证 2026-09-01）：分支名/
        # 命令 token/settings 配置是机器引用，真显示文本只占少部分且逐条混杂
        # ——整文件跳过（宁漏勿坏，防译坏分支跳转/命令解析）。判定与
        # Spine 同款（branches 键 + 全键 ⊆ 本集，全语料 0 误报）。
        return []
    out = [
        TextEntry(
            file_id=file_id or "json",
            key_path=_encode_path(path),
            original=span.value,
        )
        for path, span in document.value_spans.items()
    ]
    for entry, path in zip(out, document.value_spans, strict=True):
        if ((path and any(isinstance(seg, str) and looks_like_key_field(seg)
                          for seg in path))
                or should_skip(entry.original)):
            # 键字段值或结构值跳过：Addressables catalog 的 m_AssemblyName 值
            # （.NET 程序集全名）、m_InternalId（URL）、m_Address（Assets 路径）、
            # m_InternalIds/N（数组父段是键字段，叶子是数字索引）都是真实漏网案例
            entry.status = STATUS_SKIPPED
        elif _is_json_data_area(entry.key_path, entry.original):
            # JSON 数据区结构过滤（8 More Lives 实证 2026-08-31）：hex 颜色/
            # 数值公式/资源引用枚举/数组下标枚举都是游戏内部引用标识符，
            # 翻译成中文破坏功能；关卡地图编辑器顶层键（project-arrhythmia
            # 实证 2026-09-01）整键区跳过。判定与 asset 内嵌 TextAsset 共用
            # 同一规则（unity/extractor 调用方对同路径写回，行不写=不破坏）。
            entry.status = STATUS_SKIPPED
    return out


# ── JSON 数据区过滤（8 More Lives 实证 2026-08-31）──────────────────────
# 游戏数据文件（平衡表/全局设置/技能/装备字典）里叶子字段大量是机器可读值
# ——hex 颜色、数值公式、资源引用（音效/图标/逻辑枚举）、数组下标+枚举标签。
# 这些值翻译成中文必然破坏：颜色查表、属性公式（STR*0.2）、音效/动画/逻辑名
# （STRIKE/FIST/UNDEAD）都是游戏内部引用标识符。ARCTIC/STRIKE/MELEE 等词在
# 数据区（GlobalBiomsDistribution/0/0）与真文本区（Texts/ARCTIC/Text='Arctic'）
# 同时出现——只能按 inner_path 结构拦截（structure-based），绝不按值拦截
# （value-based 会误杀显示区）。规则与 unity/extractor 共用一份，防分叉。

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
# 关卡地图编辑器 JSON（project-arrhythmia 实证 2026-09-01）顶层键全集：
# objects/prefab_objects/prefabs/checkpoints/themes/markers/events/editor/
# parallax_settings/editor_prefab_spawn。对象是游戏内地图编辑器（玩家/关卡
# 设计者摆放实体），字符串值 = UUID（实体 id/p_id 互引）、base64 缩略图、
# 对象类型名（Checkpoint/Bullet Base）、动画曲线名（InOutSine）、纹理/节点
# 结构名（Face Root/Eye Center/X Para）、富文本关卡贴字（<rotate=90><b>SO
# COLD——地图设计者做的关卡说明贴字，玩家在游戏里看到）。判定：
#   - 顶层键全 ∈ MAP_KEYS → 编辑器数据文件（转译整文件无意义，写回位置
#     由内部结构对齐，翻译值只作字典——无显示语义）
#   - 其余 JSON 文件不受影响（顶层键部分命中不触发；触发需全部键 ∈ 集合）
_MAP_EDITOR_TOP_KEYS = frozenset((
    "editor", "editor_prefab_spawn", "parallax_settings", "checkpoints",
    "objects", "prefab_objects", "prefabs", "themes", "markers", "events",
    "triggers",  # project-arrhythmia obj4/179/190 实证：触发器表同属编辑器数据
))
# Spine 骨骼动画 JSON（soul-delivery/zero-deaths/monsters-of-new-spark 实证
# 2026-09-01）：skeleton/bones/slots/ik/skins/animations 是 Spine 运行库
# 数据，值全是骨骼名/插槽名/附件名/皮肤名/动画名引用——翻译写坏，骨骼
# 动画直接加载失败（skin 名、attachment 名、动画名查表全断）。全语料 131
# 游戏顶层键普查：这些键**只**出现在 Spine 文件（含 skeleton 的 98 处
# 100% 全键 ⊆ 本集），无任何显示文本 JSON 使用——结构信号 100% 确定。
# 文件级判定（extract_json_text）：顶层含 'skeleton' 且所有键 ⊆ 本集 →
# 整文件是 Spine 动画数据，全部条目跳过。
_SPINE_TOP_KEYS = frozenset((
    "skeleton", "bones", "slots", "ik", "skins", "animations",
    "transform",  # obj952 实证：IK 约束 transform 数组（Spine transform 约束）
    "events",     # Spine 事件表（与地图编辑器 events 同名但此处是动画事件）
))
# PAChat 终端脚本 JSON 顶层键全集（project-arrhythmia 实证 2026-09-01）：
# {settings, branches}。settings 是流程配置块（initial_branch=入口分支名），
# branches 是分支数组（name=分支标识符、elements=显示/命令元素列表）。
# 顶层全键 ⊆ 本集 → PAChat 脚本（与 Spine 同款文件级判定，0 误报）。
_PACHAT_TOP_KEYS = frozenset(("settings", "branches"))
# PAChat 命令 token 前缀（小写命令名 + 冒号）：wait::2/branch::login/
# replaceline::6::…/setbg::E0E0E0/loadscene:Main Menu/setsavedlevel:0:0/
# font-style:bold。命令名必须字节级原样保留（终端按 :: 拆分解析），
# 翻译内容会破坏命令解析 → 整串跳过。真显示文本以大写/竖线/百分比开头
# （'0%  [...]'/'| Login: |'/'Subject : Jane'/'Booting ROM'），不命中。
_PACHAT_CMD = _re.compile(r"^[a-z_]{2,}:")


def _is_spine_document(data: Any) -> bool:
    """JSON 根是否 Spine 骨骼动画文件（返回 True → 整文件跳过）。

    判定：根为 dict、含 'skeleton' 键、且全部顶层键 ⊆ _SPINE_TOP_KEYS。
    变体覆盖（全语料实证）：低版本 Spine 缺 ik/events（Ghosts.json 5 键）、
    含 transform 约束（obj952 2000.json）、事件表（events 键）。skeleton
    键是 Spine 文件专属标识（131 游戏普查 0 误报），全键子集判定防半截
    JSON 或嵌在真游戏文件里的子结构误伤。
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
    """JSON 条目 inner_path 是否数据区条目（返回 True → 跳过）。

    与 asset 内嵌 TextAsset 共用同一规则（unity/extractor 的
    _is_json_data_area），保证独立 .json 文件与 Unity 资源里的 JSON
    TextAsset 过滤口径一致，规则不分叉。

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
        # 地图编辑器数据：路径走 objects/prefabs/... 顶层 → 转译无显示语义。
        # 判定只需首段 ∈ 集合（不用 all-keys——首段命中的就是编辑器字典，
        # 内部 UUID 互引/节点结构名/曲线名全是机器引用）。显示文本永不落入
        # 该首段（对话框/字幕在 Texts 等独立顶层）。
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


def detect_indent(text: str) -> int | str | None:
    for line in text.splitlines():
        if line.startswith("\t"):
            return "\t"
        stripped = line.lstrip(" ")
        if line != stripped:
            return len(line) - len(stripped)
    return None


def apply_json(
    entries: list[TextEntry], source_text: str, ensure_ascii: bool = False,
    skipped: set[str] | None = None,
) -> str:
    """把译文按 key_path 写回 JSON 文本（原位替换，保格式）。

    skipped（0.42.1 假记账根治）：可选集合出参——**未被实际应用**的条目
    key_path 收集于此（静默丢弃路径全量暴露：key_style/键字段跳过、
    目标值不匹配跳过）。调用方（writer/审计）据此逐条记账，杜绝
    「从未落盘却记 written」的假账（审计通过但游戏里是英文的确定性
    路径）。

    目标值不匹配（P5a）：不再 raise ValueError——单条坏条目（旧库 meta
    过期/key_path 失效）跳过即可（进 skipped 集合），由调用方按
    json_target_mismatch 记 rejected；raise 会击穿整场写回（writer 不
    捕获 → RuntimeError 全场终止），与「宁漏勿坏：拿不准的写回宁可
    保留原文」相悖。
    """
    document = _load_document(source_text)
    translations: dict[TypedPath, str] = {}
    for entry in entries:
        if not entry.translation:
            continue
        if entry.status == STATUS_SKIPPED:
            # 提取期数据区/键字段条目已跳过（结构判定），异常路径若带了
            # 译文也拒绝写回——宁漏勿坏（GameColors/Hex='#…' 等内部引用
            # 一旦写坏游戏查表/公式直接失效）
            continue
        path = _resolve_path(document.data, entry.key_path)
        leaf = path[-1] if path else None
        if is_key_style_identifier(entry.original) or (
            isinstance(leaf, str) and looks_like_key_field(leaf)
        ):
            if skipped is not None:
                skipped.add(entry.key_path)
            continue
        span = document.value_spans.get(path)
        # strip 容差（Rendezvous 实证 2026-08-17）：ink 对话行提取时
        # original 经 strip（'...Tyrell! ' 尾空格被去），写回校验严格
        # 相等失败——首尾空白是格式噪音，strip 后相等即匹配
        if (span is None
                or (span.value != entry.original
                    and span.value.strip() != entry.original.strip())):
            # P5a（0.42.1）：单条目标不匹配跳过不抛——旧库 meta 过期/
            # key_path 失效只影响本条（进 skipped 集合由调用方记
            # rejected），整场写回不再被单条坏数据击穿
            if skipped is not None:
                skipped.add(entry.key_path)
            continue
        translations[path] = entry.translation

    if not translations:
        return source_text

    replacements = [
        (
            document.value_spans[path].start,
            document.value_spans[path].end,
            json.dumps(translation, ensure_ascii=ensure_ascii),
        )
        for path, translation in translations.items()
    ]
    result = source_text
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result
