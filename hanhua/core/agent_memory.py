"""AgentMemory：跨游戏经验记忆——把工具变为有记忆的翻译 agent。

三层记忆架构（2026-08-12 用户指令「设计记忆模块，使其变成一个真正的
agent」）：
  权威知识（人工）  GlossaryStore / KnowledgeStore —— 人工确认、权威
  经验记忆（自动）  AgentMemory（本模块）—— 证据驱动、语境敏感、跨游戏持久
  工作记忆（会话）  ProjectStore.memory —— 会话缓存（速度，保留现状）

核心设计（用户关键要求「必须记住好的内容，按具体情况翻译」）：
  1. 只记住好的内容：只有质量门通过 + 非回显（echo_exempt 排除）的
     翻译才能提案；单次翻译只是提案（pending），同一（原文, 语境）
     在 ≥2 条证据上译文一致才晋升 active 参与运用——杜绝「一次巧合
     进全局记忆」。
  2. 按具体情况翻译（语境敏感）：同一原文在不同语境译法不同——
     'Resume' 按钮（role=ui_button）→ 继续游戏；'Resume' 名词
     （role=display）→ 简历。记忆单元 = (type, key, context_key)
     三元组唯一；同 key 不同语境是独立记忆，运用时精确语境匹配
     优先，多语境分化的原文绝不用无语境记忆兜底（防污染）。
  3. 直接运用（混合模式，用户确认）：phrase 型高置信记忆
     （evidence ≥ 3 + 零拒绝 + 跨游戏或人工）在翻译前直接作为译文
     应用（仍过质量门复查）；一般置信（active）注入 prompt 参考。
     term 型（≤2 词短词）绝不直接应用，只注入参考——单字词对是
     术语污染源（miss/right 事故），必须留出人工判断。
  4. 反馈闭环：直接应用的译文被质量门拒绝 → rejects+1 → 退休
     （rejects ≥ 2）；采纳 → hits+1。降级/退休在报告中可见，
     用户可追踪哪些记忆不可信。
  5. 离散知识：不记住游戏，记住（原文, 语境）→ 译文 与 形态 → 处置
     的可跨游戏复用单元。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from hanhua.core.placeholders import FUNCTION_WORDS
from hanhua.core.prompts import _GAME_CONTEXT_WORDS
from hanhua.core.translator import (BUILTIN_UI_REFERENCES,
                                    builtin_ui_conflict)

# 单字词保护集（2026-08-14 全量审校实证：play→播放 污染事故）：
# 内置 UI 术语（BUILTIN_UI_REFERENCES）与游戏语境歧义词
# （_GAME_CONTEXT_WORDS）的正确译法由人工维护、随 prompt 注入；
# 自动沉淀的记忆单字对若与之冲突，几乎总是错的那个（Play→播放
# 记忆注入 prompt 参考后覆盖了内置 play→开始游戏 规则）。reference
# 注入前过滤冲突单字词——内置规则胜出。与「术语污染教训」一致：
# 单字对是污染源，组合词对才可全局注入（非冲突单字词如 Reroll→重掷
# 不受影响）。
_PROTECTED_SINGLE_WORDS = frozenset(
    {str(s).casefold() for s, _ in BUILTIN_UI_REFERENCES}
    | {str(s).casefold() for s, _, _ in _GAME_CONTEXT_WORDS})

# 纯专名词表（组合词对跨游戏安全注入白名单，2026-09-01）：
# 花札/卡牌/角色等专名译名固定无歧义，可作全局参考——即使其他游戏
# 出现同原文（如某卡牌游戏的 'AoTan'），译成「青短」也是合理固定名。
# 与「术语污染教训 C6」区分：C6 是动作/方向/设备词（Left→左摇杆）的
# 语境依赖污染，这里是**固有专名**（Deck 是通用名词 ≠ AoTan 是专名）。
# 动作/操作短语（'Deck Play'/'Call Koi Koi'）不在表中 → 被
# _context_preserve_ok 拦截不注入。
_PURE_PROPER_NOUN = frozenset({
    # 花札卡面专名（KoiKoi）
    "Matsu no Tsuru", "Ume no Uguisu", "Sakura no Maku", "Fuji no Fujoki",
    "Ayame no Hashi", "Botan no Chou", "Hagi no Inoshishi",
    "Susuki no Tsuki", "Susuki no Gan", "Kiku no Sake", "Momiji no Shika",
    "Yanagi no Michikaze", "Yanagi no Tsubame", "Kiri no Houou",
    "AoTan", "AkaTan", "Ame Shikou",
})

# 通用 UI 组合短语白名单（组合词对跨游戏安全注入）：主菜单/结算/操作
# 提示等任何游戏含义一致、语境独立的高频短语。与 _PURE_PROPER_NOUN
# 的专名固定译名并列——这些短语也满足「跨游戏复用不误伤」。
_COMMON_UI_PHRASES = frozenset({
    "press start", "play with me", "new game", "continue",
    "you win", "you lose", "press any key", "main menu",
    "options", "credits", "restart", "high score", "game over",
})

# ── 阈值（记忆生命周期） ─────────────────────────────────────────────
ACTIVE_MIN_EVIDENCE = 2    # pending → active 的证据门槛（参与注入）
DIRECT_APPLY_MIN_EVIDENCE = 3  # 直接应用的最低证据（多游戏验证级别）
RETIRE_MAX_REJECTS = 2     # 被质量门拒绝 N 次 → 退休（不可信）
TERM_MAX_WORDS = 1         # 单字词（Resume/miss/Save）绝不全自动应用——
                           # 语境依赖最强、术语污染源（ffs 事故），只注入
REPORT_TOP = 5             # 报告里展示的 TOP 记忆数
# 人工证据权重（Phase B-2，审计 §6 P1-6）：人改即终局——人工修正
# 直接以 active + 满证据落库，不经证据积累即满足直接应用准入
# （evidence ≥ 3 + 零拒绝 + source=manual，见 direct_applications）。
MANUAL_EVIDENCE = 3

# 单 token 英文功能词（介词/连词/助动词/高频副词）——绝不晋升 active：
# 做全局强制词对必然误杀自然文本（honorplusplus 实证：ON→关于/on→在/
# off→关闭 沉淀 active 后，incremental-rts 的 'Analytics is ON.'、
# URL 行 '...on+gnu%2Blinux'、inch-by-inch 的 'Start Ingredients' 全被
# 误杀）。与审核沉淀 C5 门禁（高频普通词单 token 不入全局库）对齐：
# 翻译端自动沉淀同样有功能词污染缺口（2026-08-13 补）。keep pending
# 可人工复核，不删除记录。共享定义在 placeholders.FUNCTION_WORDS
# （quality 检查端同表过滤，防漂移）。


def context_key_of(role: str = "", morph: str = "") -> str:
    """语境归一化键：role 与形态组合（固定顺序），空值省略。

    'Resume' 按钮（role=ui_button）与 'Resume' 名词（role=display）是
    不同语境键 → 独立记忆单元，互不覆盖、互不串用。
    """
    parts = []
    if role:
        parts.append(f"r:{role}")
    if morph:
        parts.append(f"m:{morph}")
    return "|".join(parts)


class AgentMemory:
    """跨游戏经验记忆库（SQLite，app_dir/agent_memory.db）。"""

    def __init__(self, db_path: str | Path):
        self.db = Path(db_path)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # 会话统计（翻译开始前 reset，报告时取）——不落库，纯运行期
        self._session: dict[str, int] = {}
        self._session_proposals: list[dict] = []  # 本会话新增提案快照
        # 延迟提案队列（翻译批处理：逐条 propose 落库太慢，flush 批量）
        self._deferred: list[tuple] = []

    def init_schema(self):
        with self._lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                key TEXT NOT NULL,
                context_key TEXT NOT NULL DEFAULT '',
                value TEXT NOT NULL,
                context TEXT NOT NULL DEFAULT '{}',
                evidence_count INTEGER NOT NULL DEFAULT 1,
                games TEXT NOT NULL DEFAULT '[]',
                hits INTEGER NOT NULL DEFAULT 0,
                rejects INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                source TEXT NOT NULL DEFAULT 'auto',
                source_game TEXT NOT NULL DEFAULT '',
                conflicts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                last_used_at TEXT NOT NULL DEFAULT '',
                UNIQUE(type, key, context_key)
            );""")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem_status"
                " ON memories(status, evidence_count)")
            self.conn.commit()

    @staticmethod
    def _now() -> str:
        import datetime as _dt
        return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _games_of(row) -> set[str]:
        try:
            return set(json.loads(row["games"]))
        except (ValueError, TypeError):
            return set()

    # ── 写入：提案 → 证据积累 → 晋升 active ──────────────────────────

    def propose(self, key: str, value: str, game: str, *,
                role: str = "", morph: str = "", source: str = "auto",
                type_: str = "phrase") -> None:
        """质量门通过 + 非回显的译文提案（证据 +1）。

        key=原文（phrase）或短词（term）；role/morph 构成语境键。
        同一 (type, key, context_key) 再次出现：
          - 译文相同 → evidence+1，games 去重合并；
          - 译文不同 → 不覆盖（不同译文是语境分化信号，交给
            detect_conflicts 报告，人工/语境键区隔）。
        证据 ≥ ACTIVE_MIN_EVIDENCE 且 rejects == 0 → 晋升 active。
        """
        if not key or not value:
            return
        if builtin_ui_conflict(key, value):
            # BUILTIN 冲突门禁（2026-09-01 记忆污染系统性根治）：
            # 单 token 原文命中内置 UI 权威表且译文与权威不符（如
            # Disabled→残疾人士）→ 拒绝沉淀——自动记忆是后续游戏
            # 的参考注入源，坏译文落库会覆盖内置规则（play→播放
            # 事故同族）。与 reference_pairs 的 _PROTECTED_SINGLE_
            # WORDS 过滤闭环：入口不沉淀，注入端不注入。
            self._session["blocked_builtin_conflicts"] = \
                self._session.get("blocked_builtin_conflicts", 0) + 1
            return
        ckey = context_key_of(role, morph)
        now = self._now()
        with self._lock:
            row = self.conn.execute(
                "SELECT id, value, evidence_count, status, rejects, games"
                " FROM memories WHERE type=? AND key=? AND context_key=?",
                (type_, key, ckey)).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO memories"
                    "(type, key, context_key, value, context, evidence_count,"
                    " games, source, source_game, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (type_, key, ckey, value,
                     json.dumps({"role": role, "morph": morph},
                                ensure_ascii=False),
                     1, json.dumps([game], ensure_ascii=False),
                     source, game, now, now))
                self._session["proposed"] = self._session.get("proposed", 0) + 1
                self._session_proposals.append(
                    {"type": type_, "key": key, "value": value,
                     "context_key": ckey, "game": game})
            else:
                if row["value"] != value:
                    # 同语境同原文不同译文：不覆盖、不积累证据——证据
                    # 不一致 = 记忆不可靠，conflicts+1 落库（报告可见，
                    # 人工复核），保留首译文
                    self.conn.execute(
                        "UPDATE memories SET conflicts=conflicts+1,"
                        " updated_at=? WHERE id=?",
                        (now, row["id"]))
                    self.conn.commit()
                    self._session["conflicts"] = \
                        self._session.get("conflicts", 0) + 1
                    return
                games = self._games_of(row) | {game}
                evidence = int(row["evidence_count"]) + 1
                status = row["status"]
                if status == "pending" and evidence >= ACTIVE_MIN_EVIDENCE \
                        and int(row["rejects"]) == 0:
                    if key.casefold() in FUNCTION_WORDS:
                        # 功能词晋升拦截（2026-08-13）：保持 pending——
                        # 不进 reference_pairs，不参与质量强制；记录
                        # 计数（报告可见），保留记录可人工复核。
                        # 功能词（on/off）做参考/强制均无价值且必误杀
                        # 自然文本（incremental-rts 'Analytics is ON.'
                        # 实证）。高频普通词（miss/health）不在此拦：
                        # 它们做参考注入有语境价值（test_term_never_
                        # direct_applied 固化 miss 只注入参考），强制
                        # 过滤在 quality 检查端（F10）
                        self._session["blocked_function_words"] = \
                            self._session.get("blocked_function_words", 0) + 1
                    else:
                        status = "active"
                        self._session["confirmed"] = \
                            self._session.get("confirmed", 0) + 1
                self.conn.execute(
                    "UPDATE memories SET evidence_count=?, games=?, status=?,"
                    " updated_at=? WHERE id=?",
                    (evidence, json.dumps(sorted(games), ensure_ascii=False),
                     status, now, row["id"]))
                self._session["evidence_added"] = \
                    self._session.get("evidence_added", 0) + 1
            self.conn.commit()

    def propose_deferred(self, key: str, value: str, game: str, *,
                         role: str = "", morph: str = "", source: str = "auto",
                         type_: str = "phrase") -> None:
        """延迟提案：进队列，flush() 时批量入库（翻译批处理性能）。"""
        self._deferred.append(
            (type_, key, value, game, role, morph, source))

    def flush(self) -> None:
        """批量处理延迟提案（翻译批 flush 时调用）。"""
        if not self._deferred:
            return
        pending, self._deferred = self._deferred, []
        for type_, key, value, game, role, morph, source in pending:
            self.propose(key, value, game, role=role, morph=morph,
                         source=source, type_=type_)

    def propose_many(self, rows: list[tuple], game: str = "", *,
                     role: str = "", source: str = "auto",
                     type_: str = "phrase") -> None:
        """批量提案（GUI/runner 收尾或测试直调）。rows: [(key, value)]"""
        for key, value in rows:
            self.propose(key, value, game, role=role, source=source,
                         type_=type_)

    def upsert_manual(self, key: str, value: str, game: str, *,
                      role: str = "", morph: str = "",
                      type_: str = "phrase") -> None:
        """人工修正最高权重写入：覆盖/新建记忆为 active 终态。

        与 propose（同译文才积累证据、异译文记 conflicts）不同——人工
        裁决终结冲突：已有记录（含 retired / conflicts>0）一律覆盖为
        source=manual、evidence=MANUAL_EVIDENCE、rejects=0、
        conflicts=0、status=active，同（type, key, context_key）自此
        以人工译文为准（人改即终局，审计 Phase B-2）。
        """
        if not key or not value:
            return
        ckey = context_key_of(role, morph)
        now = self._now()
        with self._lock:
            row = self.conn.execute(
                "SELECT games FROM memories"
                " WHERE type=? AND key=? AND context_key=?",
                (type_, key, ckey)).fetchone()
            games = sorted(self._games_of(row) | {game}) if row else [game]
            self.conn.execute(
                "INSERT INTO memories(type, key, context_key, value, context,"
                " evidence_count, games, source, source_game,"
                " created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(type, key, context_key) DO UPDATE SET"
                " value=excluded.value, evidence_count=excluded.evidence_count,"
                " rejects=0, conflicts=0, status='active',"
                " source='manual', source_game=excluded.source_game,"
                " games=excluded.games, updated_at=excluded.updated_at",
                (type_, key, ckey, value,
                 json.dumps({"role": role, "morph": morph},
                            ensure_ascii=False),
                 MANUAL_EVIDENCE, json.dumps(games, ensure_ascii=False),
                 "manual", game, now, now))
            self._session["manual_applied"] = \
                self._session.get("manual_applied", 0) + 1
            self.conn.commit()

    # ── 运用：翻译前直接应用（混合模式的高置信档） ───────────────────

    def direct_applications(self, originals: list[str],
                            role_by_original: dict[str, str] | None = None
                            ) -> dict[str, str]:
        """高置信 phrase 记忆的直接应用：{原文: 译文}。

        准入：type=phrase + status=active + evidence ≥ 3 + rejects == 0
        +（跨游戏 games ≥ 2 或 source=manual）。
        语境匹配顺序：
          1) 精确语境（role 相同）命中 → 用该语境记忆；
          2) 无精确语境 → 原文存在**唯一**无语境记忆（context_key=''）
             时兜底；
          3) 原文存在多个语境记忆（已语境分化）→ 不兜底（防 Resume
             按钮记忆污染对话文本）。
        """
        if not originals:
            return {}
        out: dict[str, str] = {}
        roles = role_by_original or {}
        with self._lock:
            for original in originals:
                # 直接应用只对多词短语（>1 词）：单字词（Resume/miss）
                # 语境依赖最强、是术语污染源（ffs 事故），全自动应用
                # 会跨语境误用——只允许参考注入
                if len(str(original).split()) <= TERM_MAX_WORDS:
                    continue
                rows = self.conn.execute(
                    "SELECT key, context_key, value, evidence_count,"
                    " rejects, status, games, source"
                    " FROM memories WHERE type='phrase' AND key=?"
                    " AND status='active'", (original,)).fetchall()
                if not rows:
                    continue
                role = roles.get(original, "")
                # 精确语境优先
                want = context_key_of(role)
                chosen = next((r for r in rows if r["context_key"] == want),
                              None)
                if chosen is None and len(rows) == 1:
                    # 无精确语境且原文只有一条记忆 → 唯一记忆兜底
                    # （尚未语境分化，用其译法安全）；多条记忆（已分化
                    # 成多语境）→ 不兜底（防 Resume 按钮记忆污染对话）
                    chosen = rows[0]
                if chosen is None:
                    continue
                # 1 次拒绝后仍给一次机会（可能是质量门误杀）；
                # ≥2 次（RETIRE_MAX_REJECTS）确认不可信 → 退休停用
                if int(chosen["rejects"]) >= RETIRE_MAX_REJECTS:
                    continue
                if int(chosen["evidence_count"]) < DIRECT_APPLY_MIN_EVIDENCE:
                    continue
                games = self._games_of(chosen)
                if len(games) < 2 and chosen["source"] != "manual":
                    continue
                out[original] = chosen["value"]
                self._session["direct_applied"] = \
                    self._session.get("direct_applied", 0) + 1
        return out

    # ── 运用：注入 prompt 参考（混合模式的参考档） ───────────────────

    @staticmethod
    def _context_preserve_ok(source: str, target: str) -> bool:
        """组合词对（含空格）是否可作全局参考注入。

        KoiKoi 实证（2026-09-01）：组合词对也会被跨游戏复用误伤——
        'Deck Play'（花札「牌堆出牌」）被提成 (Deck, 牌组) 后，其他游戏
        'Deck'（卡牌构筑 deck）被强制成「牌组」，语境不通。组合词对只在
        满足以下条件之一时注入：
          - target 含权威中文（'Koi Koi'→'Koi Koi' 保留型直接回显可注）；
          - source 全大写（'YOU WIN!'→'你赢了' 是通用 UI 全大写提示，
            语境独立）；
          - source 是**纯专名**（卡牌/武器/角色名——'AoTan'→'青短'），
            由 _PURE_PROPER_NOUN 词表界定，译名固定无歧义。
        其余组合词对（动作/操作短语如 'Deck Play'/'Call Koi Koi'）跳过
        注入，避免跨游戏语境污染（术语污染教训 C6 组合词对的语境依赖）。
        """
        src_s = str(source or "").strip()
        tgt_s = str(target or "").strip()
        if " " not in src_s:
            return True                 # 单 token 走 _PROTECTED_SINGLE_WORDS
        if not tgt_s or tgt_s.casefold() == src_s.casefold():
            return True                 # 保留型（itch→itch / Koi Koi→Koi Koi）
        if src_s.isupper():
            return True                 # 全大写通用提示（YOU WIN!）
        if src_s in _PURE_PROPER_NOUN:
            return True                 # 纯专名（AoTan→青短，译名固定）
        # 通用 UI 组合短语白名单（跨游戏高频、语境独立）：
        # Press Start/Play with me/New Game/You Win/You Lose 等主菜单
        # 与结算提示，任何游戏含义一致，注入安全（语境污染 vs 有用参考
        # 权衡——这类短语全大写提示在 metadata 也常见，翻译受益明显）。
        if src_s.casefold() in _COMMON_UI_PHRASES:
            return True
        return False

    def reference_pairs(self, limit: int = 0) -> list[tuple[str, str]]:
        """active 记忆的 (原文, 译文) 参考对，注入翻译 prompt。

        term 型（≤2 词）与 phrase 型都注入（模型在完整语境中判断，
        优于 glossary 强制约束——参考而非强制）。hits 高的靠前。

        2026-08-14（play→播放 事故）：与内置人工规则（BUILTIN_UI_
        REFERENCES / _GAME_CONTEXT_WORDS）冲突的单字词记忆不注入——
        内置规则随 prompt 恒在，冲突记忆只会覆盖正确规则（参考译例
        对模型比规则更显眼）。

        2026-09-01（KoiKoi 组合词对污染）：组合词对也加语境保护——
        非保留型/非全大写/非纯专名的动作短语（'Deck Play'→牌堆出牌
        被提炼成 Deck→牌组 污染跨游戏）不注入；纯专名（'AoTan'→
        '青短'）译名固定可全局注入。
        """
        rows = self.conn.execute(
            "SELECT key, value, hits FROM memories"
            " WHERE status='active' ORDER BY hits DESC, evidence_count DESC"
        ).fetchall()
        pairs = [(r["key"], r["value"]) for r in rows
                 if (len(str(r["key"]).split()) > TERM_MAX_WORDS
                     or str(r["key"]).casefold()
                     not in _PROTECTED_SINGLE_WORDS)
                 and self._context_preserve_ok(r["key"], r["value"])]
        if limit > 0:
            pairs = pairs[:limit]
        return pairs

    # ── 反馈闭环：应用结果 → 证据升降级 ─────────────────────────────

    def apply_feedback(self, key: str, context_key: str, *,
                       accepted: bool, type_: str = "phrase") -> None:
        """直接应用的记忆反馈：采纳 → hits+1；拒绝 → rejects+1 → 退休。

        被质量门拒绝 = 记忆不可信（该原文在真实语境被判失败）——
        rejects ≥ RETIRE_MAX_REJECTS → status=retired，不再参与任何
        运用。retired 记忆保留记录（报告可查），不删除（可人工复核
        后手动改回）。
        """
        now = self._now()
        with self._lock:
            row = self.conn.execute(
                "SELECT id, hits, rejects, status FROM memories"
                " WHERE type=? AND key=? AND context_key=?",
                (type_, key, context_key)).fetchone()
            if row is None:
                return
            if accepted:
                self.conn.execute(
                    "UPDATE memories SET hits=hits+1, last_used_at=?,"
                    " updated_at=? WHERE id=?",
                    (now, now, row["id"]))
                self._session["accepted"] = self._session.get("accepted", 0) + 1
            else:
                rejects = int(row["rejects"]) + 1
                status = "retired" if rejects >= RETIRE_MAX_REJECTS \
                    else row["status"]
                if status == "retired":
                    self._session["retired"] = \
                        self._session.get("retired", 0) + 1
                self.conn.execute(
                    "UPDATE memories SET rejects=?, status=?, updated_at=?"
                    " WHERE id=?",
                    (rejects, status, now, row["id"]))
                self._session["rejected"] = self._session.get("rejected", 0) + 1
            self.conn.commit()

    # ── 冲突检测：同 key 多译文（语境分化或污染信号） ───────────────

    def detect_conflicts(self) -> list[dict]:
        """同语境同原文出现不同译文 → 冲突组（报告/人工复核）。

        语境分化（同 key 不同 context_key 不同译文，Resume 按钮/名词）
        是正常现象、不算冲突；只有**同一语境**内译文反复不一
        （conflicts 计数）才上报——那是记忆污染信号。
        """
        rows = self.conn.execute(
            "SELECT key, context_key, value, conflicts, evidence_count,"
            " hits, status FROM memories"
            " WHERE conflicts > 0 ORDER BY conflicts DESC").fetchall()
        return [dict(r) for r in rows]

    # ── 会话统计与报告 ──────────────────────────────────────────────

    def session_reset(self):
        """翻译会话开始前重置统计。"""
        with self._lock:
            self._session = {}
            self._session_proposals = []

    def session_report(self, game: str = "") -> dict:
        """本会话记忆活动报告（写入记录文档，用户第一眼可见记忆在
        如何成长）。返回结构化 dict。"""
        with self._lock:
            counts = {s: self._session.get(s, 0) for s in (
                "proposed", "evidence_added", "confirmed", "conflicts",
                "direct_applied", "accepted", "rejected", "retired",
                "blocked_function_words", "blocked_builtin_conflicts",
                "manual_applied")}
            rows = self.conn.execute(
                "SELECT type, status, COUNT(*) c FROM memories"
                " GROUP BY type, status ORDER BY type, status").fetchall()
            library = {}
            for r in rows:
                library.setdefault(r["type"], {})[r["status"]] = r["c"]
            conflicts = self.detect_conflicts()
            top = self.conn.execute(
                "SELECT key, context_key, value, evidence_count, hits,"
                " rejects, games FROM memories WHERE status='active'"
                " ORDER BY hits DESC, evidence_count DESC LIMIT ?",
                (REPORT_TOP,)).fetchall()
            top_list = []
            for r in top:
                item = {"key": r["key"], "context_key": r["context_key"],
                        "value": r["value"],
                        "evidence": r["evidence_count"], "hits": r["hits"],
                        "rejects": r["rejects"],
                        "games": sorted(self._games_of(r))}
                top_list.append(item)
        return {
            "game": game,
            "session": counts,
            "library": library,
            "top_memories": top_list,
            "conflicts": conflicts,
        }

    # ── 管理/统计（报告、调试） ─────────────────────────────────────

    def list_all(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM memories ORDER BY status, evidence_count DESC")]

    def count(self) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) c FROM memories").fetchone()
            return row["c"] if row else 0

    def close(self):
        with self._lock:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
