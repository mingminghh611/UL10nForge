"""翻译 C5：审核术语沉淀语境保护门禁回归（2026-08-12，Phase B-3 升级）。

背景：F22-4 三连杀实证——审核沉淀 (miss,未命中)/(encore,安可)/
(Right,右拨片) 无门禁直写全局术语库，后续游戏强制约束把正常动词
用法/外语语境全部改写（deadbeat 杀 doubleshake 动词用法 → 杀 faerie
miss=想念；encore 杀法语；Right 杀 'pick the right door' 2083 条
失败）。事后靠 quality.py 豁免补丁而非沉淀端预防。

C5 门禁（只作用于审核沉淀路径 add_reviewed，Phase B-3 审计 P1-4）：
- 高频普通词单 token（miss/right/play/…）→ REJECTED，不写入全局库
- 其他词对（含组合词）→ CANDIDATE 进候选桶（format_for_prompt 不
  注入），**至少两个独立游戏、相同译法**才升级 ACTIVATED
  （Phase B-3 取消组合词直通 active——杜绝单源沉淀即强制）
- 同（归一化 term）已有不同译法 → CONFLICT：不覆盖、不升级、冲突
  游戏不计入激活证据，冲突例句留档
- note 载入语境（例句+来源游戏）
"""
import sqlite3

import pytest

from hanhua.core.glossary import (ACTIVATED, CANDIDATE, CONFLICT, REJECTED,
                                  GlossaryStore)


def _store(tmp_path, legacy=False):
    db = tmp_path / "glossary.db"
    if legacy:
        # 老库：只有 term/translation/category/note 四列（C5 迁移前形态）
        conn = sqlite3.connect(str(db))
        conn.executescript("""
        CREATE TABLE glossary(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            term TEXT UNIQUE, translation TEXT,
            category TEXT DEFAULT '术语', note TEXT DEFAULT ''
        );""")
        conn.commit()
        conn.close()
    store = GlossaryStore(db)
    store.init_schema()
    return store


def _activate(store, term, translation, games=("g1", "g2"),
              context="ctx"):
    """跨游戏复现到 active 的辅助：两个独立游戏、相同译法。"""
    for g in games:
        store.add_reviewed(term, translation, context=context, game=g)
    row = store.conn.execute(
        "SELECT status FROM glossary WHERE term=?", (term,)).fetchone()
    assert row["status"] == "active", f"{term} 未激活: {row['status'] if row else None}"


# ── 迁移 ────────────────────────────────────────────────────────────

def test_init_schema_migrates_legacy_table(tmp_path):
    """老库（无 status/games/context/term_norm 列）init_schema 后自动补列。"""
    store = _store(tmp_path, legacy=True)
    columns = {row[1] for row in store.conn.execute(
        "PRAGMA table_info(glossary)")}
    assert {"status", "games", "context", "term_norm"} <= columns
    # 老数据默认 status='active'（人工/专名路径不受门禁影响）
    store.add("foo", "酒吧", category="专名")
    row = store.conn.execute(
        "SELECT status, term_norm FROM glossary WHERE term='foo'").fetchone()
    assert row["status"] == "active"
    # 回填：旧行 term_norm 归一化（专名路径同样回填）
    assert row["term_norm"] == "foo"


def test_legacy_rows_get_term_norm_backfilled(tmp_path):
    """旧库已有条目（含大小写变体）→ init_schema 逐行回填归一化键。"""
    store = _store(tmp_path)
    store.add("Moon Key", "月光钥匙")
    store.add("moon key", "月光钥匙")  # 同源异形（旧路径允许双行）
    store2 = GlossaryStore(store.db)
    store2.init_schema()
    norms = {r["term_norm"] for r in store2.conn.execute(
        "SELECT term_norm FROM glossary")}
    assert norms == {"moonkey"}


# ── 拒绝：高频普通词单 token ────────────────────────────────────────

def test_high_frequency_word_rejected(tmp_path):
    """(miss, 未命中) 音游语境沉淀 → REJECTED，不写入全局库。"""
    store = _store(tmp_path)
    result = store.add_reviewed("miss", "未命中", context="Miss Combo x3",
                                game="deadbeat")
    assert result.status == REJECTED
    assert "拒绝沉淀" in result.reason
    assert store.list_all() == []


@pytest.mark.parametrize("term", ["miss", "right", "play", "save",
                                  "charge", "locked", "start", "on", "yes"])
def test_high_frequency_word_blacklist(tmp_path, term):
    """黑名单单 token 全部拒绝（动词/名词/方向用法无语境区分）。
    locked 2026-08-13 入列：Morfosi 64 条 "IT'S LOCKED." 被
    ('Locked','锁定') 全灭（锁定/上锁/被锁住语境变体多）。"""
    store = _store(tmp_path)
    result = store.add_reviewed(term, "测试译名", context=f"{term} xxx",
                                game="g")
    assert result.status == REJECTED
    assert store.list_all() == []


