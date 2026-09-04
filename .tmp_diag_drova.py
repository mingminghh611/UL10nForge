"""临时诊断：drova sharedassets6 全 GNode 类对象的字符串样本统计。"""
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from pathlib import Path

from UnityPy import Environment

g = Path(r"D:\游戏\drova\Drova_Data\sharedassets6.assets")
env = Environment()
env.path = str(g.parent)
env.load_files([str(g)])
from hanhua.core.unity.extractor import (
    _raw_string_entries,
    _high_freq_threshold,
    _script_class_from_head,
    scan_strings,
)

freq: dict[str, int] = {}
items = []
for obj in env.objects:
    if obj.type.name not in ("MonoBehaviour",):
        continue
    try:
        raw = obj.get_raw_data()
    except Exception:
        continue
    if not raw or b"GNode(" not in raw:
        continue
    cls = _script_class_from_head(obj)
    ss = [s for _, s in scan_strings(raw)]
    for s in ss:
        freq[s] = freq.get(s, 0) + 1
    items.append((obj.path_id, cls, raw))

thr = _high_freq_threshold(freq)
print(f"GNode objects: {len(items)}, threshold={thr}")
golden_missed = [
    "GNode(Inverter:EnemyTargetingMe)", "GNode(AI_Global Range Check)",
    "AI_Set Combat Position", "AI_Set Combat Music",
]
for path_id, cls, raw in items[:6]:
    entries = _raw_string_entries(
        "Drova_Data/sharedassets6.assets", path_id, raw, freq,
        "sharedassets6.assets", thr, cls)
    pend = [e.original for e in entries if e.status == "pending"]
    skip = [(e.original, e.meta.get("reason")) for e in entries
            if e.status == "skipped"]
    print(f"== obj{path_id} class={cls} pend={len(pend)} skip={len(skip)}")
    ss_all = [s for _, s in scan_strings(raw)]
    for s in ss_all[:12]:
        mark = "P" if s in pend else ("S" if any(x == s for x, _ in skip) else "?")
        print(f"  [{mark}] {s!r}")
