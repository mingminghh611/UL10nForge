from __future__ import annotations

import gzip

import zlib

import re

from dataclasses import dataclass, field

from pathlib import Path

from hanhua.core.engine_strings import is_engine_string

from hanhua.core.formats import (json_format, csv_format, xml_format,

                                 yaml_format, subtitle_format, po_format,

                                 ink_yarn_format, zip_format, sqlite_format,

                                 detect_eol)

from hanhua.core.knowledge import _LANGUAGE_NAMES_CASEFOLD

from hanhua.core.models import (TextEntry, STATUS_PENDING, STATUS_SKIPPED,

                                STATUS_TRANSLATED)

from hanhua.core.placeholders import should_skip

from hanhua.core.scanner import probe_head_kind





@dataclass

class ParsedFile:

    file_id: str

    rel_path: str

    format: str

    entries: list[TextEntry]

    encoding: str = "utf-8"

    eol: str = "\n"

    meta: dict = field(default_factory=dict)

    noise: bool = False      # True = 整个文件被判定为运行时噪音，不入库

    # R5 提取侧静默跳过留档：{跳过形态: 计数}——静默 continue 不产生条目

    # 也不留痕（哑识别源头），聚合后进分析报告供形态清单/召回率审查

    skipped_reasons: dict[str, int] = field(default_factory=dict)





# 无空格标识符风格（如 NavMeshLink、UnityEngine、Assembly-CSharp）——大概率不是显示文本

_NO_SPACE_TOKEN = re.compile(r"^[A-Za-z0-9_.\-/]{2,60}$")

# Unity 引擎配置文件（所有 Unity 游戏通用，文件级跳过——保留条目保证写回

# 完整性但不翻译）：

# - boot.config：引擎启动参数（gfx-enable-*/wait-for-*/scripting-runtime-version/

#   vr-enabled/hdr-display-enabled），值域是引擎枚举（legacy/net_4_x/0/1）——

#   非英文数字值（如 scripting-runtime-version=legacy）是合法配置值，翻译即

#   破坏引擎解析（dollhouse 实证：legacy →「遗产」写回）。配置文件名本身

#   全引擎通用，不是单游戏特判。

# 注：app.info 不在此列——内容是作者名/游戏名（元数据记录，引擎不解析），

# 翻译成中文是合理的标题本地化，无功能破坏。

_UNITY_ENGINE_CONFIG_FILES = {"boot.config"}

# 保留翻译的语言包目录：仅英文（en/english）。英文是游戏主语言文本

# （汉化目标）；其余语言目录（ES/DE/RS/FR/CH/ZH…）是次要语言包——汉化版

# 玩家不会以该语言游玩，翻译无意义且西语等无重音单词（Expreso/Mierda）

# 模型无法翻译恒败（containment 实证）；中文包（CH/ZH）翻译反而破坏

# 游戏自带中文 → 全部跳过

_LANGUAGE_PACK_KEEP = {"en", "english"}





def _is_non_target_language_pack(p: Path) -> bool:

    """路径含 Language/Languages/Lang/Langs 目录且语种目录非英文 → 次要

    语言包。语种目录要求 2-3 字母代码（ES/DE/CH）或语言全名

    （Spanish/German，复用知识库语言名词表）——Language/Texture 之类

    非语种目录不拦截。"""

    parts = p.parts

    for i, part in enumerate(parts[:-1]):

        if part.casefold() not in {"language", "languages", "lang", "langs"}:

            continue

        code = parts[i + 1].casefold()

        if code in _LANGUAGE_PACK_KEEP:

            return False

        if re.fullmatch(r"[a-z]{2,3}", code) or code in _LANGUAGE_NAMES_CASEFOLD:

            return True

    return False





def looks_like_noise_file(entries: list[TextEntry]) -> bool:

    """文件级噪音判定：

    1) 没有任何可译条目 → 噪音（纯配置/清单）

    2) ≥3 条可译条目且 ≥80% 为无空格标识符 → 噪音（Unity 运行时标识符文本）

    """

    pending = [e for e in entries if e.status == "pending"]

    if not pending:

        return True

    # 无空格且 ≥10 字符才是标识符特征（短单词如 Hi/OK 是正常 UI 文本）

    tokens = sum(1 for e in pending

                 if _NO_SPACE_TOKEN.match(e.original.strip()) and len(e.original.strip()) >= 10)

    ratio = tokens / len(pending)

    if len(pending) >= 3 and ratio >= 0.8:

        return True

    if len(pending) <= 2 and ratio == 1.0:

        return True

    return False





