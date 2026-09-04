from __future__ import annotations
import datetime
import hashlib
import os
import gc
import csv
import functools
import io
import json
import re
import shutil
import struct
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from hanhua.core.extractor import parse_file
from hanhua.core.formats import read_text
from hanhua.core.formats.csv_format import pick_target_col
from hanhua.core.font import build_required_glyph_set
from hanhua.core.font.pipeline import (FontCompatibilityPipeline,
                                       FontPipelineInput)
from hanhua.core.font.providers import (inject_bitmap_font,
                                        resolve_bitmap_providers)
from hanhua.core.font.publish_gate import evaluate_font_gate
from hanhua.core.font_support import (
    FontInstallResult,
    FontProviderCapability,
    resolve_font_provider,
)
from hanhua.core.memory import ProjectStore
from hanhua.core.models import FontConfig, GameProfile
from hanhua.core.paths import (_is_reparse_point, ensure_trusted_root,
                               resolve_relative_under)
from hanhua.core.placeholders import is_key_style_identifier, looks_like_key_field
from hanhua.core.quality import is_write_ready
from hanhua.core.scanner import discover
from hanhua.core.tooling.fingerprint import GameFingerprint, fingerprint_game
from hanhua.core.tooling.morphology import classify_morphology
from hanhua.core.tooling.player_layout import discover_player_candidates
from hanhua.core.tooling.il2cpp_dumper import compare_literals, run_il2cpp_dumper
from hanhua.core.tooling.manifest import ToolRegistry, ToolStatus
from hanhua.core.tooling.planner import (
    BackendStep,
    plan_backends,
    plan_is_completable,
    plan_is_unblocked,
)
from hanhua.core.tooling.runner import IsolatedToolRunner
from hanhua.core.unity import extractor as unity_extractor
from hanhua.core.unity import il2cpp as il2cpp_extractor
from hanhua.core.unity.il2cpp import SUPPORTED_LITERAL_RECORD_SIZES
from hanhua.core.unity import mono_dll as mono_extractor
from hanhua.core.unity.writer import (_should_write_entry, copy_game_dir,
                                      write_back_v2,
                                      _update_addressables_catalogs)
from hanhua.core.writer import write_back as write_back_text


@dataclass(frozen=True)
class WritebackStage:
    phase: str
    message: str
    current: int = 0
    total: int = 0


def _is_managed_dll_with_metadata(path: Path) -> bool:
    """轻量 PE 预检：确认是带完整 CLR metadata 的真实 .NET 程序集。

    检查链：MZ → PE → Optional(0x20B) → CLR 数据目录 → CLR 头 →
    metadata 根（BSJB + 非零版本长度 + 大小在文件内）。stub PE（测试
    fixture 的 1KB 假 DLL）、纯文本占位（"fixture"）都会在某一环失败，
    避免把垃圾喂给原生 TypeTreeGenerator——失败加载会污染进程状态。
    """
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if len(data) < 0x90 or data[:2] != b"MZ":
        return False
    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_off + 0x40 > len(data) or data[pe_off:pe_off + 4] != b"PE\0\0":
        return False
    opt_magic = struct.unpack_from("<H", data, pe_off + 0x18)[0]
    opt_size = struct.unpack_from("<H", data, pe_off + 0x14)[0]
    if opt_magic not in (0x10B, 0x20B) or opt_size < 0x60:
        return False
    if opt_magic == 0x10B:
        dd_off = pe_off + 0x18 + 0x60   # PE32 数据目录在 opt+96
    else:
        dd_off = pe_off + 0x18 + 0x70   # PE32+ 数据目录在 opt+112
    clr_rva, clr_size = struct.unpack_from("<II", data, dd_off + 14 * 8)
    if clr_rva == 0 or clr_size < 0x48:
        return False

    def _rva_to_offset(rva: int) -> int | None:
        n_sections = struct.unpack_from("<H", data, pe_off + 6)[0]
        sec = pe_off + 0x18 + opt_size   # 节表紧跟 optional header
        for i in range(n_sections):
            vs, va, rs, pr = struct.unpack_from(
                "<IIII", data, sec + i * 40 + 8)
            if va <= rva < va + max(vs, rs):
                return pr + (rva - va)
        return None

    clr_off = _rva_to_offset(clr_rva)
    if clr_off is None or clr_off + 0x48 > len(data):
        return False
    meta_rva, meta_size = struct.unpack_from("<II", data, clr_off + 8)
    if meta_rva == 0 or meta_size < 0x10:
        return False
    meta_off = _rva_to_offset(meta_rva)
    if meta_off is None or meta_off + 16 > len(data):
        return False
    if data[meta_off:meta_off + 4] != b"BSJB":
        return False
    ver_len = struct.unpack_from("<I", data, meta_off + 12)[0]
    if ver_len <= 0 or meta_off + 16 + ver_len > len(data):
        return False
    return True


def _emit_writeback_stage(
        callback: Callable[[WritebackStage], None] | None,
        phase: str,
        message: str,
        current: int = 0,
        total: int = 0) -> None:
    if callback is None:
        return
    try:
        callback(WritebackStage(phase, message, current, total))
    except Exception:
        # Progress consumers must not invalidate an already verified write.
        return


def _is_owned_backup(backup: Path, out_dir: Path) -> bool:
    try:
        same_parent = backup.parent.resolve() == out_dir.parent.resolve()
    except OSError:
        return False
    prefix = f".{out_dir.name}.backup-"
    suffix = backup.name[len(prefix):] if backup.name.startswith(prefix) else ""
    return (same_parent and len(suffix) == 32
            and all(char in "0123456789abcdef" for char in suffix))


def _schedule_backup_cleanup(
        keep: Path,
        out_dir: Path,
        stage_cb: Callable[[WritebackStage], None] | None,
) -> threading.Thread | None:
    """发布成功后后台清理「更早」的旧版本备份，保留本次备份供回滚。

    文档1 §3.3/§17：发布成功后必须可一键回滚到发布前版本——本次
    备份保留在磁盘（manifest 记录其路径），只删除更早的发布遗留。

    返回清理线程，调用方在写回返回前 join 等待完成——CLI（地毯式
    runner）写回后立即退出，daemon 线程会被进程终止打断，旧备份成片
    残留（0.25.0 实证：taxes 12 个 + catfiends 5 个 backup，各 353MB）。
    """
    if not _is_owned_backup(keep, out_dir):
        _emit_writeback_stage(
            stage_cb, "cleanup_warning",
            f"旧版本备份路径未通过安全校验，已保留：{keep}")
        return

    _emit_writeback_stage(
        stage_cb, "cleanup_pending",
        f"正在后台清理更早的旧版本（本次备份 {keep.name} 保留供回滚）")

    def cleanup() -> None:
        try:
            candidates = [
                p for p in out_dir.parent.glob(f".{out_dir.name}.backup-*")
                if p != keep and _is_owned_backup(p, out_dir)]
            for old in candidates:
                shutil.rmtree(old)
        except Exception as exc:  # noqa: BLE001 - cleanup failure is non-fatal
            _emit_writeback_stage(
                stage_cb, "cleanup_warning",
                f"旧版本清理失败，已保留：{type(exc).__name__}")
        else:
            _emit_writeback_stage(
                stage_cb, "cleanup_complete",
                "更早的旧版本后台清理完成（本次备份保留）")

    try:
        thread = threading.Thread(
            target=cleanup,
            name=f"hanhua-cleanup-{out_dir.name}",
            daemon=True,
        )
        thread.start()
        return thread
    except RuntimeError:
        _emit_writeback_stage(
            stage_cb, "cleanup_warning", f"无法启动旧版本清理线程，已保留：{keep}")
        return None


def _slug(
        game_dir: Path, player_root: Path | None = None,
        player_executable: Path | None = None) -> str:
    if player_root is None and player_executable is None:
        return hashlib.md5(str(game_dir).encode("utf-8")).hexdigest()[:10]
    identity = "\0".join((
        str(game_dir),
        player_root.as_posix() if player_root is not None else "",
        player_executable.as_posix() if player_executable is not None else "",
    ))
    return hashlib.md5(identity.encode("utf-8")).hexdigest()[:10]


def _replace_directory(source: Path, target: Path) -> None:
    """在 Windows 短暂句柄锁下有限重试同卷目录改名。"""
    gc.collect()
    for attempt in range(8):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.5)


def _reject_store_inside_out_dir(app_dir: Path, out_dir: Path) -> None:
    """发布阶段会把 out_dir 整体改名；若项目数据目录位于其内，SQLite 句柄
    会阻止目录重命名（WinError 5）。提前给出明确错误而非在发布时失败。"""
    try:
        app_dir.resolve().relative_to(out_dir.resolve())
    except ValueError:
        return
    raise RuntimeError(
        "项目数据目录不能位于汉化输出目录内，否则写回发布时无法替换旧输出。"
        "请在设置中把项目数据目录移到输出目录之外。"
    )

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


#: 扫描入库聚合批大小（2026-08-19 扫描性能修复，见 Project.scan）
_SCAN_DB_BATCH = 200


def _file_fingerprint(st: os.stat_result, path: Path) -> str:
    """文件指纹：小文件（≤8MB）全量 sha256（安全写回证据链仍用内容
    哈希）；大文件退化为 size+mtime_ns 组合指纹。

    2026-08-19 扫描性能修复：_tree_hashes 此前对全树逐文件 sha256——
    大游戏几十 GB 资源（.assets/ bundle/纹理/音频）在 scan_all 里被
    完整读盘 6 次（scan_manifest_before/after + scan standalone ×2 +
    scan_v2 standalone ×2），磁盘读带宽直接决定扫描时长（实测大游戏
    仅树哈希就小时级）。清单用途是「树在扫描前后是否变化」的指纹
    比对，不是内容完整性证明——大文件 size+时间戳足够捕捉任何写入
    （mtime 变化必然伴随），小文件保留内容哈希兜底（时间戳粒度问题）。
    IL2CPP 规范输入（GameAssembly/metadata）仍有独立的 _sha256_file
    校验（写回闸门），安全语义不受影响。

    2026-08-22 ctime 修正：大文件指纹此前用 mtime+ctime——Windows 上
    ctime 语义是「创建/写入目录项时间」，复制会获得新的 ctime，与
    扫描时不同 → 写回复制后 _tree_hashes(staging) != 清单，全量写回
    被误拒（dead-catch E2E 实证：复制 166 文件全一致，仅 4 个大文件
    ctime 不同 → 「复制期间原游戏输入发生变化」）。ctime 不参与指纹，
    只在原目录侧做 mtime 快照一致性校验（复制不影响原目录 mtime）。
    """
    if st.st_size <= 8 * 1024 * 1024:
        try:
            return _sha256_file(path)
        except OSError:
            pass
    return f"s{st.st_size}:{st.st_mtime_ns}"


def _tree_hashes(root: Path) -> dict[str, str]:
    """返回普通文件的稳定相对路径哈希；符号链接与 junction 不作为可写输入跟随。

    rglob 会跟随 Windows junction 下探（islink 为 False），游戏目录中一旦出现
    指向祖先的链接（OneDrive 同步、汉化副本发布残留）会无限递归卡死扫描。
    os.walk + reparse 剪枝保证树遍历终止。

    2026-08-19：单次 os.stat 驱动指纹（is_file 与指纹共用 st，避免双
    stat 系统调用——万文件树每次扫描省 2N 次 stat）。
    """
    hashes: dict[str, str] = {}
    import stat as _stat
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [
            name for name in sorted(dirnames)
            if not _is_reparse_point(Path(dirpath) / name)
        ]
        for name in sorted(filenames):
            path = Path(dirpath) / name
            try:
                st = path.stat()
            except OSError:
                continue
            if not _stat.S_ISREG(st.st_mode) or path.is_symlink():
                continue
            hashes[path.relative_to(root).as_posix()] = _file_fingerprint(st, path)
    return hashes


