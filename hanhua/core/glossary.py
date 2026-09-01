from __future__ import annotations
import re
import sqlite3
import threading
from pathlib import Path
from typing import NamedTuple

from hanhua.core.knowledge import _UPPERCASE_ACTION_VERBS
from hanhua.core.translator import BUILTIN_UI_EXACT, builtin_ui_conflict

# 归一化冲突键：大小写 + 空白压缩 + 去标点。用于检测「同源不同译」：
# "moon key" 与 "Moon Key" 是同一术语，若译名不同则模型会无所适从
# （同一原文在 prompt 里出现两个译法 → 一致性破坏）。
_CONFLICT_NORM = re.compile(r"[^a-z0-9一-鿿]+")

# ── 审核沉淀终态（审计 Phase B-3，P1-4：C5 候选/激活/冲突统计不可信）──
# 旧 add_reviewed 对 candidate 与 activated 都返回空串，调用方统一计入
# pairs_added；已存在 term 的不同 translation 会被 UPDATE 覆盖，第二
# 游戏可能把冲突词直接升级 active。现在返回结构化 DepositResult：
#   REJECTED  门禁拒绝（高频单 token / 整句 / 富文本 / 空词对），
#             不写入全局库
#   CANDIDATE 新词对入 candidate 桶（参考不强制），等待跨游戏复现
#   ACTIVATED 至少两个独立游戏、相同译法 → 升级 active（可注入/强制）
#   CONFLICT  同（归一化 term）已有不同译法：不覆盖、不升级、不把
#             冲突游戏计入激活证据——冲突上下文留档（人工复核）
REJECTED = "REJECTED"
CANDIDATE = "CANDIDATE"
ACTIVATED = "ACTIVATED"
CONFLICT = "CONFLICT"


class DepositResult(NamedTuple):
    """审核沉淀的结构化结果（Phase B-3）：调用方据此分类记账。"""
    status: str
    reason: str = ""
    games: tuple = ()
    term: str = ""

# 翻译 C5：高频普通词单 token 黑名单——这些词在游戏文本里动词/名词/
# 方向/介词用法混杂（miss=未命中/想念/错过、right=右边/正确/右拨片），
# 审核沉淀若无语境强制全局，后续游戏同一词的其他语境会被改写（F22-4
# 三连杀实证：miss/encore/Right 各自杀死 100+ 条正常翻译）。
_HIGH_FREQUENCY_WORD_PAIRS = frozenset(
    "miss right left up down play stop save load locked charge exit enter "
    "open close start end back next ok yes no on off run jump attack hit "
    "throw use talk buy sell pick drop eat drink rest savegame resume "
    "health unit damage speed power".split()
    # health 2026-08-13 实证：force-reboot 沉淀 HEALTH→健康 active，
    # incremental-rts 'Increase unit HP by {health}' 译文「生命值」被
    # 误杀——health 在游戏语境变体多（健康/生命值/血量），单 token
    # 全局强制必误杀。unit/damage/speed/power 同族高频游戏词一并列入。
    # locked 2026-08-13 实证：Morfosi 64 条 "IT'S LOCKED." 被
    # ('Locked','锁定') 全灭——locked 语境变体多（锁定/上锁/被锁住），
    # 单 token 全局强制必误杀自然句
)


# 噪音专名形态（learn_proper_names 门禁，2026-08-13 Morfosi/isolated-
# inhale 实证）：颜色码（FFC400/FFFFFF/FDFD01——UI 主题色被
# collect_known_names 当专名收进清单）、调试占位（NULL/XXXX/JXXXX）、
# 重复字母堆积（AAAAAGHHHHHH——拟声尖叫/键盘乱串）。专名保留映射对
# 噪音无意义（跨游戏必不复现），学成 active 专名只污染术语表显示与
# prompt 注入。hex 全匹配 6-8 位（F30/N64/FC801 等型号含非 hex 字母
# 不受影响）。
_NOISE_TERM_RE = re.compile(
    r"^[0-9a-f]{6,8}$"                    # 颜色码（casefold 后）
    r"|^null$"                            # 调试占位 NULL
    r"|^(?:x{2,}|[a-z]{1,2}x{3,})$")      # XXXX / JXXXX 占位


