"""写回后地毯式审计（通用版，适配任意游戏/任意格式）。

背景（containment-breach-hd 实证）：JSON 内容的 .subs 语言包被 txt 格式
处理，写回把 `"bat_nor": "9V Battery",` 用中文弯引号 + 丢逗号整段替换
→ 文件级 JSON 失效 → 游戏启动读语言包崩溃卡死。此前的写回闸门只查
条目级（rejected/truncated/逻辑键），不查「整文件结构是否被破坏」。

本模块在写回完成后做两层地毯式审计，**不绑定任何单一游戏/格式**：

  第 1 层 确定性结构审计（零模型成本，纯代码判定，任何一项 FAIL 都
         阻断发布 / 触发重写回）——对写回后的每个文本文件独立判定：
    - 字节层守恒：编码 / BOM / EOL 必须与原文件一致（静默改编码 → 游戏
      按原编码读会乱码；EOL 变化被版本控制/脚本误判——两类都是写回风险）
    - 行数守恒：写回文件行数 == 源文件行数（任何格式都适用；行重建格式
      丢行 → 游戏解析越界黑屏）
    - 结构守恒（按格式分派）：只允许「叶子值」被译文替换，容器结构
      （键集合 / 元素名 / 属性名 / 块结构 / 分隔符 / 时间码）必须原样：
        · JSON（按内容判定 {/[ 开头，与扩展名无关）：深比结构树，键集合
          与列表长度逐层一致，仅叶子值可变；非严格 JSON（缺逗号的 .subs）
          回退逐行引号成对 + 尾随逗号守恒；原文严格可解析 → 写回也必须
        · XML：ET 重解析成功 + 元素树 tag/属性键一致，仅文本节点可变
        · CSV：行数一致 + 每行列数一致（或仅目标列 +1）+ 首列 key 原样
        · YAML：行数守恒（apply_yaml 自带）+ 键行结构原样
        · PO：msgid 原文不变，仅 msgstr 槽被译文替换 + 块数守恒
        · 字幕（SRT/VTT/ASS/SSA/LRC）：时间码/序号/头/样式行原样，仅对白
        · TXT/KV：注释/节/空行原样，kv 键（分隔符左侧）不被翻译改写
    - 条目级：占位符（{0} / %s / %1$s / {name} / <b> / \\n）原文 ⊆ 译文
    - 渲染一致性：store 译文经 writer 同源渲染 + 同源编码（bytes）== 磁盘
      写回文件（写回链路与审计链路同源，任何分歧都说明写回不一致）

  第 2 层 审校模型结构/语义审计（ReviewModelService / Qwen3.5-4B，
         同翻译审核服务）——只对「第 1 层 PASS 且有差异」的文件跑，避免
         在已结构破坏的文件上浪费模型调用（它们反正要重写回）：
    - 把 (源行, 写回行) 有差异的对批量送审，组批 ≤ batch_size
    - 模型判定 PASS / STRUCTURE_BROKEN / VALUE_INVERTED / PLACEHOLDER_LOST
    - 任何非 PASS → 记录 model_flags（软复核，需人工确认）
    - 模型服务不可用 → 覆盖有缺口，标记 model_unavailable 阻断发布

  第 2 层 b 二进制对象证据卡审计（0.39.0 M3）——二进制（v2_）文件无
         「行」概念，第 2 层行对审计天然不覆盖（audit_deterministic/
         audit_model 均跳过 v2_）。但二进制写回现场已有确定性重开验证
         （writer._verify_saved_bundle / #US 单记录重读 / metadata 全池
         比对——字节层已零漏洞），缺的只是语义复核：writer 在写回现场
         记录每个成功对象的证据卡（文件/对象/类型/逐处 原文→译文），
         本层把证据卡批量送模型判定同一四值结论（PASS/STRUCTURE_BROKEN/
         VALUE_INVERTED/PLACEHOLDER_LOST），非 PASS 记 model_flags 软复核。
         证据卡为空（无 v2 写回或 v2_result 未传入）→ 本层零行为，
         不产生任何模型调用（不误报 model_unavailable）；有卡但模型不可用
         → 覆盖缺口，与文本文件同口径阻断发布。

审计结果写入 writeback/audit.txt（与 writeback.txt 并列），runner 在
写回完成后调用。第 1 层任何文件 FAIL → needs_rewrite=True（结构破坏
必须重写回）；第 2 层模型 FLAG 只记录待人工复核。

设计要点：
  - 审计不修改任何文件——只读对比源目录与写回目录
  - 第 1 层是「硬闸门」（结构破坏必然拦截，零误报）；第 2 层是「软
    复核」（模型可能误报，记 flag 不硬拦）
  - 结构与字节检查全部基于「原文 vs 写回」双端对比，不依赖 store——
    即使 store 数据本身有误，只要它写出的文件破坏结构，审计照样拦截
  - 与 writer._render + _encode 同源渲染/编码，保证审计口径与真实写回一致
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .formats import apply_format_text, detect_eol, read_text
from .models import TextEntry

# ── 数据结构 ──────────────────────────────────────────────────────


@dataclass
class FileAudit:
    rel_path: str
    format: str
    # 字节层守恒
    encoding_conserved: bool = True
    eol_conserved: bool = True
    # 结构层守恒（按格式分派）
    line_conserved: bool = True
    structure_ok: bool = True          # 格式专用结构（JSON 键树/XML 元素/…）
    quote_paired: bool = True          # JSON 内容：引号成对
    comma_conserved: bool = True       # JSON 内容：尾随逗号守恒
    strict_parse_ok: bool = True       # 原文严格可解析 → 写回也必须
    delimiter_conserved: bool = True   # CSV 分隔符/行列数
    render_consistent: bool = True     # store 渲染+编码(bytes) == 磁盘写回
    # 条目级
    placeholder_lost: list[str] = field(default_factory=list)
    ref_field_lost: list[str] = field(default_factory=list)   # 键字段枚举被译（引用断链）
    # 模型层（软复核）
    model_verdict: str = "PASS"
    model_issue: str = ""
    # 汇总
    changed_entries: int = 0
    detail: list[str] = field(default_factory=list)   # 附加说明（非失败也记）

    @property
    def passed(self) -> bool:
        return (self.encoding_conserved and self.eol_conserved
                and self.line_conserved and self.structure_ok
                and self.quote_paired and self.comma_conserved
                and self.strict_parse_ok and self.delimiter_conserved
                and self.render_consistent and not self.placeholder_lost
                and not self.ref_field_lost)


@dataclass
class AuditResult:
    files: list[FileAudit] = field(default_factory=list)
    needs_rewrite: bool = False
    model_flags: list[tuple[str, str, str]] = field(default_factory=list)
    model_unavailable: bool = False      # 模型服务不可用（覆盖缺口，阻断发布）
    # M3（0.39.0）：二进制证据卡复核统计（report 渲染用）
    v2_cards_audited: int = 0            # 送模型复核的证据卡数（去重后）
    v2_cards_sampled: int = 0            # 抽样上限截断掉的卡数

    @property
    def failed_files(self) -> list[FileAudit]:
        return [f for f in self.files if not f.passed]


# ── 通用工具 ──────────────────────────────────────────────────────

def _content_is_json_like(text: str) -> bool:
    """按内容判定是否为 JSON 结构（{ 或 [ 开头），与扩展名无关。

    这是 containment-breach-hd 的根治：.subs 语言包是 JSON 内容但后缀不在
    .json 里，旧版按扩展名误判为普通 txt。JSON 内容文件的结构守恒检查
    （引号成对/尾随逗号/严格解析）必须由内容触发，不能依赖扩展名。
    """
    stripped = text.lstrip()
    return stripped.startswith(("{", "["))


def _det_json_like_lines(rel: str) -> bool:
    """该文件的行差异是否都是「合法 JSON 内容」格式（引号值/尾随逗号）。

    用于模型层去噪：确定性已逐字节确认这些文件结构完整，若文件是 JSON 内容
    （.subs 语言包/字典），模型对 json.dumps 转义序列（\\" 等）和「值内嵌
    引号/逗号」的 STRUCTURE_BROKEN 指控几乎必是误读——确定性字节法已是最强
    结构证据，模型该指控既无独立证据又添人工确认噪音，直接丢弃 STRUCTURE
    _BROKEN 判定，只保留语义类（VALUE_INVERTED/PLACEHOLDER_LOST）。
    """
    return bool(re.search(r"\.(subs|json|jsonc|langs|languages)$", rel))


def _placeholder_tokens(text: str) -> set[str]:
    """提取占位符 token，覆盖主流格式化占位符形态。"""
    tokens: set[str] = set()
    tokens.update(re.findall(r"\{[^{}]*\}", text))          # {0} {name} {=tag}
    tokens.update(re.findall(r"%\d*\$?[sdfdiouxXeEgGc]", text))  # %s %1$s %2$d
    tokens.update(re.findall(r"\$\d+", text))               # $1
    tokens.update(re.findall(r"</?[a-zA-Z][^>]*>", text))    # <b> <i> <link=..>
    tokens.update(re.findall(r"\\\{[^{}]*\}", text))         # 转义花括号
    if "\\n" in text:
        tokens.add("\\n")
    if "\\t" in text:
        tokens.add("\\t")
    return tokens


# 键字段名 → 其值是不可翻译的运行时引用/枚举常量（翻译破坏查找）。比
# placeholders._KEY_FIELD_NAMES 更宽：containment subtitles.jsonc 实证
# `"color": "classd"`（颜色类别枚举）被模型译成「类」→ 字幕着色查找断链。
_CJK = re.compile(r"[一-鿿㐀-䶿]")
_REF_FIELD_NAMES = {
    "color", "colour", "sound", "sfx", "music", "song", "material", "sprite",
    "icon", "image", "prefab", "scene", "layer", "animation", "anim",
    "animationstate", "state", "mode", "type", "category", "kind", "class",
    "style", "shader", "font", "texture", "camera", "light", "audio", "clip",
    "model", "mesh", "effect", "particle", "controller", "renderer",
}


def _ref_value_translated(leaf: str, original: str, translation: str) -> bool:
    """键字段（key_path 叶）下、值是非中文标识符 → 值被译成中文 = 引用断链。

    只对「原文是标识符形态（无空格/无中文/非自然语言）且译文变成含中文」
    生效——`"color": "classd"` → `"color": "类"`。避免误伤正常英文短语值
    （值带空格 = 自然语言，可翻译）。
    """
    if not translation:
        return False
    if _CJK.search(original):                     # 原文已是中文 → 正常
        return False
    leaf_n = leaf.strip().lower()
    if leaf_n not in _REF_FIELD_NAMES:
        return False
    # 关键：审计目标是「磁盘写回产物是否断引用」，不是 store 语义。若该字段
    # 已被 writer 键字段保护（_KEY_FIELD_NAMES 已含 → 写回跳过、磁盘保留
    # 原文），则译文不会泄漏到磁盘，不产生实际断链 → 不 flag（否则磁盘已是
    # 正确原文仍报 needs_rewrite，重写回产物不变 → 无限循环）。
    # 若字段未被 writer 保护，译文才真正写入磁盘 → 断链 → 必须 flag。
    # 磁盘是否已被旧写回污染由 render_consistent 独立捕获。
    from .placeholders import looks_like_key_field
    if looks_like_key_field(leaf_n):
        return False
    if not _CJK.search(translation):              # 译文无中文 → 未译
        return False
    # 原文是标识符形态（无空白分隔）→ 是枚举/引用常量，翻译必断链
    orig = original.strip()
    if not orig or any(c.isspace() for c in orig):
        return False
    return True


def _lines(text: str) -> list[str]:
    return text.splitlines()


# ── 字节层守恒 ────────────────────────────────────────────────────

def _bytes_encoding_signature(raw: bytes) -> tuple[str, bool]:
    """返回 (归一化编码名, 是否带 BOM) —— 写回不得静默改变。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", True
    import chardet
    sample = raw[:65536] if len(raw) > 65536 else raw
    det = chardet.detect(sample)
    enc = (det.get("encoding") or "utf-8").lower()
    if enc == "ascii":
        enc = "utf-8"
    return enc, False


def _encoding_eol_conserved(src_bytes: bytes, out_bytes: bytes) -> tuple[bool, bool]:
    src_enc, src_bom = _bytes_encoding_signature(src_bytes)
    out_enc, out_bom = _bytes_encoding_signature(out_bytes)
    encoding_ok = (src_enc == out_enc and src_bom == out_bom)
    eol_ok = detect_eol(src_bytes) == detect_eol(out_bytes)
    return encoding_ok, eol_ok


# ── 结构层守恒（按格式分派）──────────────────────────────────────

def _json_structure_ok(src_text: str, out_text: str) -> tuple[bool, bool, bool]:
    """JSON 内容结构审计：严格解析 + 键树深比 + （非严格时）引号/逗号。"""
    quote_ok = True
    comma_ok = True
    try:
        src_obj = json.loads(src_text)
        src_strict = True
    except (json.JSONDecodeError, ValueError):
        src_strict = False
        src_obj = None
    if src_strict:
        try:
            out_obj = json.loads(out_text)
        except (json.JSONDecodeError, ValueError):
            return True, True, False    # strict_parse_ok=False
        return True, True, _json_tree_same(src_obj, out_obj)
    # 非严格 JSON（缺逗号的 .subs 等）：逐行引号成对 + 尾随逗号守恒
    src_lines = _lines(src_text)
    out_lines = _lines(out_text)
    if len(src_lines) == len(out_lines):
        for a, b in zip(src_lines, out_lines):
            if '"' in b and b.strip() and b.count('"') % 2 != 0:
                quote_ok = False
            if a.rstrip().endswith(",") != b.rstrip().endswith(","):
                comma_ok = False
    else:
        quote_ok = comma_ok = False
    return quote_ok, comma_ok, True


def _json_tree_same(src, out) -> bool:
    """深比结构：只允许叶子值不同，容器结构（键集合/列表长度）必须一致。"""
    if isinstance(src, dict):
        if not isinstance(out, dict) or set(src.keys()) != set(out.keys()):
            return False
        return all(_json_tree_same(src[k], out[k]) for k in src)
    if isinstance(src, list):
        if not isinstance(out, list) or len(src) != len(out):
            return False
        return all(_json_tree_same(a, b) for a, b in zip(src, out))
    return True    # 叶子值允许不同（是译文）


def _xml_structure_ok(src_text: str, out_text: str) -> bool:
    """XML：ET 重解析成功 + 元素树 tag/属性键一致，仅文本节点可变。"""
    import xml.etree.ElementTree as ET
    try:
        src_root = ET.fromstring(src_text)
        out_root = ET.fromstring(out_text)
    except ET.ParseError:
        return False
    return _xml_tree_same(src_root, out_root)


def _xml_tree_same(a, b) -> bool:
    if a.tag != b.tag or set(a.attrib) != set(b.attrib):
        return False
    if a.text != b.text:
        # 文本节点差异 = 译文写入（叶子变化允许）；但若一方文本为空另一
        # 方非空且都有子元素，属结构变化
        if (a.text is None) != (b.text is None):
            return False
    if a.tail != b.tail:
        if (a.tail is None) != (b.tail is None):
            return False
    if len(a) != len(b):
        return False
    return all(_xml_tree_same(x, y) for x, y in zip(a, b))


def _csv_structure_ok(src_text: str, out_text: str, suffix: str) -> bool:
    """CSV：行数一致 + 每行列数一致（或仅目标列 +1）+ 首列 key 原样。"""
    delimiter = {"tsv": "\t", "psv": "|"}.get(suffix, ",")
    try:
        src = list(csv.reader(io.StringIO(src_text), delimiter=delimiter))
        out = list(csv.reader(io.StringIO(out_text), delimiter=delimiter))
    except Exception:  # noqa: BLE001
        return False
    if not src:
        return not out
    if len(src) != len(out):
        return False
    width = len(src[0])
    for s, o in zip(src, out):
        if s and s[0] != o[0]:
            return False                       # 首列 key 被改 → 断查找
        if len(o) not in (len(s), len(s) + 1):  # 允许新增一列（目标语言列）
            return False
    return True


def _po_structure_ok(src_text: str, out_text: str) -> bool:
    """PO：msgid 原文不变 + 块数守恒（只允许 msgstr 槽被替换）。"""
    def msgid_lines(text: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        key = None
        for line in text.splitlines():
            m = re.match(r'^(?P<key>msgid(?:_plural)?) (?P<value>.*)$', line)
            if m:
                key = m.group("key")
                out.append((key, m.group("value")))
            elif line.startswith('"') and key in ("msgid", "msgid_plural"):
                out.append((key, line))
        return out
    return msgid_lines(src_text) == msgid_lines(out_text)


def _subtitle_structure_ok(src_text: str, out_text: str, suffix: str) -> bool:
    """字幕：结构行（时间码/序号/头/样式）逐行一致，仅对白/文本行可变。"""
    s = suffix.lstrip(".").lower().split(".")[0]
    ts = [
        re.compile(r"^\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}"),
        re.compile(r"^(?:\d{2}:)?\d{2}:\d{2}\.\d{1,3}\s*-->\s*(?:\d{2}:)?\d{2}:\d{2}\.\d{1,3}"),
    ]
    src_lines = _lines(src_text)
    out_lines = _lines(out_text)
    if len(src_lines) != len(out_lines):
        return False
    for a, b in zip(src_lines, out_lines):
        sa, sb = a.strip(), b.strip()
        # 时间码行 / 纯序号 / 头 / [节] 行必须原样
        if (any(r.match(sa) for r in ts) or re.match(r"^\d+\s*$", sa)
                or sa.startswith("WEBVTT") or re.match(r"^\[[^\]]+\]\s*$", sa)
                or (s == "lrc" and re.match(r"^\[[0-9:.]+\]$", sa))):
            if a != b:
                return False
    return True


def _kv_keys_preserved(src_text: str, out_text: str) -> bool:
    """TXT/KV：分隔符左侧的 key 不被翻译改写（结构行只允许叶子值变）。

    另含 markdown 列表 marker 守恒：`* foo` / `- foo` / `+ foo` / `# 标题`
    等行首结构 marker 必须原样保留——译文把列表项 `*Tesla...` 译成
    `*...*` 末尾多星号 = 列表结构破坏（Changelog.txt 实证，模型判定
    STRUCTURE_BROKEN，这里确定性拦截，免模型噪音）。
    """
    src_lines = _lines(src_text)
    out_lines = _lines(out_text)
    if len(src_lines) != len(out_lines):
        return False
    delim = re.compile(r"^(?P<key>[^=:;\r\n]+?)\s*(?P<d>[:=])\s*(?P<v>.*)$")
    # 行首结构 marker：markdown 列表（*/-/+/数字.）与标题（#），允许
    # marker 后无空格（`*Tesla Gates`、`  *Improved head-sway`——Changelog
    # 实证首行缩进 + 星号紧跟文本）
    bullet = re.compile(r"^(\s*[*-+]\s*|\s*\d+[.)]\s*|\s*#\s*)")
    for a, b in zip(src_lines, out_lines):
        ma, mb = delim.match(a), delim.match(b)
        if ma and mb and ma.group("key").strip() != mb.group("key").strip():
            return False
        # markdown 列表 marker 逐位守恒（行首 marker 原文 ⊆ 写回行首），
        # 且单字符 marker（*/-）不得在译文中新增配对（`*foo` → `*foo*`
        # 是列表项被包成强调——Changelog.txt 实证 STRUCTURE_BROKEN）
        if bullet.match(a):
            marker = bullet.match(a).group(0).strip()
            if not b.startswith(bullet.match(a).group(0)):
                return False
            if len(marker) == 1 and a.count(marker) == 1 and b.count(marker) > 1:
                return False
    return True


def _structure_audit(fmt: str, src_text: str, out_text: str,
                     src_suffix: str) -> tuple[bool, bool, bool, bool, bool]:
    """按格式分派结构审计。

    返回 (structure_ok, quote_ok, comma_ok, strict_ok, delim_ok)。
    通用兜底：任何格式都先做行数守恒；格式专用检查按需覆盖。
    """
    line_ok = len(_lines(src_text)) == len(_lines(out_text))
    quote_ok = comma_ok = strict_ok = delim_ok = True
    structure_ok = True

    json_like = _content_is_json_like(src_text)
    if fmt == "json" or json_like:
        q, c, s = _json_structure_ok(src_text, out_text)
        quote_ok, comma_ok, strict_ok = q, c, s
        if s is False:
            structure_ok = False
    elif fmt == "xml":
        structure_ok = _xml_structure_ok(src_text, out_text)
    elif fmt == "csv":
        structure_ok = _csv_structure_ok(src_text, out_text, src_suffix)
    elif fmt == "po":
        structure_ok = _po_structure_ok(src_text, out_text)
    elif fmt in ("srt", "vtt", "ass", "ssa", "lrc"):
        structure_ok = _subtitle_structure_ok(src_text, out_text, src_suffix)
    elif fmt in ("txt", "kv", "ink", "yarn"):
        structure_ok = _kv_keys_preserved(src_text, out_text)

    return (structure_ok and line_ok, quote_ok, comma_ok,
            strict_ok and line_ok, delim_ok)


# ── 渲染一致性（store 渲染 + 编码 == 磁盘 bytes）─────────────────

def _render_from_store(store, src: Path, f: dict,
                       entries: list[dict],
                       normalize_fallback_punctuation: bool = False) -> str:
    """用 store 译文按格式渲染（与 writer._render 完全同源）。

    normalize_fallback_punctuation：与 writer 写回同源——中文字体启用时
    未翻译条目的回退原文做字体标点归一化（– → —），否则设为空串。必须
    与写回时的实际渲染一致，否则 render_consistent 会被误判 FAIL。
    """
    from .models import STATUS_SKIPPED
    from .placeholders import (is_key_style_identifier, looks_like_key_field)
    from .quality import is_write_ready
    model_entries: list[TextEntry] = []
    for d in entries:
        meta = json.loads(d.get("meta") or "{}")
        model_entries.append(TextEntry(
            file_id=d["file_id"], key_path=d["key_path"],
            original=d["original"], translation=d.get("translation") or "",
            status=d.get("status", "pending"),
            locked=bool(d.get("locked")), meta=meta))
    fmt = f["format"]
    for e in model_entries:
        if not is_write_ready(e.status, e.translation, e.meta):
            if normalize_fallback_punctuation:
                from .font.punct_normalize import normalize_font_punctuation
                e.translation = normalize_font_punctuation(e.original)
            else:
                e.translation = ""
        if is_key_style_identifier(e.original) or (
                fmt == "json" and looks_like_key_field(
                    e.key_path.rsplit("/", 1)[-1])):
            e.translation = ""
            e.status = STATUS_SKIPPED
    meta = json.loads(f.get("meta") or "{}")
    text = read_text(src)
    body = apply_format_text(fmt, model_entries, text, {
        **meta, "source_suffix": src.suffix.lower(),
        "target_col": meta.get("target_col"),
    })
    if fmt in ("txt", "yaml", "ink_yarn", "subtitle", "po") and text.endswith(
            ("\n", "\r")):
        body += "\n"
    # 还原文件 EOL（writer._encode 同款，顺序必须先 LF 归一再转 CRLF）：
    # body 可能已含 \r\n（apply_format_text 逐行重建），若直接做
    # \n→\r\n 替换会把已有 \r\n 变 \r\r\n——先归一成纯 LF 再转换
    body = body.replace("\r\n", "\n")
    if (f.get("eol") or "\n") == "\r\n":
        body = body.replace("\n", "\r\n")
    return body


def _encode_from_store(body: str, src: Path, f: dict) -> bytes:
    """与 writer._encode 完全同源：body → 磁盘 bytes（含编码/BOM/EOL）。"""
    from .writer import _encode
    return _encode(body, src, f)


# ── 第 1 层：确定性结构审计 ──────────────────────────────────────

def audit_deterministic(store, game_dir: Path, out_dir: Path,
                        entries_by_file: dict[str, list[dict]],
                        *, font_enabled: bool = False,
                        on_note: Callable[[str], None] | None = None,
                        ) -> list[FileAudit]:
    """第 1 层：只读确定性结构审计所有文本文件（不修改任何文件）。

    on_note 逐文件回调（2026-08-26 用户要求写回审计实时处理流信息更
    多更明确）：每个文件审计完即报「PASS/FAIL + 译文条数 + 问题摘要」，
    让 GUI 活动流逐文件可见进度，而非只等终态一条。
    """
    audits: list[FileAudit] = []
    game_root = game_dir.resolve()
    out_root = out_dir.resolve()
    for f in store.get_files():
        if f["format"].startswith("v2_"):
            continue
        rel = f["rel_path"]
        src = (game_dir / rel).resolve()
        out = (out_dir / rel).resolve()
        audit = FileAudit(rel_path=rel, format=f["format"])
        try:
            src.relative_to(game_root)
            out.relative_to(out_root)
        except ValueError:
            audit.render_consistent = False
            audit.detail.append("路径越出根目录")
            audits.append(audit)
            continue
        if not src.is_file() or not out.is_file():
            audit.render_consistent = False
            audit.detail.append("源或写回文件缺失")
            audits.append(audit)
            continue
        try:
            src_bytes = src.read_bytes()
            out_bytes = out.read_bytes()
            src_text = read_text(src)
            out_text = read_text(out)
        except Exception:  # noqa: BLE001
            audit.render_consistent = False
            audit.detail.append("读取失败")
            audits.append(audit)
            continue

        # 字节层守恒（所有格式通用）
        audit.encoding_conserved, audit.eol_conserved = \
            _encoding_eol_conserved(src_bytes, out_bytes)

        # 结构层守恒（按格式分派）
        (audit.structure_ok, audit.quote_paired, audit.comma_conserved,
         audit.strict_parse_ok, audit.delimiter_conserved) = _structure_audit(
            f["format"], src_text, out_text, src.suffix.lower())
        audit.line_conserved = len(_lines(src_text)) == len(_lines(out_text))

        # 条目级：占位符
        entries = entries_by_file.get(f["id"], [])
        audit.changed_entries = sum(
            1 for e in entries if (e.get("translation")
                                   and e.get("status") == "translated"))
        for e in entries:
            if e.get("status") != "translated" or not e.get("translation"):
                continue
            orig_tokens = _placeholder_tokens(e.get("original") or "")
            trans_tokens = _placeholder_tokens(e.get("translation") or "")
            missing = orig_tokens - trans_tokens
            # 富文本标签（<b>/<i>/</b>）是 TMP 值内排版标记，不是文件结构
            # ——丢了只是字号/斜体/加粗样式损失，绝不导致文件失效或游戏崩溃
            # （death_173_doors/death_939 实证：模型把 <i>…</i> 译成 “…” 或
            # 直接转简体中文，都是意图内改写）。硬闸门只拦**结构性**占位符
            # （{0}/%s/$1/\n 等）：丢了会破坏取值/换行结构。富文本标签是否
            # 该保留属质量软问题，交第 2 层模型软复核，不硬拦。
            missing = missing - {t for t in missing
                                 if re.fullmatch(r"</?[a-zA-Z][^>]*>", t)}
            if missing:
                audit.placeholder_lost.append(
                    f"{e['key_path']}: {e['original'][:40]} → "
                    f"{e['translation'][:40]} 丢失 {sorted(missing)}")
        # 键字段枚举引用被译（color/sound/sprite 等 ref 字段下、原文标识符、
        # 译文变中文）——确定性拦截引用断链，免模型噪音
        for e in entries:
            if e.get("status") != "translated" or not e.get("translation"):
                continue
            leaf = e.get("key_path", "").rsplit("/", 1)[-1]
            if _ref_value_translated(leaf, e.get("original") or "",
                                     e.get("translation") or ""):
                audit.ref_field_lost.append(
                    f"{e['key_path']}: {e['original'][:30]} → "
                    f"{e['translation'][:30]} 引用字段被译")

        # 渲染一致性（store 渲染+编码 bytes == 磁盘 bytes）——跳过空改动
        if audit.changed_entries:
            try:
                rendered = _render_from_store(
                    store, src, f, entries,
                    normalize_fallback_punctuation=font_enabled)
                rendered_bytes = _encode_from_store(rendered, src, f)
                audit.render_consistent = (rendered_bytes == out_bytes)
            except Exception:  # noqa: BLE001 渲染异常视为不一致
                audit.render_consistent = False
                audit.detail.append("渲染/编码异常")
        audits.append(audit)
        if on_note:
            if audit.passed:
                on_note(f"[写回审计] PASS {rel}（{audit.changed_entries} 条译文）")
            else:
                issues = []
                for attr, label in _ISSUE_LABELS:
                    if not getattr(audit, attr):
                        issues.append(label)
                if audit.placeholder_lost:
                    issues.append(f"占位符丢失 {len(audit.placeholder_lost)} 条")
                if audit.ref_field_lost:
                    issues.append(f"引用字段被译 {len(audit.ref_field_lost)} 条")
                on_note(f"[写回审计] FAIL {rel}：{'、'.join(issues)}")
    return audits


# ── 第 2 层：审校模型结构/语义审计 ────────────────────────────────

_MODEL_SYSTEM_PROMPT = """你是游戏本地化写回结构审计员。用户会给你若干对
「源行 → 写回行」（同一文件同一行写回前后的文本）。你必须逐对严格核对
写回行是否：

1. 结构完整：引号/逗号/括号/标签/占位符（{0}、%s）是否被破坏或丢失
2. 值正确：写回行是否仍然是一个合法的键值结构（key 未被翻译/改写，
   value 是否是对应的译文而不是把 key 或整行结构改坏）
3. 无语义颠倒：译文是否与源行值含义一致（如 Walk right 译成 行走对 是
   语义颠倒，应译成 向右走）

判定标准（严格，宁严勿松）：
- PASS：结构完整 + 值是合理译文
- STRUCTURE_BROKEN：引号/逗号/括号/占位符丢失或不成对、key 被改写、
  整行变成中文弯引号包裹的非结构文本
- VALUE_INVERTED：结构完整但译文值语义与源相反或完全错位
- PLACEHOLDER_LOST：占位符 {0}/%s 在译文中丢失

只输出 JSON 数组，每条一个对象：
{"index": N, "verdict": "PASS|STRUCTURE_BROKEN|VALUE_INVERTED|PLACEHOLDER_LOST", "issue": "原因（无则空串）"}
不要输出任何解释文字。"""


def _build_model_items(src_text: str, out_text: str, max_pairs: int = 400
                       ) -> list[tuple[int, str, str]]:
    """取有差异的行对（源行, 写回行），供模型批量审核。超大文件抽样。"""
    src_lines = _lines(src_text)
    out_lines = _lines(out_text)
    items: list[tuple[int, str, str]] = []
    for i, (a, b) in enumerate(zip(src_lines, out_lines)):
        if a != b and b.strip():
            items.append((i, a, b))
    if len(items) > max_pairs:
        # 抽样保住边界（首尾各留若干 + 均匀中段），控制模型成本
        step = max(1, (len(items) - 20) // (max_pairs - 20))
        items = items[:10] + items[::step][10:-10] + items[-10:]
    return items


def _parse_model_verdicts(content: str) -> dict[int, tuple[str, str]]:
    """解析模型返回的 JSON 数组 → {index: (verdict, issue)}。"""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, list):
        return {}
    out: dict[int, tuple[str, str]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index", -1))
        except (TypeError, ValueError):
            continue
        if idx < 0:
            continue
        out[idx] = (
            str(item.get("verdict", "PASS")),
            str(item.get("issue", "") or ""))
    return out


def audit_model(store, game_dir: Path, out_dir: Path,
                entries_by_file: dict[str, list[dict]],
                service, batch_size: int = 12,
                max_pairs_per_file: int = 400,
                skip_files: set[str] | None = None,
                pass_files: set[str] | None = None,
                on_note: Callable[[str], None] | None = None) -> AuditResult:
    """第 2 层：审校模型结构审计。

    只审计「第 1 层 PASS 且有行差异」的文件（skip_files = 确定性已 FAIL 的
    文件——它们反正要重写回，且其问题已确定性拦截，模型再报是重复噪音）。
    模型 FLAG 只记录，不硬阻断——软复核。

    pass_files：确定性已 PASS 的文件集。其中「JSON 内容格式」文件（.subs/
    .jsonc/.langs）由确定性字节法逐字节确认结构完整，模型对 json.dumps
    转义序列（\\" 内嵌引号、值内尾随逗号）的 STRUCTURE_BROKEN 指控无独立
    证据且纯属误读 → 丢弃该判定（保留语义类 VALUE_INVERTED/PLACEHOLDER
    _LOST），消人工确认噪音。非 JSON 文件不受影响。
    """
    skip_files = skip_files or set()
    pass_files = pass_files or set()
    result = AuditResult()
    game_root = game_dir.resolve()
    out_root = out_dir.resolve()
    files = [f for f in store.get_files() if not f["format"].startswith("v2_")]
    for f in files:
        rel = f["rel_path"]
        if rel in skip_files:
            continue
        src = (game_dir / rel).resolve()
        out = (out_dir / rel).resolve()
        try:
            src.relative_to(game_root)
            out.relative_to(out_root)
        except ValueError:
            continue
        if not src.is_file() or not out.is_file():
            continue
        try:
            src_text = read_text(src)
            out_text = read_text(out)
        except Exception:  # noqa: BLE001
            continue
        items = _build_model_items(src_text, out_text, max_pairs_per_file)
        if not items:
            continue
        if on_note:
            on_note(f"[写回审计] {rel}: {len(items)} 行差异送模型复核")
        groups = [items[i:i + batch_size]
                  for i in range(0, len(items), batch_size)]
        for group in groups:
            prompt_lines = []
            for (i, a, b) in group:
                prompt_lines.append(f"[{i}]\n源行: {a}\n写回行: {b}")
            prompt = (
                "请审核以下写回行对（index 对应输出 index）：\n\n"
                + "\n\n".join(prompt_lines))
            try:
                content = service.chat(
                    _MODEL_SYSTEM_PROMPT + "\n\n" + prompt,
                    max_tokens=512, timeout=120)
            except Exception as exc:  # noqa: BLE001
                # 模型中途不可用：该批未审 → 覆盖有缺口，软复核不完整。
                # 标记 model_unavailable 供上层硬阻断（不允许带缺口发布）。
                result.model_unavailable = True
                result.model_flags.append(
                    (rel, "MODEL_UNAVAILABLE", str(exc)[:120]))
                continue
            verdicts = _parse_model_verdicts(content)
            for (i, a, b) in group:
                verdict, issue = verdicts.get(i, ("PASS", ""))
                if verdict == "PASS":
                    continue
                # 去噪：已被第 1 层确定性拦截的问题不重复上报（软复核只留
                # 模型独有能力——语义颠倒/未知结构），避免人工确认重复劳动
                if _issue_already_deterministic(f, entries_by_file, a, b):
                    continue
                # 去噪：JSON 内容文件（.subs/.jsonc/.langs）确定性已逐字节
                # 确认结构完整，模型的 STRUCTURE_BROKEN 是对 json.dumps 转义
                # 序列（内嵌引号/值内逗号）的误读 → 丢弃该判定，只留语义类
                if (verdict == "STRUCTURE_BROKEN"
                        and rel in pass_files
                        and _det_json_like_lines(rel)):
                    continue
                result.model_flags.append(
                    (rel, verdict, f"[{i}] {issue} | {a[:50]} → {b[:50]}"))
    return result


def _issue_already_deterministic(f: dict, entries_by_file: dict,
                                 src_line: str, out_line: str) -> bool:
    """该行的写回差异是否已被第 1 层确定性审计拦截（免模型重复噪音）。

    判定：写回行丢失了占位符/引用字段，或改变了引号成对/尾随逗号/键——
    这些确定性层已拦截，模型不必再报。
    """
    src_tokens = _placeholder_tokens(src_line)
    out_tokens = _placeholder_tokens(out_line)
    if src_tokens - out_tokens:
        return True                       # 占位符丢失 → 确定性已拦
    # 键字段枚举被译（含中文弯引号包裹的引用值）
    for e in entries_by_file.get(f["id"], []):
        if (e.get("status") == "translated" and e.get("translation")
                and e.get("translation") in out_line
                and e.get("original") in src_line):
            leaf = e["key_path"].rsplit("/", 1)[-1]
            if _ref_value_translated(leaf, e["original"], e["translation"]):
                return True               # 引用字段被译 → 确定性已拦
    if f["format"] in ("json", "txt", "kv") and _content_is_json_like(src_line):
        if out_line.count('"') % 2 != 0 or (
                src_line.rstrip().endswith(",")
                != out_line.rstrip().endswith(",")):
            return True                   # 引号/逗号破坏 → 确定性已拦
    return False


# ── 第 2 层 b：二进制对象证据卡审计（0.39.0 M3）────────────────────

# 证据卡 prompt（§65：短/固定/结构化）：与 _MODEL_SYSTEM_PROMPT 同一
# 四值结论词表，但对象是「同一对象同一处的 原文→译文 改动」而非行对。
_V2_CARD_SYSTEM_PROMPT = """你是游戏本地化写回结构审计员。用户会给你若干条
二进制资源对象的写回改动记录（每条含：文件、对象类型、定位、原文、译文）。
程序的确定性层已完成字节层验证（重开读回、字节守恒、占位符守恒），不要
评估编码或字节问题，只核对每条改动是否：

1. 语义正确：译文与原文含义一致（如 Walk right 译成 行走对 是语义颠倒）
2. 值合适：译文是对应原文的翻译，而不是错位到别的字段/整段无关文本
3. 位置语义安全：该改动确实像在翻译显示文本，而非改写了关键标识

判定标准（严格，宁严勿松）：
- PASS：译文是原文的合理翻译
- VALUE_INVERTED：译文与原文含义相反或完全错位
- PLACEHOLDER_LOST：原文中的占位符（{0}、%s 等）或关键标记在译文中丢失
- STRUCTURE_BROKEN：译文明显不是针对该原文的翻译（值错位/串行）

只输出 JSON 数组，每条一个对象：
{"index": N, "verdict": "PASS|STRUCTURE_BROKEN|VALUE_INVERTED|PLACEHOLDER_LOST", "issue": "原因（无则空串）"}
不要输出任何解释文字。"""


def _v2_evidence_cards(v2_result) -> list[dict]:
    """从 WriteResult 安全提取证据卡列表（None/属性缺失 → 空）。"""
    cards = getattr(v2_result, "object_evidence", None)
    if not isinstance(cards, list):
        return []
    # 只留有真实改动的卡（changes 非空且每项 译文 != 原文——writer 侧已
    # 过滤，这里二次防御防脏数据进 prompt）
    out: list[dict] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        changes = [
            (str(w), str(o), str(t))
            for w, o, t in card.get("changes") or []
            if str(t) != str(o)]
        if changes:
            clean = dict(card)
            clean["changes"] = changes
            out.append(clean)
    return out


def _build_v2_card_prompt(batch: list[dict]) -> str:
    """证据卡批量 prompt：每卡一行定位 + 逐处 原文→译文 改动。"""
    lines = ["请审核以下二进制对象写回改动（index 对应输出 index）："]
    for index, card in enumerate(batch):
        lines.append(
            f"[{index}] 文件 {str(card.get('rel_path') or '')[:80]} "
            f"类型 {str(card.get('type') or '')[:40]} "
            f"对象 path_id={card.get('path_id')}")
        for where, orig, trans in card.get("changes") or []:
            lines.append(f"  {where[:60]}: {orig[:80]!r} → {trans[:80]!r}")
    return "\n".join(lines)


def _audit_v2_model(v2_result, service, *, cards_per_batch: int = 12,
                    max_cards: int = 400,
                    on_note: Callable[[str], None] | None = None
                    ) -> AuditResult:
    """第 2 层 b：二进制对象证据卡模型语义复核（软复核）。

    输入是 writer 写回现场记录的证据卡（每个成功落盘对象一张，确定性
    重开验证已通过），本层只补语义复核。非 PASS 记 model_flags（软复核，
    人工确认后发布）；模型请求失败 → model_unavailable=True（覆盖缺口，
    与文本层同口径由上层阻断发布）。证据卡为空 → 零行为零模型调用。
    """
    result = AuditResult()
    cards = _v2_evidence_cards(v2_result)
    if not cards or service is None:
        if cards and service is None:
            result.model_unavailable = True
            result.v2_cards_sampled = len(cards)
        return result
    # 抽样上限：超限均匀截断（保首尾），审计层按批复核控制模型成本；
    # 截断的卡仍计数在 v2_cards_sampled（报告如实呈现覆盖缺口）。
    if len(cards) > max_cards:
        if max_cards <= 20:
            kept = cards[:max_cards]
        else:
            step = max(1, (len(cards) - 20) // (max_cards - 20))
            kept = cards[:10] + cards[::step][10:-10] + cards[-10:]
            kept = kept[:max_cards]
        result.v2_cards_sampled = len(cards) - len(kept)
        cards = kept
    if on_note:
        on_note(f"[写回审计] 二进制证据卡 {len(cards)} 张送模型语义复核")
    result.v2_cards_audited = len(cards)
    groups = [cards[i:i + cards_per_batch]
              for i in range(0, len(cards), cards_per_batch)]
    for group in groups:
        prompt = _V2_CARD_SYSTEM_PROMPT + "\n\n" + \
            _build_v2_card_prompt(group)
        try:
            content = service.chat(prompt, max_tokens=512, timeout=120)
        except Exception as exc:  # noqa: BLE001
            # 该批未审 → 覆盖缺口，与文本层同口径标记阻断
            result.model_unavailable = True
            result.model_flags.append(
                ("(binary)", "MODEL_UNAVAILABLE", str(exc)[:120]))
            continue
        verdicts = _parse_model_verdicts(content)
        for index, card in enumerate(group):
            verdict, issue = verdicts.get(index, ("PASS", ""))
            if verdict == "PASS":
                continue
            changes = card.get("changes") or []
            sample = "；".join(
                f"{o[:40]}→{t[:40]}" for _w, o, t in changes[:2])
            result.model_flags.append(
                (str(card.get("rel_path") or "(binary)"), verdict,
                 f"path_id={card.get('path_id')} {issue[:80]} | {sample}"))
    return result


def audit_writeback(store, game_dir: Path, out_dir: Path,
                    service=None, *, run_model: bool = True,
                    app_dir: str | Path | None = None,
                    batch_size: int = 12,
                    max_pairs_per_file: int = 400,
                    font_enabled: bool = False,
                    v2_result=None,
                    v2_cards_per_batch: int = 12,
                    max_v2_cards: int = 400,
                    on_note: Callable[[str], None] | None = None,
                    ) -> AuditResult:
    """完整写回审计（第 1 层确定性 + 第 2 层模型）。

    service 为空且 run_model=True 时，若给了 app_dir 则尝试从
    ReviewModelService 按需构建（模型可用才跑模型层；不可用 → 覆盖
    缺口，阻断发布）。第 1 层任何文件 FAIL → needs_rewrite=True。

    v2_result（0.39.0 M3）：write_back_v2 的 WriteResult——非 None 且
    object_evidence 非空时，第 2 层 b 对二进制对象证据卡做模型语义
    复核（详见模块 docstring）。None / 空证据 → 该层零行为（兼容
    0.38.0 调用方与纯文本游戏）。
    """
    # 按 file_id 分组条目（O(1) 查表）
    entries_by_file: dict[str, list[dict]] = {}
    for e in store.get_entries():
        entries_by_file.setdefault(e["file_id"], []).append(e)

    audits = audit_deterministic(
        store, game_dir, out_dir, entries_by_file,
        font_enabled=font_enabled, on_note=on_note)
    result = AuditResult(files=audits)
    result.needs_rewrite = bool(result.failed_files)

    if run_model:
        if service is None and app_dir is not None:
            try:
                from .review_server import ReviewModelService
                service = ReviewModelService(Path(app_dir).resolve())
            except Exception as exc:  # noqa: BLE001
                if on_note:
                    on_note(f"[写回审计] 模型服务构建失败，跳过模型层：{exc}")
                service = None
        if service is not None:
            model_res = audit_model(
                store, game_dir, out_dir, entries_by_file,
                service, batch_size=batch_size,
                max_pairs_per_file=max_pairs_per_file,
                skip_files={a.rel_path for a in result.failed_files},
                pass_files={a.rel_path for a in result.files},
                on_note=on_note)
            result.model_flags.extend(model_res.model_flags)
            result.model_unavailable = model_res.model_unavailable
            # 第 2 层 b：二进制对象证据卡（M3）——与文本行对同模型、
            # 同四值结论、同软复核语义；模型不可用与文本层同口径阻断。
            if v2_result is not None:
                v2_res = _audit_v2_model(
                    v2_result, service,
                    cards_per_batch=v2_cards_per_batch,
                    max_cards=max_v2_cards, on_note=on_note)
                result.model_flags.extend(v2_res.model_flags)
                result.model_unavailable = (
                    result.model_unavailable or v2_res.model_unavailable)
                result.v2_cards_audited = v2_res.v2_cards_audited
                result.v2_cards_sampled = v2_res.v2_cards_sampled
        else:
            # 模型层是写回审计的第二道防线（专补确定性层无法判的语义/结构
            # 漏洞）。用户要求「写回审核不需要用户参与、不允许任何错误」：
            # 模型请求了却不可用 → 无法保证完整覆盖 → 视为审计不完整，
            # 阻断发布（否则一个未审的写回可能带着未发现的破坏发布）。
            # 二进制证据卡同口径：有卡未审 = 覆盖缺口，同样阻断。
            result.model_unavailable = True
            cards = _v2_evidence_cards(v2_result)
            if cards:
                result.v2_cards_audited = 0
                result.v2_cards_sampled = len(cards)
            if on_note:
                on_note("[写回审计] 模型服务不可用——审计不完整，阻断发布")
    return result


# ── 报告渲染 ──────────────────────────────────────────────────────

_ISSUE_LABELS = [
    ("encoding_conserved", "编码/BOM 被改变"),
    ("eol_conserved", "行终止符被改变"),
    ("line_conserved", "行数不一致"),
    ("structure_ok", "结构被破坏（键/元素/块/分隔符）"),
    ("quote_paired", "引号不成对"),
    ("comma_conserved", "尾随逗号结构被破坏"),
    ("strict_parse_ok", "严格 JSON 解析失败"),
    ("delimiter_conserved", "CSV 分隔符/行列数变化"),
    ("render_consistent", "store 渲染与磁盘写回不一致"),
]


def render_audit_report(result: AuditResult, game_name: str = "") -> str:
    """审计结果渲染为 writeback/audit.txt 文本。"""
    lines = [
        f"游戏：{game_name}",
        f"写回审计时间：{_now()}",
        "",
        f"确定性结构审计文件：{len(result.files)} 个",
    ]
    failed = result.failed_files
    lines += [
        f"结构破坏（FAIL）：{len(failed)} 个"
        + ("（→ 需重新写回）" if result.needs_rewrite else ""),
        "",
    ]
    for audit in result.files:
        status = "FAIL" if not audit.passed else "PASS"
        issues = []
        for attr, label in _ISSUE_LABELS:
            if not getattr(audit, attr):
                issues.append(label)
        if audit.placeholder_lost:
            issues.append(f"占位符丢失 {len(audit.placeholder_lost)} 条")
        if audit.ref_field_lost:
            issues.append(f"引用字段被译 {len(audit.ref_field_lost)} 条")
        lines.append(
            f"- [{status}] {audit.rel_path}"
            f"（{audit.changed_entries} 条译文）"
            + (f"：{'、'.join(issues)}" if issues else ""))
        for p in audit.placeholder_lost:
            lines.append(f"    {p}")
        for p in audit.ref_field_lost:
            lines.append(f"    {p}")
        for d in audit.detail:
            lines.append(f"    {d}")
    if result.model_flags:
        lines += ["", f"审校模型复核 FLAG：{len(result.model_flags)} 条",
                  "（软复核，需人工确认，不阻断）"]
        for rel, verdict, issue in result.model_flags:
            lines.append(f"- {rel} [{verdict}] {issue}")
    if result.v2_cards_audited or result.v2_cards_sampled:
        lines += ["",
                  f"二进制对象证据卡复核：送审 {result.v2_cards_audited} 张"
                  + (f"（超上限截断 {result.v2_cards_sampled} 张）"
                     if result.v2_cards_sampled else "")]
    if result.model_unavailable:
        lines += ["", "审校模型复核：模型服务不可用——审计覆盖有缺口，"
                  "已阻断发布（model_unavailable）"]
    lines += ["",
              f"结论：{'需重新写回' if result.needs_rewrite else '结构完整'}"]
    return "\n".join(lines)


def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