def _layout_identity(fingerprint: GameFingerprint) -> tuple:
    root = fingerprint.game_dir

    def relative(path: Path | None) -> str | None:
        return path.relative_to(root).as_posix() if path is not None else None

    return (
        fingerprint.layout_kind,
        relative(fingerprint.player_root),
        relative(fingerprint.executable),
        relative(fingerprint.data_dir),
        tuple(relative(path) for path in fingerprint.application_assemblies),
        relative(fingerprint.game_assembly),
        relative(fingerprint.metadata),
    )


def _count_write_ready_translations(
        store: ProjectStore, text_only: bool = False) -> int:
    files = {item["id"]: item for item in store.get_files()}
    count = 0
    for entry in store.get_entries():
        file_record = files.get(entry["file_id"])
        if file_record is None or not is_write_ready(
                entry.get("status", ""), entry.get("translation", ""),
                entry.get("meta", "{}")):
            continue
        if entry["translation"] == entry["original"]:
            continue
        if file_record["format"].startswith("v2_"):
            if text_only:
                continue
            if not _should_write_entry(entry):
                continue
        elif is_key_style_identifier(entry["original"]) or (
                file_record["format"] == "json"
                and looks_like_key_field(entry["key_path"].rsplit("/", 1)[-1])):
            continue
        count += 1
    return count


@dataclass(frozen=True)
class _GlyphEntry:
    """store 行 → build_required_glyph_set 所需的不可变轻量视图。

    与写回语义一致：write_ready 条目取译文，其余保留原文（原样写回
    仍会被渲染，方框字审计 §7.2：需求集 = 本次发布实际渲染文本）。
    快照在 install_static_fonts 调用前定格——后续写回不得影响字形需求。"""

    file_id: str
    key_path: str
    original: str
    translation: str = ""


def _font_required_glyph_set(store: ProjectStore):
    """不可变翻译快照 → 本次发布实际渲染字形需求集（字体闭环 Phase 1/2）。

    store 行先转不可变视图（后续调用不随 store 变化），再交给
    build_required_glyph_set：富文本/空白不产生需求、<sprite> 排除、
    非 BMP 按单 scalar 计。返回 RequiredGlyphSet（scalars + locator 回溯）
    ——Phase 2 install_static_fonts 逐消费者覆盖验证的比对基准。
    """
    from hanhua.core.font.punct_normalize import normalize_font_punctuation
    snapshot: list[_GlyphEntry] = []
    for entry in store.get_entries():
        original = entry.get("original", "") or ""
        translation = entry.get("translation", "") or ""
        if translation and is_write_ready(
                entry.get("status", ""), translation,
                entry.get("meta", "{}") or "{}"):
            text = translation
        else:
            # 需求集必须与实际写出的字节一致：未翻译条目写回时回退原文，
            # 字体替换后该文本同样由新 bundle 渲染——与 writer._render 的
            # 回退归一化配套，bundle 缺字标点（– → —）从需求集中消除
            # （hickory 实证：skipped/blocked 条目回退原文含 U+2013 →
            # MISSING_CODEPOINT → 发布门永久 BLOCKED）。
            text = normalize_font_punctuation(original)
        snapshot.append(_GlyphEntry(
            entry["file_id"], entry["key_path"], original, text))
    return build_required_glyph_set(snapshot)


def _scalar_label(scalar: int) -> str:
    """码点展示标签：可打印字符带字形，其余 U+XXXX（计划 §11 缺字格式）。"""
    if 0x20 <= scalar < 0x7F or 0x4E00 <= scalar <= 0x9FFF:
        return f"{chr(scalar)} (U+{scalar:04X})"
    return f"U+{scalar:04X}"


def _font_coverage_summary(coverage, plan=None) -> dict | None:
    """逐栈/逐码点覆盖摘要（计划 §11：每种渲染栈消费者数 + 缺字 Top-N
    回溯 + 终态分布；record_writer 与 runner 输出统一口径）。"""
    if coverage is None:
        return None
    stack_counts: dict[str, int] = {}
    state_counts: dict[str, int] = {}
    missing_rows: list[dict] = []
    for cc in coverage.consumers:
        kind = cc.consumer.kind
        stack_counts[kind] = stack_counts.get(kind, 0) + 1
        state_counts[cc.state.name] = state_counts.get(cc.state.name, 0) + 1
        for scalar in sorted(cc.missing_scalars):
            sources = plan.sources_of(scalar) if plan is not None else ()
            missing_rows.append({
                "scalar": _scalar_label(scalar),
                "consumer": cc.consumer.consumer_id,
                "kind": kind,
                "locators": sources[:3],
            })
    return {
        "overall": coverage.overall.name,
        "stack_counts": stack_counts,
        "state_counts": state_counts,
        "missing": missing_rows,
    }


def _normalize_store_font_punctuation(store: ProjectStore) -> int:
    """字体标点兼容归一化（写回入口单接缝，字体启用时调用）。

    译文里 bundle 缺的标点（– EN DASH 等）→ 中文排版等价标点并持久化
    到 store（保留 meta/status，幂等）——译文、静态写回、重开验证、
    运行时插件表、字形需求集全部读 store，零漂移；缺字 → □ 在写回前
    即被消除（hickory 实证：用户 SDF 字符表缺 U+2013，发布门
    MISSING_CODEPOINT 永久 BLOCKED）。返回实际更新条数。
    """
    from hanhua.core.font.punct_normalize import (
        needs_normalization, normalize_font_punctuation)
    rows: list[tuple] = []
    for entry in store.get_entries():
        translation = entry.get("translation", "") or ""
        if needs_normalization(translation):
            rows.append((
                normalize_font_punctuation(translation),
                entry.get("status", "translated"),
                entry["file_id"], entry["key_path"]))
            continue
        # 未翻译条目（skipped/blocked/…）写回时回退原文——原文含 bundle
        # 缺字标点时同样归一化（保留 status：条目仍不可自动写回，译文
        # 仅作为「回退字节的归一化事实」持久化，与 writer._render 的
        # 回退归一化 + _font_required_glyph_set 需求集三者一致）。
        original = entry.get("original", "") or ""
        status = entry.get("status", "")
        if (status != "translated" and not translation
                and needs_normalization(original)):
            rows.append((
                normalize_font_punctuation(original),
                status, entry["file_id"], entry["key_path"]))
    if rows:
        store.batch_update_translations(rows)
    return len(rows)


