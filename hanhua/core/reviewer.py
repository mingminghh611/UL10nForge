"""翻译语义审核器（任务一阶段 1：四级深审闭环，本地 4B 推理）。

历史沿革：
- 2026-08-12 首版：走云端 deepseek API 审核（二值 pass/flag）
- 2026-08-13 全面升级：按执行指令删除云端审核——审核统一走本地
  Qwen3.5-4B（llama.cpp 单实例，--reasoning off 固化在 review spec
  server_args），不存在云端路径

闭环（实施计划 T1-1~T1-7，阶段 1）：
  质量门 → risk_gate 分流（4B 调用率 <15% 硬约束）→ 4B 四级判定
  （PASS/MINOR/MAJOR/CRITICAL，九维审核）→ apply_verdict 分发
  （PASS 写库 / MINOR 记录放行 / MAJOR 修正 / CRITICAL 重译）→
  反馈式重译（审核理由注入）→ 再审收敛（≤2 轮）→ 记忆门禁
  （MAJOR/CRITICAL 不进记忆）→ 审核日志（review_report.md）

四级定义（实施计划 §4.1）：
- PASS     语义/结构/术语全部正确 → 写库 + 记忆候选
- MINOR    语义正确但有小瑕疵，不改变含义 → 记录放行
- MAJOR    语义有偏差（术语误用/语气不符/信息缺失未颠倒）→ 修正再审
- CRITICAL 语义错误（否定颠倒/关系颠倒/数量时间错误/含义完全不同）→ 重译

审核维度（§4.1 九项）：语义一致性/否定/人物关系/条件与因果/数量/
时间/语气/信息完整性/术语一致性/风格。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from .models import STATUS_TRANSLATED, TextEntry
from .review_failures import ERROR_REVIEW, build_review_failure
from .review_outcome import (APPROVED, APPROVED_MINOR, BLOCKED, CANCELLED,
                             NEEDS_REVISION, PARSE_ERROR, REVIEW_ERROR,
                             TRANSPORT_ERROR, UNAVAILABLE, apply_outcome,
                             review_publishable)
from .risk_gate import gate_entries
from .review_server import ReviewModelService
from .prompts import build_game_context_block
from .translator import builtin_ui_conflict

# ── 审核维度与四级判定标准 ─────────────────────────────────────────

_REVIEW_SYSTEM_PROMPT = """你是游戏本地化质量审核员。审核必须严格，逐项核对十二
个审核维度，任一维度有问题就按严重程度定级，宁严勿松。

审核维度（十二项，#43 重构指令 §10 + 2026-08-24 原文低质量豁免 +
2026-09-07 源语言认知 C12b）：
1. 语义准确：译文是否传达原文全部含义，不张冠李戴、不增删信息。
   核对：否定（not/no/never/without——「不」被吞是 CRITICAL）、
   人物关系（主宾颠倒 A 打 B 译成 B 打 A 是 CRITICAL）、条件与因果
   （if/unless/only/因为/所以）、数量（50 HP 译成 60 HP 是 CRITICAL）、
   时间（时态/早晚/先后）
2. 游戏语境：译文是否符合本条文本类型与游戏场景（按钮动词/对话口语/
   剧情叙述/系统提示各有规范；UI 文本与剧情文本译法不同）。
   短标签专项（≤2 词原文）：单字/单词标签（Pan/Hearts/Gold…）的
   译文必须与单词本义相符——Pan 译「先生」、Charge 译「等级」这类
   与词义无关的输出是幻觉，直接 CRITICAL；不确定词义时按常见游戏
   用语判断（Pan=平移/摇镜、Mine=地雷、Charge=充能、Heart=爱心/
   生命），不得因原文短就默认译文正确
3. 术语一致：游戏术语与 UI 标准词必须用行业标准译法——
   Resume=继续（不是简历/恢复）、Start=开始（不是播放）、Save=保存、
   Load=读取、Quit=退出、Options=选项、New Game=新游戏、
   Main Menu=主菜单、Back=返回、Settings=设置、Continue=继续、
   Disabled=已禁用（不是残疾人士）、Enabled=已启用（不是已启动）。
   若提示词给出「术语参考/语境参考」，译文必须与其一致。
   硬规则（2026-08-14 minato 实证）：术语参考只约束其中列出的词条，
   未列出的英文单词翻译成中文是正确行为——普通英文 UI 词
   （Button=按钮、Menu=菜单、Exit=退出）译成中文绝不判「违反术语
   一致性、应保留原文」；「X=保留原文」标记只表示 X 这一个专名
   需保留，不得推广到其他词。不得用「应保留原文」作为判错理由，
   除非该词确实是游戏专名/代号且术语参考明确列出
4. 自然度：译文是否通顺自然、符合中文表达习惯，无生硬直译
5. 风格：语气/正式度与原文和文本类型匹配（按钮简短、对话口语、
   剧情书面，形容词/感叹词传神）
6. 完整性：不丢字不丢句，长句不截断、不合并
7. 幻觉：译文是否添加原文完全没有的信息（编造数字/物品/行为，
   把原文没有的内容译出来是 CRITICAL）
8. 结构完整：占位符（{0}、%s）、换行、HTML/富文本标签、数量词
   格式必须原样保留（{0} 被吞或改位是 CRITICAL）
9. 歧义：原文多义词（Resume/Save/Charge…）是否按本条语境选择了
   正确义项（「Resume 简历」在游戏中多指「继续」）
10. 机翻痕迹：是否存在英文语序直译腔、逐词对应、滥用「的/被/
    进行/一个」等翻译腔——游戏文本必须读起来像母语写作
11. 原文低质量/梗/自嘲豁免：当原文本身是拼写错误、语法混乱、网络
    梗、leetspeak（4=for、ur=your、wan=want）、故意不通的自嘲/吐槽
    文本时，审核关注点是「译文是否传达了原文意思」，而不是「译文
    是否修正了原文错误」。原文的拼写/语法错误不是译文的缺陷：
    - 不得以「原文拼写错误、译文未修正」判错——原文错拼不是译文的错；
    - 不得要求译文修正原文错误（修正了不加分也不扣分，保持大意即可）；
    - 译文口语化/保留英文感叹词（Oops/Wow）是可接受风格；
    - 译文传达原文大意 → PASS/MINOR，不应强制重译；
    - 仅当译文本身颠倒/丢失了原文真实传达的意思（如漏译否定、语义
      完全跑偏）才判 MAJOR/CRITICAL。判断原文是否低质量以「原文
      本身」为准（含多个明显错拼/语法混乱/无完整语义），不得只看
      译文是否通顺。

    低质量原文示例（原文低质量时，译文传达大意即可，不得强制重译）：
    - 原文：i cant start playing my games if im not consumed the consume
      译文：如果我不消耗，我就无法开始玩游戏。→ PASS（原文是故意不通
      的自嘲，译文传达大意即可，不得抠「consumed」的词性）
    - 原文：not only does flex taps powerful adhesive hold the mountain up
      译文：不仅柔韧胶带凭借强力粘合固定了山脉。→ PASS（not only...but
      also 是肯定强调句，译文「不仅」已正确传达，不得当「否定漏译」）
    - 原文：wide putin
      译文：宽普钦 → PASS（原文是错拼梗，译文音译保留大意即可）
    - 原文：the enemy does not drop any gold
      译文：敌人会掉落金币。→ CRITICAL（原文真否定被漏译，语义相反）
12. 源语言认知：原文**不一定总是英文**——多语言游戏的语料可能包含
    法语/德语/西班牙语/日语等非英语原文（字段名 ContentFr/TextDe…
    或词形可辨）。审核任务恒定是「把原文译成简体中文」：把法语/德语
    原文译成中文不是错误，恰恰是正确完成本职工作。
    - 严禁以「原文为法语/德语/其他语言，译文误译成中文」「语言完全
      错误」「原文非英语不该翻译」为由判错——这些理由在中文本地化
      流程里永远不成立，是幻觉；
    - 判「翻译错误」只能基于语义：译文是否忠实传达了**该语言原文**
      的意思（法语 lecture=读者不是讲师，德语 Fehler=错误不是失败）；
    - 原文含非英语词但译文语义正确 → PASS。

级别定义：
- PASS：语义/结构/术语全部正确，可直接写库
- MINOR：语义正确但有瑕疵（语序不自然、用词不够地道），不改变含义
- MAJOR：语义有偏差（术语误用、语气不符、信息缺失但未颠倒），需修正
- CRITICAL：语义错误（否定颠倒、人物关系颠倒、数量/时间错误、幻觉
  增义、含义完全不同），译文不可用

