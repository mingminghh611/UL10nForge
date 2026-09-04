"""金标准识别率测量：已汉化项目库的验证译文 vs 当前提取池。

口径：
- 金标准 = ~/.hanhua/projects/<md5(game_dir)[:10]>/project.db 中
  status=translated 且 translation 非空的条目原文（人类验收过的真实
  显示文本全集）；
- 提取池 = 当前提取器（asset/mono/il2cpp 三通道）产出的全部条目原文；
- 识别率 = |金标准 ∩ 提取池| / |金标准|——「已知文本必须进池」的
  真实召回数字；
- 遗漏按来源文件分解：缺口在哪类载体直接可见。

用法：runtime/python/python.exe scripts/_golden_recall.py <语料目录> [游戏名过滤...]
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hanhua.core.recognition_report import build_report


def _project_dbs(game_dir: Path) -> list[Path]:
    """GUI 库（~/.hanhua）与 runner 批量库（~/.hanhua_sweep）都查。"""
    slug = hashlib.md5(str(game_dir).encode("utf-8")).hexdigest()[:10]
    candidates = [
        Path.home() / ".hanhua" / "projects" / slug / "project.db",
        Path.home() / ".hanhua_sweep" / "projects" / slug / "project.db",
    ]
    return [db for db in candidates if db.is_file()]


def _golden_entries(db: Path) -> dict[str, dict]:
    """原文 → {translation, status, file_id}（display 角色全状态）。

    分母口径：旧库 role=display 的全部条目（translated + failed +
    pending）——translated 子集偏向「模型翻得动的简单文本」，把
    难文本（failed）排除出分母会高估召回。skip/ 前缀的留档样本行
    排除（它们是审计样本不是显示文本）。"""
    import json as _json
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT original, translation, status, file_id, meta"
            " FROM entries WHERE status IN"
            " ('translated','failed','pending')").fetchall()
    finally:
        conn.close()
    out: dict[str, dict] = {}
    for original, translation, status, file_id, meta_raw in rows:
        try:
            meta = _json.loads(meta_raw) if meta_raw else {}
        except (_json.JSONDecodeError, TypeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        role = str(meta.get("role", "display"))
        if role in {"structural", "code", "key"}:
            continue
        if str(meta.get("kind", "")).startswith("skip"):
            continue
        if original is None or not str(original).strip():
            continue
        out[str(original)] = {
            "translation": translation, "status": status,
            "file_id": file_id,
            "meta": meta,
        }
    return out


_KV_LINE = __import__("re").compile(
    r"^(?P<key>[^=:\t\r\n]+?)\s*[:=]\s*(?P<value>.*)$")


def _current_kv_keys(game_dir: Path) -> dict[str, set]:
    """当前游戏文件的 KV 键全集（按资源文件名分桶）。

    旧库可能来自游戏的旧版本——键已不存在的金标准线是陈旧数据，
    不应计入召回分母（electric-trains 实证：61/63 遗漏键在当前文件
    中不存在，游戏更新过）。
    """
    from UnityPy import Environment
    keys: dict[str, set] = {}
    # 数据目录名可能与目录名不同（electric-trains → Electric Trains_Data）
    asset_files = list(game_dir.glob("**/*.assets"))
    if not asset_files:
        return keys
    try:
        env = Environment()
        env.load([str(a) for a in asset_files])
    except Exception:  # noqa: BLE001
        return keys
    for obj in env.objects:
        if obj.type.name != "TextAsset":
            continue
        try:
            data = obj.read()
        except Exception:  # noqa: BLE001
            continue
        script = getattr(data, "m_Script", None)
        if not isinstance(script, str):
            continue
        asset_name = str(getattr(obj, "assets_file", None)
                         and getattr(obj.assets_file, "name", "") or "")
        # bucket = 资产文件在当前游戏目录内的相对路径（与旧库 file_id 对齐）
        try:
            rel = (Path(asset_name).resolve()
                   .relative_to(game_dir.resolve())).as_posix()
        except (OSError, ValueError):
            rel = asset_name.replace("\\", "/")
        bucket = rel
        keys.setdefault(bucket, set())
        for line in script.splitlines():
            m = _KV_LINE.match(line.strip())
            if m:
                keys[bucket].add(m.group("key").strip())
    return keys


def _match_forms(original: str) -> tuple[str, ...]:
    """金标准原文的池匹配形态：整行 + KV 值（旧库整行原文
    'missions=Missioni' 对应新提取的值条目 'Missioni'）。"""
    m = _KV_LINE.match(original.strip())
    if m and m.group("value").strip():
        return (original, m.group("value").strip())
    return (original,)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1])
    filters = sys.argv[2:]
    games = sorted(p for p in root.iterdir()
                   if p.is_dir() and not p.name.startswith(("_", "."))
                   and (not filters or p.name in filters))
    print(f"语料 {len(games)} 个游戏，反查已汉化库……")
    with_projects = [(g, db) for g in games
                     for db in _project_dbs(g)]
    print(f"{len(with_projects)} 个游戏有已汉化项目库\n")
    total_golden = total_found = 0
    for game, db in with_projects:
        golden = _golden_entries(db)
        if not golden:
            continue
        print(f"===== {game.name}：金标准 {len(golden)} 条 =====")
        try:
            report = build_report(game)
        except Exception as exc:  # noqa: BLE001
            print(f"  [ERROR] 提取失败: {exc!r}\n")
            continue
        pool = report.pool_originals
        current_keys = _current_kv_keys(game)
        # 多语言词典污染归一：旧库对同一 key 的 19 张语言表各有一条
        # 译文——新行为只提取源表。KV 行按「键在当前文件存在」计识别
        # （游戏更新后值文字可能漂移，键位置被识别才是召回语义）；
        # 非 KV 行按原文匹配。
        kv_groups: dict[tuple, list[str]] = {}
        text_groups: dict[tuple, list[str]] = {}
        for text, info in golden.items():
            m = _KV_LINE.match(text.strip())
            # 值非空且键无空白才是 KV 行（'ДОСТУПНЫЕ ОЧКИ:' 空值与
            # 'правила: игра...' 句子冒号都不是词典行，electric-trains
            # 实证）
            if (m and m.group("value").strip()
                    and not any(ch.isspace() for ch in m.group("key"))):
                kv_groups.setdefault(
                    (info["file_id"], m.group("key").strip()), []).append(text)
            else:
                text_groups.setdefault((info["file_id"], text), []).append(text)
        pool = report.pool_originals
        actionable = report.pool_actionable
        found = 0
        found_actionable = 0
        stale = 0
        missed: list[str] = []
        # KV 桶只对「当前文件确实是 KV 词典载体」的 file_id 生效：
        # level/DLL 里的 'Potions: {0}/{1}' 是格式串不是词典行——
        # 无 KV 桶匹配时必须回落到整行/值形态的池匹配，否则已进池
        # 的条目被误计为遗漏（drova 'Aether: {0}' 实证）。
        bucket_ids = set(current_keys)
        for (fid, key), texts in kv_groups.items():
            has_bucket = any(fid.endswith(b) or b.endswith(fid)
                             for b in bucket_ids)
            if not has_bucket:
                text_groups.setdefault((fid, texts[0]), []).extend(texts)
                continue
            exists: bool | None = None
            for bucket, keys in current_keys.items():
                if fid.endswith(bucket) or bucket.endswith(fid):
                    exists = key in keys
                    break
            if exists:
                found += 1
                # 可译：任一语言的线/值形态是可译条目
                if any(any(form in actionable for form in _match_forms(t))
                       for t in texts):
                    found_actionable += 1
            elif exists is False:
                stale += 1  # 键在旧版存在、当前文件已移除
            else:
                missed.append(texts[0])
        for (_fid, text), texts in text_groups.items():
            if text in pool:
                found += 1
                if text in actionable:
                    found_actionable += 1
            else:
                missed.append(text)
        current_total = len(kv_groups) + len(text_groups) - stale
        total_golden += current_total
        total_found += found
        recall = found / current_total if current_total else 0.0
        actionable_rate = (found_actionable / current_total
                           if current_total else 0.0)
        print(f"  进池率: {found}/{current_total} = {recall:.1%}"
              + (f"（陈旧键 {stale} 条不计——游戏文件已更新）"
                 if stale else ""))
        print(f"  可译率: {found_actionable}/{current_total}"
              f" = {actionable_rate:.1%}"
              f"（disposition=translate 的可译条目口径）")
        missed = missed
        if missed:
            by_file: dict[str, list[str]] = {}
            for text in missed[:200]:
                by_file.setdefault(golden[text]["file_id"], []).append(text)
            print(f"  遗漏 {len(missed)} 条（前 200 按来源文件分解）：")
            for fid, texts in sorted(
                    by_file.items(), key=lambda kv: -len(kv[1]))[:8]:
                sample = texts[0][:60].replace("\n", " ")
                print(f"    {fid} ×{len(texts)}  例: {sample!r}")
        print()
    if total_golden:
        print(f"===== 合计识别率: {total_found}/{total_golden} = "
              f"{total_found / total_golden:.1%} =====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
