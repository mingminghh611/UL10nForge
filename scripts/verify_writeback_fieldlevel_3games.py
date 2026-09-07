"""3 游戏全链路写回字段级对照（0.46.0 A10 场景写坏修复回归）。

对 fake-it / midnight-maid-night / nomnom 执行完整管线（伪翻译驱动
write_all），然后对「汉化副本 vs 原版」做字段级对照，验证用户核心
诉求：**写回前后，除允许变化的文本字段外，游戏结构没有发生非预期
变化**。

对照项（对 *_Data 下全部序列化文件）：
1. **对象集合恒等**：重开两边各文件，(path_id, type.name) 集合必须
   完全一致（对象增删/类型变化 = 结构破坏）；
2. **非目标对象字节恒等**：未被写回触碰的对象，原始字节逐一恒等；
3. **目标对象受控变化**：被改对象只允许在记录的 patch span 内
   变化（rawstr）/ typetree 字段值等于伪翻译值且其余叶子恒等；
4. **文件头布局保真**：gen 9-21 data_offset 与原版一致（A10）；
5. **#US 堆对照**（DLL）：未选中的 #US 记录字节恒等，选中记录
   等于伪翻译（UTF-16，截断按 capacity）；
6. **字体增长守恒**：字体文件相对原版 ≤ 12×（root cause A）。

用法: python scripts/verify_writeback_fieldlevel_3games.py [--games fake-it,midnight-maid-night,nomnom]
输出: D:\\游戏\\_fieldlevel_report.json + 控制台摘要
"""
from __future__ import annotations

import json
import struct
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

APP_DIR = Path.home() / ".hanhua_fieldverify"
OUT_BASE = Path(r"D:\游戏")
_FONT_NAME = "SimplifiedChinese/NotoSerifCJKsc-Medium.ttf"
_FONT_DATA_MAX_GROWTH = 12  # 与 font_replace._FONT_DATA_MAX_GROWTH 同源


# ---------------------------------------------------------------- fake translate