输出严格 JSON 对象，不要输出任何其他文字（包括思考、解释）：
{"level": "PASS|MINOR|MAJOR|CRITICAL", "reason": "<一句话：问题摘要+修正要点，≤50 字>"}
reason 必须包含修正要点供自动重译直接使用（如「Resume 误译为简历，
应译为继续」）；PASS 时 reason 写「正确」。
（2026-08-14 提速：输出 token 是审核吞吐瓶颈——4B 逐 token 生成，
精简为 level+reason 两项，评审判定信息完整；旧字段 overall_score/
dimensions/issues 可选，解析器仍兼容）"""

# 批量审核输出要求（2026-08-14 全量送审提速：一次给多条，模型逐条
# 独立判定输出数组——上下文共享，减少往返；缺失/坏条目外层逐条兜底）。
# 元素精简为 level+reason（2026-08-14 二次提速：每条 overall_score/
# issues/suggestion 多出 50-150 token × 20 条 ≈ 2-3k 输出 token，4B
# 生成它们要几十秒——评审判定只需级别+理由，修正要点并入 reason）。
_REVIEW_BATCH_OUTPUT = (
    "本次一次给出 {n} 条待审核条目，请逐条独立审核（每条按同样十二维"
    "标准），输出严格 JSON 数组，数组元素与条目一一对应，不得遗漏"
    "任何一条、不得合并、不得输出任何其他文字（包括思考、解释）：\n"
    '[{{"entry_id": "<该条 ID>", "level": "PASS|MINOR|MAJOR|CRITICAL", '
    '"reason": "<一句话：问题摘要+修正要点，≤50 字；PASS 写正确>"}}]')

_LEVELS = ("PASS", "MINOR", "MAJOR", "CRITICAL")

# 4B 反馈重译固定角色（2026-08-14 用户要求：任何翻译路径必须给模型
# 固定角色，至少是游戏翻译者——4B 审核模型承担反馈重译时，prompt 无
# 角色会输出解释/直译腔；与 prompts.build_system_prompt 的角色段同源
# 精简版）
_RETRANSLATE_ROLE = (
    "你是专业游戏本地化翻译专家，负责把游戏文本翻译为简体中文。"
    "译文要像中文玩家母语中看到的游戏文本，不要逐词直译、不要续写、"
    "不要解释，只输出译文。")

# 组批输入 token 预算（2026-08-14 提速）：条目段估算累计超限即拆组。
# 系统 prompt ~1400 + 组输入 ≤4500 + 输出 ~800 ≈ 6.7k < ctx 8192 安全；
# 显存紧张档（planner 可能降 4096）下组更小、逐条兜底仍正确不截断。
_GROUP_INPUT_TOKEN_BUDGET = 4500

# 旧二值审核词形 → 四级映射（兼容历史模型输出）
_LEGACY_FLAG_VALUES = frozenset({
    "flag", "fail", "incorrect", "bad", "wrong", "poor", "failed",
    "不合格", "错误", "需优化", "需要优化", "不通过", "改进",
})

# 批审幻觉防护（2026-08-14 minato 实证）：4B 批量审校成段输出
# 「译文缺失，仅保留术语参考」的罐头理由——实际译文非空且正确
# （时间/心脏/黄金 30+ 条）。「译文缺失」是可验证主张：译文明明
# 非空却报缺失 → 判定不可信 → 逐条重审兜底（单条路径噪声少、
# 无批内互相干扰）。
_MISSING_TRANSLATION_CLAIMS = (
    "译文缺失", "译文为空", "未翻译原文", "仅保留术语", "仅列出术语",
    "界面空白", "按钮失效", "按钮无效", "按钮功能失效",
)


def _reason_claims_missing_translation(reason: str) -> bool:
    """reason 是否声称译文缺失/未翻译（可验证主张）。"""
    r = reason or ""
    return any(p in r for p in _MISSING_TRANSLATION_CLAIMS)


# 批审幻觉防护（2026-08-24 扩展）：「否定漏译/语义相反」类罐头理由——
# come-back-and-make-a-sequel 实证 4B 把 not only...but also 强调句当
# 「否定句漏译否定词」（译文用「不仅…」已正确传达）、把「如果我不消耗」
# 当「否定逻辑错误」。「否定词被吞 → 语义相反」是可验证主张：原文剥离
# 强调结构（not only/not just/not merely/not simply）后仍含否定词、译文
# 却无对应中文否定 → 判定才成立；否则与事实矛盾 → 逐条重审兜底。
_NEGATION_DROPPED_CLAIMS = ("否定词", "漏译否定", "否定句", "语义相反",
                            "语义颠倒")
# 原文否定词（含缩写 n't 与 don't/doesn't 等词尾 n't 形态）
_NEGATION_SOURCE_RE = re.compile(
    r"\b(?:not|no|never|without|cannot)\b|n't", re.I)
# not only/just/merely/simply...but also 是肯定强调，not 不是「否定漏译」
_NOT_EMPHASIS_RE = re.compile(r"\bnot\s+(?:only|just|merely|simply)\b", re.I)
# 中文否定词（译文表达否定语义的常规字）
_NEGATION_ZH_RE = re.compile(r"不|没|未|无|别|莫|勿|非|岂|难")


def _reason_claims_negation_dropped(reason: str) -> bool:
    """reason 是否声称「否定漏译/语义相反」（可验证主张）。"""
    r = reason or ""
    return any(p in r for p in _NEGATION_DROPPED_CLAIMS)


def _translation_dropped_negation(original: str, translation: str) -> bool:
    """译文是否真的漏译了原文否定（可验证）：原文（剥离 not only/not
    just 强调后）含否定词、译文却无对应中文否定 → 真漏译；否则是 4B
    幻觉（not only...but also 的 not 不是否定、译文用「不仅/绝不」等
    已传达）。剥离强调结构：'not only does flex...hold the mountain up'
    的 not 属于 not only 强调，不触发漏译主张。
    """
    source = _NOT_EMPHASIS_RE.sub(" ", str(original or ""))
    if not _NEGATION_SOURCE_RE.search(source):
        return False
    return not _NEGATION_ZH_RE.search(str(translation or ""))


# 批审幻觉防护（2026-09-07 fake-it 实证，C12b）：4B 对非英语原文
# （法语 ContentFr 列）成批输出「原文为法语，译文误译成中文」「语言
# 完全错误」——120+ 条被阻断，但「把原文译成中文」正是本职工作，
# 语言本身永远不是判错理由。这是可验证主张：reason 声称语言错误
# → 判定不可信 → 逐条重审兜底（重审单条时系统 prompt 的源语言
# 认知维度生效）。
_LANGUAGE_ERROR_CLAIMS = (
    "原文为法语", "原文为德语", "原文为日语", "原文为韩语",
    "原文为西语", "原文为俄语", "误译成中文", "语言完全错误",
    "语言错误", "应保留原语言", "不应翻译成中文", "原文非英语",
    "应为法语", "应为德语", "应为日语", "译回原文", "语言不一致",
)


def _reason_claims_language_error(reason: str) -> bool:
    """reason 是否声称「语言错误/不该译成中文」（可验证主张）。

    中文本地化流程里该主张恒不成立——原文无论什么语言，译成简体
    中文都是任务本身。命中即判幻觉，逐条重审兜底。
    """
    r = reason or ""
    return any(p in r for p in _LANGUAGE_ERROR_CLAIMS)



# + 审核条目段字段行）。重译模型偶尔回显 prompt 结构而非纯译文——
# 多行修复取「首个非空行」会恰好抓到「[来源文件] …」这类行写进游戏
# （0.39.1 fromivan 证据：multi-line candidate 首行为元数据回显）。
_METADATA_ECHO_RE = re.compile(
    r"^\s*(?:\[(?:来源文件|定位键|文本角色|识别置信度|上下文|条目)\]"
    r"|原文：|译文：|类型：|术语参考：|语境参考：|游戏语境参考：)")


def _repair_multiline_candidate(original: str, candidate: str) -> str:
    """单行原文的多行候选 → 取首个非空行（2026-08-14 minato 实证：
    4B/1.8B 反馈重译稳定输出双候选「左滑\n左移」——原文单行时
    newline_mismatch + line_content_mismatch 恒败，正确译文被机械门
    杀死 → BLOCKED 留人工）。

    仅做单行原文修复（多行原文截断有信息丢失风险，留给重试/人工）；
    首行非空才修；修复结果与候选不同才返回（否则空串）。
    首行是提示词元数据回显（「[来源文件] …」/「原文：…」）时拒绝
    修复——那是模型复述 prompt 结构，不是译文，写回即污染（0.39.1）。
    """
    if not candidate or len(re.split(r"\\n|\r\n|\r|\n", original)) != 1:
        return ""
    lines = [line.strip() for line in re.split(
        r"\\n|\r\n|\r|\n", candidate.strip())]
    if len(lines) <= 1:
        return ""
    first = next((line for line in lines if line), "")
    if not first or first == candidate.strip():
        return ""
    if _METADATA_ECHO_RE.match(first):
        return ""
    return first


def _parse_level(raw: str | None) -> str:
    """解析四级判定（兼容旧 verdict 词形：pass/flag/incorrect…）。

    优先级：精确四级值 → 前缀匹配 → 子串匹配 → 旧 flag 词形 →
    默认 PASS。
    """
    v = (raw or "").strip().upper()
    if not v:
        return "PASS"
    for level in _LEVELS:
        if v == level:
            return level
    for level in _LEVELS:
        if v.startswith(level) or f"({level})" in v or f"[{level}]" in v:
            return level
    if v in {w.upper() for w in _LEGACY_FLAG_VALUES}:
        return "MAJOR"
    for start in ("INCORRECT", "FAIL", "FLAG", "BAD", "WRONG", "不合格",
                  "需优化", "需要优化"):
        if v.startswith(start):
            return "MAJOR"
    return "PASS"


def _parse_verdict(raw: str) -> str:
    """旧二值解析兼容（部分调用方仍按 pass/flag 处理）。"""
    return "flag" if _parse_level(raw) in ("MAJOR", "CRITICAL") else "pass"


def _short_exc(exc: Exception) -> str:
    """异常摘要（截断避免坏文本撑爆 meta/reason）。"""
    return str(exc).strip()[:120] or type(exc).__name__


@dataclass
class ReviewConfig:
    """审核行为配置（超时/长度/批大小；模型由 ModelRegistry 管理）。"""
    timeout: float = 120.0
    max_tokens: int = 1024
    batch_size: int = 1        # 4B 单实例并发 1，逐条送审
    enabled: bool = True


@dataclass
class ReviewItem:
    """单条待审核翻译。"""
    entry_id: str
    original: str
    translation: str
    text_type: str = ""
    # #43 阶段 E（重构指令 §16 知识优先级链）：审核模型参考提示词。
    # term_hint 术语表参考（"Resume=继续；Save=保存"）；context_hint
    # 语境证据摘要（"「继续」(context_exact, 置信 0.90)"）——提示词注入
    # 由调用方（review_entries）检索，空串 = 无参考（行为与旧版一致）。
    term_hint: str = ""
    context_hint: str = ""
    # 设计文档 §16：Game Context 注入（游戏背景/语言风格/相关角色/
    # 相关术语/翻译注意事项）——审校模型看到与翻译同一份游戏语境。
    # 由调用方注入（空串 = 无语境，行为与旧版一致）。
    game_context_hint: str = ""


@dataclass
class ReviewResult:
    """单条四级审核结论。"""
    entry_id: str
    level: str = "PASS"          # PASS / MINOR / MAJOR / CRITICAL
    reason: str = ""
    issues: tuple[dict, ...] = ()    # [{"type", "detail", "suggestion"}]
    reviewed: bool = True
    # 兼容旧字段（旧二值接口：verdict/issue/suggestion）
    verdict: str = "pass"        # flag = level in (MAJOR, CRITICAL)
    issue: str = ""              # 首个 issue type
    suggestion: str = ""         # 首个 issue suggestion
    # Phase A（2026-08-13 审计 §5 P0-6）：审核错误必须显式化，不得伪装成
    # 判定。error 为空 = 正常判定；否则为 TRANSPORT_ERROR / PARSE_ERROR /
    # UNAVAILABLE / CANCELLED。错误条目不可发布、不沉淀记忆/术语。
    error: str = ""
    # #43 阶段 E（重构指令 §10 十维审校）：LLM 综合评分 + 十维维度分。
    # 模型未输出时默认 0/{}（兼容旧模型/旧测试，零破坏）。供 Review
    # Fusion 与 GUI 展示；not used 于判定级别（level 仍是唯一裁决）。
    overall_score: int = 0
    dimensions: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.level not in _LEVELS:
            self.level = _parse_level(self.level)
        # 兼容旧二值构造点：显式 verdict="flag" 且未显式传 level →
        # 映射为 MAJOR（旧语义「需优化」= 现 MAJOR 起；真 PASS 的
        # verdict 默认值在下方重算不会误映射）
        if self.level == "PASS" and self.verdict == "flag":
            self.level = "MAJOR"
        self.verdict = "flag" if self.level in ("MAJOR", "CRITICAL") else "pass"
        if self.issues:
            first = self.issues[0]
            self.issue = str(first.get("type") or self.issue)
            self.suggestion = str(first.get("suggestion") or self.suggestion)

    @property
    def needs_optimization(self) -> bool:
        """兼容旧语义：需优化 = MAJOR 或 CRITICAL（MINOR 记录放行不打扰）。"""
        return self.level in ("MAJOR", "CRITICAL")

    @property
    def is_error(self) -> bool:
        """审核错误（传输/解析/服务/取消）——错误不等于「没有发现问题」。"""
        return bool(self.error)

    def apply_verdict(self, entry: TextEntry) -> str:
        """T1-2 处置分发：按级别对条目落终态 + meta 标记（Phase A）。

        返回处置名：
          write        PASS → APPROVED（可发布），meta 记 review_level
          pass_minor   MINOR → APPROVED_MINOR（可发布）
          revise       MAJOR → NEEDS_REVISION（不可发布）+ need_revision
          retranslate  CRITICAL → NEEDS_REVISION（不可发布）+ need_retranslate
        MAJOR/CRITICAL 不再是「保持写库态」——发布资格由单一终态把关
        （quality_passed 被压 False，写回门双重拒绝）。
        """
        entry.meta = dict(entry.meta)
        entry.meta["review_level"] = self.level
        if self.reason:
            entry.meta["review_reason"] = self.reason[:400]
        if self.suggestion:
            entry.meta["review_suggestion"] = self.suggestion[:200]
        if self.level == "PASS":
            apply_outcome(entry, APPROVED, level="PASS")
            return "write"
        if self.level == "MINOR":
            apply_outcome(entry, APPROVED_MINOR, level="MINOR")
            return "pass_minor"
        if self.level == "MAJOR":
            entry.meta["need_revision"] = True
            apply_outcome(entry, NEEDS_REVISION, level="MAJOR")
            return "revise"
        entry.meta["need_retranslate"] = True
        apply_outcome(entry, NEEDS_REVISION, level="CRITICAL")
        return "retranslate"


def _build_item_prompt(item: ReviewItem) -> str:
    """构造单条十维审核 prompt（系统维度说明 + 条目 + 术语/语境参考）。

    #43 阶段 E（重构指令 §16 知识优先级链）：term_hint/context_hint
    为调用方检索注入的知识库参考（术语表 + 语境证据摘要），空串跳过
    ——旧调用方（不传 hint）行为与旧版完全一致。

    截断口径 _review_text_cap（220 短文本 / 600 长文本，0.39.1）：
    220 短文本提速口径保留；长文本放宽到 600——220 字符英文信件
    截断处之后的内容审核模型看不到，是「幻觉增义」误判重灾区
    （fromivan Dear Ivan 信实证）。预算拆组保证组批不超 ctx。
    """
    return _REVIEW_SYSTEM_PROMPT + _build_item_body(item)


def _estimate_prompt_tokens(item: ReviewItem) -> int:
    """估算单条审核 prompt 的输入 token（中文 1 tok/字、英文 0.25）。

    组批预算拆组用（2026-08-14 提速）：llama-server 对超 ctx 的 prompt
    静默截断尾部——按估算累加拆组，保证任意组必然放得下，后半批不再
    丢失。截断口径与 _build_item_body 一致（共用 _review_text_cap，
    0.39.1：长文本放宽到 600 后估算同口径，预算拆组不失真）。
    """
    text = (_review_text_cap(item.original)
            + _review_text_cap(item.translation)
            + (item.term_hint or "")[:120]
            + (item.context_hint or "")[:120]
            + (item.game_context_hint or "")[:300])
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return int(cjk + (len(text) - cjk) * 0.25) + 40   # +40 类型/格式行


def _parse_result(raw: str, entry_id: str) -> ReviewResult | None:
    """解析审核模型 JSON 输出 → ReviewResult（容错：剥离代码围栏）。

    围栏形态处理：```json\n{...}\n``` → 去掉首行 ```json 与尾部 ```；
    直接 ```{...}``` → 去掉首尾 ```。
    """
    try:
        text = raw.strip()
        if text.startswith("```"):
            body = text[3:]
            if "```" in body:
                body = body.split("```", 1)[0]
            body = body.strip()
            if body.startswith("json"):
                body = body[4:].strip()
            text = body
        data = json.loads(text)
    except (json.JSONDecodeError, AttributeError):
        # 非 JSON 兜底（Phase A P0-6）：整段视为 reason，级别按词形粗判，
        # 但显式标记 PARSE_ERROR——错误不得伪装成「没有发现问题」。
        level = _parse_level(raw)
        return ReviewResult(entry_id=entry_id, level=level,
                            reason=raw.strip()[:300], reviewed=False,
                            error=PARSE_ERROR)
    if not isinstance(data, dict):
        return ReviewResult(entry_id=entry_id, level=_parse_level(""),
                            reason="审核模型输出非 JSON 对象",
                            reviewed=False, error=PARSE_ERROR)
    return _review_result_from_dict(data, entry_id)