def _detect_encoding(raw: bytes) -> str:
    """编码检测：只喂头部样本——chardet.detect 是 O(N) 统计分析，
    2026-08-19 扫描性能修复：几十 MB 的大文本文件（对话库/语料库）
    全量检测会同时占住 raw 副本 + 检测状态数秒，是大文本游戏扫描
    内存暴涨（规律性涨落 = 大文件整读 → 检测 → 解析 → GC）的主源
    之一。编码判断只需头部统计（BOM/前几十 KB 足够稳定）。"""
    import chardet
    sample = raw[:65536] if len(raw) > 65536 else raw
    det = chardet.detect(sample)
    enc = (det.get("encoding") or "utf-8").lower()
    if enc in ("ascii",):
        return "utf-8"
    return enc





def parse_file(path: str | Path, file_id: str | None = None) -> ParsedFile:

    p = Path(path)

    suffix = p.suffix.lower()

    fid = file_id or p.name

    raw = p.read_bytes()

    encoding = _detect_encoding(raw)

    eol = detect_eol(raw)

    if suffix in (".gz",):

        entries, fmt, meta = _parse_compressed(raw, p, fid)

    elif suffix in (".bat", ".cmd"):

        # Windows 启动脚本（electric-trains/outrun-clone 实证假盲区）：
        # .bat/.cmd 引用 exe 路径/窗口参数，翻译破坏启动；census 普查
        # 已排除（_CENSUS_SKIP_SUFFIXES），提取侧同样整文件跳过——
        # 每行留 1 条 skipped 占位（line_no/raw 保真），写回逐行原样
        # 重建，不产生空文件也不破坏启动脚本。registry：
        # _UNITY_ENGINE_CONFIG_FILES 同款「整文件跳过」语义。
        text = _decode_text(raw)
        entries = [TextEntry(
            file_id=fid, key_path=f"plain/{i}", original=line,
            status=STATUS_SKIPPED,
            meta={"kind": "launcher_script", "line_no": i, "raw": line})
            for i, line in enumerate(text.splitlines())]
        fmt, meta = "txt", {}

    elif suffix in (".json", ".json5", ".jsonc", ".jsonl", ".ndjson", ".arb"):

        entries = json_format.extract_json(p, fid)

        fmt, meta = "json", {}

    elif suffix in (".csv", ".tsv", ".psv"):

        entries, target_col = csv_format.extract_csv(p, target_lang="zh-CN", file_id=fid)

        fmt, meta = "csv", {"target_col": target_col}

    elif suffix in (".xml", ".resx", ".xlf", ".xliff", ".tmx", ".ttml"):

        entries = xml_format.extract_xml(p, fid)

        fmt, meta = "xml", {}

    elif suffix in (".yaml", ".yml"):

        entries = yaml_format.extract_yaml(p, fid)

        fmt, meta = "yaml", {}

    elif suffix in (".srt", ".vtt", ".ass", ".ssa", ".lrc"):

        entries = subtitle_format.extract_subtitle(p, fid, kind=suffix.lstrip("."))

        fmt, meta = "subtitle", {}

    elif suffix == ".po":

        entries = po_format.extract_po(p, fid)

        fmt, meta = "po", {}

    elif suffix in (".ink", ".yarn"):

        entries = ink_yarn_format.extract_ink_yarn(p, fid, kind=suffix.lstrip("."))

        fmt, meta = "ink_yarn", {}

    elif suffix == ".zip":

        entries, meta = zip_format.extract_zip(p, fid)

        fmt = "zip"

    elif suffix in (".db", ".sqlite", ".sqlite3"):

        entries = sqlite_format.extract_sqlite(p, fid)

        fmt, meta = "sqlite", {}

    elif suffix in (".bytes", ".dat", ".bin", ".save", ".datas", ""):

        # 伪装/无扩展名：按内容路由（魔数 → 文本/容器；其余回退 txt）

        entries, fmt, meta = _parse_by_content(raw, p, fid)

    else:

        # 未知扩展名（.subs/.langs/自定义文本变体等）：扩展名不是唯一

        # 依据——按内容路由，JSON/XML 内容按结构化解析（否则 txt 行

        # 拆分会把 JSON 行拆成半行条目，写回破坏文件）

        entries, fmt, meta = _parse_by_content(raw, p, fid)

    # 次要语言包（Language/ES 等）：跳过全部条目——保留条目保证写回

    # 完整性（游戏内该语言原样保留），但不翻译（见 _is_non_target_language_pack）

    if _is_non_target_language_pack(p):

        for e in entries:

            if e.status == "pending":

                e.status = STATUS_SKIPPED

    # Unity 引擎配置文件（boot.config）：值域是引擎枚举，翻译即破坏引擎解析

    if p.name in _UNITY_ENGINE_CONFIG_FILES:

        for e in entries:

            if e.status == "pending":

                e.status = STATUS_SKIPPED

    # 智能过滤：纯数字/URL/路径/程序集名/引擎字符串等标记为跳过（保留条目保证写回完整性）

    for e in entries:

        if e.status == "pending" and (should_skip(e.original) or is_engine_string(e.original)):

            e.status = STATUS_SKIPPED

    # 翻译 C6（阶段 2）：相邻文本窗口采集——为可译条目附加文件内前后

    # 各 2 条非空文本（ctx_before/ctx_after），供语境库指纹与多义词消歧

    # （Resume 在主菜单语境=继续 vs 简历）。采集失败降级为无语境不阻塞；

    # 只附加字段不改既有 meta 键，写回不受影响。

    _attach_context_window(entries, window=2)

    noise = looks_like_noise_file(entries)

    return ParsedFile(fid, str(p), fmt, entries, encoding, eol, meta, noise)