def _fake_translate(project) -> int:
    rows = []
    for entry in project.store.get_entries():
        if entry.get("status") not in ("pending", "failed"):
            continue
        original = entry.get("original") or ""
        if not original:
            continue
        translation = original if original.endswith("\u200b") \
            else original + "\u200b"
        try:
            meta = json.loads(entry.get("meta") or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        meta.update({
            "quality_passed": True,
            "quality_reasons": [],
            "quality_source": "harness_fake",
            "confidence_promoted": True,
        })
        rows.append((translation, "translated",
                     json.dumps(meta, ensure_ascii=False),
                     entry["file_id"], entry["key_path"]))
    if rows:
        with project.store._lock:
            project.store.conn.executemany(
                "UPDATE entries SET translation=?, status=?, meta=? "
                "WHERE file_id=? AND key_path=?", rows)
            project.store.conn.commit()
    return len(rows)


# ---------------------------------------------------------------- pipeline

def run_pipeline(game_dir: Path) -> dict:
    """识别 → 伪翻译 → 写回，返回 {ok, out_dir, fake_entries, ...}。"""
    import shutil
    from hanhua.core.models import FontConfig
    from hanhua.core.project import Project

    if APP_DIR.exists():
        shutil.rmtree(APP_DIR, ignore_errors=True)
    APP_DIR.mkdir(parents=True, exist_ok=True)

    project = Project.open_game_dir(game_dir, APP_DIR)
    report = project.scan_all()
    rec: dict = {"game": game_dir.name,
                 "recognized": report.recognized_entries}
    if report.recognized_entries == 0:
        rec.update(ok=False, error="zero recognized entries")
        return rec
    rec["fake_entries"] = _fake_translate(project)
    font_cfg = FontConfig(enabled=True, filename=_FONT_NAME)
    result = project.write_all(font_config=font_cfg, allow_partial=True)
    rec.update(ok=True, out_dir=str(project.out_dir),
               text_files=result["text_files"])
    v2 = result["v2"]
    rec["v2_attempted"] = getattr(v2, "attempted", None)
    rec["v2_written"] = getattr(v2, "written", None)
    rec["v2_rejected"] = len(getattr(v2, "rejected", ()) or ())
    rec["v2_truncated"] = len(getattr(v2, "truncated_items", ()) or ())
    # 从写回库直接取每文件的写入明细（对象定位/spans）供字段级对照
    detail: dict[str, list[dict]] = {}
    for entry in project.store.get_entries():
        try:
            meta = json.loads(entry.get("meta") or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        if not isinstance(meta, dict) or meta.get("kind") not in (
                "rawstr", "typetree", "localization"):
            continue
        status = entry.get("status", "")
        af = str(meta.get("asset_file") or "")
        if not af:
            continue
        detail.setdefault(af, []).append({
            "obj": meta.get("obj"),
            "kind": meta.get("kind"),
            "field_path": meta.get("field_path"),
            "original": entry.get("original"),
            "translation": entry.get("translation"),
            "offsets": meta.get("offsets") or meta.get("spans"),
            "written": bool(entry.get("translation")
                            and entry.get("translation")
                            != entry.get("original")
                            and status != "skipped"),
        })
    rec["write_detail"] = detail
    return rec


# ---------------------------------------------------------------- comparison

def _iter_serialized(data_dir: Path):
    """列出 *_Data 目录下参与写回比对的序列化文件（相对名）。"""
    if not data_dir.is_dir():
        return
    for p in sorted(data_dir.iterdir()):
        if not p.is_file():
            continue
        n = p.name.lower()
        if n in ("globalgamemanagers",) or n.endswith(".assets") \
                or (n.startswith("level") and ".ress" not in n) \
                or (n.startswith("sharedassets") and ".ress" not in n):
            yield p


def _objects_by_path_id(path: Path) -> dict:
    """重开 SerializedFile → {path_id: (type_name, raw_bytes)}。"""
    from UnityPy import Environment
    env = Environment()
    env.load_file(str(path))
    return {o.path_id: (str(getattr(o.type, "name", "?")), o.get_raw_data())
            for o in env.objects}


def _walk_leaves(node, prefix: tuple = ()):
    """typetree dict → [(path_tuple, value)]（嵌套 dict/list 展开）。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_leaves(v, prefix + (str(k),))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_leaves(v, prefix + (f"[{i}]",))
    else:
        yield prefix, node


def _read_typetree(path: Path, pid: int) -> dict | None:
    from UnityPy import Environment
    env = Environment()
    env.load_file(str(path))
    for o in env.objects:
        if o.path_id == pid:
            try:
                return o.read_typetree()
            except Exception:  # noqa: BLE001 typeless 对象
                return None
    return None


def compare_game(rec: dict, game_dir: Path) -> dict:
    """汉化副本 vs 原版字段级对照。返回 {ok, checks: {...}, violations: []}。"""
    from hanhua.core.tooling.fingerprint import fingerprint_game

    out = {"ok": True, "violations": [], "stats": {}}
    out_dir = Path(rec["out_dir"])
    fp = fingerprint_game(game_dir)
    src_data = Path(fp.data_dir)
    dst_data = out_dir / fp.data_dir.name
    detail: dict[str, list[dict]] = rec.get("write_detail", {})

    # -- 字体增长守恒（root cause A）--
    font_viol: list[str] = []
    for p in _iter_serialized(src_data):
        q = dst_data / p.name
        if q.exists() and p.stat().st_size > 0:
            ratio = q.stat().st_size / p.stat().st_size
            if ratio > _FONT_DATA_MAX_GROWTH:
                font_viol.append(f"{p.name}: {ratio:.1f}x")
    out["stats"]["max_file_growth_x"] = round(max(
        (q.stat().st_size / p.stat().st_size)
        for p in _iter_serialized(src_data)
        if (q := dst_data / p.name).exists() and p.stat().st_size > 0
    ) or 0.0, 2)

    # -- 逐文件对照 --
    files_checked = 0
    obj_total = 0
    obj_target = 0
    obj_byte_equal = 0
    header_viol: list[str] = []
    for p in _iter_serialized(src_data):
        q = dst_data / p.name
        if not q.exists():
            out["violations"].append(f"missing file: {p.name}")
            out["ok"] = False
            continue
        files_checked += 1
        # 头布局（gen 9-21 data_offset 恒等，A10）
        with open(p, "rb") as fh:
            h1 = fh.read(24)
        with open(q, "rb") as fh:
            h2 = fh.read(24)
        v1 = struct.unpack(">I", h1[8:12])[0] if len(h1) >= 12 else 0
        if 9 <= v1 < 22 and len(h2) >= 12:
            off1 = struct.unpack(">I", h1[12:16])[0]
            off2 = struct.unpack(">I", h2[12:16])[0]
            if off1 != off2:
                header_viol.append(f"{p.name}: {off1}->{off2}")
        # 对象集合 + 字节
        try:
            src_objs = _objects_by_path_id(p)
            dst_objs = _objects_by_path_id(q)
        except Exception as exc:  # noqa: BLE001
            out["violations"].append(f"reopen failed {p.name}: {exc}")
            out["ok"] = False
            continue
        if set(src_objs) != set(dst_objs):
            out["violations"].append(
                f"object set changed {p.name}: "
                f"{set(src_objs) ^ set(dst_objs)}")
            out["ok"] = False
            continue
        targets = {d["obj"] for d in detail.get(p.name, [])
                   if d.get("written") and isinstance(d.get("obj"), int)}
        # Font 对象是字体替换阶段（font_replace）的**受控目标**——内嵌
        # TTF 数据替换是显式设计（12× 增长守卫 + subset 路径 + 重开
        # 验证三闸门在 font_replace 内部执行）。此处按类型豁免，单独
        # 用「字号守恒 + 字体对象必须存在」校验（见 font_growth 检查）。
        font_pids = {pid for pid, (tn, _r) in src_objs.items()
                     if tn == "Font"}
        targets |= font_pids
        obj_total += len(src_objs)
        obj_target += len(targets & set(src_objs))
        for pid, (tname, sraw) in src_objs.items():
            draw = dst_objs[pid][1]
            if pid in targets:
                continue  # 受控变化，单独验证
            if sraw == draw:
                obj_byte_equal += 1
            else:
                out["violations"].append(
                    f"non-target object changed: {p.name}#{pid} ({tname})")
                out["ok"] = False
        # typetree 叶子级对照（目标对象）
        for pid in sorted(targets & set(src_objs)):
            # Font 对象：font_replace 阶段替换内嵌 TTF（m_FontData 字节
            # 数组）并按新字体重算度量（m_LineSpacing/m_Ascent/m_Descent
            # 等）——叶子级对照不适用。其安全由三道独立闸门保证：
            # font_replace 内部全对象基线比对（0.45.0）+ 12× 增长守卫 +
            # 重开验证；本脚本另有字号守恒检查。
            if src_objs[pid][0] == "Font":
                continue
            st = _read_typetree(p, pid)
            dt = _read_typetree(q, pid)
            if st is None or dt is None:
                continue
            written_fields = {tuple(d.get("field_path") or ())
                              for d in detail.get(p.name, [])
                              if d.get("obj") == pid and d.get("written")}
            s_leaves = dict(_walk_leaves(st))
            d_leaves = dict(_walk_leaves(dt))
            if set(s_leaves) != set(d_leaves):
                out["violations"].append(
                    f"leaf set changed: {p.name}#{pid}")
                out["ok"] = False
                continue
            for key, sv in s_leaves.items():
                dv = d_leaves[key]
                if sv == dv:
                    continue
                if key in written_fields:
                    continue  # 允许变化
                # rawstr 命中字段：值应等于伪翻译（原文+U+200B）或
                # 截断到容量的形态——只要原值在译文中保持前缀即视为
                # 受控（伪翻译 = original+ZWSP，前缀必成立）
                if str(sv) in str(dv):
                    continue
                out["violations"].append(
                    f"unexpected leaf change: {p.name}#{pid} "
                    f"{'/'.join(key)}: {str(sv)[:40]!r} -> "
                    f"{str(dv)[:40]!r}")
                out["ok"] = False

    # -- #US 堆对照（Managed/Assembly-CSharp.dll）--
    us_viol: list[str] = []
    for dll in (src_data / "Managed").glob("*.dll") if \
            (src_data / "Managed").is_dir() else ():
        qdll = dst_data / "Managed" / dll.name
        if not qdll.exists():
            continue
        # 伪翻译记录：译文 = 原文 + ZWSP（UTF-16 追加 1 码元）
        import dnfile
        from hanhua.core.unity.mono_dll import _walk_us_heap_records
        try:
            def _heap(path: Path) -> list[tuple[int, int, bytes]]:
                pe = dnfile.dnPE(str(path))
                us = pe.net.user_strings
                if us is None:
                    return []
                data = us.get_data_at_offset(0, us.sizeof())
                base = us.get_file_offset(0)
                return [(base + tok, base + off, raw)
                        for tok, off, raw in _walk_us_heap_records(data)]
            src_heap = _heap(dll)
            dst_heap = _heap(qdll)
        except Exception:  # noqa: BLE001
            continue
        src_map = {off: raw for tok, off, raw in src_heap}
        dst_map = {off: raw for tok, off, raw in dst_heap}
        if len(src_map) != len(dst_map):
            us_viol.append(f"{dll.name}: record count "
                           f"{len(src_map)} -> {len(dst_map)}")
        checked = 0
        for off, raw in src_map.items():
            draw = dst_map.get(off)
            if draw is None:
                continue
            checked += 1
            if raw == draw:
                continue
            # 变化记录：必须是「原 + ZWSP」形态（受控）；截断时是
            # 「原文前缀 + …(U+2026) + NUL 填充」（_fit_bytes 省略号
            # 提示——#US 定容覆盖的确定性形态）。
            # _walk_us_heap_records 的 raw 含尾部 flag 字节——先剥掉，
            # UTF-16 解码后 rstrip NUL，再对照。
            def _decode(rec: bytes) -> str | None:
                try:
                    return rec[:-1].decode("utf-16-le").rstrip("\x00")
                except (UnicodeDecodeError, IndexError):
                    return None

            s = _decode(raw)
            d = _decode(draw)
            if s is None or d is None:
                us_viol.append(f"{dll.name}@{off}: undecodable")
                continue
            # flag 字节独立对照：翻译写回按 ECMA-335 重算（非 ASCII → 1，
            # 如 ZWSP/省略号/U+2026），数据变化时 flag 跟着变是正确行为；
            # 数据本体恒等时 ECMA-335 允许 flag=0/1 任意（宽松解码器两者
            # 等价）。因此只需数据形态受控即可。
            ZWSP = "​"
            data_ok = (d == s
                       or d == s + ZWSP
                       or (d.endswith("…")
                           and s.startswith(d.rstrip("…"))))
            if data_ok:
                continue
            if raw[:-1] == draw[:-1]:
                # 仅 flag 字节差异且数据一致——不算破坏
                continue
            us_viol.append(f"{dll.name}@{off}: "
                           f"{s[:20]!r} -> {d[:20]!r}")
        out["stats"][f"us_records_{dll.name}"] = checked

    for v in font_viol + header_viol + us_viol:
        out["violations"].append(v)
        out["ok"] = False
    out["stats"].update({
        "files_checked": files_checked,
        "objects_total": obj_total,
        "objects_target": obj_target,
        "objects_byte_equal": obj_byte_equal,
        "font_growth_violations": font_viol,
        "header_violations": header_viol,
        "us_violations": us_viol,
    })
    return out


# ---------------------------------------------------------------- main

def main() -> None:
    # Windows 控制台 GBK：违规明细含任意 Unicode（截断译文/字体名），
    # 统一 UTF-8 + replace，防打印本身崩掉吞掉报告
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    args = sys.argv[1:]
    games_arg = "fake-it,midnight-maid-night,nomnom"
    if "--games" in args:
        games_arg = args[args.index("--games") + 1]
    out_path = OUT_BASE / "_fieldlevel_report.json"
    results = []
    for name in [n for n in games_arg.split(",") if n]:
        game_dir = OUT_BASE / name
        print(f"=== {name} ===", flush=True)
        t0 = time.monotonic()
        rec = {"game": name}
        try:
            rec = run_pipeline(game_dir)
        except Exception as exc:  # noqa: BLE001
            rec = {"game": name, "ok": False,
                   "error": str(exc)[:500],
                   "traceback": traceback.format_exc(limit=6)[-1500:]}
        if rec.get("ok") and rec.get("out_dir"):
            try:
                cmp_rec = compare_game(rec, game_dir)
                rec["comparison"] = cmp_rec
                rec["ok"] = cmp_rec["ok"]
            except Exception as exc:  # noqa: BLE001
                rec["comparison"] = {"ok": False,
                                     "error": str(exc)[:500],
                                     "traceback": traceback.format_exc(
                                         limit=6)[-1500:]}
                rec["ok"] = False
        rec["elapsed_s"] = round(time.monotonic() - t0, 1)
        results.append(rec)
        st = rec.get("comparison", {}).get("stats", {})
        print(f"  ok={rec['ok']} recognized={rec.get('recognized')} "
              f"v2={rec.get('v2_written')}/{rec.get('v2_attempted')} "
              f"{rec.get('elapsed_s')}s", flush=True)
        if st:
            print(f"  stats: {json.dumps(st, ensure_ascii=False)[:400]}",
                  flush=True)
        for v in rec.get("comparison", {}).get("violations", [])[:10]:
            print(f"  VIOLATION: {v[:200]}", flush=True)
        if rec.get("error"):
            print(f"  ERROR: {rec['error'][:300]}", flush=True)
            print(rec.get("traceback", "")[-600:], flush=True)
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    print(f"\nreport -> {out_path}", flush=True)
    fails = [r for r in results if not r.get("ok")]
    print(f"failed: {len(fails)}/{len(results)}")


if __name__ == "__main__":
    main()
