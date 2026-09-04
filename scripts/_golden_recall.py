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
from hanhua.core.placeholders import should_skip


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

# 对象标识名形态（0.38.0 测量口径修正）：drova 实证 143 条「遗漏」全在
# MonoBehaviour m_Name 跨度——旧库把 AI 节点名 'GNode(...)' / 能力名
# 'ABI_Regeneration _InstantHeal' / 任务键 'SubQuest_FOA_HelpFrom NPC_Hunter'
# 过提取为 display。当前提取器按 _mono_object_name_span 正确跳过，
# 这些串不是召回缺陷，是金标准陈旧。对象名几乎总是代码标识符形态
# （无空格 CamelCase/括号结构/下划线连接），而真显示文本（句子/含空格
# UI 词）极少命中——形态归因只解释「旧库过提取」，不掩盖真缺口。
_IDENTITY_NAME_SHAPES = (
    __import__("re").compile(r"^GNode\("),                  # drova AI 节点
    __import__("re").compile(r"^A[IB][Il]_[A-Za-z0-9_]+$"), # AI_Set/ABI_ 能力
    __import__("re").compile(r"^[A-Z][a-zA-Z]+_[A-Z][a-zA-Z0-9_]+$"),  # Pascal_Pascal 键
)
# 含空格对象名的首段/尾段形态（drova 实证 4 条带空格对象名：
# 'AI_Set Combat Music' / 'ABI_Regeneration _InstantHeal' /
# 'SubQuest_FOA_HelpFrom NPC_Hunter'）——首段必须是下划线标识符，
# 尾段必须含大写或下划线，'Combat Music'（真 UI 词）与
# 'Quest_1 completed'（尾段小写句词）都不命中。
# drova 残留实证还有 'GNode(Global Attack Cooldown)'（对象名含空格短语，
# 已被 GNode 前缀形态覆盖）与整串对象名 'StatusEffect_StickyWeb 1' /
# 'Misc_MysteryNote 1'——后两者是「形态键 + 数字后缀」的 Unity 自动去重
# 命名（同名字对象第 N 个加 ' N'），亦为对象标识名。
_UNDERSCORE_IDENTIFIER = __import__("re").compile(
    r"^[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]*$")
_TOKEN_ALNUM = __import__("re").compile(r"^[A-Za-z0-9_]+$")
# 形态键+数字后缀（Unity 同名对象自动去重 ' N' 后缀）：前段命中
# 下划线键形态（含 _，混合大小写），后段是纯数字。
_NUMERIC_SUFFIX = __import__("re").compile(
    r"^(?P<key>[A-Za-z0-9_]*_[A-Za-z0-9_]*)\s+\d+$")


def _is_object_identity_name(text: str, file_id: str) -> bool:
    """金标准原文是否为对象标识名（m_Name 位置串，正确跳过不计遗漏）。

    严格判定防误豁免：仅当串命中标识符形态、且来源是 MonoBehaviour
    常驻载体（.assets/level/sharedassets/资源容器——rawstr 路径的
    m_Name 保护正是对这些对象生效）时才成立。DLL/il2cpp 来源不含
    m_Name，不豁免。"""
    stripped = text.strip()
    if not stripped:
        return False
    fid = (file_id or "").replace("\\", "/").casefold()
    if not any(seg in fid for seg in (".assets", "level", "sharedassets",
                                      ".bundle", ".ab", ".unity3d")):
        return False
    if any(pattern.match(stripped) for pattern in _IDENTITY_NAME_SHAPES):
        return True
    tokens = stripped.split(" ")
    # 形态键 + 数字后缀（'StatusEffect_StickyWeb 1'）：Unity 同名对象
    # 自动去重命名，键段必须含下划线（防 'Level 1' 真关卡名误豁免）。
    m = _NUMERIC_SUFFIX.match(stripped)
    if m and "_" in m.group("key"):
        return True
    # 'Seq:' 前缀（drova 行为序列节点名 'Seq:Medium Circle'）。
    if stripped.startswith("Seq:"):
        return True
    # 无空格标识符兜底：键风格 / 下划线+混合大小写（'AI_SetCombatMusic'）
    if len(tokens) == 1:
        return bool(should_skip(stripped) or (
                "_" in stripped
                and stripped != stripped.lower()
                and stripped != stripped.upper()))
    # 含空格：首段下划线标识符（AI_Set / ABI_Regeneration /
    # SubQuest_FOA_HelpFrom）+ 其余段也是标识符形态（含大写或下划线，
    # 无小写句词/句读标点）。
    if not _UNDERSCORE_IDENTIFIER.match(tokens[0]):
        return False
    for token in tokens[1:]:
        if not _TOKEN_ALNUM.match(token):
            return False
        if not (any(ch.isupper() for ch in token) or "_" in token):
            return False
    return True


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
        # 测量口径修正（0.38.0）：对象标识名（m_Name 位置串）是旧库
        # 过提取的正确跳过，不算召回遗漏也不进分母（同陈旧键口径）——
        # 单独归因列出，防止报告把「保护正确」误读成「识别缺口」
        # （drova 143 条实证）。
        identity: list[str] = []
        for text in missed:
            if _is_object_identity_name(text, golden[text]["file_id"]):
                identity.append(text)
        if identity:
            identity_set = set(identity)
            missed = [t for t in missed if t not in identity_set]
        current_total = (len(kv_groups) + len(text_groups) - stale
                         - len(identity))
        total_golden += current_total
        total_found += found
        recall = found / current_total if current_total else 0.0
        actionable_rate = (found_actionable / current_total
                           if current_total else 0.0)
        print(f"  进池率: {found}/{current_total} = {recall:.1%}"
              + (f"（陈旧键 {stale} 条不计——游戏文件已更新）"
                 if stale else "")
              + (f"（对象标识名 {len(identity)} 条不计——旧库过提取，"
                 f"m_Name 正确跳过）" if identity else ""))
        print(f"  可译率: {found_actionable}/{current_total}"
              f" = {actionable_rate:.1%}"
              f"（disposition=translate 的可译条目口径）")
        if identity:
            print(f"  对象标识名样例: "
                  f"{identity[0][:60].replace(chr(10), ' ')!r}")
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