def test_builtin_ui_conflict_rejected(tmp_path):
    """BUILTIN 冲突门禁（2026-09-01 污染系统性根治）：审核沉淀单 token
    Disabled→残疾人士（UI 状态标签被误判「残疾」）→ REJECTED，不写全局
    库——权威译名（已禁用）由内置表恒胜出；权威译文与非冲突词照常沉淀。"""
    store = _store(tmp_path)
    bad = store.add_reviewed("Disabled", "残疾人士", context="天赋卡片",
                             game="g1")
    assert bad.status == REJECTED
    assert "内置 UI 术语" in bad.reason
    # 权威译文照常沉淀（candidate）
    good = store.add_reviewed("Disabled", "已禁用", context="天赋卡片",
                              game="g1")
    assert good.status == CANDIDATE
    # 非冲突词不受影响（多词短语/保留型专名）
    keep = store.add_reviewed("Press any key", "按任意键", context="c", game="g1")
    assert keep.status == CANDIDATE
    assert store.add_reviewed("itch", "itch", context="c", game="g1").status \
        == CANDIDATE
    # 库内只有合法条目（冲突译名未写入）
    rows = store.conn.execute("SELECT term FROM glossary").fetchall()
    assert sorted(r["term"] for r in rows) == [
        "Disabled", "Press any key", "itch"]


def test_builtin_ui_conflict_case_insensitive(tmp_path):
    """大小写形态同样拒绝（disabled/ENABLED 命中内置权威表）。"""
    store = _store(tmp_path)
    assert store.add_reviewed("disabled", "残疾人", context="c", game="g").status \
        == REJECTED
    assert store.add_reviewed("ENABLED", "已启动", context="c", game="g").status \
        == REJECTED


def test_blacklist_ignores_case(tmp_path):
    """大写形态同样拒绝（审核建议常大写原文词）。"""
    store = _store(tmp_path)
    result = store.add_reviewed("RIGHT", "右拨片", context="Hat RIGHT",
                                game="ffs")
    assert result.status == REJECTED


# ── candidate 桶：非黑名单词对 ──────────────────────────────────────

def test_single_token_goes_candidate(tmp_path):
    """非黑名单单 token（encore 等专有/术语形态）→ candidate 桶。"""
    store = _store(tmp_path)
    result = store.add_reviewed("encore", "安可", context="Encore!",
                                game="faerie")
    assert result.status == CANDIDATE
    assert result.games == ("faerie",)
    row = store.conn.execute(
        "SELECT status, games FROM glossary WHERE term='encore'").fetchone()
    assert row["status"] == "candidate"
    assert "faerie" in row["games"]


def test_combo_pair_goes_candidate_first(tmp_path):
    """Phase B-3：组合词对不再直通 active——单源沉淀一律候选。

    F22-4 教训的严格化：任何词对（含组合词）都要「至少两个独立游戏、
    相同译法」才激活，杜绝单次审核建议即全局强制。"""
    store = _store(tmp_path)
    result = store.add_reviewed("Left Paddle", "左拨片",
                                context="Left Paddle: 左拨片", game="ffs")
    assert result.status == CANDIDATE
    row = store.conn.execute(
        "SELECT status FROM glossary WHERE term='Left Paddle'").fetchone()
    assert row["status"] == "candidate"
    assert "Left Paddle" not in store.format_for_prompt()


def test_sentence_like_pair_rejected(tmp_path):
    """整句/长短语拒绝（isolated-inhale 多语言盲区实证）：≥5 词或以句尾
    标点结尾的审核建议不是词级术语——沉淀 active 强制后续翻译必误用
    （'Docke an die Sauerstoffstation an' 6 词 / 'Premi Invio per
    continuare...' 省略号结尾，译文 'Premi' 被误译「提交奖励」）。"""
    store = _store(tmp_path)
    result = store.add_reviewed(
        "Docke an die Sauerstoffstation an", "对接氧气站",
        context="Docke an die Sauerstoffstation an", game="g")
    assert result.status == REJECTED
    assert "整句" in result.reason
    result2 = store.add_reviewed(
        "Premi Invio per continuare...", "按回车继续",
        context="Premi Invio per continuare...", game="g")
    assert result2.status == REJECTED
    assert "整句" in result2.reason
    assert store.list_all() == []
    # 对照：词级 UI 短语照常入候选（4 词内 + 无句尾标点），跨游戏后激活
    store.add_reviewed("RESUME WITH CURRENT SIZE", "使用当前大小继续",
                       context="RESUME WITH CURRENT SIZE", game="g1")
    assert store.list_all()[0]["status"] == "candidate"
    store.add_reviewed("RESUME WITH CURRENT SIZE", "使用当前大小继续",
                       context="RESUME WITH CURRENT SIZE", game="g2")
    assert store.list_all()[0]["status"] == "active"


