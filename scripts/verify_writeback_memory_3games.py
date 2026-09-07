"""3 游戏写回内存验证（0.45.0 E6 修复回归）。

对 drova / wicked / fromivan 执行完整写回管线（伪翻译驱动），同时后台线程
每 500ms 采样 RSS，验证：
1. 写回全流程成功（无异常/闸门拒绝）；
2. 字体阶段不再出现 GB 级内存锯齿（E6 修复前 drova 单容器 870s 振荡）；
3. 内存峰值受控、无持续增长（泄漏）。

用法: python scripts/verify_writeback_memory_3games.py [--games drova,wicked,fromivan]
输出: D:\\游戏\\_writeback_mem_report.json + 控制台逐游戏摘要
"""
from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

APP_DIR = Path.home() / ".hanhua_memverify"
OUT_BASE = Path(r"D:\游戏")
_FONT_NAME = "SimplifiedChinese/NotoSerifCJKsc-Medium.ttf"


def _fresh_app_dir() -> Path:
    import shutil
    if APP_DIR.exists():
        shutil.rmtree(APP_DIR, ignore_errors=True)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    return APP_DIR


class MemSampler:
    """后台 RSS 采样器：峰值 / 当前 / 采样数。"""

    def __init__(self, interval=0.5):
        self.interval = interval
        self.proc = psutil.Process() if psutil else None
        self.peak_mb = 0.0
        self.cur_mb = 0.0
        self.samples = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        if not self.proc:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                rss = self.proc.memory_info().rss / (1024 * 1024)
                self.cur_mb = rss
                if rss > self.peak_mb:
                    self.peak_mb = rss
                self.samples += 1
            except Exception:  # noqa: BLE001 进程退出竞态
                return
            self._stop.wait(self.interval)

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        return {"peak_mb": round(self.peak_mb, 1),
                "final_mb": round(self.cur_mb, 1),
                "samples": self.samples}


def _fake_translate(project) -> int:
    rows = []
    for entry in project.store.get_entries():
        if entry.get("status") not in ("pending", "failed"):
            continue
        original = entry.get("original") or ""
        if not original:
            continue
        translation = original if original.endswith("\u200b") else original + "\u200b"
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
        rows.append((translation, "translated", json.dumps(meta, ensure_ascii=False),
                     entry["file_id"], entry["key_path"]))
    if rows:
        with project.store._lock:
            project.store.conn.executemany(
                "UPDATE entries SET translation=?, status=?, meta=? "
                "WHERE file_id=? AND key_path=?", rows)
            project.store.conn.commit()
    return len(rows)


def run_game(game_dir: Path) -> dict:
    from hanhua.core.models import FontConfig
    from hanhua.core.project import Project
    rec: dict = {"game": game_dir.name, "ok": False}
    t0 = time.monotonic()
    sampler = MemSampler()
    try:
        project = Project.open_game_dir(game_dir, _fresh_app_dir())
        sampler.start()
        report = project.scan_all()
        if report.recognized_entries == 0:
            rec.update(ok=True, no_text=True,
                       elapsed_s=round(time.monotonic() - t0, 1),
                       memory=sampler.stop())
            return rec
        n_faked = _fake_translate(project)
        rec["fake_entries"] = n_faked
        font_cfg = FontConfig(enabled=True, filename=_FONT_NAME)
        result = project.write_all(font_config=font_cfg, allow_partial=True)
        rec["ok"] = True
        rec["text_files"] = result["text_files"]
        v2 = result["v2"]
        rec["v2_attempted"] = getattr(v2, "attempted", None)
        rec["v2_written"] = getattr(v2, "written", None)
        rec["verification"] = result["verification"]
        rec["font_level"] = result["verification"].get("font_level")
        rec["font_gate"] = (result["verification"].get("font_gate") or {}).get("status")
        rec["elapsed_s"] = round(time.monotonic() - t0, 1)
        rec["memory"] = sampler.stop()
    except Exception as exc:  # noqa: BLE001
        rec["error"] = str(exc)[:500]
        rec["traceback"] = traceback.format_exc(limit=5)[-1500:]
        rec["elapsed_s"] = round(time.monotonic() - t0, 1)
        rec["memory"] = sampler.stop()
    return rec


def main() -> None:
    args = sys.argv[1:]
    games_arg = "drova,wicked,fromivan"
    if "--games" in args:
        games_arg = args[args.index("--games") + 1]
    names = [n for n in games_arg.split(",") if n]
    out_path = OUT_BASE / "_writeback_mem_report.json"
    results = []
    for name in names:
        game_dir = OUT_BASE / name
        if not game_dir.exists():
            results.append({"game": name, "ok": False, "error": "dir not found"})
            continue
        print(f"=== {name} ===", flush=True)
        rec = run_game(game_dir)
        results.append(rec)
        mem = rec.get("memory", {})
        print(f"  ok={rec['ok']} text={rec.get('text_files')} "
              f"v2={rec.get('v2_written')}/{rec.get('v2_attempted')} "
              f"font={rec.get('font_level')} gate={rec.get('font_gate')} "
              f"{rec.get('elapsed_s')}s", flush=True)
        print(f"  mem: peak={mem.get('peak_mb')}MB final={mem.get('final_mb')}MB "
              f"samples={mem.get('samples')}", flush=True)
        if not rec["ok"]:
            print(f"  ERROR: {rec.get('error', '')[:200]}", flush=True)
            print(rec.get("traceback", "")[-800:], flush=True)
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    print(f"\nreport -> {out_path}", flush=True)


if __name__ == "__main__":
    main()
