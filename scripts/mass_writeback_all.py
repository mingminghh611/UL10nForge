"""全量写回回归：对 D:\\游戏 每个 Unity 游戏执行完整写回管线。

用「伪翻译」（原文 + U+200B 零宽空格）驱动 write_all —— 目标是验证写回
机制本身（bundle 重写、字体替换、重开验证、发布），不消耗翻译模型 token。
伪翻译只写入 out_dir 副本，原游戏不受影响。

输出: D:\\游戏\\_writeback_report.json（每个游戏：写回状态/验证结果/字体级别/异常）
用法: python scripts/mass_writeback_all.py [--limit N] [--games a,b,c] [--rescan]
"""
from __future__ import annotations
import json
import sys
import time
import traceback
from pathlib import Path

APP_DIR = Path.home() / ".hanhua_mass"


def _fresh_app_dir() -> Path:
    """每次审计使用全新项目库目录（旧库残留条目会污染本轮 scan/写回）。

    实测：持久化 APP_DIR 里上一轮审计的条目与本次 scan 合并，meta 语义
    过期条目（如 kind=kv 写回路径）在 write_all 中抛 KeyError('kv')。
    审计 = 全新建库幂等流程，库目录每次重建最稳妥。
    """
    import shutil
    if APP_DIR.exists():
        shutil.rmtree(APP_DIR, ignore_errors=True)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    return APP_DIR

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hanhua.core.models import FontConfig  # noqa: E402
from hanhua.core.project import Project  # noqa: E402

OUT_BASE = Path(r"D:\游戏")
_FONT_NAME = "SimplifiedChinese/NotoSerifCJKsc-Medium.ttf"


def _fake_translate(project) -> int:
    """把所有 pending/failed 条目标记为已翻译（原文+零宽空格），返回条目数。

    注意不能用 upsert_entries：它只处理 pending/skipped 状态合并，status=
    "translated" 的行不会更新现有条目（实测写回计数仍为 0）。这里直接
    executemany 单次提交，保留原有 meta（disposition/kind/role）并叠加
    quality_passed 标记。
    """
    rows = []
    for entry in project.store.get_entries():
        if entry.get("status") not in ("pending", "failed"):
            continue
        original = entry.get("original") or ""
        if not original:
            continue
        if original.endswith("​"):
            translation = original  # 已带标记
        else:
            translation = original + "​"
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
        rows.append((
            translation, "translated", json.dumps(meta, ensure_ascii=False),
            entry["file_id"], entry["key_path"],
        ))
    if rows:
        with project.store._lock:
            project.store.conn.executemany(
                "UPDATE entries SET translation=?, status=?, meta=? "
                "WHERE file_id=? AND key_path=?", rows)
            project.store.conn.commit()
    return len(rows)