_CTX_WINDOW_MAX_LEN = 40





def _attach_context_window(entries: list[TextEntry], window: int = 2) -> None:

    """为每条可译条目附加相邻文本窗口（meta.ctx_before/ctx_after）。



    窗口取文件内按 key_path 顺序的前后 window 条**非空原文**（跳过空行/

    纯标点行），单条截断 _CTX_WINDOW_MAX_LEN 字符。语境库指纹据此区分

    同原文的不同语境（Resume 按钮 vs Resume 存档说明）。

    """

    non_empty = [e for e in entries if e.original and e.original.strip()]

    for index, entry in enumerate(non_empty):

        if entry.status not in (STATUS_PENDING, STATUS_TRANSLATED):

            continue

        before = [e.original.strip()[:_CTX_WINDOW_MAX_LEN]

                  for e in non_empty[max(0, index - window):index]

                  if e.original.strip()]

        after = [e.original.strip()[:_CTX_WINDOW_MAX_LEN]

                 for e in non_empty[index + 1:index + 1 + window]

                 if e.original.strip()]

        if before or after:

            entry.meta["ctx_before"] = before

            entry.meta["ctx_after"] = after





def _parse_compressed(raw: bytes, p: Path, fid: str):

    """GZip 内容：解压（≤100MB）后按内容路由；二进制解压产物降级为空。"""

    try:

        data = gzip.decompress(raw)

    except (OSError, EOFError, zlib.error):

        # D6 路由测试实证：损坏 gz 抛 zlib.error（不是 OSError）——

        # 不捕获会崩整条提取管线（畸形输入降级 txt 空结果）

        return [], "txt", {}

    if len(data) > 100 * 1024 * 1024:

        return [], "txt", {}

    return _parse_by_content(data, p, fid)