def _runtime_exact_translations(store: ProjectStore) -> dict[str, str]:
    """返回无歧义、可写且明确用于显示的运行时原文映射。"""
    candidates: dict[str, set[str]] = {}
    for entry in store.get_entries():
        try:
            meta = json.loads(entry.get("meta", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        original = entry.get("original", "")
        translation = entry.get("translation", "")
        if (
            meta.get("disposition") != "translate"
            or original == translation
            or not is_write_ready(
                entry.get("status", ""), translation, meta)
        ):
            continue
        candidates.setdefault(original, set()).add(translation)
    unambiguous = {
        original: next(iter(translations))
        for original, translations in sorted(candidates.items())
        if len(translations) == 1
    }
    sources = set(unambiguous)
    return {
        original: translation
        for original, translation in unambiguous.items()
        if translation not in sources
    }


def _expected_text_translations(store: ProjectStore) -> dict[str, dict[str, str]]:
    expected: dict[str, dict[str, str]] = {}
    for file_record in store.get_files():
        if file_record["format"].startswith("v2_"):
            continue
        for entry in store.get_entries():
            if entry["file_id"] != file_record["id"] or not is_write_ready(
                    entry.get("status", ""), entry.get("translation", ""),
                    entry.get("meta", "{}")):
                continue
            if entry["translation"] == entry["original"]:
                continue
            if is_key_style_identifier(entry["original"]) or (
                    file_record["format"] == "json"
                    and looks_like_key_field(
                        entry["key_path"].rsplit("/", 1)[-1])):
                continue
            expected.setdefault(file_record["id"], {})[
                entry["key_path"]] = entry["translation"]
    return expected


def _reopen_written_outputs(store: ProjectStore, output_root: Path) -> int:
    """逐 file/key_path 重开核对普通文本译文，返回验证成功的实际补丁数。"""
    files = {item["id"]: item for item in store.get_files()}
    entries = store.get_entries()
    expected_by_file = _expected_text_translations(store)
    verified = 0
    for file_id, expected in expected_by_file.items():
        file_record = files[file_id]
        output = resolve_relative_under(output_root, file_record["rel_path"])
        if not output.is_file():
            raise RuntimeError(f"文本重开验证缺少输出文件：{file_record['rel_path']}")
        if file_record["format"] == "csv":
            delimiter = "\t" if output.suffix.lower() == ".tsv" else ","
            rows = list(csv.reader(io.StringIO(read_text(output)), delimiter=delimiter))
            header = rows[0] if rows else []
            target_col = pick_target_col(header, "zh-CN")
            actual = {}
            for entry in entries:
                if entry["file_id"] != file_id or entry["key_path"] not in expected:
                    continue
                meta = json.loads(entry.get("meta") or "{}")
                row = int(meta.get("row", -1))
                if target_col is not None and 0 <= row < len(rows) \
                        and target_col < len(rows[row]):
                    actual[entry["key_path"]] = rows[row][target_col]
            mismatched = [
                key_path for key_path, translation in expected.items()
                if actual.get(key_path) != translation
            ]
        elif file_record["format"] == "txt":
            # 行号定位 + 行内容检查：txt 的 key_path 是 line/N | plain/N |
            # kv/<key>/N（N 为行号）。整行翻译可能改变行首结构（如去掉前导
            # tab 后重开解析会从 plain 变成 kv），严格 key 匹配会误判未写入；
            # 译文写入文件后，其所在行必然包含该译文文本。
            # kv 值形态（kv/<key>/N）另做「值级验证」：整行若含 ASCII 引号 +
            # 尾随逗号（Unity .subs JSON 语言包，如 'S.C. Franklin',），写回
            # 会把译文重包成 JSON 字符串字面量（直引号+转义，见 txt_format.
            # _rewrap_json）——译文与磁盘行的键语义不再逐字相等（磁盘行含
            # ASCII 引号/尾随逗号）。此时比较「值」而非「整行」：去掉行内
            # key 与引号逗号后的值 == 译文（含中文弯引号包裹等价）。
            lines = read_text(output).splitlines()
            mismatched = []
            for key_path, translation in expected.items():
                try:
                    line_no = int(key_path.rsplit("/", 1)[1])
                except (ValueError, IndexError):
                    mismatched.append(key_path)
                    continue
                if line_no >= len(lines):
                    mismatched.append(key_path)
                    continue
                line = lines[line_no]
                if translation in line:
                    continue
                # markdown 列表行（plain）译文在写回时被 _replace_plain 剥除了
                # 行首/行尾单字符 marker（*修复...* → 修复...，磁盘保留行首
                # marker）——译文与磁盘行不再逐字相等（store 译文含包裹星号）。
                # 用同款归一化后的候选再查一次，避免误判未写入。
                norm = translation
                for _ch in ("*", "-", "+"):
                    if len(norm) >= 2 and norm.startswith(_ch) \
                            and norm.endswith(_ch):
                        norm = norm[1:-1]
                        break
                    if norm.startswith(_ch):
                        norm = norm[1:].lstrip()
                        break
                if norm != translation and norm in line:
                    continue
                # 值级降级验证：kv 行的「值部分」若是 JSON 字符串值（含 ASCII
                # 引号 + 尾随逗号，Unity .subs 语言包如 "S.C. Franklin",）——
                # 写回会把译文重包成 JSON 字符串字面量（直引号+转义，见
                # txt_format._rewrap_json）→ 译文与磁盘行不再逐字相等（磁盘
                # 行含 ASCII 引号/尾随逗号）。此时比较「值」而非「整行」：
                # 剥 key + 引号逗号，JSON 解码值与译文（含弯引号包裹等价）。
                kv_m = re.match(
                    r"^[^=:;\r\n]+?\s*(?P<d>[:=])\s*(?P<v>.*)$", line.strip())
                if kv_m:
                    val = kv_m.group("v").rstrip(",").strip()
                    if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
                        try:
                            val_u = json.loads(val)
                        except (json.JSONDecodeError, ValueError):
                            val_u = val[1:-1]
                        # 译文弯引号包裹/全角逗号回显剥除（与 _rewrap_json 同源）：
                        # “xxx” / “xxx”， → xxx
                        cand = translation
                        if cand.startswith("“") and cand.endswith("”"):
                            cand = cand[1:-1]
                        elif cand.startswith("“") and cand.endswith("，"):
                            cand = cand[1:-2]
                        if val_u == cand:
                            continue
                mismatched.append(key_path)
        else:
            parsed = parse_file(output, file_id=file_id)
            actual = {entry.key_path: entry.original for entry in parsed.entries}
            mismatched = [
                key_path for key_path, translation in expected.items()
                if actual.get(key_path) != translation
            ]
        if mismatched:
            raise RuntimeError(
                f"译文未写入或 locator 重开不一致：{file_record['rel_path']} "
                f"{', '.join(mismatched[:5])}")
        verified += len(expected)
    return verified


def _set_route_status(route: tuple[BackendStep, ...], step_id: str,
                      status: str, reason: str | None = None) -> tuple[BackendStep, ...]:
    return tuple(
        replace(step, status=status,
                confidence="low" if status in {"failed", "blocked"} else step.confidence,
                reason=reason or step.reason)
        if step.step_id == step_id else step
        for step in route
    )


def _is_dumper_version_gap(reason: str, fingerprint: GameFingerprint) -> bool:
    """dumper 失败是否为版本缺口：错误描述不支持 metadata 版本，且 native
    解析器声明支持该版本，且能实际解析成功（三证据齐备才降级）。"""
    lowered = reason.casefold()
    if "not a supported version" not in lowered \
            and "notsupportedexception" not in lowered:
        return False
    if fingerprint.metadata_version not in SUPPORTED_LITERAL_RECORD_SIZES:
        return False
    try:
        raw = fingerprint.metadata.read_bytes()
        il2cpp_extractor.parse_string_literals(raw)
    except Exception:  # noqa: BLE001 native 解析失败则不构成降级证据
        return False
    return True


@dataclass(frozen=True)
class ToolAnalysisResult:
    tool_id: str
    status: str
    required: bool
    cache_hit: bool = False
    elapsed_ms: int = 0
    details: tuple[tuple[str, str], ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class PipelineEvent:
    phase: str
    status: str
    message: str
    current: int = 0
    total: int = 0


@dataclass(frozen=True)
class AnalysisReport:
    fingerprint: GameFingerprint
    tool_statuses: tuple[ToolStatus, ...]
    route: tuple[BackendStep, ...]
    font_capability: FontProviderCapability
    text_files: int = 0
    v2_files: int = 0
    recognized_entries: int = 0
    status_counts: tuple[tuple[str, int], ...] = ()
    confidence_counts: tuple[tuple[str, int], ...] = ()
    tool_results: tuple[ToolAnalysisResult, ...] = ()
    input_protected: bool = True
    unblocked: bool = False
    completable: bool = False
    warnings: tuple[str, ...] = ()
    # 形态覆盖统计：(形态名, 文件数, 条目数)——形态注册表见
    # hanhua/core/tooling/morphology.py（显式清单 + 文本先验）
    morphology_stats: tuple[tuple[str, int, int], ...] = ()
    # R5 提取侧静默跳过留档：{跳过形态: 计数}——continue 不产生条目的
    # 串聚合可见（哑识别可观测，召回率审查数据源）
    skipped_reasons: dict[str, int] = field(default_factory=dict)


class Project:
    """一个游戏目录 = 一个项目：扫描入库，输出到独立目录。"""

    def __init__(
        self,
        game_dir: Path,
        app_dir: Path,
        font_config: FontConfig | None = None,
        *,
        player_root: Path | None = None,
        player_executable: Path | None = None,
    ):
        self.game_dir = Path(game_dir).expanduser().absolute()
        self.app_dir = Path(app_dir)
        selected = (
            fingerprint_game(
                self.game_dir,
                player_root=player_root,
                player_executable=player_executable,
            )
            if player_root is not None or player_executable is not None
            else None
        )
        self.player_root = (
            selected.player_root.relative_to(selected.game_dir)
            if selected is not None and selected.player_root is not None else None
        )
        self.player_executable = (
            selected.executable.relative_to(selected.game_dir)
            if selected is not None and selected.executable is not None else None
        )
        game_dir = self.game_dir
        self.font_config = (
            replace(font_config) if font_config is not None else FontConfig(enabled=False)
        )
        self.out_dir = game_dir.parent / (game_dir.name + "_汉化")
        self.store = ProjectStore(
            self.app_dir / "projects" /
            _slug(self.game_dir, self.player_root, self.player_executable) /
            "project.db")
        self._last_analysis_report: AnalysisReport | None = None
        self._last_scan_morphology: tuple[tuple[str, int, int], ...] = ()
        self._last_scan_morph_warnings: tuple[str, ...] = ()
        # R5：提取侧静默跳过聚合（scan + scan_v2 合并；scan_all 开始时清零）
        self._last_scan_skipped: dict[str, int] = {}
        self._last_il2cpp_input_hashes: tuple[str, str] | None = None
        self._last_source_manifest: dict[str, str] | None = None
        # 文本阶段 standalone 扫描时的全树快照：scan_v2 绑定前要求文本
        # 条目与当前树同源，防止陈旧文本条目对新树写回（review 实证）
        self._last_text_scan_manifest: dict[str, str] | None = None
        # --resume 续跑恢复：上次成功扫描绑定的清单持久化在库中（跳过
        # 扫描的续跑必须恢复，否则 write_all 输入闸门与 IL2CPP 规范输入
        # 证据均为 None → 写回被拒——faerie 续跑实证 2026-08-12）。
        # 校验仍有效：恢复的旧清单会与实际树 hash 比对，输入被改动仍拒绝。
        _il = self.store.get_profile_value("il2cpp_input_hashes")
        self._last_il2cpp_input_hashes = (
            tuple(_il) if isinstance(_il, list) else None)
        self._last_source_manifest = self.store.get_profile_value(
            "source_manifest")
        self._last_text_scan_manifest = self.store.get_profile_value(
            "text_scan_manifest")
        # IL2CPP 交叉验证证据持久化恢复：write_all 闸门要求 report 携带
        # il2cpp_dumper 交叉验证结果（native_total/agreement 等）——resume
        # 跳过扫描时从库恢复轻量 report（ffs 续跑实证 2026-08-12：写回被拒
        # 「缺少本次项目成功的 native/Il2CppDumper 交叉验证证据」）。输入
        # 一致性由 il2cpp_input_hashes 校验兜底，指纹以当前检测为准。
        _ev = self.store.get_profile_value("il2cpp_cross_check")
        if isinstance(_ev, dict):
            _route_status = _ev.get("route_status")
            _tool_status = _ev.get("tool_status")
            route = (
                (BackendStep("tool_analysis", "il2cpp_dumper",
                             _route_status, True, "high", ""),)
                if _route_status else ()
            )
            tool_results = (
                (ToolAnalysisResult(
                    "il2cpp_dumper", _tool_status, True,
                    details=tuple((str(k), str(v)) for k, v in
                                  (_ev.get("details") or {}).items()),
                    reason=str(_ev.get("tool_reason") or "")),)
                if _tool_status else ()
            )
            try:
                self._last_analysis_report = AnalysisReport(
                    fingerprint=self._fingerprint(),
                    tool_statuses=(),
                    route=route,
                    font_capability=FontProviderCapability(
                        provider_id="", runtime="", architecture="",
                        provider_supported=True, payload_available=False),
                    tool_results=tool_results,
                    input_protected=bool(_ev.get("input_protected", False)),
                    unblocked=bool(_ev.get("unblocked", False)),
                )
            except Exception:  # noqa: BLE001 - 恢复失败不阻断（重扫后证据重建）
                self._last_analysis_report = None
        self._scan_all_active = False

    def _store_scan_state(self) -> None:
        """持久化扫描绑定状态（--resume 续跑恢复用）。

        在所有设置/清空扫描清单的点之后调用：把三个字段当前值统一存库。
        存 JSON null 与删除等价（get_profile_value 解析 null → None）。
        """
        self.store.set_profile_value(
            "source_manifest", self._last_source_manifest)
        self.store.set_profile_value(
            "il2cpp_input_hashes", self._last_il2cpp_input_hashes)
        self.store.set_profile_value(
            "text_scan_manifest", self._last_text_scan_manifest)

    def _fingerprint(self) -> GameFingerprint:
        return fingerprint_game(
            self.game_dir,
            player_root=self.player_root,
            player_executable=self.player_executable,
        )

    def _selected_player_root(
            self, fingerprint: GameFingerprint | None = None) -> Path:
        current = fingerprint or self._fingerprint()
        if current.player_root is None:
            if "ambiguous_player_layout" in current.evidence:
                raise RuntimeError("Unity player layout is ambiguous")
            return self.game_dir
        return current.player_root

    @staticmethod
    def _excluded_sibling_data_roots(
            fingerprint: GameFingerprint) -> tuple[Path, ...]:
        if (
            fingerprint.layout_kind != "standard"
            or fingerprint.player_root is None
            or fingerprint.data_dir is None
        ):
            return ()
        return tuple(sorted(
            candidate.data_dir
            for candidate in discover_player_candidates(fingerprint.game_dir)
            if candidate.player_root == fingerprint.player_root
            and candidate.executable != fingerprint.executable
        ))

    @staticmethod
    def _excluded_sibling_player_roots(
            fingerprint: GameFingerprint) -> tuple[Path, ...]:
        selected_root = fingerprint.player_root
        if selected_root is None:
            return ()
        excluded: list[Path] = []
        for candidate in discover_player_candidates(fingerprint.game_dir):
            if candidate.player_root == selected_root:
                continue
            try:
                candidate.player_root.relative_to(selected_root)
            except ValueError:
                continue
            excluded.append(candidate.player_root)
        return tuple(sorted(set(excluded)))

    def _structured_scan_root(self, fingerprint: GameFingerprint) -> Path:
        selected_root = self._selected_player_root(fingerprint)
        if self._excluded_sibling_data_roots(fingerprint):
            if fingerprint.data_dir is None:
                raise RuntimeError("selected Unity player has no data directory")
            return fingerprint.data_dir
        return selected_root

    def analyze(self) -> AnalysisReport:
        """只读检测游戏、固定工具完整性和确定性自动路由。"""
        fingerprint = self._fingerprint()
        app_root = Path(__file__).resolve().parents[2]
        registry = ToolRegistry.load(app_root)
        statuses = registry.statuses()
        font_capability = resolve_font_provider(
            self.game_dir, fingerprint.runtime,
            player_root=fingerprint.player_root)
        route = plan_backends(
            fingerprint, {tool_id: status.state
                          for tool_id, status in statuses.items()},
            font_capability=font_capability,
            # Phase 5：位图 provider 计数（0 = 无可注入资产 → 旧语义）
            bitmap_provider_count=len(resolve_bitmap_providers(
                self.game_dir, fingerprint,
                exclude_roots=(self.out_dir,))),
        )
        report = AnalysisReport(
            fingerprint=fingerprint,
            tool_statuses=tuple(statuses[key] for key in sorted(statuses)),
            route=route,
            font_capability=font_capability,
            input_protected=True,
            unblocked=plan_is_unblocked(route),
            completable=plan_is_completable(route),
        )
        return report

    def scan_all(self, event_cb: Callable[[PipelineEvent], None] | None = None,
                 csv_overwrite_source: bool = False) -> AnalysisReport:
        """统一执行只读检测、原生扫描和受控工具交叉分析。"""
        def emit(phase: str, status: str, message: str,
                 current: int = 0, total: int = 0) -> None:
            if event_cb:
                event_cb(PipelineEvent(phase, status, message, current, total))

        scan_manifest_before = _tree_hashes(self.game_dir)
        self._last_source_manifest = None
        # 建表必须发生在任何提前返回之前（如 ambiguous_player_layout 的
        # blocked 路径）：否则 blocked 项目 DB 无 entries 表，后续
        # get_entries 抛 OperationalError（ned-flanders 真实案例）
        self.store.init_schema()
        base = self.analyze()
        self._last_il2cpp_input_hashes = None
        self._store_scan_state()
        if (
            base.fingerprint.player_root is None
            and "ambiguous_player_layout" in base.fingerprint.evidence
        ):
            emit("detection", "blocked", "ambiguous_player_layout")
            self._last_analysis_report = base
            return base
        route = base.route
        warnings: list[str] = []
        tool_results: list[ToolAnalysisResult] = []
        protected_paths = tuple(path for path in (
            base.fingerprint.executable,
            base.fingerprint.game_assembly,
            base.fingerprint.metadata,
        ) if path is not None and path.is_file())
        before_hashes = {path: _sha256_file(path) for path in protected_paths}
        emit("detection", "succeeded",
             f"{base.fingerprint.runtime} · Unity {base.fingerprint.unity_version}")

        self._scan_all_active = True
        self._last_scan_skipped = {}   # R5：扫描前清零，scan+scan_v2 聚合
        try:
            text_files = self.scan()
            emit("text_scan", "succeeded", f"结构化文本文件 {text_files} 个")
            v2_files = self._scan_v2_with_progress(emit,
                                                   csv_overwrite_source)
        finally:
            self._scan_all_active = False
        warnings.extend(self._last_scan_morph_warnings)
        route = _set_route_status(route, "text_scan", "succeeded")
        emit("binary_scan", "succeeded", f"Unity 二进制资源 {v2_files} 个")

        fingerprint = base.fingerprint
        if fingerprint.runtime == "il2cpp":
            status_by_id = {status.tool_id: status for status in base.tool_statuses}
            dumper_status = status_by_id["il2cpp_dumper"]
            if dumper_status.state == "verified":
                started = time.perf_counter()
                try:
                    if fingerprint.game_assembly is None or fingerprint.metadata is None:
                        raise RuntimeError("IL2CPP 规范输入缺失")
                    app_root = Path(__file__).resolve().parents[2]
                    registry = ToolRegistry.load(app_root)
                    run_result, sidecar = run_il2cpp_dumper(
                        IsolatedToolRunner(self.app_dir / "tooling"),
                        registry.specs["il2cpp_dumper"],
                        fingerprint.game_assembly,
                        fingerprint.metadata,
                        app_root / "tools" / "Il2CppDumper" / "config.json",
                    )
                    raw = fingerprint.metadata.read_bytes()
                    native = [raw[pos:pos + length].decode("utf-8")
                              for _, length, pos in il2cpp_extractor.parse_string_literals(raw)]
                    anchors = ("[PICK UP]",) if "[PICK UP]" in native else ()
                    comparison = compare_literals(
                        native, sidecar, required_anchors=anchors)
                    if (comparison.agreement < 0.98 or comparison.sidecar_only != 0
                            or comparison.anchors_missing):
                        raise RuntimeError(
                            "IL2CPP 交叉验证未达到一致率/sidecar-only 安全门")
                    tool_results.append(ToolAnalysisResult(
                        "il2cpp_dumper", "succeeded", True,
                        cache_hit=run_result.cache_hit,
                        elapsed_ms=run_result.elapsed_ms,
                        details=(("native_total", str(comparison.native_total)),
                                 ("sidecar_total", str(comparison.sidecar_total)),
                                 ("intersection", str(comparison.intersection)),
                                 ("agreement", f"{comparison.agreement:.6f}")),
                    ))
                    route = _set_route_status(route, "tool_analysis", "succeeded")
                    emit("tool_analysis", "succeeded", "Il2CppDumper 交叉验证通过")
                except Exception as exc:  # noqa: BLE001 外部工具失败不得丢弃原生结果
                    reason = str(exc) or type(exc).__name__
                    elapsed_ms = int((time.perf_counter() - started) * 1000)
                    tool_results.append(ToolAnalysisResult(
                        "il2cpp_dumper", "failed", True,
                        elapsed_ms=elapsed_ms, reason=reason))
                    if _is_dumper_version_gap(reason, fingerprint):
                        # dumper 二进制不支持该 metadata 版本，但 native 解析器
                        # 已声明支持且实际解析成功：降级为 skipped 且不再必需
                        # （审计保留 failed 记录），不阻断流水线；升级 dumper
                        # 后自动恢复为 succeeded 交叉验证。
                        route = _set_route_status(route, "tool_analysis", "skipped",
                                                  reason)
                        route = tuple(
                            replace(step, required=False)
                            if step.step_id == "tool_analysis" else step
                            for step in route)
                        warnings.append(
                            f"Il2CppDumper：{reason}（native 解析器已验证 "
                            f"v{fingerprint.metadata_version}，降级为 skipped）")
                        emit("tool_analysis", "skipped", reason)
                    else:
                        route = _set_route_status(route, "tool_analysis", "failed",
                                                  reason)
                        warnings.append(f"Il2CppDumper：{reason}")
                        emit("tool_analysis", "failed", reason)
            else:
                reason = dumper_status.reason or f"工具状态：{dumper_status.state}"
                tool_results.append(ToolAnalysisResult(
                    "il2cpp_dumper", "blocked", True, reason=reason))
                route = _set_route_status(route, "tool_analysis", "blocked", reason)
                warnings.append(f"Il2CppDumper：{reason}")
                emit("tool_analysis", "blocked", reason)
        else:
            emit("tool_analysis", "skipped", "Mono 游戏无需 Il2CppDumper")

        after_hashes = {path: _sha256_file(path) for path in protected_paths}
        scan_manifest_after = _tree_hashes(self.game_dir)
        input_protected = (
            before_hashes == after_hashes
            and scan_manifest_before == scan_manifest_after
        )
        if not input_protected:
            warnings.append("分析期间检测到完整输入文件树变化")
            route = _set_route_status(
                route, "tool_analysis", "failed", "关键输入哈希发生变化")

        self.store.init_schema()
        rows = self.store.get_entries()
        status_counts = tuple((status, sum(row["status"] == status for row in rows))
                              for status in ("pending", "translated", "failed",
                                             "skipped", "blocked"))
        confidence = {"high": 0, "medium": 0, "low": 0}
        for row in rows:
            try:
                meta = json.loads(row.get("meta") or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            level = meta.get("confidence", "medium")
            if level in confidence and row["status"] != "skipped":
                confidence[level] += 1
        unblocked = input_protected and plan_is_unblocked(route)
        completable = input_protected and plan_is_completable(route)
        emit("complete", "succeeded" if unblocked else "blocked",
             "分析可继续" if unblocked else "存在必需能力阻断")
        report = AnalysisReport(
            fingerprint=fingerprint,
            tool_statuses=base.tool_statuses,
            route=route,
            font_capability=base.font_capability,
            text_files=text_files,
            v2_files=v2_files,
            recognized_entries=sum(row["status"] != "skipped" for row in rows),
            status_counts=status_counts,
            confidence_counts=tuple(confidence.items()),
            tool_results=tuple(tool_results),
            input_protected=input_protected,
            unblocked=unblocked,
            completable=completable,
            warnings=tuple(warnings),
            morphology_stats=self._last_scan_morphology,
            skipped_reasons=dict(self._last_scan_skipped),
        )
        self._last_analysis_report = report
        if report.input_protected and report.unblocked:
            self._last_source_manifest = dict(scan_manifest_after)
        successful_il2cpp = next((
            item for item in report.tool_results
            if item.tool_id == "il2cpp_dumper" and item.status == "succeeded"
        ), None)
        # 版本缺口降级（#183）同样锚定规范输入：native 解析器已实际解析成功
        degraded_il2cpp = next((
            item for item in report.tool_results
            if item.tool_id == "il2cpp_dumper" and item.status == "failed"
            and _is_dumper_version_gap(item.reason or "", fingerprint)
        ), None)
        if (
            report.unblocked
            and (successful_il2cpp is not None or degraded_il2cpp is not None)
            and fingerprint.game_assembly is not None
            and fingerprint.metadata is not None
        ):
            self._last_il2cpp_input_hashes = (
                before_hashes[fingerprint.game_assembly],
                before_hashes[fingerprint.metadata],
            )
        # IL2CPP 交叉验证证据持久化（resume 写回闸门需要 report 携带
        # il2cpp_dumper 结果——ffs 续跑实证：report=None → 写回被拒）。
        # 与三清单同一模式：scan_all 成功时存，__init__ 恢复轻量 report。
        if fingerprint.runtime == "il2cpp":
            _dumper = next((
                item for item in report.tool_results
                if item.tool_id == "il2cpp_dumper"), None)
            _route_status = next((
                step.status for step in route
                if step.step_id == "tool_analysis"), None)
            self.store.set_profile_value("il2cpp_cross_check", {
                "route_status": _route_status,
                "tool_status": _dumper.status if _dumper else None,
                "tool_reason": _dumper.reason if _dumper else None,
                "details": dict(_dumper.details) if _dumper else {},
                "input_protected": report.input_protected,
                "unblocked": report.unblocked,
            })
        else:
            self.store.set_profile_value("il2cpp_cross_check", None)
        self._store_scan_state()
        return report

    def _flush_scan_batch(self, batch_files: list[tuple],
                          batch_rows: list[dict]) -> None:
        """聚合批落库（2026-08-19 扫描性能修复，见 scan 的批说明）。

        文件记录 executemany 单语句，条目走 upsert_entries（既有合并
        语义不变：skipped 覆盖/译文继承）；空批零成本。"""
        if not batch_files:
            return
        for file_id, rel, fmt, encoding, eol, meta in batch_files:
            self.store.add_file(file_id, rel, fmt, encoding, eol, meta)
        self.store.upsert_entries(batch_rows)

    def scan(self) -> int:
        """扫描并入库，返回保留的文本文件数。规则升级后被淘汰的旧文件自动清理。"""
        standalone_before = None
        prev_manifest = self._last_source_manifest
        if not self._scan_all_active:
            self._last_source_manifest = None
            standalone_before = _tree_hashes(self.game_dir)
            self._store_scan_state()
        fingerprint = self._fingerprint()
        selected_root = self._structured_scan_root(fingerprint)
        excluded_roots = self._excluded_sibling_player_roots(fingerprint)
        self.store.init_schema()
        files = discover(selected_root, exclude_roots=excluded_roots)
        found_ids: set[str] = set()
        kept = 0
        # 2026-08-19 扫描性能修复：逐文件 add_file + upsert_entries 各
        # 一次 commit——万文件游戏一次扫描 2 万次 commit（WAL 下每次
        # commit 仍是事务开销 + RLock 串行）。改为聚合批：文件记录与
        # 条目累积到 _scan_batch_size（或遍历结束）一次性提交。中途
        # 异常时已完成批已落库，重扫幂等（upsert 语义）。
        batch_files: list[tuple] = []
        batch_rows: list[dict] = []
        try:
            for f in files:
                rel = str(f.relative_to(self.game_dir)).replace("\\", "/")
                pf = parse_file(f, file_id=rel)
                if pf.noise:
                    continue      # 整文件为运行时噪音，不入库
                # R5：提取侧静默跳过聚合（哑识别可见化）
                for morph, count in pf.skipped_reasons.items():
                    self._last_scan_skipped[morph] = (
                        self._last_scan_skipped.get(morph, 0) + count)
                found_ids.add(rel)
                batch_files.append((
                    pf.file_id, rel, pf.format, pf.encoding, pf.eol,
                    pf.meta))
                batch_rows.extend(
                    {"file_id": e.file_id, "key_path": e.key_path,
                     "original": e.original, "status": e.status,
                     "meta": e.meta}
                    for e in pf.entries)
                kept += 1
                if len(batch_files) >= _SCAN_DB_BATCH:
                    self._flush_scan_batch(batch_files, batch_rows)
                    batch_files = []
                    batch_rows = []
        finally:
            self._flush_scan_batch(batch_files, batch_rows)
        for old in self.store.get_files():
            if old["format"].startswith("v2_"):
                continue      # v2 文件由 scan_v2 管理，不能在此清理
            if old["id"] not in found_ids:
                self.store.remove_file(old["id"])
        if standalone_before is not None:
            standalone_after = _tree_hashes(self.game_dir)
            has_v2_records = any(
                f["format"].startswith("v2_")
                for f in self.store.get_files())
            if standalone_before == standalone_after:
                # 记录文本阶段全树快照：scan_v2 绑定前用它核对文本条目
                # 与当前树同源（陈旧文本条目对新树写回会错位）
                self._last_text_scan_manifest = dict(standalone_after)
                # _tree_hashes 是全树快照（含二进制资源），源未变即可作为
                # 清单证据。但 store 已存在 v2 文件记录时（先前统一扫描或
                # 手动导入过），只跑文本扫描不更新 v2 条目，store 清单不
                # 完整——必须失效 baseline 直到 scan_v2 完成（test_project
                # 实证）。从未有 v2 记录则绑定：写回只涉及已入库的文本条目，
                # 树 hash 覆盖全部输入。先 scan_v2 后 scan 的顺序（v2 已
                # 绑定且树未变）保持绑定，避免顺序依赖假拒绝。
                if not has_v2_records:
                    self._last_source_manifest = dict(standalone_after)
                elif prev_manifest == standalone_after:
                    # 仅当 v2 条目真实入库（scan_v2 实际扫描过）时才保持
                    # 绑定：手动 add_file 只加文件记录无条目时，文本扫描
                    # 不覆盖这些输入，store 清单不完整——保持失效直到
                    # scan_all（混合定位器 review 实证）
                    v2_file_ids = [
                        f["id"] for f in self.store.get_files()
                        if f["format"].startswith("v2_")]
                    # 2026-08-19：get_entries() 全表遍历 → SQL COUNT
                    if self.store.count_by_files(v2_file_ids):
                        self._last_source_manifest = dict(standalone_after)
        self._store_scan_state()
        return kept

    # ── v2：Unity 二进制资源扫描（.assets / DLL / IL2CPP metadata） ──
    def _build_typetree_generator(self, fingerprint):
        """MonoBehaviour typetree 生成器：资产未带 typetree（DisableWrite
        TypeTree / Player 构建 strip）时，Mono 游戏可从本地 Managed DLL
        生成脚本 typetree——hickory 实证 1890/1898 读取失败（文本全漏 +
        TMP 字体无法静态替换）→ 生成后 1884/1898 成功。IL2CPP（无 DLL）
        或生成器缺失时返回 None 静默回退 raw scan 兜底，不影响扫描。
        扫描与写回共用同一生成器，保证两侧对 MonoBehaviour 视图一致。

        预检：只加载带完整 CLR metadata 的真实 .NET 程序集。原生生成器
        在假/损坏 DLL（stub PE、纯文本占位）上会留下不稳定状态，同进程
        多次失败后进程级崩溃（test_project 实测 exit 127）；真游戏
        Managed 目录也常混有垃圾占位文件，一律过滤。
        """
        if fingerprint.data_dir is None or not fingerprint.unity_version:
            return None
        managed_dir = fingerprint.data_dir / "Managed"
        if not managed_dir.is_dir():
            return None
        dlls = [p for p in sorted(managed_dir.glob("*.dll"))
                if _is_managed_dll_with_metadata(p)]
        if not dlls:
            return None
        try:
            from UnityPy.helpers.TypeTreeGenerator import (
                TypeTreeGenerator)
            tt_generator = TypeTreeGenerator(
                fingerprint.unity_version)
            for dll in dlls:
                tt_generator.load_dll(dll.read_bytes())
            return tt_generator
        except Exception:  # noqa: BLE001 生成器不可用不阻断扫描
            return None

    def _scan_v2_with_progress(self, emit, csv_overwrite_source: bool) -> int:
        """scan_v2 包装：把逐文件进度转成 binary_scan PipelineEvent。

        大型游戏 scan_v2 可能跑数分钟——GUI 侧此前拿不到任何中间进度，
        「识别」节点全程停在首个 running。这里以 1/5 文件为粒度转发
        binary_scan running 事件（当前/总数来自 scan_v2 的 progress_cb），
        完成/异常分别补 succeeded/failed。"""
        def cb(done: int, total: int) -> None:
            if total and (done == 1 or done % 5 == 0 or done == total):
                emit("binary_scan", "running",
                     f"Unity 二进制资源 {done}/{total}", done, total)
        try:
            n = self.scan_v2(progress_cb=cb,
                             csv_overwrite_source=csv_overwrite_source)
        except Exception:
            emit("binary_scan", "failed", "scan_v2 异常")
            raise
        return n

    def scan_v2(self, progress_cb: Callable | None = None,
                csv_overwrite_source: bool = False) -> int:
        """扫描二进制资源并入库，返回保留的资源文件数。"""
        standalone_before = None
        if not self._scan_all_active:
            self._last_source_manifest = None
            standalone_before = _tree_hashes(self.game_dir)
            self._store_scan_state()
        fingerprint = self._fingerprint()
        selected_root = self._structured_scan_root(fingerprint)
        excluded_roots = self._excluded_sibling_player_roots(fingerprint)
        self.store.init_schema()
        found_ids: set[str] = set()
        kept = 0
        tt_generator = self._build_typetree_generator(fingerprint)
        sources: list[tuple[Callable, Path, str]] = []
        for f in unity_extractor.find_asset_files(
                selected_root, data_dir=fingerprint.data_dir,
                exclude_roots=excluded_roots):
            rel = str(f.relative_to(self.game_dir)).replace("\\", "/")
            if tt_generator is not None:
                sources.append((
                    lambda f_, file_id=None, gen=tt_generator,
                    ovr=csv_overwrite_source:
                    unity_extractor.extract_asset_file(
                        f_, file_id=file_id, typetree_generator=gen,
                        csv_overwrite_source=ovr), f, rel))
            else:
                sources.append((
                    lambda f_, file_id=None, ovr=csv_overwrite_source:
                    unity_extractor.extract_asset_file(
                        f_, file_id=file_id,
                        csv_overwrite_source=ovr), f, rel))
        # 跨程序集 UI sink 闭包（证明链扩展）：多 DLL 游戏的显示方法链
        # （Fungus 等插件方法 → 插件内部 set_text）在逐程序集证明中不可见，
        # 一次性联合计算后传给每个程序集提取器（deadbeat 实证 +10 条
        # 真实 UI 文本被证明）。闭包失败静默回退逐程序集证明，不阻断扫描。
        cross_sinks: frozenset = frozenset()
        if len(fingerprint.application_assemblies) >= 2:
            try:
                import dnfile
                cross_sinks = mono_extractor._cross_assembly_ui_sinks([
                    dnfile.dnPE(str(f))
                    for f in fingerprint.application_assemblies])
            except Exception:  # noqa: BLE001
                cross_sinks = frozenset()
        for f in fingerprint.application_assemblies:
            rel = str(f.relative_to(self.game_dir)).replace("\\", "/")
            sources.append((
                functools.partial(
                    mono_extractor.extract_dll_user_strings,
                    cross_sinks=cross_sinks), f, rel))
        meta = fingerprint.metadata
        if meta is not None:
            rel = str(meta.relative_to(self.game_dir)).replace("\\", "/")
            sources.append((il2cpp_extractor.extract_metadata_strings, meta, rel))
        # 形态覆盖统计（注册表显式清单）：未知形态 → 显式告警而非静默处理
        morph_files: dict[str, int] = {}
        morph_entries: dict[str, int] = {}
        morph_warnings: list[str] = []
        for i, (fn, f, rel) in enumerate(sources):
            if progress_cb:
                progress_cb(i + 1, len(sources))
            morph = classify_morphology(rel)
            if morph is None:
                morph_warnings.append(
                    f"未知文本形态：{rel}（未注册形态清单，请先登记再接线，"
                    f"见 docs/识别形态覆盖与遗漏处理.md）")
            else:
                morph_files[morph] = morph_files.get(morph, 0) + 1
            pf = fn(f, file_id=rel)
            if pf.noise:
                continue
            # R5：提取侧静默跳过聚合（哑识别可见化）
            for morph, count in pf.skipped_reasons.items():
                self._last_scan_skipped[morph] = (
                    self._last_scan_skipped.get(morph, 0) + count)
            found_ids.add(rel)
            if morph is not None:
                morph_entries[morph] = (
                    morph_entries.get(morph, 0) + len(pf.entries))
            self.store.add_file(pf.file_id, rel, pf.format, pf.encoding, pf.eol, pf.meta)
            new_keys = {e.key_path for e in pf.entries}
            # 重扫后不再存在的旧条目（如已被规则过滤的键位置）→ 删除，避免残留写回
            # 2026-08-19 扫描性能修复：旧实现每文件 store.get_entries() 全库
            # SELECT *（几万条目 × 数百文件 = O(N×M)，实测 412 文件 33s 纯
            # 浪费 + dict 构造内存抖动）——改为单文件 key_path 轻量查询。
            old_keys = self.store.get_entry_key_paths(rel) - new_keys
            self.store.upsert_entries([
                {"file_id": e.file_id, "key_path": e.key_path,
                 "original": e.original, "status": e.status, "meta": e.meta}
                for e in pf.entries])
            if old_keys:
                self.store.remove_entries(rel, old_keys)
            kept += 1
        for old in self.store.get_files():
            if old["format"].startswith("v2_") and old["id"] not in found_ids:
                self.store.remove_file(old["id"])
        if standalone_before is not None:
            standalone_after = _tree_hashes(self.game_dir)
            if standalone_before == standalone_after:
                # scan_v2 只更新 v2 条目，文本条目来源树必须与当前树同源
                # （_last_text_scan_manifest 由 scan() 记录；从未跑过文本
                # 阶段则视为无文本条目）。否则 scan() 绑定后文件被改、再
                # 单独 scan_v2 会用新树覆盖绑定，陈旧文本条目对新树写回
                # ——无条件绑定会绕过 write_all 输入清单闸门（review 实证）
                text_same = (
                    self._last_text_scan_manifest is None
                    or standalone_after == self._last_text_scan_manifest)
                if text_same:
                    self._last_source_manifest = dict(standalone_after)
        self._last_scan_morphology = tuple(sorted(
            (m, morph_files.get(m, 0), morph_entries.get(m, 0))
            for m in morph_files))
        self._last_scan_morph_warnings = tuple(morph_warnings)
        self._store_scan_state()
        return kept

    # ── 写回（文本 + 二进制资源） ──
    def _validate_write_route(
            self, write_ready: int, font_config: FontConfig,
            ) -> tuple[
                GameFingerprint,
                tuple[BackendStep, ...],
                FontProviderCapability,
            ]:
        """Re-evaluate the core write capability before creating staging."""
        fingerprint = self._fingerprint()
        if (
            self._last_analysis_report is not None
            and _layout_identity(fingerprint) != _layout_identity(
                self._last_analysis_report.fingerprint)
        ):
            raise RuntimeError(
                "Unity player layout/backend inputs changed after scan")
        app_root = Path(__file__).resolve().parents[2]
        statuses = ToolRegistry.load(app_root).statuses()
        font_capability = resolve_font_provider(
            self.game_dir, fingerprint.runtime,
            player_root=fingerprint.player_root)
        route = plan_backends(
            fingerprint,
            {tool_id: status.state for tool_id, status in statuses.items()},
            font_capability=font_capability,
        )
        if fingerprint.runtime == "unknown":
            raise RuntimeError("未识别 Unity 运行时，已拒绝写回")
        if self.store.get_files():
            route = _set_route_status(route, "text_scan", "succeeded")
        if not font_config.enabled:
            route = _set_route_status(
                route, "font", "succeeded", "用户未启用运行时字体覆盖")
        if fingerprint.runtime == "il2cpp" and self._last_analysis_report is not None:
            analyzed_tool = next((
                step for step in self._last_analysis_report.route
                if step.step_id == "tool_analysis"
            ), None)
            if analyzed_tool is not None:
                route = _set_route_status(
                    route, "tool_analysis", analyzed_tool.status, analyzed_tool.reason)
                # 版本缺口降级把 tool_analysis 置为不再必需（#183），
                # 写回预检继承该语义，避免降级被当作未完成先决步骤
                if not analyzed_tool.required:
                    route = tuple(
                        replace(step, required=False)
                        if step.step_id == "tool_analysis" else step
                        for step in route)
        blockers = [
            step for step in route
            if step.required and step.status in {"blocked", "failed"}
        ]
        if blockers:
            summary = "；".join(
                f"{step.step_id}: {step.reason}" for step in blockers)
            raise RuntimeError(f"必需 writer 路由不可用，已拒绝写回：{summary}")
        execution_steps = {"font", "writeback"}
        if fingerprint.runtime == "il2cpp":
            report = self._last_analysis_report
            current_input_hashes = (
                _sha256_file(fingerprint.game_assembly)
                if fingerprint.game_assembly is not None else "",
                _sha256_file(fingerprint.metadata)
                if fingerprint.metadata is not None else "",
            )
            if (
                self._last_il2cpp_input_hashes is not None
                and current_input_hashes != self._last_il2cpp_input_hashes
            ):
                raise RuntimeError(
                    "IL2CPP 交叉验证后的规范输入发生变化，已拒绝写回")
            tool_result = next((
                item for item in (report.tool_results if report else ())
                if item.tool_id == "il2cpp_dumper"
            ), None)
            route_status = next((
                step.status for step in (report.route if report else ())
                if step.step_id == "tool_analysis"
            ), None)
            details = dict(tool_result.details) if tool_result else {}
            try:
                native_total = int(details["native_total"])
                sidecar_total = int(details["sidecar_total"])
                intersection = int(details["intersection"])
                agreement = float(details["agreement"])
            except (KeyError, TypeError, ValueError):
                native_total = sidecar_total = intersection = 0
                agreement = 0.0
            cross_checked = (
                route_status == "succeeded"
                and tool_result is not None
                and tool_result.status == "succeeded"
                and native_total > 0
                and sidecar_total > 0
                and intersection > 0
                and agreement >= 0.98
            )
            # 版本缺口降级（dumper 二进制不支持 metadata 版本，native 解析器
            # 声明支持且实际解析成功）提供等价的写回证据链：#183
            version_gap_degraded = (
                route_status == "skipped"
                and tool_result is not None
                and tool_result.status == "failed"
                and _is_dumper_version_gap(tool_result.reason or "", fingerprint)
            )
            evidence_valid = (
                report is not None
                and report.fingerprint == fingerprint
                and report.input_protected
                and report.unblocked
                and current_input_hashes == self._last_il2cpp_input_hashes
                and (cross_checked or version_gap_degraded)
            )
            if not evidence_valid:
                raise RuntimeError(
                    "IL2CPP 写回缺少本次项目成功的 native/Il2CppDumper 交叉验证证据")
        if write_ready <= 0:
            raise RuntimeError("没有通过质量门的可写译文，已拒绝写回")
        quality_step = next((
            step for step in route if step.step_id == "translation_quality"
        ), None)
        if quality_step is None or quality_step.status not in {"blocked", "failed"}:
            route = _set_route_status(
                route, "translation_quality", "succeeded",
                f"{write_ready} 条译文通过统一质量门",
            )
        pending_prerequisites = [
            step for step in route
            if step.required and step.step_id not in execution_steps
            and step.status != "succeeded"
        ]
        if pending_prerequisites:
            summary = "；".join(
                f"{step.step_id}: {step.reason}" for step in pending_prerequisites)
            raise RuntimeError(f"必需写回先决步骤尚未完成：{summary}")
        return fingerprint, route, font_capability

    def _verify_copied_il2cpp_inputs(
            self, fingerprint: GameFingerprint, staging: Path) -> None:
        if fingerprint.runtime != "il2cpp":
            return
        if (
            fingerprint.game_assembly is None
            or fingerprint.metadata is None
            or self._last_il2cpp_input_hashes is None
        ):
            raise RuntimeError("IL2CPP 写回缺少可复核的规范输入证据")
        game_root = self.game_dir.resolve()
        relative_inputs = (
            fingerprint.game_assembly.relative_to(game_root),
            fingerprint.metadata.relative_to(game_root),
        )
        source_hashes = tuple(_sha256_file(path) for path in (
            fingerprint.game_assembly, fingerprint.metadata))
        staged_hashes = tuple(
            _sha256_file(resolve_relative_under(staging, relative))
            for relative in relative_inputs
        )
        if (
            source_hashes != self._last_il2cpp_input_hashes
            or staged_hashes != self._last_il2cpp_input_hashes
        ):
            raise RuntimeError(
                "IL2CPP 交叉验证后的规范输入发生变化，已拒绝写回")

    @staticmethod
    def _evaluate_writeback_gates(
            *, text_files: int, v2, text_verified: int,
            font, font_level: str, active_font_config: FontConfig,
            rejected: list, truncated: int, allow_partial: bool,
            ready_text_translations: int,
            written_total: int = 0,
            logic_mismatch_count: int = 0,
            logic_reverted: int = 0,
            font_coverage=None,
            font_candidate_confirm: bool | None = None) -> dict:
        """写回安全闸门 P0-1：把“写回成功”拆成文件/容器/对象/运行时
        四态，禁止单一 succeeded 掩盖后续失败。

        写回 C6b/c：截断与逻辑审计计数闸门联动——truncated 不再是
        无条件的 WARN 照写，批量截断（语义残缺成片）升级为与 rejected
        同级（默认 BLOCKED）；logic_mismatches（重开逻辑验证失败）即使
        异常路径被吞也要兜底拦截；logic_reverted 大面积自动回退（输入
        绑定区域疑似受损、该翻的键翻不了）升级 WARN 提示。"""
        def gate(status: str, detail: str = "") -> dict:
            return {"status": status, "detail": detail}

        # 文件级：文本文件已写入并通过重开核对
        if text_files > 0 and text_verified > 0:
            file_gate = gate(
                "PASS", f"{text_files} 个文本文件写回，{text_verified} 条重开核对通过")
        elif ready_text_translations == 0:
            file_gate = gate("N/A", "无待写文本条目")
        else:
            file_gate = gate("BLOCKED", "存在待写文本条目但未通过重开核对")

        # 容器级：每个补丁过的 Unity 容器均已重开验证（验证失败会抛错）
        if v2 is not None and v2.files > 0:
            container_gate = gate(
                "PASS", f"{v2.files} 个容器写回并重开验证通过")
        else:
            container_gate = gate("N/A", "无二进制容器补丁")

        # 对象级：rejected 阻断默认发布（P0-2）；truncated 是容量内的
        # 部分翻译（译文主体 + 省略号已写入，容量限制下最优解），只进
        # 报告 WARN 不阻断——阻断会让 1 条超长译文拖垮整场写回
        # （taxes 'I did ' 实证：短串中文译文必然超 ASCII 容量）。
        #
        # 写回 C6b：truncated 不无条件 WARN 照写——批量截断（≥5 条且
        # 占待写条目 ≥10%）是「语义残缺成片」信号（如整表容量腰斩），
        # 与 rejected 同级：默认 BLOCKED、allow_partial 确认才放行。
        attempted = written_total + truncated
        bulk_truncated = (truncated >= 5 and attempted > 0
                          and truncated / attempted >= 0.1)
        detail_parts: list[str] = []
        if rejected:
            detail_parts.append(f"拒绝 {len(rejected)}")
        if truncated:
            detail_parts.append(f"截断 {truncated}"
                                + (f"（占待写 {attempted} 条的"
                                   f"{truncated * 100 // attempted}%）"
                                   if attempted else ""))
        # 写回 C6c：重开逻辑验证失败（字符串边界不一致）是写坏信号，
        # 即使异常路径被外层吞掉，闸门也必须兜底拦截，绝不发布
        if logic_mismatch_count:
            detail_parts.append(f"逻辑验证失败 {logic_mismatch_count}")
            object_gate = gate(
                "BLOCKED",
                "存在重开逻辑验证失败（写坏风险），已拒绝发布副本")
        elif rejected or bulk_truncated:
            if allow_partial:
                object_gate = gate(
                    "WARN",
                    f"存在未完全写入条目（{'、'.join(detail_parts)}），"
                    "用户已确认允许部分发布")
            else:
                object_gate = gate(
                    "BLOCKED",
                    f"存在未完全写入条目（{'、'.join(detail_parts)}），"
                    "默认阻断发布，需用户明确确认")
        elif truncated:
            object_gate = gate(
                "WARN",
                f"截断 {truncated} 条（容量内部分翻译已写入，含省略号提示）")
        elif logic_reverted and attempted > 0 \
                and logic_reverted / attempted >= 0.3:
            # 写回 C6c：逻辑审计计数联动——自动回退（译文→原文防断链）
            # 是安全行为，但大面积回退（≥30% 待写条目）说明输入绑定区域
            # 疑似整片受损、该翻的键翻不了，升级 WARN 提示人工关注
            object_gate = gate(
                "WARN",
                f"逻辑审计自动回退 {logic_reverted} 条（占待写 "
                f"{attempted} 条的 {logic_reverted * 100 // attempted}%），"
                "输入绑定区域疑似大范围受损，建议人工检查")
        else:
            object_gate = gate("PASS", "全部条目完整写入")

        # 运行时级：字体/运行时回退层（Phase 4：覆盖终态决策表 §8.2——
        # 静态 coverage 优先，CANDIDATE_ONLY/BLOCKED 阻断正式发布；
        # allow_partial 只把 PENDING/CANDIDATE_ONLY 降级为候选 WARN，
        # 绝不绕过 BLOCKED（§8.3））
        font_gate = evaluate_font_gate(
            coverage=font_coverage,
            runtime_verified=font.runtime_verified,
            payload_deployed=font.payload_deployed,
            provider_supported=font.provider_supported,
            font_enabled=active_font_config.enabled,
            # 与 pipeline.run 同一解析值（None → 跟随 allow_partial），
            # 保证 pipeline 内门与四态闸门终态一致
            allow_unverified_font_candidate=(
                allow_partial if font_candidate_confirm is None
                else font_candidate_confirm))
        runtime_gate = gate(font_gate["status"], font_gate["detail"])

        gates = {
            "file": file_gate,
            "container": container_gate,
            "object": object_gate,
            "runtime": runtime_gate,
        }
        statuses = [item["status"] for item in gates.values()]
        if "BLOCKED" in statuses:
            overall = "BLOCKED"
        elif "WARN" in statuses:
            overall = "WARN"
        else:
            overall = "PASS"
        gates["overall"] = gate(overall, "")
        return gates

    @staticmethod
    def _write_publish_manifest(
            out_dir: Path, source_hashes: dict, output_hashes: dict,
            fingerprint: GameFingerprint, gates: dict,
            allow_partial: bool,
            backup_name: str | None = None) -> str | None:
        """P0-3：发布后生成 source/target manifest，列出全部文件 hash
        （含未修改文件）。返回清单文件名；失败返回 None 由调用方记警告。

        backup_name 非 None 时记录备份目录名与恢复步骤（文档1 §15：
        报告须含备份路径/回滚命令）。"""
        rels = sorted(set(source_hashes) | set(output_hashes))
        files = []
        for rel in rels:
            source_hash = source_hashes.get(rel, "")
            target_hash = output_hashes.get(rel, "")
            files.append({
                "path": rel,
                "source_sha256": source_hash,
                "target_sha256": target_hash,
                "changed": source_hash != target_hash,
            })
        source_manifest_hash = hashlib.sha256(
            json.dumps(source_hashes, sort_keys=True).encode("utf-8")
        ).hexdigest()
        manifest = {
            "schema": 1,
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "game": {
                "unity_version": fingerprint.unity_version,
                "runtime": fingerprint.runtime,
            },
            "source_manifest_hash": source_manifest_hash,
            "allow_partial": allow_partial,
            "gates": gates,
            "file_count": len(files),
            "changed_files": sum(1 for item in files if item["changed"]),
            "files": files,
        }
        if backup_name:
            # 发布前版本备份（回滚凭据）：恢复 = 将备份目录改名为
            # 输出目录名（旧输出已整体换名，无逐文件覆盖风险）
            manifest["backup"] = {
                "path": backup_name,
                "restore": (
                    f"将 {out_dir.parent / backup_name} 改名为 "
                    f"{out_dir} 即恢复发布前版本"),
            }
        path = out_dir / ".hanhua-manifest.json"
        try:
            path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except OSError:
            return None
        return ".hanhua-manifest.json"

    def write_all(
        self,
        progress_cb: Callable | None = None,
        *,
        font_config: FontConfig | None = None,
        stage_cb: Callable[[WritebackStage], None] | None = None,
        allow_partial: bool = False,
        allow_unverified_font_candidate: bool | None = None,
    ) -> dict:
        """复制游戏目录到输出目录，依次写回文本与二进制资源。

        allow_partial：存在 rejected/truncated 条目时是否允许发布
        （默认 False → BLOCKED 阻断；True → WARN 放行并完整记录）。

        allow_unverified_font_candidate：None → 跟随 allow_partial（GUI
        勾选语义）；True → 字体候选确认（PENDING_RUNTIME_ATTESTATION /
        CANDIDATE_ONLY 降级为候选 WARN），对象级闸门（rejected/truncated/
        逻辑验证）仍受 allow_partial 严格约束——批量闭环（免实机 attest，
        2026-08-12 指令）用它确认候选字体发布，不影响条目完整性门。
        """
        _emit_writeback_stage(stage_cb, "preflight", "正在执行写回预检")
        _reject_store_inside_out_dir(self.app_dir, self.out_dir)
        # 字体候选确认与 allow_partial 的合并语义（docstring：None → 跟随
        # allow_partial）——pipeline.run 与 _evaluate_writeback_gates 必须
        # 用同一解析值，否则 runner 传 allow_unverified_font_candidate=True
        # 而 allow_partial 默认 False 时，pipeline 内门 WARN 而四态闸门用
        # allow_partial 重估 → runtime=BLOCKED（hickory 实证 2026-08-13）。
        font_candidate_confirm = (
            allow_partial if allow_unverified_font_candidate is None
            else allow_unverified_font_candidate)
        active_font_config = replace(font_config or self.font_config)
        if active_font_config.enabled:
            _normalize_store_font_punctuation(self.store)
        write_ready = _count_write_ready_translations(self.store)
        runtime_translations = _runtime_exact_translations(self.store)
        for file_record in self.store.get_files():
            resolve_relative_under(self.game_dir, file_record["rel_path"])
            resolve_relative_under(self.out_dir, file_record["rel_path"])
        fingerprint, route, font_capability = self._validate_write_route(
            write_ready, active_font_config)
        if self._last_source_manifest is None:
            raise RuntimeError("缺少成功扫描绑定的完整输入清单，请重新执行统一扫描")
        source_hashes = dict(self._last_source_manifest)
        if _tree_hashes(self.game_dir) != source_hashes:
            raise RuntimeError("成功扫描后的原游戏输入发生变化，已拒绝写回")
        on_copy = None
        if progress_cb:
            def on_copy(done, total):
                progress_cb(done, total + 4)
        parent = self.out_dir.parent
        parent.mkdir(parents=True, exist_ok=True)
        staging: Path | None = Path(tempfile.mkdtemp(
            prefix=f".{self.out_dir.name}.staging-", dir=parent))
        backup: Path | None = None
        published = False
        static = None  # install_static_fonts 结果（未启用字体时为 None）
        try:
            ensure_trusted_root(staging)
            _emit_writeback_stage(stage_cb, "copying", "正在复制原游戏")
            copy_total = copy_game_dir(self.game_dir, staging, on_copy)
            progress_total = copy_total + 4
            ensure_trusted_root(staging)
            for file_record in self.store.get_files():
                resolve_relative_under(staging, file_record["rel_path"])
            if (
                _tree_hashes(self.game_dir) != source_hashes
                or _tree_hashes(staging) != source_hashes
            ):
                raise RuntimeError(
                    "复制期间 IL2CPP/其他原游戏输入或未补丁副本发生变化，"
                    "与扫描清单不一致，已拒绝写回")
            self._verify_copied_il2cpp_inputs(fingerprint, staging)
            _emit_writeback_stage(stage_cb, "patching", "正在写入静态译文")
            n_text = write_back_text(
                self.store, self.game_dir, staging,
                normalize_fallback_punctuation=active_font_config.enabled)
            if progress_cb:
                progress_cb(copy_total + 1, progress_total)
            # 扫描/写回同源生成器（Mono typeless bundle 写回必须能
            # 再生脚本 typetree；hickory 实证：写回侧无生成器 → 261 条
            # typetree_unavailable 全拒）。字体管线复用同一实例
            # （load_dll 122 个程序集代价高，构建两次翻倍启动耗时）。
            tt_generator = self._build_typetree_generator(fingerprint)
            v2 = write_back_v2(self.store, self.game_dir, staging,
                               typetree_generator=tt_generator)
            writer_outcome = v2.outcome
            # W3 运行时排除表：静态写回被回退（保留原文防断链）的逻辑键
            # 原文——插件翻译表必须剔除，否则游戏运行时被插件换成中文 →
            # 按名比较断链（按键失灵反复出现的机制之一）。
            # C4：写回侧拒绝的条目静态层同样保留原文（写不进的对象里往往
            # 是键清单/类型描述等结构串），原文同样并入排除表——拒绝本身
            # 仍走 rejected 闸门阻断默认发布，这里只补插件侧防断链。
            reverted_sources = (
                set(v2.logic_reverted_sources)
                | set(getattr(v2, "rejected_sources", ()) or ()))
            if progress_cb:
                progress_cb(copy_total + 2, progress_total)
            _emit_writeback_stage(stage_cb, "runtime_payload", "正在部署中文字体")
            # Phase 4：GUI/headless/批量共用同一字体闭环——project 只编排
            # FontCompatibilityPipeline（plan → apply_static → verify_static
            # → deploy_runtime → evaluate_publish），不再手工推导字体终态。
            required_set = _font_required_glyph_set(self.store)
            # Phase 5：位图字体 provider（NGUI/BMFont 栈）——发现原游戏
            # .fnt + 装配 BMFont 工具链 executor。排除历史汉化输出（防止
            # 把上次注入的 .fnt 当作原游戏资产反复注入）；工具链缺失 →
            # pipeline 记 pending warning，消费者保持未覆盖（发布门阻断）。
            bitmap_providers = resolve_bitmap_providers(
                self.game_dir, fingerprint,
                exclude_roots=(self.out_dir,))
            bmfont_executor = None
            if bitmap_providers:
                try:
                    app_root = Path(__file__).resolve().parents[2]
                    registry = ToolRegistry.load(app_root)
                    spec = registry.specs["bmfont"]
                    runner = IsolatedToolRunner(self.app_dir / "tooling")
                    font_file = (
                        app_root / "fonts" / "SimplifiedChinese"
                        / "NotoSerifCJKsc-Medium.ttf")
                    if font_file.is_file():
                        def _bmfont_executor(provider, staging_fnt, plan):
                            return inject_bitmap_font(
                                provider, staging_fnt, plan,
                                runner=runner, spec=spec, font_file=font_file)
                        bmfont_executor = _bmfont_executor
                except KeyError:
                    bmfont_executor = None  # 清单无 bmfont → pending 警告
            pipeline = FontCompatibilityPipeline(FontPipelineInput(
                game_dir=self.game_dir, staging=staging,
                font_config=active_font_config,
                unity_version=fingerprint.unity_version,
                runtime=fingerprint.runtime,
                player_root=fingerprint.player_root,
                capability=font_capability,
                translations=runtime_translations,
                exclude=frozenset(reverted_sources),
                required_set=required_set,
                bitmap_providers=bitmap_providers,
                bmfont_executor=bmfont_executor,
                typetree_generator=tt_generator,
            ))
            outcome = pipeline.run(
                reverted_sources=frozenset(reverted_sources),
                allow_unverified_font_candidate=font_candidate_confirm)
            static = outcome.static
            font = outcome.font
            static_warnings = (
                [f"字体替换跳过: {skip}" for skip in (
                    static.skipped if static is not None else ())]
                + list(static.warnings if static is not None else ())
                + list(outcome.warnings))
            if static is not None and static.replaced:
                # C5：静态字体替换整容器重建了 bundle（os.replace），
                # 其 content CRC 与 write_back_v2 更新 catalog.bin 时
                # 不一致 → 二次同步，否则 Addressables 运行时 CRC
                # Mismatch 拒载。失败语义与 write_back_v2 末尾一致：
                # catalog 是运行时必需，更新失败必须阻断发布。
                _update_addressables_catalogs(
                    self.game_dir, staging,
                    [{"rel_path": path}
                     for path in static.replaced_paths])
            if active_font_config.enabled:
                if font.installed:
                    font_reason = (
                        "静态字体替换完成（Font/TMP_FontAsset 换入中文字库）"
                        if font.provider_id == "static_font_replace"
                        else "运行时中文字体回退安装完成")
                    route = _set_route_status(
                        route, "font", "succeeded", font_reason)
                elif not font.provider_supported:
                    route = tuple(
                        replace(
                            step, status="skipped", required=False,
                            reason=font.unsupported_reason)
                        if step.step_id == "font" else step
                        for step in route)
                else:
                    route = _set_route_status(
                        route, "font", "failed",
                        "已启用中文字体，但运行时回退安装未完成")
                    raise RuntimeError("中文字体运行时回退安装失败，已拒绝发布副本")
            if progress_cb:
                progress_cb(copy_total + 3, progress_total)

            _emit_writeback_stage(stage_cb, "verifying", "正在重开并验证汉化输出")
            input_protected = source_hashes == _tree_hashes(self.game_dir)
            if not input_protected:
                raise RuntimeError("写回期间检测到原游戏输入哈希变化，已拒绝发布副本")
            text_verified = _reopen_written_outputs(self.store, staging)
            reopen_verified = True
            output_hashes = _tree_hashes(staging)
            changed_files = sum(
                source_hashes.get(relative) != output_hashes.get(relative)
                for relative in source_hashes.keys() | output_hashes.keys()
            )
            written_translations = text_verified + v2.entries
            if written_translations <= 0:
                raise RuntimeError("没有通过重开验证的实际译文补丁，已拒绝发布副本")
            warnings = list(getattr(v2, "warnings", ()) or ())
            warnings.extend(static_warnings)
            if (active_font_config.enabled and not font.installed
                    and not font.provider_supported):
                warnings.append(font.unsupported_reason)
            elif active_font_config.enabled and not font.installed:
                warnings.append("已启用中文字体，但没有生成可验证的运行时回退层")
            font_level = (
                "runtime_verified" if font.runtime_verified
                else "payload_deployed" if font.payload_deployed
                else "unsupported" if (active_font_config.enabled
                                        and not font.provider_supported)
                else "unavailable" if active_font_config.enabled
                else "disabled"
            )
            # P0-2：rejected 阻断默认发布（用户明确确认才放行）；
            # truncated 是容量内部分翻译（主体+省略号已写入），进报告
            # WARN 不阻断——1 条超长译文不应拖垮整场写回（taxes 实证）
            rejected_entries = [
                {"locator": item.locator, "reason": item.reason}
                for item in writer_outcome.rejected
            ]
            truncated_items = list(getattr(v2, "truncated_items", ()) or ())
            # 写回逻辑层审计数据（logic_audit：写回前敏感形态 / raw_expansions：
            # rawstr 扩容 / logic_mismatches：重开逻辑验证失败）
            logic_audit = list(getattr(v2, "logic_audit", ()) or ())
            raw_expansions = list(getattr(v2, "raw_expansions", ()) or ())
            logic_mismatches = list(getattr(v2, "logic_mismatches", ()) or ())
            logic_reverted = int(getattr(v2, "logic_reverted", 0) or 0)
            logic_reverted_items = list(
                getattr(v2, "logic_reverted_items", ()) or ())
            if logic_mismatches:
                warnings.append(
                    f"重开逻辑验证失败 {len(logic_mismatches)} 项："
                    + "；".join(str(item)[:80] for item in logic_mismatches[:3]))
            if logic_reverted:
                warnings.append(
                    f"逻辑键自动回退 {logic_reverted} 条（译文保留原文，防"
                    f"断链）：" + "；".join(
                        str(item) for item in logic_reverted_items[:5]))
            if truncated_items:
                warnings.append(
                    f"截断 {len(truncated_items)} 条（容量内部分翻译已写入）："
                    + "；".join(str(item) for item in truncated_items[:5]))
            if rejected_entries and not allow_partial:
                examples = "；".join(
                    f"{item['locator']}: {item['reason']}"
                    for item in rejected_entries[:5])
                raise RuntimeError(
                    f"写回存在被拒绝条目（拒绝 {len(rejected_entries)} 条），"
                    f"已阻断默认发布。{examples}"
                    "如需强制发布请在界面勾选“允许部分写入并发布”")
            verification = {
                "input_protected": input_protected,
                "reopen_verified": reopen_verified,
                "changed_files": changed_files,
                "written_translations": written_translations,
                "writer_outcome": {
                    "attempted": writer_outcome.attempted,
                    "written": writer_outcome.written,
                    "rejected": rejected_entries,
                    "truncated": writer_outcome.truncated,
                },
                "rejected_entries": rejected_entries,
                "truncated_entries": truncated_items,
                "blocked_entries": (
                    rejected_entries
                    + [{"locator": f"truncated#{index + 1}",
                        "reason": item}
                       for index, item in enumerate(truncated_items)]),
                "font_level": font_level,
                "font_provider_id": font.provider_id,
                "font_payload_deployed": bool(font.payload_deployed),
                "font_runtime_verified": font.runtime_verified,
                # Phase 4：发布门 + 逐栈/逐码点覆盖摘要（record_writer 与
                # runner 输出统一口径，计划 §11）
                "font_gate": outcome.gate,
                "font_coverage": _font_coverage_summary(
                    outcome.coverage, required_set),
                # Phase 5：位图字体注入摘要（providers/injected/pending）
                "font_bitmap": (
                    {
                        "providers": [p.provider_id
                                      for p in outcome.bitmap.providers],
                        "injected": outcome.bitmap.injected,
                        "audited": outcome.bitmap.audited,
                        "pending": outcome.bitmap.pending,
                    }
                    if outcome.bitmap is not None else None),
                "allow_partial": allow_partial,
                "logic_audit": logic_audit,
                "raw_expansions": raw_expansions,
                "logic_mismatches": logic_mismatches,
                "logic_reverted": logic_reverted,
                "logic_reverted_items": logic_reverted_items,
                "warnings": warnings,
            }
            # P0-1：四态闸门（文件/容器/对象/运行时），
            # 任一 BLOCKED 都不得发布副本
            gates = self._evaluate_writeback_gates(
                text_files=n_text, v2=v2, text_verified=text_verified,
                font=font, font_level=font_level,
                active_font_config=active_font_config,
                rejected=rejected_entries, truncated=len(truncated_items),
                allow_partial=allow_partial,
                # 字体候选确认必须与 pipeline.run 同值（合并后），否则
                # runner 场景（候选确认 True + allow_partial False）四态
                # 闸门用 allow_partial 重估 → runtime=BLOCKED（hickory
                # 实证 2026-08-13）。_evaluate_writeback_gates 内部对
                # 字体门使用该值做 allow_unverified_font_candidate。
                font_candidate_confirm=font_candidate_confirm,
                ready_text_translations=_count_write_ready_translations(
                    self.store, text_only=True),
                # 写回 C6b/c：截断/逻辑审计计数闸门联动（见闸门内注释）
                written_total=text_verified + int(getattr(v2, "entries", 0)
                                                  or 0),
                logic_mismatch_count=len(logic_mismatches),
                logic_reverted=logic_reverted,
                font_coverage=(
                    static.coverage if static is not None else None))
            overall = gates["overall"]["status"]
            verification["gates"] = gates
            verification["overall"] = overall
            if overall == "BLOCKED":
                blocked_parts = [
                    f"{name}={item['status']}"
                    for name, item in gates.items()
                    if item["status"] == "BLOCKED"]
                # 2026-08-14 用户实证「写回还是出错」：runtime=BLOCKED
                # （字体候选未实机验证）的错误只有状态没有原因与指引。
                # 免实机闭环下 PENDING_RUNTIME_ATTESTATION/CANDIDATE_ONLY
                # 是常态——勾选「允许部分写入」确认候选即放行（WARN）；
                # IL2CPP 无 provider/未知渲染栈（detail 带「不可绕过」）
                # 是硬阻断，候选确认不可放行，需修复后重试。
                hint = ""
                runtime_gate = gates.get("runtime") or {}
                if runtime_gate.get("status") == "BLOCKED":
                    detail = runtime_gate.get("detail") or ""
                    if "不可绕过" in detail:
                        hint = ("。字体覆盖当前无法自动保证"
                                "（候选确认不可绕过），详见发布报告")
                    else:
                        hint = ("。字体候选未实机验证：可勾选翻译页"
                                "「允许部分写入」确认候选后重试")
                raise RuntimeError(
                    f"写回闸门 BLOCKED（{'、'.join(blocked_parts)}），"
                    f"已拒绝发布副本。详见发布报告{hint}")
            route = _set_route_status(
                route, "writeback", "succeeded",
                "写回、输入保护、重开验证与四态闸门通过"
                if overall == "PASS"
                else f"写回完成（overall={overall}），详见发布报告")
            base_report = (
                self._last_analysis_report
                if self._last_analysis_report is not None
                and self._last_analysis_report.fingerprint == fingerprint
                else self.analyze()
            )
            final_report = replace(
                base_report,
                route=route,
                font_capability=replace(
                    font_capability,
                    payload_deployed=bool(font.payload_deployed),
                    runtime_verified=font.runtime_verified,
                ),
                unblocked=plan_is_unblocked(route),
                completable=plan_is_completable(route),
            )
            if not final_report.completable:
                pending = [
                    step.step_id for step in route
                    if step.required and step.status != "succeeded"
                ]
                raise RuntimeError(
                    f"写回完成状态不完整，已拒绝发布副本：{', '.join(pending)}")
            if _tree_hashes(self.game_dir) != source_hashes:
                raise RuntimeError("发布前检测到原游戏输入发生变化，已拒绝替换旧输出")
            _emit_writeback_stage(stage_cb, "publishing", "正在原子发布汉化游戏")
            backup_name = None
            if self.out_dir.exists():
                backup = parent / f".{self.out_dir.name}.backup-{uuid.uuid4().hex}"
                _replace_directory(self.out_dir, backup)
                backup_name = backup.name
            # P0-3（写回 C7 修复）：source/target manifest（回滚凭据）
            # 先写入 staging，随 rename 原子落位——原实现 rename 后才写
            # manifest，rename 与写清单之间存在崩溃窗口：新版本已发布、
            # 旧备份凭据却未落盘，一旦异常 finally 还会删掉备份，回滚
            # 彻底不可达（指南 §3.2「删除或保留供诊断」只实现删除半）。
            manifest_name = self._write_publish_manifest(
                staging, source_hashes, output_hashes, fingerprint,
                gates, allow_partial, backup_name)
            if manifest_name is None:
                warnings.append("发布清单写入失败（.hanhua-manifest.json）")
            # rename 是发布流程的最后一步写操作：此后不再有磁盘变更，
            # 崩溃窗口归零
            _replace_directory(staging, self.out_dir)
            staging = None
            published = True
            verification["manifest"] = manifest_name
            verification["backup"] = backup_name
            verification["warnings"] = warnings
            # 写回 C10：回退决策持久化——逻辑审计自动回退（保留原文防
            # 断链）的条目，store 状态同步 skipped。发布成功后才持久化
            # （失败时游戏实际状态未变）；GUI 不再显示 translated 与
            # 游戏实际状态脱节（评估 C10 P2）。
            reverted_locators = getattr(
                v2, "reverted_locators", set()) or set()
            persisted = 0
            for locator in reverted_locators:
                file_id, sep, key_path = str(locator).partition(":")
                if not sep:
                    continue
                self.store.set_status(file_id, key_path, "skipped")
                persisted += 1
            if persisted:
                verification["reverted_persisted"] = persisted
            # 写回 C10 补漏：语义回退（按钮名/对象名/逻辑键等宁漏勿坏）的
            # 原文必须在翻译记忆中同步撤销——审后 settle_translation_memory
            # 已把此类回退原文 promote 进 memory（pending=0），若不撤销，
            # get_memory_hits 会在后续游戏翻译时把同一坏译文直接命中应用，
            # 跨游戏重复引入按键失灵/断链（用户「知识库是否会导致后续失败」）。
            # remove_memory_all 按原文跨 model/lang 全撤：此类原文无论哪个
            # 模型翻译都危险，不得再自动命中。rejected_sources 不撤（纯写
            # 失败，译文未必坏，且 reject 已阻断默认发布）。
            purged = 0
            for original in (set(v2.logic_reverted_sources) or set()):
                purged += self.store.remove_memory_all(original)
            if purged:
                verification["reverted_memory_purged"] = purged
            self._last_analysis_report = final_report
            if progress_cb:
                progress_cb(progress_total, progress_total)
            result = {
                "text_files": n_text,
                "v2": v2,
                "font": font,
                "verification": verification,
                "analysis_report": final_report,
            }
            _emit_writeback_stage(stage_cb, "published", "汉化游戏已发布")
            if backup is not None:
                cleanup_target = backup
                backup = None
                cleanup_thread = _schedule_backup_cleanup(
                    cleanup_target, self.out_dir, stage_cb)
                # 等待清理完成再返回：CLI 写回后立即退出，不等会成片残留
                if cleanup_thread is not None:
                    cleanup_thread.join(timeout=60)
            return result
        except Exception:
            if backup is not None and not self.out_dir.exists() and backup.exists():
                try:
                    _replace_directory(backup, self.out_dir)
                except PermissionError as restore_error:
                    raise RuntimeError(
                        f"输出提交失败，旧版本恢复也被文件锁阻止；备份已保留在：{backup}"
                    ) from restore_error
            raise
        finally:
            if staging is not None and staging.exists():
                # 写回 C7：staging 失败时保留供诊断（改名 .diagnostic-*），
                # 不再直接删除——修复失败的根因排查需要现场（指南 §3.2
                # 「删除或保留供诊断」只实现删除半）
                try:
                    staging.replace(
                        parent / f".{self.out_dir.name}.diagnostic-"
                        f"{uuid.uuid4().hex}")
                except OSError:
                    shutil.rmtree(staging, ignore_errors=True)
            if (backup is not None and backup.exists()
                    and not published and not self.out_dir.exists()):
                # 写回 C7：发布未成功时备份是唯一回滚凭据，保留不删；
                # 发布成功后备份已交给 _schedule_backup_cleanup（保留本次
                # 备份、只清理更早遗留）——不再有 finally 无脑删除路径
                _emit_writeback_stage(
                    stage_cb, "cleanup_warning",
                    f"发布失败，旧版本备份已保留供回滚/诊断：{backup}")

    # ── 项目级游戏档案（每个游戏独立） ──
    @property
    def profile(self) -> GameProfile:
        return self.store.get_profile()

    def save_profile(self, profile: GameProfile):
        self.store.set_profile(profile)

    @staticmethod
    def open_game_dir(
        game_dir: str | Path,
        app_dir: str | Path,
        font_config: FontConfig | None = None,
        *,
        player_root: str | Path | None = None,
        player_executable: str | Path | None = None,
    ) -> "Project":
        return Project(
            Path(game_dir), Path(app_dir), font_config=font_config,
            player_root=Path(player_root) if player_root is not None else None,
            player_executable=(
                Path(player_executable)
                if player_executable is not None else None),
        )