def _review_result_from_dict(data: dict, entry_id: str) -> ReviewResult:
    """dict → ReviewResult（单条与批量数组元素共用解析）。

    #43 阶段 E（重构指令 §10）：十维审校 JSON 字段——overall_score
    综合分 + dimensions 维度分。容错：缺失/非法值 → 默认（0/{}），
    旧模型输出（仅 level/reason/issues）零破坏。
    """
    issues_raw = data.get("issues") or []
    issues = tuple(
        {str(k): str(v) for k, v in item.items() if isinstance(item, dict)}
        for item in issues_raw if isinstance(item, dict)) \
        if isinstance(issues_raw, list) else ()
    overall_score, dimensions = 0, {}
    score_raw = data.get("overall_score")
    if isinstance(score_raw, (int, float)) and not isinstance(score_raw, bool):
        overall_score = max(0, min(100, int(score_raw)))
    dims_raw = data.get("dimensions")
    if isinstance(dims_raw, dict):
        for k, v in dims_raw.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                dimensions[str(k)[:40]] = max(0, min(100, int(v)))
    return ReviewResult(
        entry_id=entry_id,
        level=_parse_level(data.get("level")),
        reason=str(data.get("reason") or "").strip(),
        issues=issues,
        overall_score=overall_score,
        dimensions=dimensions,
    )


def _parse_batch_result(raw: str,
                        group_items: list) -> dict[str, ReviewResult]:
    """解析批量审核 JSON 数组 → {entry_id: ReviewResult}。

    只返回成功解析的条目；缺失/非法元素不进 dict（调用方对缺失条目
    逐条兜底 review_one，降级不降质）。整体非数组/JSON 失败 → 空
    dict（全部逐条兜底，绝不整组伪装 PASS）。
    """
    text = raw.strip()
    if text.startswith("```"):
        body = text[3:]
        if "```" in body:
            body = body.split("```", 1)[0]
        body = body.strip()
        if body.startswith("json"):
            body = body[4:].strip()
        text = body
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, AttributeError):
        return {}
    if not isinstance(data, list):
        return {}
    ids = {item.entry_id for item in group_items}
    out: dict[str, ReviewResult] = {}
    for element in data:
        if not isinstance(element, dict):
            continue
        eid = str(element.get("entry_id") or "")
        if eid not in ids or eid in out:
            continue
        out[eid] = _review_result_from_dict(element, eid)
    return out


def _review_text_cap(text: str, cap: int = 220, long_cap: int = 600) -> str:
    """审核送审字段截断（0.39.1 fromivan 修正）：

    短文本维持 220 cap（2026-08-14 提速口径不变）；长文本（>cap）
    放宽到 600——中文每字 ≈1 token，同样 220 字符中文 ≈ 语义量 4 倍
    于英文，长英文信件在 220 处截断后审核模型看不到的原文区间恰好
    是「幻觉增义」误判重灾区（Dear Ivan 信「across the Soviet
    Union」段被截 → 模型把译文合理演绎判为无中生有）。

    上限 600 与上游 ReviewItem 构造（str(e.original)[:600]）对齐——
    上游已带 600，此处放宽不引入新数据需求。调用方（_build_item_body
    与 _estimate_prompt_tokens）必须共用本函数，保证单条/批量/估算
    同口径，预算拆组不失真。
    """
    text = str(text or "")
    if len(text) <= cap:
        return text
    return text[:long_cap]


def _build_item_body(item: ReviewItem, title: bool = False) -> str:
    """条目段（类型/原文/译文 + 术语/语境参考 + Game Context），单条与批量共用。

    截断口径 _review_text_cap（220 短文本 / 600 长文本，0.39.1）：
    单条/批量/预算估算三者必须同口径——此前批量与估算用 220/120 而
    单条曾用 600/400，口径不一致会让预算拆组失真。title=True 时带
    「### 条目」标题（批量数组输出需要条目标识；单条路径保持旧格式）。

    game_context_hint（设计文档 §16）：审校模型看到与翻译同一份游戏
    语境（游戏背景/语言风格/相关角色/相关术语/翻译注意事项）——判
    定「语气不符角色设定」「术语与世界观不匹配」类问题有据可依。
    """
    head = f"\n### 条目 {item.entry_id}\n" if title else "\n"
    parts = [
        head + f"类型：{item.text_type or '未知'}\n"
        f"原文：{_review_text_cap(item.original)}\n"
        f"译文：{_review_text_cap(item.translation)}",
    ]
    if item.game_context_hint:
        parts.append(f"游戏语境参考：{item.game_context_hint[:300]}")
    if item.term_hint:
        parts.append(f"术语参考：{item.term_hint[:120]}")
    if item.context_hint:
        parts.append(f"语境参考：{item.context_hint[:120]}")
    return "".join(parts)


def _build_batch_prompt(items: list) -> str:
    """构造批量审核 prompt（十维系统提示 + N 条目独立段 + 数组输出要求）。

    条目段逐条自带术语/语境参考（检索结果按条目注入，组批不混）。
    系统提示的单条「输出 JSON 对象」指令在输出段剥离——批量要求
    数组输出，两条指令并存会让模型困惑。
    """
    core = _REVIEW_SYSTEM_PROMPT.split("输出严格 JSON 对象", 1)[0].rstrip()
    parts = [core, _REVIEW_BATCH_OUTPUT.format(n=len(items))]
    for item in items:
        parts.append(_build_item_body(item, title=True))
    return "".join(parts)


class SemanticReviewer:
    """四级审核器（2026-09-06 云端链路接通）。

    默认本地 Qwen3.5-4B（服务生命周期由 ReviewModelService 管理，
    review_runtime.json 跨实例复用）；online_cfg 传入完整云端配置时
    （base_url/api_key/model 齐全）走在线端点——review_one/review_batch/
    retranslate_with_feedback/_re_review 全链路同一 service.chat 出口。
    审核失败返回空结果（调用方保守处理 + 告警）。
    """

    def __init__(self, config: ReviewConfig | None = None,
                 service: ReviewModelService | None = None,
                 app_dir: str | Path | None = None,
                 online_cfg=None):
        self.config = config or ReviewConfig()
        self.service = service or ReviewModelService(
            Path(app_dir or Path.cwd()).resolve(),
            online_cfg=online_cfg)

    @property
    def usable(self) -> bool:
        """本地模式：模型文件存在即可用（服务按需启动）。"""
        return self.service is not None

    @property
    def model_name(self) -> str:
        """当前审核模型名（GGUF 文件名，registry 实际定位结果）。

        旧接口 ReviewConfig.model 不存在（审核模型由 ModelRegistry
        按 models/ 目录管理，配置类只管超时/长度）——报告等展示侧
        一律走本属性（hickory 实证：runner 访问 config.model 崩溃）。
        """
        try:
            spec = self.service._spec()
            if spec.path.is_file():
                return spec.path.stem
            return "Qwen3.5-4B"   # 模型缺失时展示约定名而非兜底文件名
        except Exception:  # noqa: BLE001 - 注册表异常不阻断展示
            return "Qwen3.5-4B"

    def review_one(self, item: ReviewItem) -> ReviewResult:
        """单条四级审核（本地 4B）。失败返回带 error 类别的结果（fail-closed）。

        Phase A（2026-08-13 审计 §5 P0-6）：传输/解析/服务错误一律显式
        标记（TRANSPORT_ERROR / PARSE_ERROR / UNAVAILABLE），绝不伪装成
        「没有发现问题」。调用方据 error 归类为 REVIEW_ERROR 终态。
        """
        try:
            content = self.service.chat(
                _build_item_prompt(item),
                max_tokens=self.config.max_tokens,
                timeout=self.config.timeout)
        except Exception as exc:  # noqa: BLE001 - 传输/服务错误 → 显式 error
            return ReviewResult(
                entry_id=item.entry_id,
                reason=f"审核请求失败：{_short_exc(exc)}"[:300],
                reviewed=False, error=TRANSPORT_ERROR)
        try:
            return _parse_result(content, item.entry_id)
        except Exception as exc:  # noqa: BLE001 - 解析意外异常 → 显式 error
            return ReviewResult(
                entry_id=item.entry_id,
                reason=f"审核结果解析失败：{_short_exc(exc)}"[:300],
                reviewed=False, error=PARSE_ERROR)

    def review_batch(self, items: list[ReviewItem], *,
                     on_progress: Callable[[int, int], None] | None = None,
                     cancellation_event=None) -> tuple[dict[str, ReviewResult],
                                                       int]:
        """审核一批（config.batch_size > 1 时组批，一次给模型多条）。

        批量（2026-08-14 全量送审提速：上下文共享、往返减半；用户
        「一次给多个节约时间」）：组内一次 chat 输出 JSON 数组；缺失/
        坏条目逐条 review_one 兜底（降级不降质）；请求异常 → 整组逐条
        （保留错误语义）。batch_size=1（默认）时与旧版逐条路径一致。

        on_progress(done, total) 每完成一组（逐条时每条）回调——
        GUI/runner 借此实时展示审核进度（2026-08-13 用户实证：送审
        期间界面无任何反馈，只有全部完成后一条总结）。
        cancellation_event：取消时提前终止，剩余条目计入 cancelled_count
        （取消是显式终态，不得归入 error 或 pass）。

        返回 (results, cancelled_count)：results 含 error 类结果；调用方按
        error 归类 REVIEW_ERROR（Fail-closed，不再是「未覆盖 = pass」）。
        """
        out: dict[str, ReviewResult] = {}
        if not items:
            return out, 0
        cancelled_count = 0
        done = 0
        batch_size = max(1, int(getattr(self.config, "batch_size", 1) or 1))
        if batch_size <= 1:
            # 逐条路径（与旧版逐字节一致，测试/单条 force_send 走此）
            for index, item in enumerate(items, 1):
                if (cancellation_event is not None
                        and cancellation_event.is_set()):
                    cancelled_count = len(items) - index + 1
                    break
                out[item.entry_id] = self.review_one(item)
                done = index
                if on_progress is not None:
                    on_progress(index, len(items))
            return out, cancelled_count
        # 组批路径：batch_size 是组内条数上限，另按估算 token 预算拆组
        # （2026-08-14 提速：超 ctx 的 prompt 被 llama-server 静默截断
        # 尾部，后半批模型没看到 → 输出缺失 → 逐条兜底翻倍慢——预算
        # 拆组保证任意组必然放得下，截断与兜底不再出现）。
        groups: list[list[ReviewItem]] = []
        current: list[ReviewItem] = []
        current_tokens = 0
        for item in items:
            tokens = _estimate_prompt_tokens(item)
            if current and (len(current) >= batch_size
                            or current_tokens + tokens
                            > _GROUP_INPUT_TOKEN_BUDGET):
                groups.append(current)
                current, current_tokens = [], 0
            current.append(item)
            current_tokens += tokens
        if current:
            groups.append(current)
        for group in groups:
            if (cancellation_event is not None
                    and cancellation_event.is_set()):
                cancelled_count = len(items) - done
                break
            try:
                content = self.service.chat(
                    _build_batch_prompt(group),
                    # 输出上限收紧（2026-08-14 提速）：此前
                    # 1024×len(group)（20 条 = 20480）是话痨放大器——
                    # 4B 一旦长输出就到分钟级且截断即全组兜底。精简
                    # 输出后每条 ~40-80 token，128/条 + 256 余量足够
                    max_tokens=max(self.config.max_tokens,
                                   min(4096, 128 * len(group) + 256)),
                    timeout=self.config.timeout)
                results = _parse_batch_result(content, group)
            except Exception:  # noqa: BLE001 传输/服务错误 → 整组逐条兜底
                results = {}
            for item in group:
                result = results.get(item.entry_id)
                if result is None:
                    result = self.review_one(item)  # 组内缺失逐条兜底
                out[item.entry_id] = result
            done += len(group)
            if on_progress is not None:
                on_progress(done, len(items))
        return out, cancelled_count

    def retranslate_with_feedback(
            self, original: str, translation: str, feedback: str,
            *, max_tokens: int = 1024, text_type: str = "",
            term_hint: str = "", context_hint: str = "",
            game_context_hint: str = "") -> str:
        """4B 反馈式重译（2026-08-14 request_error 根因修复）。

        用户实证：不合格条目的反馈重译走翻译服务（1.8B）——但 6~8GB
        档审核 4B 常驻后 1.8B 换入显存不足 OOM（llama-server 502/
        崩溃），重译请求全批失败（request_error），简单文本也落入
        人工层。审核模型 4B 已在显存且是通用指令模型，直接承担反馈
        重译——零模型切换、无显存冲突。

        固定游戏本地化角色（_RETRANSLATE_ROLE，2026-08-14 用户要求：
        任何翻译路径必须带角色，至少是游戏翻译者）。text_type 注入
        文本类型语境（按钮动词/对话口语…，消歧 Play=开始 vs 游玩）；
        term_hint/context_hint 注入首审阶段按需检索的参考（2026-08-14
        用户要求：审校途中的重译同样检索向量知识库，找与文本相关的
        术语/语境参考注入翻译——与批量翻译的按需检索同源）。

        调用方（_retranslate_with_feedback）对输出再过机械质量门 +
        再审收敛判定；本方法只负责产出译文，请求失败抛异常由调用方
        回退 translator（1.8B/API）通道。
        """
        type_line = f"文本类型：{text_type}\n" if text_type else ""
        term_line = f"术语参考：{term_hint}\n" if term_hint else ""
        ctx_line = f"语境参考：{context_hint}\n" if context_hint else ""
        gc_line = (f"游戏语境参考：{game_context_hint}\n"
                   if game_context_hint else "")
        # 结构硬约束（2026-08-15 minato 实证：重译候选被机械门以
        # placeholder_mismatch/numeric_mismatch/newline_mismatch 拒——
        # 模型重译时丢占位符/改数字/改行结构，反馈本身没提，模型不会
        # 主动保留。显式列出与原文一致的结构要求，从源头防机械门失败）
        user = (
            f"{_RETRANSLATE_ROLE}\n\n"
            "请根据下面的审核反馈，修正这条游戏文本的译文，"
            "只输出修正后的译文，不要解释：\n"
            f"{type_line}{term_line}{ctx_line}{gc_line}"
            f"原文：{original}\n上次译文：{translation}\n"
            f"审核反馈：{feedback}\n"
            "输出要求：译文必须完整保留原文的全部占位符"
            "（{0}、%s、<b>…</b> 等）、全部数字和换行结构，"
            "不得增删改位；只输出译文本身。")
        content = self.service.chat(
            user, max_tokens=max_tokens, timeout=self.config.timeout)
        return str(content or "").strip()