def _parse_by_content(raw: bytes, p: Path, fid: str):

    """内容路由：文本 → JSON/XML/TXT；容器 → ZIP/SQLite；其余为空。"""

    kind = probe_head_kind(raw[:8192])

    if kind == "zip":

        entries, meta = _extract_zip_bytes(raw, p, fid)

        return entries, "zip", meta

    if kind == "sqlite":

        return _extract_sqlite_bytes(raw, p, fid)

    if kind == "text":

        text = _decode_text(raw)

        stripped = text.lstrip()

        if stripped.startswith(("{", "[")):

            try:

                return json_format.extract_json_text(text, fid), "json", {}

            except Exception:  # noqa: BLE001

                pass

        if stripped.startswith("<") and "<" in text[:512]:

            try:

                entries = xml_format.extract_xml_text(text, fid)

                if entries:

                    return entries, "xml", {}

            except Exception:  # noqa: BLE001

                pass

        # F51（shellcore 实证 900+ 条对话真盲区）：.corescript 是
        # NodeEditorFramework 对话脚本（Text("key", "对话") 行），
        # 不能走普通 txt 行拆分（Text(" 前缀会整行进池、key 被翻译
        # 破坏对话定位）——专用解析提取引号内 value
        if p.suffix.lower() == ".corescript":
            return _extract_txt_text_corescript(text, fid), "txt", {}
        return _extract_txt_text(text, fid), "txt", {}

    return [], "txt", {}





def _extract_txt_text_corescript(text: str, fid: str) -> list[TextEntry]:
    """F51：.corescript 对话脚本行提取——与 txt_format.extract_txt 的
    corescript 分支对齐（Text("key", "对话内容")：key 保留原文，
    引号内 value 进池，写回按行号 _replace_tail 替换）。"""
    from hanhua.core.formats.txt_format import _CORESCRIPT_TEXT
    entries: list[TextEntry] = []
    for i, line in enumerate(text.splitlines()):
        stripped = line.strip()
        meta = {"line_no": i, "raw": line}
        if not stripped:
            entries.append(TextEntry(
                file_id=fid, key_path=f"line/{i}", original=line,
                status=STATUS_SKIPPED, meta={**meta, "kind": "blank"}))
        elif (stripped.startswith("//") and (len(stripped) == 2
                                             or stripped[2].isspace())):
            # 注释行（//Text(...) 是注释掉的对话历史，跳过）
            entries.append(TextEntry(
                file_id=fid, key_path=f"line/{i}", original=line,
                status=STATUS_SKIPPED, meta={**meta, "kind": "comment"}))
        elif _CORESCRIPT_TEXT.match(stripped):
            m = _CORESCRIPT_TEXT.match(stripped)
            entries.append(TextEntry(
                file_id=fid, key_path=f"corescript/{m.group('key')}/{i}",
                original=m.group("value"),
                meta={**meta, "kind": "corescript",
                      "cs_key": m.group("key")}))
        else:
            # 其他行（空白/非 Text 语法行）：原样保留
            entries.append(TextEntry(
                file_id=fid, key_path=f"line/{i}", original=line,
                status=STATUS_SKIPPED, meta={**meta, "kind": "other"}))
    return entries


def _extract_sqlite_bytes(raw: bytes, _p: Path, fid: str):

    import tempfile

    handle = None

    try:

        handle = tempfile.NamedTemporaryFile(prefix="hanhua_sql_", suffix=".db",

                                             delete=False)

        handle.write(raw)

        handle.close()

        return sqlite_format.extract_sqlite(handle.name, fid), "sqlite", {}

    except Exception:  # noqa: BLE001

        return [], "sqlite", {}

    finally:

        if handle is not None:

            _unlink_temp(handle)





def _extract_zip_bytes(raw: bytes, _p: Path, fid: str):

    """zip 内容路由（伪装扩展名容器）：extract_zip 只接受路径——

    BytesIO 直传会 TypeError 崩（D6 路由测试发现的真实缺陷）。"""

    import tempfile

    handle = None

    try:

        handle = tempfile.NamedTemporaryFile(prefix="hanhua_zip_",

                                             suffix=".zip", delete=False)

        handle.write(raw)

        handle.close()

        return zip_format.extract_zip(handle.name, fid)

    except Exception:  # noqa: BLE001

        return [], {}

    finally:

        if handle is not None:

            _unlink_temp(handle)





def _unlink_temp(handle) -> None:

    """临时文件清理：写入中途失败时句柄未关闭，Windows 对未关闭句柄

    unlink 抛 PermissionError 被吞 → 临时文件永久泄漏——先关句柄再删。"""

    import os

    try:

        handle.close()

    except Exception:  # noqa: BLE001

        pass

    try:

        os.unlink(handle.name)

    except OSError:

        pass