def test_rich_text_fragment_translation_rejected(tmp_path):
    """译文含富文本标记 → 文本片段不是术语译名（isolated-inhale 实证：
    'ffcc' → '高亮按钮</color>以显示可用的命令列' 整句上下文当译名）。"""
    store = _store(tmp_path)
    result = store.add_reviewed("ffcc", "高亮按钮</color>以显示可用的命令列",
                                context="ffcc xxx", game="g")
    assert result.status == REJECTED
    assert "富文本" in result.reason
    assert store.list_all() == []


def test_symbol_only_term_rejected(tmp_path):
    """纯标点/符号无归一化词形（!!!）→ 拒绝（无法跨游戏识别）。"""
    store = _store(tmp_path)
    result = store.add_reviewed("!!!", "感叹号", context="!!!", game="g")
    assert result.status == REJECTED
    assert store.list_all() == []


def test_learn_proper_names_skips_noise_terms(tmp_path):
    """噪音形态不学成专名（2026-08-13 实证：NULL/XXXX/JXXXX/FFC400/
    AAAAAGHHHHHH 被 collect_known_names 收进清单学成 active 专名，
    污染术语表显示与 prompt 注入——颜色码/占位/尖叫跨游戏必不复现）。"""
    from hanhua.core.models import TextEntry
    store = _store(tmp_path)
    names = ["NULL", "XXXX", "JXXXX", "FFC400", "AAAAAGHHHHHH",
             "GLISLYA", "SCP-173"]
    entries = [
        TextEntry(file_id="f", key_path=n, original=f"PICK THE {n} NOW",
                  translation=f"PICK THE {n} NOW", status="translated",
                  meta={"quality_passed": True})
        for n in names]
    learned = store.learn_proper_names(entries, names, "test-game")
    rows = {r["term"] for r in store.list_all()}
    assert "GLISLYA" in rows          # 正常专名照学
    assert "SCP-173" in rows          # 带数字型号照学
    assert not ({"NULL", "XXXX", "JXXXX", "FFC400",
                 "AAAAAGHHHHHH"} & rows)
    assert learned == 2


def test_candidate_not_injected_into_prompt(tmp_path):
    """format_for_prompt 只注入 active——candidate 仅参考不强制。"""
    store = _store(tmp_path)
    store.add_reviewed("encore", "安可", context="Encore!", game="g1")
    store.add_reviewed("Left Paddle", "左拨片", context="Left Paddle",
                       game="g1")
    assert "encore" not in store.format_for_prompt()
    assert "Left Paddle" not in store.format_for_prompt()
    # 跨游戏复现激活后才注入
    store.add_reviewed("Left Paddle", "左拨片", context="Left Paddle",
                       game="g2")
    assert "Left Paddle" in store.format_for_prompt()


def test_candidate_promotes_on_cross_game_repeat(tmp_path):
    """candidate 跨游戏复现（第二次审核沉淀，相同译法）→ ACTIVATED。"""
    store = _store(tmp_path)
    first = store.add_reviewed("encore", "安可", context="Encore!",
                               game="faerie")
    assert first.status == CANDIDATE
    second = store.add_reviewed("encore", "安可", context="Encore!",
                                game="ffs")
    assert second.status == ACTIVATED
    row = store.conn.execute(
        "SELECT status, games FROM glossary WHERE term='encore'").fetchone()
    assert row["status"] == "active"
    assert row["games"] == "faerie,ffs"
    assert "encore" in store.format_for_prompt()


# ── Phase B-3：激活/冲突证据模型 ────────────────────────────────────

def test_same_game_repeat_does_not_activate(tmp_path):
    """同一游戏重复沉淀 → 仍是 candidate（激活必须独立游戏）。"""
    store = _store(tmp_path)
    store.add_reviewed("encore", "安可", context="Encore!", game="faerie")
    result = store.add_reviewed("encore", "安可", context="Encore!",
                                game="faerie")
    assert result.status == CANDIDATE
    row = store.conn.execute(
        "SELECT status, games FROM glossary WHERE term='encore'").fetchone()
    assert row["status"] == "candidate"
    assert row["games"] == "faerie"  # games 去重


def test_conflict_does_not_overwrite_or_activate(tmp_path):
    """同源异译（P1-4 核心）：不覆盖已有译法、不升级、冲突游戏不计入。"""
    store = _store(tmp_path)
    store.add_reviewed("encore", "安可", context="Encore! 安可",
                       game="faerie")
    result = store.add_reviewed("encore", "重演", context="Encore! 重演",
                                game="ffs")
    assert result.status == CONFLICT
    assert "安可" in result.reason and "重演" in result.reason
    row = store.conn.execute(
        "SELECT translation, status, games, note FROM glossary"
        " WHERE term='encore'").fetchone()
    # 首译文保留、不升级
    assert row["translation"] == "安可"
    assert row["status"] == "candidate"
    # 冲突游戏不进入 games（不污染激活证据）
    assert row["games"] == "faerie"
    # 冲突例句留档（人工复核证据）
    assert "冲突例句" in row["note"]
    assert "ffs" in row["note"]