# ── 文本类型推断 ────────────────────────────────────────────────────

def text_type_for(meta: dict) -> str:
    """从条目 meta 推断文本类型（供审核模型语境判断，runner 与 GUI 共用）。"""
    kind = meta.get("kind") or ""
    if kind == "us":
        return "DLL 字符串"
    if kind == "il2cpp":
        return "IL2CPP 字符串"
    if kind == "textasset":
        return "文本脚本"
    if kind == "plain":
        return "纯文本"
    role = str(meta.get("role") or "")
    if "button" in role or role == "display" and len(role) < 12:
        return "UI 显示文本"
    # 对话分支先于日志分支：「dialog」含子串「log」，顺序颠倒会误判
    # 对话文本为调试日志（2026-08-14 全量记录实证）
    if "dialog" in role or "conv" in role or "chat" in role:
        return "对话文本"
    if "log" in role or "debug" in role:
        return "调试日志"
    return "游戏文本"


# ── 词对提取 → 术语沉淀（保留：C5 语境保护门禁链） ─────────────────

_TERM_ISSUES = {"术语错误", "专名误译"}
_TERM_PAIR_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9 .'\-]{0,48}?)[：:→\->]+\s*"
    r"([一-鿿][^，。；;\n]{1,20})")
_ENG_WORD = re.compile(r"[A-Za-z]{2,}")
_STOP_WORDS = {"and", "the", "of", "to", "in", "on", "for", "you", "your",
               "with", "from", "this", "that", "not", "are", "was", "were"}