def _decode_text(raw: bytes) -> str:
    """字节 → 文本：BOM 优先，chardet 只喂头部样本（见 _detect_encoding
    的性能说明——全量 chardet 是大文件内存暴涨源头），strict 解码 +
    gbk/latin-1 兜底。"""
    import chardet
    sample = raw[:65536] if len(raw) > 65536 else raw
    det = chardet.detect(sample)
    encoding = (det.get("encoding") or "utf-8").lower()

    if raw.startswith(b"\xef\xbb\xbf"):

        encoding = "utf-8-sig"

    elif encoding == "ascii":

        encoding = "utf-8"

    try:

        return raw.decode(encoding, errors="strict")

    except (UnicodeDecodeError, LookupError):

        for fallback in ("gbk", "latin-1"):

            try:

                return raw.decode(fallback, errors="strict")

            except (UnicodeDecodeError, LookupError):

                continue

        return raw.decode("utf-8", errors="replace")





def _extract_txt_text(text: str, fid: str) -> list[TextEntry]:

    """txt 文本直取（无临时文件）：与 txt_format.extract_txt 相同的行分类。"""

    entries: list[TextEntry] = []

    for i, line in enumerate(text.splitlines()):

        stripped = line.strip()

        meta = {"line_no": i, "raw": line}

        if not stripped:

            entries.append(TextEntry(file_id=fid, key_path=f"line/{i}", original=line,

                                     status=STATUS_SKIPPED, meta={**meta, "kind": "blank"}))

        elif stripped.startswith("#") or stripped.startswith(";"):

            entries.append(TextEntry(file_id=fid, key_path=f"line/{i}", original=line,

                                     status=STATUS_SKIPPED, meta={**meta, "kind": "comment"}))

        elif (stripped.startswith("//")

              and (len(stripped) == 2 or stripped[2].isspace())):

            # C# 风格注释行（与 txt_format 对齐；// 后跟空白才是注释，

            # 协议相对 URL //host 无空白已在 is_hard_structural 处理）

            entries.append(TextEntry(file_id=fid, key_path=f"line/{i}", original=line,

                                     status=STATUS_SKIPPED, meta={**meta, "kind": "comment"}))

        elif stripped.startswith("[") and stripped.endswith("]"):

            entries.append(TextEntry(file_id=fid, key_path=f"line/{i}", original=line,

                                     status=STATUS_SKIPPED, meta={**meta, "kind": "section"}))

        else:

            from hanhua.core.formats import txt_format as _txt

            m = _txt._TAB.match(line) or _txt._KV.match(line)

            if m:

                value = m.group("value").strip()

                delim = "\t" if m.re is _txt._TAB else m.group("delim")

                if not value:

                    # 空值 kv 行（nolog= / key= 空参数）：配置项置空，不是文本。

                    # 与 txt_format.extract_txt 对齐（否则 nolog= 落 plain 被模型

                    # 回显 → untranslated_text 恒败，backrooms boot.config 实证）。

                    entries.append(TextEntry(

                        file_id=fid, key_path=f"kv/{m.group('key').strip()}/{i}",

                        original=value, status=STATUS_SKIPPED,

                        meta={**meta, "kind": "kv_empty",

                              "key": m.group("key"), "delim": delim}))

                elif should_skip(value):

                    # _TAB 正则无 delim 组，不能无条件 group("delim")

                    # （Daggerfall Unity 的 TAB 分隔 kv 行实测 IndexError）

                    entries.append(TextEntry(

                        file_id=fid, key_path=f"kv/{m.group('key').strip()}/{i}",

                        original=value, status=STATUS_SKIPPED,

                        meta={**meta, "kind": "kv_structural",

                              "key": m.group("key"), "delim": delim}))

                else:

                    entries.append(TextEntry(

                        file_id=fid, key_path=f"kv/{m.group('key').strip()}/{i}",

                        original=value,

                        meta={**meta, "kind": "kv", "key": m.group("key"), "delim": delim}))

            else:

                entries.append(TextEntry(file_id=fid, key_path=f"plain/{i}",

                                         original=line.rstrip("\r"),

                                         meta={**meta, "kind": "plain"}))

    return entries