def test_conflict_game_not_counted_toward_activation(tmp_path):
    """g1 确认、g2 冲突、g3 与 g1 同译 → 只有 g1+g3 两独立游戏激活。

    冲突游戏若计入 games，第二游戏就能把冲突词顶成 active——正是
    P1-4 审计的问题。"""
    store = _store(tmp_path)
    store.add_reviewed("Power Core", "能量核心", game="g1")
    store.add_reviewed("Power Core", "电源芯", game="g2")   # CONFLICT
    result = store.add_reviewed("Power Core", "能量核心", game="g3")
    assert result.status == ACTIVATED
    row = store.conn.execute(
        "SELECT status, games FROM glossary WHERE term='Power Core'"
    ).fetchone()
    assert row["status"] == "active"
    assert row["games"] == "g1,g3"   # g2 不在（它不同意译法）


def test_normalized_term_uniqueness_merges_variants(tmp_path):
    """唯一语义 = 归一化 term："Moon Key" 与 "moon key" 同一术语。

    相同译法 → 合并进同一行（games 累积）；不同译法 → 冲突（即使
    大小写/空白不同）。"""
    store = _store(tmp_path)
    store.add_reviewed("Moon Key", "月光钥匙", game="g1")
    result = store.add_reviewed("moon key", "月光钥匙", game="g2")
    assert result.status == ACTIVATED
    rows = store.conn.execute("SELECT term FROM glossary").fetchall()
    assert len(rows) == 1   # 同源归一化到一行
    # 异形异译 → 冲突而非双行
    store2 = _store(tmp_path)
    store2.add_reviewed("MOON KEY", "月光钥匙", game="g1")
    conflict = store2.add_reviewed("moon key", "月亮键", game="g2")
    assert conflict.status == CONFLICT
    assert len(store2.list_all()) == 1


def test_activation_requires_same_translation(tmp_path):
    """激活前提「相同译法」：两个游戏译法不一 → 永不激活。"""
    store = _store(tmp_path)
    store.add_reviewed("Bubble", "泡泡", game="g1")
    store.add_reviewed("Bubble", "气泡", game="g2")   # CONFLICT
    store.add_reviewed("Bubble", "气泡", game="g3")   # 同 g2 异 g1 → CONFLICT
    row = store.conn.execute(
        "SELECT status, games FROM glossary WHERE term='Bubble'").fetchone()
    assert row["status"] == "candidate"
    assert row["games"] == "g1"     # 只有首个确认者


def test_repeat_activation_is_idempotent(tmp_path):
    """已 active 条目再次同译沉淀 → ACTIVATED（幂等），games 去重合并。"""
    store = _store(tmp_path)
    _activate(store, "Left Paddle", "左拨片", games=("ffs", "g2"))
    result = store.add_reviewed("Left Paddle", "左拨片", context="c2",
                                game="ffs")
    assert result.status == ACTIVATED
    row = store.conn.execute(
        "SELECT status, games FROM glossary WHERE term='Left Paddle'"
    ).fetchone()
    assert row["status"] == "active"
    assert row["games"] == "ffs,g2"


# ── 语境留档 ────────────────────────────────────────────────────────

def test_note_carries_example_and_game(tmp_path):
    """note 载入原文例句+来源游戏，不再只写「来源 X」。"""
    store = _store(tmp_path)
    store.add_reviewed("Left Paddle", "左拨片",
                       context="Left Paddle to open menu", game="ffs")
    row = store.conn.execute(
        "SELECT note FROM glossary WHERE term='Left Paddle'").fetchone()
    assert "来源 ffs" in row["note"]
    assert "Left Paddle to open menu" in row["note"]


def test_empty_pair_rejected(tmp_path):
    """空词对不沉淀。"""
    store = _store(tmp_path)
    assert store.add_reviewed("", "译名", context="c", game="g").status \
        == REJECTED
    assert store.add_reviewed("term", "", context="c", game="g").status \
        == REJECTED
    assert store.list_all() == []


def test_deposit_result_shape(tmp_path):
    """DepositResult 结构化字段（status/reason/games/term）。"""
    store = _store(tmp_path)
    r = store.add_reviewed("Power Core", "能量核心", context="Power Core",
                           game="g1")
    assert r.status == CANDIDATE
    assert r.games == ("g1",)
    assert r.term == "Power Core"
    assert r.reason == ""