def extract_term_pairs(results: list[ReviewResult],
                       originals: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """从需修正结果中提取术语词对（英文原词→建议译法）。

    只收术语/专名类问题（issue type 或 suggestion 含分隔符形态）。
    供知识库沉淀：后续游戏翻译时按词对约束模型输出。
    """
    pairs: list[tuple[str, str]] = []
    try:
        return _extract_term_pairs_impl(results, originals)
    except Exception:  # noqa: BLE001 - 词对提取失败不阻断审核主流程
        return pairs


def _extract_term_pairs_impl(results: list[ReviewResult],
                             originals: dict[str, str] | None) -> list[tuple[str, str]]:
    """词对提取主体（2026-08-12 根因修复：`|` 后装饰字符类未闭合，
    `)` 裸括号 → unbalanced parenthesis 崩溃——修正为字符类 + 外层
    try/except 防御。词对是附带产出，绝不允许拖垮审核主流程）。"""
    pairs: list[tuple[str, str]] = []
    _STRIP_PREFIX = re.compile(
        r"^(?:建议)?(?:应当|应|宜|建议)?(?:译为|翻译为|翻译成|译成|译作|"
        r"翻成|翻为|作|为)+")
    _STRIP_WRAP_ZH = re.compile(
        r"[“”\"'「『【（( ]*[\"']?|[\"']?[“”\"'」』】）) 。]*")
    for r in results:
        if not r.needs_optimization:
            continue
        sug = r.suggestion.strip()
        if not sug or len(sug) > 60:
            continue
        m = _TERM_PAIR_RE.search(sug)
        if m:
            source = m.group(1).strip()
            if len(source.split()) <= 5:
                pairs.append((source, m.group(2)))
            continue
        sug_clean = _STRIP_PREFIX.sub("", sug)
        if re.fullmatch(
                r"[“”\"'「『【（( ]*[一-鿿][^，。；;\n]{1,24}[“”\"'」』】）) 。]*",
                sug_clean):
            orig = ((originals or {}).get(r.entry_id, "") or "").strip()
            if re.fullmatch(
                    r"[A-Za-z0-9 .'\-]{1,60}", orig
            ) and 1 <= len(orig.split()) <= 5:
                pairs.append(
                    (orig, _STRIP_WRAP_ZH.sub("", sug_clean).strip()))
    return pairs


# ── 主入口：翻译后深审闭环 ─────────────────────────────────────────

def _active_glossary_pairs(glossary, game: str = "") -> list[tuple[str, str]]:
    """术语库 active 词对（candidate 只参考不强制，不参与分流信号）。

    game 过滤（2026-08-14 minato 实证）：术语库跨游戏积累，词对带
    games 归属（逗号分隔）。审校/分流只取本游戏词对 + 无归属词对
    （games 空 = 通用沉淀）；明确归属其他游戏的词对不参与——否则
    其他游戏的「英文=英文」保留型专名（GLISLYA=GLISLYA 等）注入
    minato 审校，教唆 4B 把 Button→按钮 这类正确翻译判为「违反术语
    一致性，应保留原文」。
    """
    try:
        rows = glossary.list_all()
    except Exception:  # noqa: BLE001 - 术语库异常不阻断审核
        return []
    pairs = []
    for row in rows:
        status = str(row.get("status") or "active")
        term = str(row.get("term") or "")
        trans = str(row.get("translation") or "")
        if status != "active" or not term or not trans:
            continue
        games = [g.strip() for g in re.split(
            r"[,，]", str(row.get("games") or "")) if g.strip()]
        if games and game and game not in games:
            continue
        pairs.append((term, trans))
    return pairs


def _memory_apply(memory, entry: TextEntry, level: str, model: str,
                  lang: str) -> None:
    """T1-6 记忆门禁 + Phase B PendingEvidence 审后提交/撤销：

    - MAJOR/CRITICAL（终态非 approved）→ 撤销：移除翻译管线在机械门
      通过后入 pending 桶的坏记忆（P0-3 深审前污染的主要来源）；
    - PASS/修正后译文 → promote：pending → 已提交（可命中）；反馈重译
      产生的新译文（从未入桶）经 upsert 直接提交。
    """
    if memory is None or not entry.translation or not lang:
        return
    try:
        if level in ("MAJOR", "CRITICAL"):
            memory.remove_memory(entry.original, model, lang)
        else:
            # BUILTIN 冲突门禁（2026-09-01 污染系统性根治）：PASS 系
            # 审后 promote 前再拦一道——审核模型把 Disabled 判「残疾
            # 人士」PASS（审核端只注入了术语维度无此强制）时，坏译文
            # 不得成为可命中记忆。promote_memory 内部同样过滤（双保险）。
            if builtin_ui_conflict(entry.original, entry.translation):
                return
            promote = getattr(memory, "promote_memory", None)
            if promote is not None:
                promote([(entry.original, entry.translation, model, lang)])
            else:
                memory.add_memory(entry.original, entry.translation, model, lang)
    except Exception:  # noqa: BLE001 - 记忆门禁失败不阻断审核主流程
        pass


def _failed_review_candidate(entry: TextEntry) -> str:
    """机械失败（status=failed）条目的可诊断候选。

    优先级：最后尝试译文（translation）→ 模型原始输出（raw_output）。
    两者皆空 = 无候选（纯传输失败，无任何译文证据）→ 无法语义诊断，
    由 review_entries 直接 BLOCKED，不伪装成已审核（审计 §5 P0-4）。
    """
    for candidate in (entry.translation,
                      (entry.meta or {}).get("raw_output", "")):
        if candidate and str(candidate).strip():
            return str(candidate)[:600]
    return ""


def _failed_reason(entry: TextEntry) -> str:
    """机械失败理由摘要（供无候选 BLOCKED 记录审计证据）。"""
    reasons = entry.quality_reasons or (entry.meta or {}).get(
        "quality_reasons") or ()
    detail = "、".join(str(r) for r in reasons) or "未知机械失败"
    return f"机械质量门最终失败（{detail}）且无候选译文可诊断"


# 机械失败原因 → 重译修正指引（2026-08-15 minato 实证：4B 判 PASS 但
# 机械门 failed 的条目强制重译时，反馈只有干巴巴的原因列表——模型
# 不知道具体修什么，重译输出再被同一机械门拒 → BLOCKED 留人工）
# 2026-09-06 P5 迁移：映射表单一来源化到 quality_fix_hints.py（审校页
# 质量门展示共用同一张表，防止两处文案漂移）；此处保留原名导入兼容。
from hanhua.core.quality_fix_hints import (
    QUALITY_FIX_HINTS as _QUALITY_FIX_HINTS,
    quality_fix_hint as _quality_fix_hint_of,
)


def _quality_fix_hints(reasons) -> str:
    """机械失败原因 → 重译反馈的具体修正指引。"""
    seen: list[str] = []
    for name, hint in _QUALITY_FIX_HINTS:
        if name in tuple(reasons) and hint not in seen:
            seen.append(hint)
    if not seen:
        return "请按原文语义重译出合格译文"
    return "；".join(seen) + "；请重译出合格译文"


def review_entries(entries, glossary, *, game_name: str = "",
                   on_note: Callable[[str], None] | None = None,
                   on_progress: Callable[[int, int], None] | None = None,
                   on_disposition_progress: Callable[[int, int], None] | None = None,
                   translator=None, memory=None, store=None,
                   app_dir: str | Path | None = None,
                   data_dir: str | Path | None = None,
                   model_name: str = "", lang: str = "zh-CN",
                   max_send_rate: float = 1.0,
                   cancellation_event=None,
                   force_send: bool = False,
                   online_review_cfg=None,
                   review_batch_size: int | None = None,
                   profile=None) -> dict:
    # 在线 API 模式（2026-08-14 环境设置页 per-kind 配置）：传入审核的
    # ApiConfig——审核走云端端点（不启动本地 4B）。None = 本地路径
    #（现有行为）。语境证据检索（embed/rerank）恒本地 0.6B 轻量运行。
    """翻译后深审闭环核心（runner 与 GUI 共用；翻译 C6 闭口升级版）。

    Phase A（2026-08-13 架构审计）后成为统一审核管线接口：GUI/headless
    都传同一组 translator / memory / store，管线内部完成：
      分流（risk_gate，mandatory 不受配额 + discretionary 受预算）→
      4B 逐条判定 → apply_verdict 分发（终态化）→ 反馈式重译 + 再审
      收敛（≤2 轮）→ 终态决定（APPROVED/APPROVED_MINOR/NEEDS_REVISION/
      BLOCKED/REVIEW_ERROR/CANCELLED）→ 原子持久化（store 传入时）→
      记忆门禁 → 术语词对沉淀（只对 VERDICT 判定，错误不沉淀）。

    P0-4（审计 §5）：status=failed（quality_failed 强制通道）条目带
    候选（最后译文或 raw_output）即送语义诊断；任何判定级别都不让机械
    拒绝的候选直接发布（4B 判 PASS/MINOR 时以机械证据为准强制重译）；
    无候选条目直接 BLOCKED（failed_no_candidate 计数，不占审核请求）。

    返回 summary：
      used          是否发出审核请求
      sent          送审条数（分流后）；rate 只统计 discretionary 送审率
      mandatory / discretionary / deferred_due_to_budget  双通道计数
      failed_no_candidate  无候选机械失败条目（直接 BLOCKED，未送审）
      reviewed      正常判定条数（无 error）；errors 审核错误条数；
                    cancelled 被取消条数——reviewed + errors + cancelled == sent
      results       {eid: ReviewResult} 全量判定（含 error 结果）
      flagged       list[ReviewResult]（MAJOR+CRITICAL 正常判定——旧「需优化」）
      levels        {PASS/MINOR/MAJOR/CRITICAL/REVIEW_ERROR: n}
      outcomes      {APPROVED: n, APPROVED_MINOR: n, BLOCKED: n, ...}
      retranslated / converged / blocked / pairs_added / pairs_rejected
      pairs_conflict / pairs_candidate / pairs_activated（Phase B-3 分类记账）
      originals / locators
      review_failures  list[dict] 结构化失败（review_failure_v1，Phase B-5）：
        MAJOR/CRITICAL 语义错误 + REVIEW_ERROR 管线错误，收敛与未收敛
        均记；correct_translation 仅终态 APPROVED 系译文（否则空串）
    """
    summary: dict = {"used": False, "reviewed": 0, "errors": 0,
                     "cancelled": 0, "results": {}, "flagged": [],
                     "levels": {}, "outcomes": {}, "sent": 0, "rate": 0.0,
                     "mandatory": 0, "discretionary": 0,
                     "deferred_due_to_budget": 0,
                     "retranslated": 0, "converged": 0, "blocked": 0,
                     "failed_no_candidate": 0,
                     "pairs_added": 0, "pairs_rejected": {},
                     "pairs_conflict": {}, "pairs_candidate": 0,
                     "pairs_activated": 0,
                     "originals": {}, "locators": {},
                     "review_failures": []}
    if not entries:
        return summary
    items: list[ReviewItem] = []
    item_entries: list[TextEntry] = []
    failed_without_candidate: list[TextEntry] = []
    for e in entries:
        if e.status == "translated":
            if not e.translation or str(e.translation) == str(e.original):
                continue                   # 回显跳过非审核对象
            candidate = e.translation
        elif e.status == "failed":
            # P0-4（审计 §5）：quality_failed 强制信号必须真实可达——
            # failed 条目有候选（最后译文/raw_output）就送语义诊断；
            # 无候选直接 BLOCKED（不伪装成已审核，不占审核请求）。
            candidate = _failed_review_candidate(e)
            if not candidate:
                apply_outcome(e, BLOCKED, level="MECHANICAL_FAILURE",
                              reason=_failed_reason(e),
                              clear_translation=True)
                failed_without_candidate.append(e)
                summary["blocked"] += 1
                summary["failed_no_candidate"] += 1
                continue
        else:
            continue
        eid = f"e{len(items)}"
        items.append(ReviewItem(
            entry_id=eid,
            original=str(e.original)[:600],
            translation=candidate,
            text_type=text_type_for(e.meta),
        ))
        item_entries.append(e)
        summary["originals"][eid] = str(e.original)
        summary["locators"][eid] = f"{e.file_id}:{e.key_path}"
    # 设计文档 §16：Game Context 注入审校（一次构建，全量条目共用同一
    # 份游戏语境参考）。profile 为 None 或未带 context_* 字段时返回空串
    # （与旧版行为完全一致，零破坏）。
    game_context_hint = ""
    if profile is not None:
        try:
            game_context_hint = build_game_context_block(profile)
        except Exception:  # noqa: BLE001 - 语境注入失败不阻断审核主流程
            game_context_hint = ""

    def _persist_early(rows: list[TextEntry]) -> None:
        """提前返回路径的原子落库（P0-4：无候选 BLOCKED 不因早退丢失）。"""
        for e in rows:
            outcome = e.meta.get("review_outcome") or "PENDING"
            summary["outcomes"][outcome] = (
                summary["outcomes"].get(outcome, 0) + 1)
        if store is not None and rows:
            store.batch_update_translation_results(rows)

    if not items:
        _persist_early(failed_without_candidate)
        return summary
    reviewer = SemanticReviewer(
        app_dir=app_dir or Path.cwd(), online_cfg=online_review_cfg,
        config=ReviewConfig(batch_size=review_batch_size or 1)
        if review_batch_size else None)
    if not reviewer.usable:
        if on_note:
            on_note("语义审核跳过：本地审核服务不可用（模型缺失或启动失败）")
        return summary
    # 风险分流：mandatory 强制送审 + discretionary 预算送审
    pairs = (_active_glossary_pairs(glossary, game=game_name)
             if glossary is not None else [])
    # #43 阶段 D（重构指令 Case 5 / §16）：错误模式 + 语境证据检索。
    # 历史错误命中（error_patterns.db）→ 提高风险识别；语境证据 →
    # 多义词消歧（支持候选 → 直放；全部反对 → context_conflict 送审）
    # 并注入审校 prompt（阶段 E 知识优先级链）。两条链路全部 try/except
    # 降级——知识库缺失/损坏绝不阻断主流程；force_send 也共享检索
    # （人工强制送审时术语/语境同样作参考）。
    error_patterns_by_id: dict[str, list] = {}
    context_evidence_by_id: dict[str, list] = {}
    # 数据根/资源根分离（2026-08-14）：app_dir 是模型资源根
    # （models/*.gguf 与 embed_runtime.json 所在地，GUI 传
    # resource_dir），data_dir 是数据根（context.db/vector.db/
    # error_patterns.db 所在地，GUI 传 ~/.hanhua）——此前检索库在
    # 资源根建空库，审校的按需检索注入打不到翻译阶段沉淀的语境证据。
    data_root = Path(data_dir or app_dir or Path.cwd())
    try:
        from hanhua.core.error_patterns import ErrorPatternStore
        ep_path = data_root / "error_patterns.db"
        if ep_path.exists():
            ep_store = ErrorPatternStore(ep_path)
            for e in item_entries:
                hits = ep_store.search(str(e.original))
                if hits:
                    error_patterns_by_id[
                        e.id if e.id is not None
                        else f"{e.file_id}:{e.key_path}"] = hits
    except Exception:  # noqa: BLE001 错误模式库故障不影响分流
        pass
    try:
        from hanhua.core.knowledge_retrieval import (
            create_knowledge_retrieval)
        kr = create_knowledge_retrieval(
            data_root,
            service_dir=Path(app_dir or data_root),
            game=game_name)
        if kr.usable:
            for e in item_entries:
                evidence = kr.query(
                    str(e.original), game=game_name,
                    text_type=text_type_for(e.meta), top_k=5)
                if evidence:
                    context_evidence_by_id[
                        e.id if e.id is not None
                        else f"{e.file_id}:{e.key_path}"] = evidence
    except Exception:  # noqa: BLE001 语境/向量检索故障不影响分流
        pass
    # C16 审核反例召回（2026-09-07 知识库审计）：fail_case/审核 域此前
    # 只写不读——1674 条「原文→误译」反例从未回流审核链路。同原文/
    # 同语义单元的历史误译以「勿重蹈」提示注入 context_hint（批审
    # prompt 语境参考段），首审即看到本作/同文本的历史翻车现场。
    counterexamples_by_id: dict[str, list[dict]] = {}
    try:
        from hanhua.core.knowledge import KnowledgeBase
        kb_path = data_root / "knowledge.db"
        if kb_path.exists():
            kb = KnowledgeBase(kb_path)
            try:
                for e in item_entries:
                    hits = kb.review_counterexamples(
                        str(e.original), game=game_name, limit=2)
                    if hits:
                        counterexamples_by_id[
                            e.id if e.id is not None
                            else f"{e.file_id}:{e.key_path}"] = hits
            finally:
                kb.close()
    except Exception:  # noqa: BLE001 反例库故障不影响审核
        pass
    if force_send:
        # 人工强制送审（审校页「重新审核」按钮）：不做风险分流，全量
        # 送审——人工复审的语义就是无条件再判，不能因无风险信号被直放
        # （否则按钮点了无响应）。
        to_review = list(item_entries)
        summary["sent"] = len(to_review)
        summary["mandatory"] = summary["discretionary"] = 0
        summary["deferred_due_to_budget"] = 0
        summary["rate"] = 0.0
    else:
        to_review, _passed, _deferred, gate_stats = gate_entries(
            item_entries, pairs, max_send_rate=max_send_rate,
            error_patterns_by_id=error_patterns_by_id,
            context_evidence_by_id=context_evidence_by_id)
        summary["sent"] = gate_stats["sent"]
        summary["mandatory"] = gate_stats["mandatory"]
        summary["discretionary"] = gate_stats["discretionary"]
        summary["deferred_due_to_budget"] = gate_stats["deferred_due_to_budget"]
        summary["rate"] = gate_stats["rate"]
    if not to_review:
        if on_note:
            on_note("语义审核：无风险条目（分流直放），4B 零调用")
        summary["used"] = False
        _persist_early(failed_without_candidate)
        return summary
    summary["used"] = True
    if on_note:
        on_note(f"风险分流：送审 {len(to_review)}/{len(items)} 条"
                f"（discretionary 送审率 {summary['rate']:.0%} ≤ "
                f"{max_send_rate:.0%}；mandatory {summary['mandatory']} 条强制）…")
        # 模型首次送审前提示（2026-08-13 用户实证：3GB 审核模型首次
        # 启动 30-120s 期间界面无反馈）——先报准备期，再报逐条进度
        on_note("语义审核：正在连接审核模型…"
                "（首次启动约 30-120 秒，进度逐条刷新）")
    # #43 阶段 E（重构指令 §16 知识优先级链）：送审条目注入术语参考
    # （active 词对）与语境证据摘要（context_exact/similar/rerank 前三）。
    # 证据在阶段 D 已检索（context_evidence_by_id），此处仅格式化。
    # 2026-08-14 minato 实证修复：术语参考必须按条目注入（只注入
    # 词对 term 在本条原文中命中的词对，≤5 条）——此前全局前 20 条
    # active 词对注入每条审校 prompt，其中大量其他游戏的「英文=英文」
    # 保留型专名（GLISLYA=GLISLYA…），4B 被教唆「英文应保留原文」，
    # 把 Button→按钮 / Menu→菜单 等正确翻译批量误判 CRITICAL；同时
    # 无命中词对的条目不再带噪声参考，批审幻觉（「译文缺失，仅保留
    # 术语参考」）随之消失。
    def _term_hint_for(original: str, hint_pairs) -> str:
        hits: list[str] = []
        for t, trans in hint_pairs:
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])",
                         original, re.IGNORECASE):
                # 保留型词对（term==translation）加语义标注，避免 4B
                # 误读为「该词应翻译成英文」（其实是专名保留原文）；
                # 标注「译中文通称不判错」与质量门豁免对齐（FPS→帧率
                # 是合理译法，backrooms 实证）
                hits.append(f"{t}={trans}"
                            if t.casefold() != trans.casefold()
                            else f"{t}=专名保留原文（译中文通称不判错）")
                if len(hits) >= 5:
                    break
        return "；".join(hits)

    review_items: list[ReviewItem] = []
    for it, e in zip(items, item_entries):
        if e not in to_review:
            continue
        ctx_hint = ""
        evidence = context_evidence_by_id.get(
            e.id if e.id is not None else f"{e.file_id}:{e.key_path}")
        if evidence:
            ctx_hint = "；".join(
                f"「{ev.translation}」({ev.kind}, 置信 {ev.confidence:.2f})"
                for ev in evidence[:3] if ev.translation)
        # C16：历史误译反例注入（本条原文/同语义单元的历史翻车现场）
        counter = counterexamples_by_id.get(
            e.id if e.id is not None else f"{e.file_id}:{e.key_path}")
        if counter:
            parts = []
            for c in counter:
                tag = (f"【{c['game']}】" if c.get("game") else "")
                reason = (f"（{c['review_reason'][:60]}）"
                          if c.get("review_reason") else "")
                parts.append(
                    f"{tag}「{c['original'][:60]}」曾被误译为"
                    f"「{c['wrong_translation'][:60]}」{reason}，勿重蹈")
            ctx_hint = (f"{ctx_hint}；" if ctx_hint else "") + \
                "历史错译反例：" + "；".join(parts)
        review_items.append(ReviewItem(
            entry_id=it.entry_id, original=it.original,
            translation=it.translation, text_type=it.text_type,
            term_hint=_term_hint_for(str(it.original), pairs),
            context_hint=ctx_hint,
            game_context_hint=game_context_hint))

    def _progress(done: int, total: int) -> None:
        # 节流：约每 10% 一条日志 + 末条必报（不刷屏）；
        # on_progress 由 GUI 接进度信号做实时 UI 反馈（不刷屏由调用方节流）
        if on_note is not None:
            step = max(1, total // 10)
            if done == total or done % step == 0:
                on_note(f"语义审核：{done}/{total} 条…")
        if on_progress is not None:
            on_progress(done, total)

    results, cancelled_count = reviewer.review_batch(
        review_items, on_progress=_progress,
        cancellation_event=cancellation_event)
    # 批审幻觉防护（2026-08-14 minato 实证：4B 批量审校成段输出
    # 「译文缺失，仅保留术语参考」罐头理由——译文实为非空且正确，
    # Time→时间/Hearts→心脏 30+ 条被误判 CRITICAL）：「译文缺失」是
    # 可验证主张，与事实矛盾（译文非空且 ≠ 原文）→ 逐条重审一次
    # （单条路径噪声少、无批内互相干扰）。重审结果不再防护（防循环）；
    # 每条目至多一次兜底（批量/单条路径统一在此处理）。
    # 2026-08-24 扩展：同样可验证的「否定漏译/语义相反」罐头理由——
    # come-back 实证 4B 把 not only 强调句当「否定句漏译否定词」、把
    # 已带「不」的译文当「否定逻辑错误」；「原文剥离强调后仍含否定、
    # 译文却无对应中文否定」才成立，否则与事实矛盾 → 逐条重审兜底。
    failed_entry_ids_ = {
        it.entry_id for it, e in zip(items, item_entries)
        if e.status == "failed"}
    for eid, r in list(results.items()):
        if r.is_error:
            continue
        item = next((it for it in review_items
                     if it.entry_id == eid), None)
        if item is None or not str(item.translation or "").strip():
            continue
        if str(item.translation).strip() == str(item.original).strip():
            continue
        if _reason_claims_missing_translation(r.reason):
            if on_note:
                on_note(f"语义审核：条目 {eid} 判定与事实矛盾（译文非空却报"
                        f"缺失）→ 逐条重审")
            results[eid] = reviewer.review_one(item)
        elif (_reason_claims_negation_dropped(r.reason)
              and not _translation_dropped_negation(
                  item.original, item.translation)):
            if on_note:
                on_note(f"语义审核：条目 {eid} 判定与事实矛盾（译文已含否定"
                        f"却报否定漏译）→ 逐条重审")
            results[eid] = reviewer.review_one(item)
        elif _reason_claims_language_error(r.reason):
            # C12b：把原文译成中文是本职工作，「语言错误」类理由恒不
            # 成立 → 批审判定不可信，逐条重审兜底
            if on_note:
                on_note(f"语义审核：条目 {eid} 判定与事实矛盾（把原文译成"
                        f"中文是任务本身，语言类理由不成立）→ 逐条重审")
            results[eid] = reviewer.review_one(item)
        # 2026-08-26 专名/品牌保留回显类机械门兜底（'Out of the Loop
        # studio' 案例）：条目带 proper_name_echo 豁免打标（原文与译文
        # 字母序列相同、模型保留原文是合理行为）且 4B 判 PASS 时——按
        # 语义审核这已经是可用译文，但机械门把「回显保留」视为未翻译
        # （untranslated_text/target_script_mismatch，status=failed）→
        # 若放行则坏候选进发布槽；若强推重译则无 translator 时直接
        # BLOCKED 留人工（近于永久卡死）。带 proper_name_echo 标记的
        # 回显是「审校确认的专名保留」→ 视同 APPROVED（机械门与语义
        # 审核意见一致的可用译文，可直接发布；写回端 meta 打标可供
        # 审计筛选）。仅适用于 proper_name_echo（纯专名/品牌保留），
        # 普通回显（untranslated_text 无豁免标记）仍走强制重译闭环。
        elif (item.entry_id in failed_entry_ids_
              and r.level == "PASS"
              and str((item_entries and _entry_for(
                  items, item_entries, item.entry_id).meta or {}).get(
                      "echo_exempt", "")).startswith("proper_name")):
            if on_note:
                on_note(f"语义审核：条目 {item.entry_id} 为专名保留回显"
                        f"（4B PASS）→ 视同通过，可发布")
            results[eid] = replace(r, level="PASS")
            _bypass = _entry_for(items, item_entries, item.entry_id)
            if _bypass is not None:
                _bypass.status = STATUS_TRANSLATED
                _bypass.meta = dict(_bypass.meta)
                _bypass.meta["review_outcome"] = "APPROVED"
                _bypass.meta["review_level"] = "PASS"
                _bypass.meta["review_blocked"] = False
                _bypass.meta["quality_passed"] = True
                _bypass.meta["quality_reasons"] = []
                _bypass.meta["echo_exempt"] = "proper_name_reviewed"
    # P0-4：机械失败条目（quality_failed 强制通道）——任何判定级别都不能
    # 让机械拒绝的候选直接发布；4B 与机械门意见相左（PASS/MINOR）时以
    # 机械证据为准，强制进入反馈重译（重译输出再过机械门才可 APPROVED）。
    failed_entry_ids = {
        it.entry_id for it, e in zip(items, item_entries)
        if e.status == "failed"}
    summary["cancelled"] = cancelled_count
    summary["results"] = results
    verdict_results = [r for r in results.values() if not r.is_error]
    error_results = [r for r in results.values() if r.is_error]
    summary["reviewed"] = len(verdict_results)
    summary["errors"] = len(error_results)
    for level in _LEVELS:
        summary["levels"][level] = 0
    summary["levels"]["REVIEW_ERROR"] = len(error_results)
    for r in verdict_results:
        summary["levels"][r.level] = summary["levels"].get(r.level, 0) + 1
    flagged = [r for r in verdict_results if r.needs_optimization]
    summary["flagged"] = flagged
    # Phase B-5：送审时快照——重译/清空会就地改 entry.translation，
    # 错误译文必须在处置分发前定格（错误例 = 重译前的坏译文）
    wrong_by_id = {it.entry_id: e.translation
                   for it, e in zip(items, item_entries)}
    # 报告错译栏数据源（2026-08-14 提速后审核输出只含 level+reason，
    # suggestion 恒空——报告 CRITICAL 明细的错译栏不能再依赖
    # result.suggestion，改从本快照取）
    summary["wrong_translations"] = dict(wrong_by_id)
    # 处置分发 + 反馈重译闭环 + 终态化（Phase A）
    # 2026-08-14 用户实证「审核完成后到最终结果出来之间什么提示都
    # 没有」：判定（review_batch）有逐条进度，处置阶段（反馈重译 +
    # 再审收敛，逐条串行 2-30 秒/条）完全静默——UI 停在「N/N 条
    # 完成」后界面静止数分钟。判定完成先广播阶段切换，处置期间按
    # ≥5 条节流广播，让等待有可见进度。
    # 参考注入映射（2026-08-14 用户要求：审校途中的重译同样按需
    # 检索注入——首审阶段检索的术语命中（词对）+ 语境证据（向量
    # 知识库/语境库，context_exact/similar/rerank）随条目带入重译
    # 与再审，翻译模型看到与本条文本相关的参考译例）
    hints_by_id = {it.entry_id: (it.term_hint, it.context_hint)
                   for it in review_items}

    def _hint_for_entry(eid: str) -> tuple[str, str]:
        return hints_by_id.get(eid, ("", ""))

    dispatch_total = len(results)
    if on_note:
        on_note(f"语义审核：判定完成（{dispatch_total} 条），正在处置"
                f"不合格条目（反馈重译 + 再审收敛）…")
    # 处置进度回调（2026-08-20 全链路 3-3-3-1 进度条 60-90% 段）：
    # 判定阶段有 on_progress（逐条 4B 判定），处置阶段（反馈重译 +
    # 再审收敛，串行 2-30s/条）原本完全静默——UI 停在「N/N 条完成」
    # 后界面静止数分钟。on_disposition_progress 按处置分发计数回调，
    # 让等待有可见进度。total=0（无不合格条目）时调用方直接跳满该段。
    if on_disposition_progress is not None and dispatch_total > 0:
        on_disposition_progress(0, dispatch_total)
    dispatched = 0
    persisted: list[TextEntry] = []
    for r in results.values():
        entry = _entry_for(items, item_entries, r.entry_id)
        if entry is None:
            continue
        dispatched += 1
        # 处置进度回调（每条处置后推进 60-90% 段，串行重译期间可见）
        if on_disposition_progress is not None:
            on_disposition_progress(dispatched, dispatch_total)
        if on_note and (dispatched == dispatch_total
                        or dispatched % 5 == 0):
            on_note(f"语义审核：处置中 {dispatched}/{dispatch_total} 条…")
        if on_note:
            # 2026-08-14 用户要求：审校逐条日志——所有需审核文本的
            # 原文/译文/判定/理由/建议全量输出（与翻译日志同风格，
            # 供人工追溯；「莫名未通过」必须有痕可查）
            orig = summary["originals"].get(r.entry_id, "")
            if r.is_error:
                # 只输出日志，不 continue——错误处置走下方 is_error 分支
                on_note(f"[审核] 审核错误（{r.error}）：{orig}")
            level_zh = {"PASS": "通过", "MINOR": "轻微问题",
                        "MAJOR": "较大问题",
                        "CRITICAL": "严重问题"}.get(r.level, r.level)
            reason = r.reason or "；".join(
                str(i.get("detail") or "") for i in r.issues
                if i.get("detail"))
            suggestion = r.suggestion or "；".join(
                str(i.get("suggestion") or "") for i in r.issues
                if i.get("suggestion"))
            on_note(f"[审核] 原文：{orig}")
            on_note(f"[审核] 译文：{wrong_by_id.get(r.entry_id, '')}")
            on_note(f"[审核] 判定：{level_zh} · 理由：{reason}"
                    + (f" · 建议：{suggestion}" if suggestion else ""))
        if r.is_error:
            # 审核错误：显式 REVIEW_ERROR 终态，不得转 PASS、不沉淀
            apply_outcome(entry, REVIEW_ERROR, level=r.level,
                          reason=r.reason, error_kind=r.error)
            persisted.append(entry)
            if on_note:
                on_note(f"语义审核：条目 {r.entry_id} 审核错误"
                        f"（{r.error}）→ 不可发布")
            continue
        if r.entry_id in failed_entry_ids:
            # P0-4：机械失败候选走强制重译闭环（无 translator 时
            # fail-closed → BLOCKED，机械坏候选不留在发布槽位）
            if translator is None:
                apply_outcome(entry, BLOCKED, level=r.level,
                              reason=(r.reason or _failed_reason(entry)),
                              rejected_candidate=_failed_review_candidate(
                                  entry),
                              clear_translation=True)
                summary["blocked"] += 1
                _memory_apply(memory, entry, "CRITICAL", model_name, lang)
                persisted.append(entry)
                continue
            extra = ""
            if r.level in ("PASS", "MINOR"):
                # 4B 认为可用但机械门判失败：以机械证据为准，强制修正。
                # 反馈带具体修正指引（2026-08-15 minato 实证：原因列表
                # 干巴巴，模型重译再被同一机械门拒 → BLOCKED）
                reasons = entry.quality_reasons or (entry.meta or {}).get(
                    "quality_reasons") or ()
                extra = (_failed_reason(entry) + "；"
                         + _quality_fix_hints(reasons))
            hint = _hint_for_entry(r.entry_id)
            outcome = _retranslate_with_feedback(
                translator, entry, r, on_note, extra_feedback=extra,
                reviewer=reviewer, app_dir=app_dir,
                term_hint=hint[0], context_hint=hint[1],
                game_context_hint=game_context_hint)
            if outcome == "converged":
                summary["converged"] += 1
            elif outcome == "blocked":
                summary["blocked"] += 1
            elif outcome == "error":
                summary["errors"] += 1
            summary["retranslated"] += 1
            _memory_apply(memory, entry, _memory_level_for(entry),
                          model_name, lang)
            persisted.append(entry)
            continue
        action = r.apply_verdict(entry)
        if action in ("revise", "retranslate"):
            if translator is not None:
                hint = _hint_for_entry(r.entry_id)
                outcome = _retranslate_with_feedback(
                    translator, entry, r, on_note,
                    reviewer=reviewer, app_dir=app_dir,
                    term_hint=hint[0], context_hint=hint[1],
                    game_context_hint=game_context_hint)
                if outcome == "converged":
                    summary["converged"] += 1
                elif outcome == "blocked":
                    summary["blocked"] += 1
                elif outcome == "error":
                    summary["errors"] += 1
                summary["retranslated"] += 1
        # 记忆门禁（按终态判定：approved 系进记忆，其余移除坏记忆）
        _memory_apply(memory, entry, _memory_level_for(entry),
                      model_name, lang)
        persisted.append(entry)
    # 终态分布 + 原子持久化（Phase A：重启后写回门仍正确）
    # P0-4：无候选机械失败条目的 BLOCKED 同样原子落库（重启仍阻断）
    persisted.extend(failed_without_candidate)
    for e in persisted:
        outcome = e.meta.get("review_outcome") or "PENDING"
        summary["outcomes"][outcome] = summary["outcomes"].get(outcome, 0) + 1
    if store is not None and persisted:
        store.batch_update_translation_results(persisted)
    # Phase B-5（审计 P1-7）：结构化审核失败闭环——CRITICAL/MAJOR 语义
    # 错误与 REVIEW_ERROR 管线错误全部记录（收敛/未收敛均记）；只有
    # 终态 APPROVED 系（二审收敛或人工确认）译文可作正确例，其余终态
    # correct_translation 留空（坏译文不当正确例学习）。错误译文取送审
    # 时快照（重译前的坏译文），locator+game 作幂等 pattern。
    failures: list[dict] = []
    for r in [*flagged, *error_results]:
        entry = _entry_for(items, item_entries, r.entry_id)
        if entry is None:
            continue
        outcome = entry.meta.get("review_outcome") or "PENDING"
        reason = r.reason
        if r.is_error:
            error_kind = entry.meta.get("review_error_kind") or r.error
            reason = f"{error_kind}: {reason}" if reason else error_kind
        failures.append(build_review_failure(
            game=game_name,
            model=model_name or "",
            error_type=ERROR_REVIEW if r.is_error else r.level,
            original=summary["originals"].get(r.entry_id, ""),
            wrong_translation=wrong_by_id.get(r.entry_id, ""),
            correct_translation=(entry.translation
                                 if outcome in (APPROVED, APPROVED_MINOR)
                                 else ""),
            review_reason=reason,
            suggestion=r.suggestion,
            converged=outcome in (APPROVED, APPROVED_MINOR),
            final_outcome=outcome,
            locator=summary["locators"].get(r.entry_id, r.entry_id)))
    summary["review_failures"] = failures
    # #48（2026-08-14 用户要求「审校后输出记录，记录所有待审核文本
    # 信息」）：全量送审明细——每条送审文本的原文/译文/AI 判定/未通过
    # 原因/终态全部留档（write_review_report 全量章节）。PASS 也记录，
    # 不只看不合格项；原文保留富文本标签（与审核模型输入一致）。
    text_types = {it.entry_id: it.text_type for it in items}
    detail: list[dict] = []
    for r in results.values():
        entry = _entry_for(items, item_entries, r.entry_id)
        if entry is None:
            continue
        outcome = entry.meta.get("review_outcome") or "PENDING"
        detail.append({
            "locator": summary["locators"].get(r.entry_id, r.entry_id),
            "text_type": text_types.get(r.entry_id, ""),
            "original": summary["originals"].get(r.entry_id, ""),
            "translation": wrong_by_id.get(r.entry_id, ""),
            "final_translation": (
                entry.translation if outcome in (APPROVED, APPROVED_MINOR)
                else ""),
            "level": r.level,
            "reason": r.reason,
            "suggestion": r.suggestion,
            "issues": [dict(i) for i in r.issues],
            "overall_score": r.overall_score,
            "dimensions": dict(r.dimensions),
            "error": r.error,
            "quality_reasons": list(entry.meta.get("quality_reasons") or ()),
            "outcome": outcome,
            "review_round": entry.meta.get("review_round"),
        })
    summary["detail"] = detail
    # 术语词对沉淀（C5 语境保护门禁链；只对 VERDICT flagged，错误不沉淀）
    if flagged:
        originals = summary["originals"]
        contexts: dict[str, str] = {}
        for r in flagged:
            orig = (originals.get(r.entry_id, "") or "").strip()
            for term, _trans in extract_term_pairs(
                    flagged, originals):
                if term and term in orig and term not in contexts:
                    contexts[term] = orig[:120]
        for term, trans in extract_term_pairs(flagged, originals):
            try:
                # Phase B-3（审计 P1-4）：结构化 DepositResult——
                # 候选/激活/拒绝/冲突分别记账，不再一律计入 pairs_added
                result = glossary.add_reviewed(
                    term, trans, context=contexts.get(term, ""),
                    game=game_name)
                if result.status == "REJECTED":
                    summary["pairs_rejected"][term] = result.reason
                elif result.status == "CONFLICT":
                    summary["pairs_conflict"][term] = result.reason
                else:
                    summary["pairs_added"] += 1
                    if result.status == "CANDIDATE":
                        summary["pairs_candidate"] += 1
                    elif result.status == "ACTIVATED":
                        summary["pairs_activated"] += 1
            except Exception:  # noqa: BLE001 - 词对沉淀失败不阻断审核主流程
                pass
    return summary


def _memory_level_for(entry: TextEntry) -> str:
    """按终态推导记忆门禁用级别：approved 系进记忆，其余移除坏记忆。"""
    outcome = entry.meta.get("review_outcome")
    if outcome in (APPROVED, APPROVED_MINOR):
        return "PASS"
    if entry.meta.get("review_level") in ("PASS", "MINOR"):
        return "PASS"
    return "CRITICAL"


def _entry_for(items: list[ReviewItem], entries: list[TextEntry],
               entry_id: str) -> TextEntry | None:
    for it, e in zip(items, entries):
        if it.entry_id == entry_id:
            return e
    return None


def _apply_quality_compat(translator, entry: TextEntry, candidate: str,
                          skip_consistency: bool = False) -> bool:
    """_apply_quality 兼容封装：新 BatchTranslator 支持 skip_consistency
    （反馈重译豁免批内一致性）；旧实现/测试桩无该参数 → TypeError
    且错误信息含参数名时回退普通调用（只回退参数缺失，不吞内部错误）。"""
    try:
        return bool(translator._apply_quality(
            entry, candidate, skip_consistency=skip_consistency))
    except TypeError as exc:
        if "skip_consistency" not in str(exc):
            raise
        return bool(translator._apply_quality(entry, candidate))


def _retranslate_with_feedback(translator, entry: TextEntry,
                               result: ReviewResult,
                               on_note: Callable[[str], None] | None,
                               max_rounds: int = 2,
                               extra_feedback: str = "",
                               reviewer=None, app_dir=None,
                               term_hint: str = "",
                               context_hint: str = "",
                               game_context_hint: str = "") -> str:
    """T1-4/T1-5 反馈式重译 + 再审收敛（上限 2 轮）。

    注入审核理由重译 → 过质量门 → 再审（若再审器可用）→
    仍 CRITICAL → BLOCKED（保留坏译文供人工复核，恢复安全状态）。

    返回 'converged'（降到 MINOR 以下或 PASS）| 'blocked' |
    'error'（再审判定失败——fail-closed，绝不转 PASS）。

    Phase A（2026-08-13 审计 §5 P0-6/P0-7）：
    - 再审失败不再「保守放行 PASS」→ 显式 REVIEW_ERROR（不可发布）；
    - BLOCKED 是完整领域状态：status=blocked、quality_passed=False、
      发布译文清空、坏译文存入 rejected_candidate——写回门与写回端
      双重拒绝，重启后状态仍正确。

    extra_feedback（P0-4）：机械失败条目（quality_failed 强制通道）在
    4B 判 PASS/MINOR 时附加机械失败原因——机械证据优先，强制重译；
    收敛路径恢复 status=translated（机械失败条目的重译输出已过机械门，
    可发布）。
    """
    feedback = extra_feedback
    if result.reason:
        feedback = f"{feedback}；{result.reason}" if feedback else result.reason
    if result.suggestion:
        feedback = (f"{feedback}；建议译文：{result.suggestion}"
                    if feedback else f"建议译文：{result.suggestion}")
    last_translation = entry.translation
    for round_no in range(1, max_rounds + 1):
        # 4B 通道优先（2026-08-14 用户实证：request_error 全批——审核
        # 4B 常驻后翻译 1.8B 换入 OOM，反馈重译必败；4B 已在显存且带
        # 固定游戏本地化角色，承担重译零切换、质量通常更高）。4B 输出
        # 仍过机械质量门（translator._apply_quality，纯本地计算）——
        # 门不过回退 1.8B 通道再试，两者都败才 BLOCKED。
        candidate = ""
        if reviewer is not None:
            try:
                candidate = reviewer.retranslate_with_feedback(
                    str(entry.original)[:600],
                    str(entry.translation or "")[:600],
                    feedback, max_tokens=1024,
                    text_type=text_type_for(entry.meta),
                    term_hint=term_hint[:120],
                    context_hint=context_hint[:120],
                    game_context_hint=game_context_hint[:300])
            except Exception:  # noqa: BLE001 - 4B 通道失败回退 translator
                candidate = ""
        ok, translation = False, ""
        if candidate:
            if translator is not None:
                # 反馈重译豁免批内一致性（2026-08-14 minato 实证：
                # 翻译阶段缓存的坏译文以 consistency_mismatch 拒绝
                # 正确重译——Pan 先生→左滑、Hearts 心脏→爱心 全被杀，
                # 重译收敛率 45% 的直接根因）
                ok = _apply_quality_compat(
                    translator, entry, candidate, skip_consistency=True)
                translation = entry.translation if ok else ""
                if not ok:
                    # 多行候选确定性修复（minato 实证：4B 稳定输出
                    # 「左滑\n左移」双候选，单行原文下 newline 恒败）
                    entry.translation = last_translation
                    repaired = _repair_multiline_candidate(
                        str(entry.original), candidate)
                    if repaired:
                        ok = _apply_quality_compat(
                            translator, entry, repaired,
                            skip_consistency=True)
                        translation = entry.translation if ok else ""
                        if ok and on_note:
                            on_note(f"重译候选多行修复："
                                    f"{candidate!r} → {repaired!r}")
            else:
                entry.translation = candidate
                ok = True
                translation = candidate
            if not ok:
                # 4B 输出被机械门拒绝 → 恢复原译文，防污染下一通道的
                # 「上次译文」反馈（_apply_quality 已就地改 entry）
                entry.translation = last_translation
        if not ok or not translation:
            try:
                ok, translation = translator.retranslate_with_feedback(
                    entry, feedback, round_no=round_no)
                if not ok and translator is not None:
                    # 1.8B 兜底候选同样做多行修复重验（其内部已过门
                    # 失败，返回的 candidate 保留在 translation 参数）
                    entry.translation = last_translation
                    raw_candidate = translation or ""
                    repaired = _repair_multiline_candidate(
                        str(entry.original), raw_candidate)
                    if repaired:
                        ok = _apply_quality_compat(
                            translator, entry, repaired,
                            skip_consistency=True)
                        translation = entry.translation if ok else ""
                        if ok and on_note:
                            on_note(f"重译候选多行修复："
                                    f"{raw_candidate!r} → {repaired!r}")
                        if not ok:
                            entry.translation = last_translation
            except Exception:  # noqa: BLE001 - 重译失败 → BLOCKED 终止循环
                ok, translation = False, ""
        if not ok or not translation:
            # P3（2026-09-06 fromivan 实证）：BLOCKED 必须带具体原因——
            # 此前两个 BLOCKED 出口不传 reason，meta["review_reason"]
            # 永远缺失，blocked.txt 只剩「BLOCKED（CRITICAL）」无任何
            # 理由（33 条全部无审核理由），人工无从复核（「莫名卡住」）。
            # 证据合成：最近一次机械门失败原因（entry.quality_reasons
            # 由 _apply_quality 落在条目上，是本轮最具体的拒绝证据，
            # 反馈串里没有——反馈只含首审理由与上轮再审理由）+ 重译
            # 反馈（首审理由/建议，随轮次更新）。
            reasons = entry.quality_reasons or (entry.meta or {}).get(
                "quality_reasons") or ()
            mech = ""
            if reasons:
                mech = ("机械质量门拒绝（"
                        + "、".join(str(r) for r in reasons)
                        + "）：" + _quality_fix_hints(reasons))
            reason = "；".join(
                p for p in (mech, str(feedback or result.reason or ""))
                if p)
            apply_outcome(entry, BLOCKED, level=result.level,
                          reason=reason[:400] or _failed_reason(entry),
                          rejected_candidate=last_translation,
                          rounds=round_no, clear_translation=True)
            return "blocked"
        last_translation = translation
        entry.translation = translation
        entry.meta = dict(entry.meta)
        entry.meta["review_level"] = "RETRANSLATED"
        entry.meta["review_round"] = int(round_no)
        # 再审（1 轮内判定收敛；再审失败 → 显式错误，不得转 PASS）
        # #20：复用主审核的 reviewer/app_dir——此前每轮新建 SemanticReviewer
        # 且回退 cwd 找模型（#17 已修主路径，再审路径漏掉），模型在
        # resource_dir 时再审必挂 TRANSPORT_ERROR → 重译永不收敛
        # 2026-08-14：再审与首审注入同一术语/语境参考（此前再审无
        # hint，同一译文两次判定输入不一致，收敛判定波动）
        re_result = _re_review(entry, reviewer=reviewer, app_dir=app_dir,
                               term_hint=term_hint,
                               context_hint=context_hint,
                               game_context_hint=game_context_hint)
        if re_result is None or re_result.is_error:
            apply_outcome(entry, REVIEW_ERROR,
                          level=re_result.level if re_result else "RETRANSLATED",
                          reason=(re_result.reason if re_result
                                  else "再审服务不可用")[:400],
                          error_kind=(re_result.error if re_result
                                      else TRANSPORT_ERROR),
                          rejected_candidate=last_translation)
            return "error"
        if re_result.level in ("PASS", "MINOR"):
            outcome = APPROVED if re_result.level == "PASS" else APPROVED_MINOR
            apply_outcome(entry, outcome, level=re_result.level,
                          reason=re_result.reason)
            # #47（2026-08-14 全量审校）：重译收敛后打「已重译」标记——
            # 审校页「已重译」筛选可查（有问题的文本重返审校确认，人工
            # 确认/修改后即可发布；不设硬门：已过两轮再审收敛，发布资格
            # 与普通 APPROVED 相同）。apply_outcome 后写：其 _safe_meta
            # 拷贝过 dict，标记不丢。
            entry.meta = dict(entry.meta)
            entry.meta["retranslated"] = True
            # P0-4：机械失败条目收敛后恢复可发布状态（重译输出已过机械门；
            # 状态不恢复则写回门因 status != translated 永久拒绝）
            entry.status = STATUS_TRANSLATED
            return "converged"
        feedback = re_result.reason or feedback   # 新一轮反馈
    # P3：max_rounds 耗尽的 BLOCKED 同样必须带原因——feedback 此时
    # 持有最近一轮再审理由（1761 行已更新）或首审理由/建议，机械门
    # 拒绝证据取条目上最后落盘的 quality_reasons。
    reasons = entry.quality_reasons or (entry.meta or {}).get(
        "quality_reasons") or ()
    mech = ""
    if reasons:
        mech = ("机械质量门拒绝（"
                + "、".join(str(r) for r in reasons)
                + "）：" + _quality_fix_hints(reasons))
    reason = "；".join(
        p for p in (mech, str(feedback or result.reason or "")) if p)
    apply_outcome(entry, BLOCKED, level=result.level,
                  reason=reason[:400] or _failed_reason(entry),
                  rejected_candidate=last_translation,
                  rounds=max_rounds, clear_translation=True)
    return "blocked"


def _re_review(entry: TextEntry, reviewer: SemanticReviewer | None = None,
               app_dir: str | Path | None = None,
               online_cfg=None, term_hint: str = "",
               context_hint: str = "",
               game_context_hint: str = "") -> ReviewResult | None:
    """再审一次（收敛判定）。失败返回 None（保守放行）。

    term_hint/context_hint/game_context_hint 与首审一致（2026-08-14：
    同一译文两次判定输入一致，收敛判定不因参考缺失而波动）。
    """
    if reviewer is None:
        reviewer = SemanticReviewer(app_dir=app_dir or Path.cwd(),
                                    online_cfg=online_cfg)
    return reviewer.review_one(ReviewItem(
        entry_id=f"re_{entry.id or entry.key_path}",
        original=str(entry.original)[:600],
        translation=str(entry.translation)[:600],
        text_type=text_type_for(entry.meta),
        term_hint=term_hint[:120],
        context_hint=context_hint[:120],
        game_context_hint=game_context_hint[:300],
    ))


def write_review_report(summary: dict, report_path: str | Path,
                        game_name: str = "") -> Path:
    """T1-7 审核日志：生成 review_report.md（每游戏一份）。

    内容：送审数/各级分布/重译收敛率/CRITICAL 明细（原文/错译/
    审核理由/正确译文——含 suggestions）。

    #43 阶段 F（重构指令 §13/§14）：补充风险分布（risk_levels）、
    结构化失败明细（review_failures）、LLM 维度分（overall_score/
    dimensions）——全部从 summary 可选字段读取，旧调用方（无新字段）
    输出与旧版完全一致（零破坏）。
    """
    path = Path(report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    levels = summary.get("levels") or {}
    flagged = summary.get("flagged") or []
    originals = summary.get("originals") or {}
    locators = summary.get("locators") or {}
    # flagged 口径兼容（P4 记录落盘）：review_entries 返回 list[
    # ReviewResult]（可迭代取 CRITICAL）；持久化/JSON 还原形态可能是
    # 计数 int——此时跳过 CRITICAL 逐条明细，只走 detail 全量明细节。
    if isinstance(flagged, int):
        flagged = []
    criticals = [r for r in flagged if r.level == "CRITICAL"]
    sent = summary.get("sent", 0)
    total = summary.get("reviewed", 0)
    converged = summary.get("converged", 0)
    blocked = summary.get("blocked", 0)
    retranslated = summary.get("retranslated", 0)
    lines = [
        f"# 审核日志 {game_name}".rstrip(),
        "",
        f"- 送审：{sent} 条（送审率 {summary.get('rate', 0.0):.0%}）"
        f" · 判定：{total} 条",
        f"- 级别分布：PASS {levels.get('PASS', 0)} / MINOR "
        f"{levels.get('MINOR', 0)} / MAJOR {levels.get('MAJOR', 0)} / "
        f"CRITICAL {levels.get('CRITICAL', 0)} / PARSE_FAIL "
        f"{levels.get('PARSE_FAIL', 0)}",
        f"- 反馈重译：{retranslated} 次 · 收敛 {converged} · 未收敛阻塞 "
        f"{blocked}（重译收敛率 "
        f"{converged / max(1, retranslated):.0%}）",
        f"- 术语词对沉淀：+{summary.get('pairs_added', 0)}"
        f"（候选 {summary.get('pairs_candidate', 0)} · 激活 "
        f"{summary.get('pairs_activated', 0)} · 冲突 "
        f"{len(summary.get('pairs_conflict', {}))} · 拒绝 "
        f"{len(summary.get('pairs_rejected', {}))}）",
    ]
    # #43 阶段 F：风险分布（重构指令 §14 分流统计）
    risk_levels = summary.get("risk_levels") or {}
    if risk_levels:
        lines.append(
            "- 风险分布：LOW " + str(risk_levels.get("LOW", 0))
            + " / MEDIUM " + str(risk_levels.get("MEDIUM", 0))
            + " / HIGH " + str(risk_levels.get("HIGH", 0))
            + " / CRITICAL " + str(risk_levels.get("CRITICAL", 0)))
    failures = summary.get("review_failures") or []
    if failures:
        lines.append(f"- 结构化失败：{len(failures)} 条"
                     f"（MAJOR/CRITICAL 语义错误 + 审核管线错误，"
                     f"知识库 fail_case 域已记录反例）")
    # C5 门禁拒绝清单（术语沉淀时产生；runner 报告并入统一格式）
    rejected = summary.get("pairs_rejected") or {}
    if rejected and isinstance(rejected, dict):
        lines += [
            "## C5 门禁拒绝清单（高频普通词单 token，无语境强制会"
            "误杀其他语境，不入全局库）", "",
        ]
        lines += [f"- `{term}`：{reason}"
                  for term, reason in sorted(rejected.items())]
        lines += [""]
    lines += ["", "## CRITICAL 明细（语义错译，需人工复核）", ""]
    if not criticals:
        lines.append("无（本轮无 CRITICAL 级错译）。")
    # 终译对照（2026-08-14 修复）：重译收敛条目的最终译文按 locator
    # 关联，与错译并列展示——人工复核可直接判断「重译是否修复」。
    final_by_loc = {
        str(d.get("locator") or ""): d
        for d in (summary.get("detail") or [])}
    for r in criticals:
        loc = locators.get(r.entry_id, "")
        lines.append(f"### {loc or r.entry_id}")
        lines.append(f"- 原文：{originals.get(r.entry_id, '')}")
        lines.append(f"- 错译：{_translation_of(r, summary)}")
        lines.append(f"- 审核理由：{r.reason}")
        fin = final_by_loc.get(str(loc), {})
        if fin.get("final_translation"):
            lines.append(f"- 重译后：{fin['final_translation']}"
                         f"（终态 {fin.get('outcome', '')}"
                         f"{' · 轮次 ' + str(fin['review_round'])
                          if fin.get('review_round') else ''}）")
        # #43 阶段 E：LLM 综合分/维度分透出（有值才显示）
        if r.overall_score:
            lines.append(f"- 综合分：{r.overall_score}/100")
            if r.dimensions:
                dims = "、".join(
                    f"{k} {v}" for k, v in list(r.dimensions.items())[:5])
                lines.append(f"- 维度分：{dims}")
        for issue in r.issues:
            if issue.get("suggestion"):
                lines.append(f"- 正确译文建议：{issue['suggestion']}")
        lines.append("")
    # #43 阶段 F：结构化失败明细（幂等失败留档的原文/错译/正确译文）
    if failures:
        lines += ["## 结构化失败明细（知识库反例，可人工核对）", ""]
        for fail in failures:
            lines.append(f"- 原文：{fail.get('original', '')}")
            wrong = fail.get("wrong_translation") or fail.get(
                "translation", "")
            if wrong:
                lines.append(f"  错译：{wrong}")
            if fail.get("correct_translation"):
                lines.append(f"  正确译文：{fail['correct_translation']}")
            lines.append(f"  判定：{fail.get('level', '')} · "
                         f"{fail.get('reason', '')[:120]}")
            if fail.get("game") and fail.get("locator"):
                lines.append(f"  来源：{fail['game']}:{fail['locator']}")
            lines.append("")
    # #48（2026-08-14 用户要求）：全量送审明细——每条待审核文本的
    # 原文/译文/AI 判定/未通过原因/终态完整留档（PASS 也记录，供
    # 人工逐条追溯；原文保留富文本标签，与审核模型输入一致）。
    # 旧 summary（无 detail 字段）输出与旧版完全一致（零破坏）。
    detail = summary.get("detail") or []
    if detail:
        level_zh = {"PASS": "通过", "MINOR": "轻微问题",
                    "MAJOR": "较大问题", "CRITICAL": "严重问题",
                    "REVIEW_ERROR": "审核错误"}
        lines += [f"## 全量送审明细（{len(detail)} 条）", ""]
        for d in detail:
            head = f"### {d.get('locator') or d.get('entry_id', '')}"
            if d.get("text_type"):
                head += f"（{d['text_type']}）"
            lines.append(head)
            lines.append(f"- 原文：{d.get('original', '')}")
            wrong = d.get("translation", "")
            if wrong:
                lines.append(f"- 送审译文：{wrong}")
            final_t = d.get("final_translation", "")
            if final_t and final_t != wrong:
                lines.append(f"- 终译（重译收敛）：{final_t}")
            level = level_zh.get(d.get("level", ""), d.get("level", ""))
            reason = d.get("reason", "")
            if d.get("error"):
                lines.append(f"- AI 判定：{d.get('error')} · {reason}")
            else:
                line = f"- AI 判定：{level}"
                if d.get("overall_score"):
                    line += f"（{d['overall_score']}/100）"
                lines.append(line)
                if reason:
                    lines.append(f"  理由：{reason}")
            if d.get("suggestion"):
                lines.append(f"  建议：{d['suggestion']}")
            dims = d.get("dimensions") or {}
            if dims:
                lines.append("  维度分：" + "、".join(
                    f"{k} {v}" for k, v in list(dims.items())[:5]))
            qreasons = d.get("quality_reasons") or []
            if qreasons:
                lines.append(f"- 未通过原因（机械质量门）："
                             f"{'、'.join(str(r) for r in qreasons)}")
            outcome = d.get("outcome", "")
            if outcome:
                tail = f"- 终态：{outcome}"
                if d.get("review_round"):
                    tail += f" · 重译轮次：{d['review_round']}"
                lines.append(tail)
            lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def _translation_of(result: ReviewResult,
                    summary: dict | None = None) -> str:
    """取错译文本（2026-08-14 修复：审核输出提速为 level+reason 后
    suggestion 恒空，错译栏曾全部显示「（无译文记录）」——改为从
    summary["wrong_translations"] 送审快照取；快照缺失时回退
    suggestion 兼容旧数据）。"""
    if summary is not None:
        wrong = (summary.get("wrong_translations") or {}).get(
            result.entry_id, "")
        if wrong:
            return str(wrong)
    return result.suggestion or "（无译文记录）"