def _is_noise_term(term: str) -> bool:
    if not term or not term.strip():
        return True
    if _NOISE_TERM_RE.match(term.strip().casefold()):
        return True
    letters = [c for c in term if c.isalpha()]
    # 重复字母堆积（AAAAAGHHHHHH 长 12 仅 {A,G,H} 三字母）：拟声尖叫/
    # 键盘乱串不是专名（GLISLYA/OMELEETETE 等真实专名 ≥4 不同字母）
    return len(letters) >= 8 and len(set(letters)) <= 3


class GlossaryStore:
    """全局术语表（SQLite，跨项目共享）。"""

    def __init__(self, db_path: str | Path):
        self.db = Path(db_path)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def init_schema(self):
        with self._lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS glossary(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                term TEXT UNIQUE, translation TEXT, category TEXT DEFAULT '术语',
                note TEXT DEFAULT ''
            );""")
            # 翻译 C5：审核沉淀语境保护——候选桶（candidate 只参考不强制）、
            # 沉淀游戏列表（跨游戏复现升级）、原文例句（语境留档）。
            # 翻译 C6（阶段 2 术语库升级）：forbidden_translation（禁止
            # 译法——审核检出错误译法）、part_of_speech（词性）、
            # game_specific_meaning（游戏特指义）、usage_example（用法例句，
            # format_for_prompt 附带语境提示）。
            # 老库迁移：缺列则 ALTER TABLE 补上。
            columns = {row["name"] for row in self.conn.execute(
                "PRAGMA table_info(glossary)")}
            for column, ddl in (
                    ("status", "TEXT DEFAULT 'active'"),
                    ("games", "TEXT DEFAULT ''"),
                    ("context", "TEXT DEFAULT ''"),
                    ("forbidden_translation", "TEXT DEFAULT ''"),
                    ("part_of_speech", "TEXT DEFAULT ''"),
                    ("game_specific_meaning", "TEXT DEFAULT ''"),
                    ("usage_example", "TEXT DEFAULT ''"),
                    # Phase B-3（审计 P1-4）：唯一语义 = 归一化 term +
                    # 语境键——"moon key" 与 "Moon Key" 视为同一术语，
                    # 异译不覆盖而是记冲突。旧库逐行回填。
                    ("term_norm", "TEXT DEFAULT ''"),
                    # #43 阶段 A（重构指令 §7/§17/§8）：术语置信度 + 生命
                    # 周期 + 优先级。全带 DEFAULT，旧行零迁移。
                    ("confidence", "REAL DEFAULT 1.0"),
                    ("priority", "INTEGER DEFAULT 0"),
                    ("source_ref", "TEXT DEFAULT ''")):
                if column not in columns:
                    self.conn.execute(
                        f"ALTER TABLE glossary ADD COLUMN {column} {ddl}")
            for row in self.conn.execute(
                    "SELECT id, term FROM glossary"
                    " WHERE term_norm IS NULL OR term_norm=''"):
                self.conn.execute(
                    "UPDATE glossary SET term_norm=? WHERE id=?",
                    (self._conflict_key(row["term"]), row["id"]))
            self.conn.commit()

    def add(self, term, translation, category="术语", note="",
            forbidden_translation="", part_of_speech="",
            game_specific_meaning="", usage_example=""):
        """入库（翻译 C6 字段升级）：扩展字段可空，向后兼容旧调用。"""
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO glossary"
                "(term, term_norm, translation, category, note,"
                " forbidden_translation, part_of_speech,"
                " game_specific_meaning, usage_example)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (term, self._conflict_key(term), translation, category, note,
                 forbidden_translation, part_of_speech,
                 game_specific_meaning, usage_example))
            self.conn.commit()

    def add_reviewed(self, term, translation, context: str = "",
                     game: str = "", forbidden_translation: str = ""
                     ) -> DepositResult:
        """审核沉淀专用门禁（翻译 C5 + 审计 Phase B-3）：结构化终态。

        F22-4 三连杀实证：审核沉淀 (miss,未命中)/(encore,安可)/(Right,右拨片)
        无门禁直写全局术语库，后续游戏强制约束把正常动词用法/外语语境
        全部改写（deadbeat 杀 doubleshake 动词用法 → 杀 faerie miss=想念；
        encore 杀法语；Right 杀 'pick the right door' 2083 条失败）——
        事后靠 quality.py 豁免补丁而非沉淀端预防。

        门禁规则（只作用于审核沉淀路径，人工/专名路径不受影响）：
        - 高频普通词单 token 词对（miss/right/play/…）：REJECTED——
          污染源（无语境可区分动词/名词/方向用法），不写入全局库；
        - 其他词对（含组合词）：CANDIDATE 进候选桶（参考不强制）；
        - 至少两个**独立游戏、相同译法** → ACTIVATED（升级 active，
          可注入 prompt/参与质量强制）；
        - 同（归一化 term）已有不同译法 → CONFLICT：不覆盖、不升级，
          冲突游戏不计入激活证据（否则第二游戏可把冲突词直接顶成
          active——P1-4 审计问题），冲突例句留档供人工复核；
        - 全部条目 note 载入语境（原文例句+来源游戏），不再只写
          「来源 X」。

        唯一语义 = 归一化 term（term_norm，大小写/空白/标点无关）+
        语境键（Phase B-4 拆出 canonical 前，以原文例句作冲突证据，
        不参与唯一性判定——本阶段由 detect_conflicts 与人工复核兜底）。
        """
        term_s = str(term).strip()
        trans_s = str(translation).strip()
        if not term_s or not trans_s:
            return DepositResult(REJECTED, "空词对", term=term_s)
        norm = self._conflict_key(term_s)
        if not norm:
            # 纯标点/符号（"!!!"）无归一化身份，无法跨游戏识别
            return DepositResult(REJECTED, f"拒绝沉淀：{term_s!r} 无归一化词形",
                                 term=term_s)
        # 整句/长短语拒绝（2026-08-13 isolated-inhale 多语言盲区实证）：
        # 审核把整句（'Docke an die Sauerstoffstation an' 6 词 /
        # 'Premi Invio per continuare...' 句尾省略号）当术语建议沉淀
        # active——术语表只收词级术语，整句是无上下文可验证的长串
        # （多语言盲区译文不可信，'Premi' 被误译「提交奖励」），沉淀后
        # 强制约束后续翻译必误用。≥5 词或以句尾标点结尾 → 拒绝。
        if (len(term_s.split()) >= 5
                or term_s.rstrip().endswith((".", "!", "?", "…", "。",
                                             "！", "？"))):
            return DepositResult(
                REJECTED,
                f"拒绝沉淀：{term_s!r} 是整句/长短语，非词级术语"
                f"（无上下文可验证，强制约束必误用）", term=term_s)
        # 译文含富文本标记（</color>/</>/&gt;——isolated-inhale 实证：
        # '高亮按钮</color>以显示可用的命令列'）→ 提取的是文本片段不是
        # 术语译名（审核把整句上下文当译名建议）
        if "<" in trans_s or ">" in trans_s:
            return DepositResult(
                REJECTED,
                f"拒绝沉淀：{term_s!r} 的译名含富文本标记"
                f"（{trans_s[:40]!r}——文本片段不是术语译名）", term=term_s)
        if (" " not in term_s
                and term_s.casefold() in _HIGH_FREQUENCY_WORD_PAIRS):
            return DepositResult(
                REJECTED,
                f"拒绝沉淀：{term_s!r} 是高频普通词单 token 词对"
                f"（无语境可区分动词/名词/方向用法，全局强制会误杀"
                f"其他语境——F22-4 三连杀实证）", term=term_s)
        # BUILTIN 冲突门禁（2026-09-01 记忆库/知识库污染系统性根治）：
        # 单 token 原文命中内置 UI 权威表且译文与权威不符（Disabled→
        # 残疾人士）→ 拒绝沉淀。审核把 UI 状态标签误判成「残疾」是
        # 语义错误，沉淀成全局词对会让后续游戏全部误用。权威译名
        # （已禁用/已启用）由内置表 + 确定性直填恒胜出，此门防坏
        # 译名成为候选/激活。
        if builtin_ui_conflict(term_s, trans_s):
            authoritative = BUILTIN_UI_EXACT.get(term_s.strip().casefold())
            return DepositResult(
                REJECTED,
                f"拒绝沉淀：{term_s!r} 是内置 UI 术语，权威译名应为"
                f"{authoritative!r}（不是 {trans_s!r}——'Disabled' 是"
                f"控件启用状态标签，审核判错会污染全局术语）",
                term=term_s)
        games = [g for g in re.split(r"[,，]", game or "") if g]
        note = f"来源 {game or '?'}"
        if context:
            note += f" · 例句: {context[:120]}"
        with self._lock:
            row = self.conn.execute(
                "SELECT id, translation, status, games, note FROM glossary"
                " WHERE term_norm=? ORDER BY id LIMIT 1", (norm,)).fetchone()
            if row is not None:
                existing_games = [g for g in re.split(r"[,，]", row["games"] or "")
                                  if g]
                same_translation = (
                    str(row["translation"] or "").casefold()
                    == trans_s.casefold())
                if not same_translation:
                    # 同源异译（Phase B-3）：不覆盖、不升级、冲突游戏
                    # 不计数。冲突证据留档（第二例句 + 冲突源），供
                    # detect_conflicts / 人工复核；games 保持纯净
                    # （只含确认相同译法的游戏）。
                    conflict_note = row["note"] or ""
                    if context:
                        conflict_note += (f" · 冲突例句: {context[:120]}"
                                          f"（来源 {game or '?'}）")
                    self.conn.execute(
                        "UPDATE glossary SET note=? WHERE id=?",
                        (conflict_note, row["id"]))
                    self.conn.commit()
                    return DepositResult(
                        CONFLICT,
                        f"同源异译：{term_s!r} 已有译法"
                        f" {row['translation']!r}，本次 {trans_s!r} 不覆盖"
                        f"（冲突游戏不计入激活证据，需人工复核）",
                        games=tuple(existing_games), term=term_s)
                merged = list(dict.fromkeys(existing_games + games))
                status = row["status"] or "active"
                # Phase B-3（审计 P1-4）：激活必须「至少两个独立游戏、
                # 相同译法」——同游戏重复沉淀不升级，冲突游戏更不升级。
                if status != "active" and len(merged) >= 2:
                    status = "active"
                # forbidden_translation 只在传入时刷新（空值不抹已有禁止译法）
                # #43 阶段 A：跨游戏激活（status→active）置信度升 0.95
                # （两独立游戏同译法复现，可信度高于单次审核沉淀）
                new_confidence = 0.95 if status == "active" else 0.85
                if forbidden_translation:
                    self.conn.execute(
                        "UPDATE glossary SET translation=?, note=?, games=?,"
                        " status=?, context=?, forbidden_translation=?,"
                        " confidence=? WHERE id=?",
                        (trans_s, note, ",".join(merged), status, context,
                         forbidden_translation, new_confidence, row["id"]))
                else:
                    self.conn.execute(
                        "UPDATE glossary SET translation=?, note=?, games=?,"
                        " status=?, context=?, confidence=? WHERE id=?",
                        (trans_s, note, ",".join(merged), status, context,
                         new_confidence, row["id"]))
                self.conn.commit()
                if status == "active":
                    return DepositResult(
                        ACTIVATED, "", games=tuple(merged), term=term_s)
                return DepositResult(
                    CANDIDATE, "", games=tuple(merged), term=term_s)
            # 新词对：一律进 candidate 桶（Phase B-3 取消组合词直通
            # active——任何词对的激活都要求跨游戏复现同译法，杜绝
            # 单源沉淀即强制）。#43 阶段 A：审核沉淀置信度 0.85
            # （重构指令 §7：审核通过 = 0.85，低于人工确认 1.0）
            self.conn.execute(
                "INSERT OR REPLACE INTO glossary"
                "(term, term_norm, translation, category, note, status,"
                " games, context, forbidden_translation, confidence)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (term_s, norm, trans_s, "审核术语", note, "candidate",
                 ",".join(games), context, forbidden_translation, 0.85))
            self.conn.commit()
            return DepositResult(CANDIDATE, "", games=tuple(games),
                                 term=term_s)

    def update(self, term, translation, category, note=""):
        with self._lock:
            self.conn.execute("UPDATE glossary SET translation=?, category=?, note=? WHERE term=?",
                              (translation, category, note, term))
            self.conn.commit()

    def set_status(self, term, status: str) -> None:
        """人工切换条目状态（候选 ↔ 生效），仅接受 active/candidate。

        候选词对（审核沉淀未跨游戏复现）用户可在术语库确认升级为生效；
        生效词对也可降回候选（觉得该词对当前游戏语境不合适时）。
        """
        if status not in ("active", "candidate"):
            return
        with self._lock:
            self.conn.execute(
                "UPDATE glossary SET status=? WHERE term=?", (status, term))
            self.conn.commit()

    def delete(self, term):
        with self._lock:
            self.conn.execute("DELETE FROM glossary WHERE term=?", (term,))
            self.conn.commit()

    def list_all(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM glossary ORDER BY id")]

    def by_category(self, category: str) -> list[str]:
        with self._lock:
            return [r["term"] for r in self.conn.execute(
                "SELECT term FROM glossary WHERE category=?", (category,))]

    def format_for_prompt(self, limit: int = 0) -> str:
        rows = self.list_all()
        if not rows:
            return ""
        # 翻译 C5：只注入 active 条目——candidate 桶（审核沉淀未跨游戏
        # 复现的词对）仅参考不强制：候选可能在当前游戏恰好是错误语境
        # （如 miss=未命中 沉淀自音游，却在剧情游戏里是 想念），注入为
        # 强制约束会误杀；active 条目已跨游戏复现或为组合词对，语境充分。
        rows = [r for r in rows if r.get("status", "active") == "active"]
        if limit > 0:
            # 全局术语库跨游戏持续积累后可能很大；注入 prompt 只取最新 limit 条
            # （ID 单调递增，最新学习的最贴近当前需求）。
            rows = rows[-limit:]
        # 翻译 C6：usage_example 存在时附带用法例句（消歧提示，仅提示不
        # 强制——model 参考词义而非复制句子）。
        lines = []
        for r in rows:
            line = f"{r['term']} → {r['translation']}（{r['category']}）"
            if r.get("usage_example"):
                line += f" 例：{r['usage_example'][:60]}"
            lines.append(line)
        return "\n".join(lines)

    def known_names_for(self, collected: list[str] | None = None) -> list[str]:
        """专名注入清单：当前游戏收集的专名优先，全局术语库专名兜底。

        术语库的专名条目（category='专名'）跨游戏积累——后续游戏遇到
        同名专名时，即使当前池子未收集到，也能保持译名一致。
        """
        names: list[str] = []
        seen: set[str] = set()
        for n in (collected or []):
            if n not in seen:
                names.append(n)
                seen.add(n)
        for row in self.list_all():
            if row["category"] == "专名" and row["term"] not in seen:
                names.append(row["term"])
                seen.add(row["term"])
        return names[:50]

    def learn_proper_names(self, entries, names: list[str],
                           source_game: str) -> int:
        """从已确认翻译中学习专名（保留型）写入全局术语库。

        输入：全部条目 + 疑似专名清单。仅使用质量门通过的 translated 条目
        作为证据：专名在其原文中多次出现、且译文保留了原文形态（未误译、
        未丢失），则记「term → 原文」保留映射——后续游戏命中该词时，
        [术语命中] 强制该词保留原文，防止 HY-MT2 丢失/意译专名。

        音译型（译文为中文）无法可靠定位对应片段，不自动提取（人工可在
        术语库补充）。返回新学习条数。
        """
        evidence: dict[str, dict] = {}
        for e in entries:
            if e.status != "translated" or not e.translation:
                continue
            if not e.meta.get("quality_passed"):
                continue
            for n in names:
                # 噪音形态（颜色码/占位/尖叫）不是专名：学成保留映射只
                # 污染术语表（NULL/XXXX/FFC400/AAAAAGHHHHHH 实证——UI
                # 主题色与调试串被收进专名清单）——跨游戏必不复现
                if _is_noise_term(n):
                    continue
                # 动作动词不是专名：TOSS TRASH 的 TOSS 是动作指令文本的词，
                # 学成专名后「TOSS → TOSS」保留映射会与知识库译例
                # 「TOSS TRASH → 丢垃圾」在 references 里冲突，模型采纳
                # 专名保留 → 输出半翻译 TOSS 垃圾（taxes 实证）
                if n.casefold() in _UPPERCASE_ACTION_VERBS:
                    continue
                if n in e.original:
                    ev = evidence.setdefault(n, {"total": 0, "kept": 0})
                    ev["total"] += 1
                    # 保留检测大小写不敏感（模型可能保留为 Glislya 变体）
                    if n.casefold() in e.translation.casefold():
                        ev["kept"] += 1
        learned = 0
        for n, ev in evidence.items():
            if ev["total"] >= 1 and ev["kept"] >= ev["total"] * 0.5:
                with self._lock:
                    row = self.conn.execute(
                        "SELECT id, translation FROM glossary WHERE term=?",
                        (n,)).fetchone()
                    if row is not None:
                        # 已存在：仅当旧条目无译名证据时刷新来源备注
                        if not row["translation"]:
                            self.conn.execute(
                                "UPDATE glossary SET note=? WHERE id=?",
                                (f"auto:{source_game}:保留", row["id"]))
                    else:
                        self.conn.execute(
                            "INSERT OR REPLACE INTO glossary"
                            "(term, translation, category, note)"
                            " VALUES (?,?,?,?)",
                            (n, n, "专名", f"auto:{source_game}:保留"))
                        learned += 1
        self.conn.commit()
        return learned

    @staticmethod
    def _conflict_key(term: str) -> str:
        return _CONFLICT_NORM.sub("", term.strip().casefold())

    def detect_conflicts(self) -> list[dict]:
        """同源异译冲突检测（P2）：大小写/空白/标点变体视为同源，
        同源但译名不同的条目返回冲突组（供人工合并修订）。

        返回: [{"key": 归一化键, "rows": [同源条目 dict, ...]}, ...]
        每组至少 2 个不同译名才上报。
        """
        buckets: dict[str, list[dict]] = {}
        for row in self.list_all():
            key = self._conflict_key(row["term"])
            if key:
                buckets.setdefault(key, []).append(row)
        return [
            {"key": key, "rows": rows}
            for key, rows in buckets.items()
            if len({r["translation"] for r in rows}) > 1
        ]

    def close(self):
        with self._lock:
            self.conn.close()