def _child_game_dirs(parent: Path) -> list[tuple[Path, str]]:
    """合集目录下钻：根目录不是 Unity 游戏时，在子目录（深 1 层）中找真实
    游戏（EXE + 同名 *_Data 配对）。返回 [(子游戏目录, 报告显示名)]。

    ned-flanders-kills-the-simpsons 实测：一个目录打包两个独立 Unity 游戏，
    fingerprint_game(root) 只能识别根级 EXE/Data 对 → runtime=unknown。
    """
    from hanhua.core.tooling.fingerprint import fingerprint_game
    found: list[tuple[Path, str]] = []
    for child in sorted(parent.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        # 排除汉化副本目录（out_dir 命名：<游戏名>_汉化）：副本含 exe/_Data/
        # BepInEx，误识别为子游戏会把它当游戏再写回（0Harmony.xml 实测失败）。
        if child.name.endswith("_汉化"):
            continue
        try:
            fp = fingerprint_game(child)
        except Exception:  # noqa: BLE001 子目录不是合法游戏则忽略
            continue
        if fp.runtime == "unknown" or fp.data_dir is None:
            continue
        found.append((child, f"{parent.name}/{child.name}"))
    return found


def writeback_game(game_dir: Path, rescan: bool) -> list[dict]:
    """对一个游戏（或合集目录下钻出的多个子游戏）执行写回，返回报告列表。

    注意：报告只由 main 统一写入（json.dumps(out)），这里不得写——否则
    合集目录的中间写入会覆盖完整报告（实测报告被截断为 2 条）。
    """
    from hanhua.core.tooling.fingerprint import fingerprint_game
    try:
        root_fp = fingerprint_game(game_dir)
        root_is_game = (
            root_fp.runtime != "unknown" and root_fp.data_dir is not None)
    except Exception:  # noqa: BLE001
        root_is_game = False
    # 根目录本身就是合法 Unity 游戏时不下钻（防止误拆含子目录的游戏）
    children = () if root_is_game else _child_game_dirs(game_dir)
    targets = children if children else [(game_dir, game_dir.name)]
    return [_writeback_single(target, display, rescan)
            for target, display in targets]


def _writeback_single(game_dir: Path, display: str, rescan: bool) -> dict:
    rec: dict = {"game": display, "ok": False}
    t0 = time.monotonic()
    try:
        project = Project.open_game_dir(game_dir, _fresh_app_dir())
        # 必须在本进程内 scan：_last_source_manifest / il2cpp 交叉验证证据是
        # 内存态，跨进程不持久化；不 scan 直接 write_all 会拒绝（实测）。
        report = project.scan_all()
        if report.recognized_entries == 0:
            # 零可翻译文本游戏（dollhouse 实测：纯 3D 场景无 UI 文本、DLL
            # 仅 11KB 空逻辑）→ 标记跳过而非失败，避免误报「漏检」。
            rec["ok"] = True
            rec["no_text"] = True
            rec["unity_version"] = report.fingerprint.unity_version
            rec["runtime"] = report.fingerprint.runtime
            rec["elapsed_s"] = round(time.monotonic() - t0, 1)
            return rec
        n_faked = _fake_translate(project)
        rec["fake_entries"] = n_faked
        font_cfg = FontConfig(enabled=True, filename=_FONT_NAME)
        # 伪翻译 = 原文 + U+200B 零宽空格，在固定容量池中必然截断——审计的
        # 目标是验证写回机制本身（重开验证/闸门状态/字体部署），截断放行
        # 并完整记录，与 UI「允许部分写入并发布」语义一致（P0-2）。
        result = project.write_all(font_config=font_cfg, allow_partial=True)
        rec["ok"] = True
        rec["text_files"] = result["text_files"]
        v2 = result["v2"]
        rec["v2_attempted"] = getattr(v2, "attempted", None)
        rec["v2_written"] = getattr(v2, "written", None)
        rec["v2_truncated"] = len(getattr(v2, "truncated_items", ()) or ())
        rec["v2_rejected"] = len(getattr(v2, "rejected", ()) or ())
        rec["verification"] = result["verification"]
        rec["font"] = {
            "installed": bool(result["font"].installed),
            "provider_id": result["font"].provider_id,
            "level": result["verification"].get("font_level"),
            "runtime_verified": result["verification"].get("font_runtime_verified"),
            # Phase 4：统一发布门终态 + 覆盖摘要（与 GUI/runner 同口径）
            "gate": (result["verification"].get("font_gate") or {}).get("status"),
            "coverage": (result["verification"].get("font_coverage") or {}).get(
                "overall"),
            # Phase 5：位图注入摘要（provider/injected/pending）
            "bitmap": result["verification"].get("font_bitmap"),
        }
        rec["unity_version"] = report.fingerprint.unity_version
        rec["runtime"] = report.fingerprint.runtime
        rec["route"] = [
            {"id": s.step_id, "status": s.status, "required": s.required}
            for s in report.route
        ]
        rec["elapsed_s"] = round(time.monotonic() - t0, 1)
    except Exception as exc:  # noqa: BLE001
        rec["ok"] = False
        rec["error"] = str(exc)[:500]
        rec["traceback"] = traceback.format_exc(limit=5)[-1500:]
        rec["elapsed_s"] = round(time.monotonic() - t0, 1)
    return rec


def main() -> None:
    args = [a for a in sys.argv[1:]]
    limit = None
    only = None
    rescan = "--rescan" in args
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    if "--games" in args:
        only = set(args[args.index("--games") + 1].split(","))
    survey = json.loads((OUT_BASE / "_survey.json").read_text(encoding="utf-8"))
    games = [OUT_BASE / r["name"] for r in survey if r["unity"]]
    if only:
        games = [g for g in games if g.name in only]
    if limit:
        games = games[:limit]
    out = []
    t0 = time.monotonic()
    for i, game_dir in enumerate(games, 1):
        print(f"[{i}/{len(games)}] {game_dir.name} ...", flush=True)
        recs = writeback_game(game_dir, rescan)
        out.extend(recs)
        (OUT_BASE / "_writeback_report.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        for rec in recs:
            font = rec.get("font", {})
            bitmap = font.get("bitmap") or {}
            bitmap_text = (f"bmp={bitmap.get('injected')}/{bitmap.get('pending')}"
                           if bitmap else "")
            print(f"  ok={rec['ok']} text={rec.get('text_files')} "
                  f"v2={rec.get('v2_written')}/{rec.get('v2_attempted')} "
                  f"font={font.get('provider_id')}({font.get('level')}) "
                  f"gate={font.get('gate')}({font.get('coverage')}) "
                  f"{bitmap_text} {rec.get('elapsed_s')}s", flush=True)
            if rec.get("no_text"):
                print("  (零可翻译文本，跳过写回)", flush=True)
            if not rec["ok"]:
                print(f"  ERROR: {rec.get('error', '')[:200]}", flush=True)
            elif rec.get("verification", {}).get("warnings"):
                for w in rec["verification"]["warnings"][:5]:
                    print(f"  warn: {w[:180]}", flush=True)
    print(f"TOTAL {round(time.monotonic() - t0, 1)}s "
          f"-> {OUT_BASE / '_writeback_report.json'}")
    fails = [r for r in out if not r["ok"]]
    print(f"failed: {len(fails)}/{len(out)}")
    for r in fails:
        print(f"  - {r['game']}: {r.get('error', '')[:150]}")


if __name__ == "__main__":
    main()
