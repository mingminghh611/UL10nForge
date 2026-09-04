from __future__ import annotations
import json
from dataclasses import dataclass, field

# 延迟导入：should_skip / is_engine_string_core 在 _final_structural_backstop
# 内局部导入（placeholders 反向 import models 里的 TextEntry/states，模块级
# 硬导入会成环；函数级导入已由 Python 惰性求值保证安全）。
import re as _re
# C#/引擎转义换行符（字面 2 字符 \n / \r / \t）：多行对话/HUD 显示模板的
# 合法结构，硬结构判定前保护替换（见 _final_structural_backstop）。
_PROTECT_ESCAPES = _re.compile(r"\\[nrt]")

# .NET 程序集限定类型名的「值形态」（Unity 序列化字段值，非字段路径）：
#   'GameMaster, Assembly-CSharp' / 'System.Boolean, mscorlib' / 'UnityEngine.
#   EventSystems.UnityEvent, UnityEngine.UI'。m_TargetAssemblyTypeName 等字段
#   已由字段名级证据（_EVENT_BINDING_FIELDS）在 typetree 路径跳过；rawstr
#   路径无字段名，靠值形态兜底。至少一边带点才命中（F53 _NET_ASSEMBLY_
#   QUALIFIED_TYPE 要求「一边含点」防 'Hello, world'——但裸类名 + 裸程序集
#   段（GameMaster + Assembly-CSharp 都无点）是 Unity m_TargetAssemblyTypeName
#   最常见的写法，故此处放宽到任一边无点，用第二段形态兜底：'Assembly-CSharp'
#   （连字符 PascalCase 程序集名）/'UnityEngine'（命名空间单段）在真实句子
#   的第二段几乎不出现。两段都是裸普通词（'Hello, world'）仍需防误杀——
#   用第二段是否像「程序集/命名空间段」判定：第二段含连字符大写、或以
#   UnityEngine/Assembly-/mscorlib/System/Unity 等已知程序集名为准。
_ASSEMBLY_SEGMENT = _re.compile(
    r"^(?:Assembly-CSharp|Assembly-CSharp-firstpass|mscorlib|netstandard|"
    r"UnityEngine(?:\.[A-Za-z_][A-Za-z0-9_.]*)?|Unity[A-Za-z]*|"
    r"[A-Za-z_][A-Za-z0-9_]*[.-][A-Za-z0-9_]+|System(?:\.[A-Za-z_][A-Za-z0-9_.]*)?)$")
_ASSEMBLY_QUALIFIED_VALUE = _re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.]*\s*,\s*(?P<asm>[A-Za-z_][A-Za-z0-9_.\-]*)$")
# 输入动作映射路径值：'PlayerActionsXbox/Move' / 'PlayerInUI/New action'
# （Unity InputSystem 运行时按名查找的 action 路径；编辑器默认 'New action'
# 是模板未填值）。含斜杠映射路径段 + 动作段 = 确定性的输入绑定引用。
_ACTION_MAP_PATH = _re.compile(
    r"^[A-Za-z0-9_]+/[A-Za-z][A-Za-z0-9_ ]*$")
# 音效/动画触发键值（audio_trigger）：'Boss'/'Inimigo_Trigger'/'Restaura_Vida'
# ——单值形态无法与 UI 词区分，交由 raw 对象级过滤，此处不拦（防误伤）。


def _final_structural_backstop(entry: TextEntry) -> bool:
    """翻译前最终结构兜底：**无歧义**结构文本绝不允许进翻译队列。

    本函数是 is_actionable_translation 的内部最终闸门，只拦「无论如何都不该
    进队列」的内容：
    - is_hard_structural：URL/路径/GUID/JSON/程序集限定名/纯数字/版本号/
      混合符号 token/hard structural 反模式——翻译必然破坏引用或无可译内容；
    - is_engine_string_core：确定性引擎串（着色器属性/字体名/Timeline 轨道/
      FMOD 事件/InputSystem 绑定/hex）——即使提取层标 display 也是引擎内部
      串（default sprite asset 等）。

    不做键风格标识符判定（is_key_style_identifier）：'ui_newGame' / 'text0' /
    'Level1' 这类单 token 是「上下文决定」的——真按钮/关卡名（Level1/Room2）
    与键同形，提取器已按对象语境正确分层（role=display 才进 pending），终检
    再拦会误伤正常对话名，且与既有提取逻辑重复。键风格标识符由各提取器的
    should_skip 已拦，本闸门只补提取器可能漏判的无歧义机器结构。

    白名单 UI 词豁免：'2d'/'3d'（图形设置标签）命中 hex 形态
    （is_engine_string_core('3d')=True）但却是真 UI 词——与 is_hard_structural
    的 DISPLAY_WORDS 先例一致，白名单优先于形态猜测。
    """
    from hanhua.core.engine_strings import is_engine_string_core
    from hanhua.core.placeholders import DISPLAY_WORDS, is_hard_structural
    # 先 strip：首尾空白是格式噪音不是结构信号——`is_hard_structural` 的
    # _WHITESPACE_PADDED_FRAGMENT 会误伤带换行 padding 的真显示模板
    # （'\r\nSettings\r\n\r\n{0}kg\n£{1:0.00}\r\n' 是设置项模板，必须可译）。
    # 所有核心结构判定（URL/路径/GUID/json/程序集名）在内部都 strip 后再查，
    # 故 strip 不削弱结构拦截；仅去掉「整段是空白片段」的误报。
    text = (entry.original or "").strip()
    # C# 转义换行是**显示文本的结构标记**（多行对话/HUD 模板常用字面 \n），
    # 不是噪音——`_MIXED_SYMBOL_TOKEN` 把反斜杠当强代码符号会误伤
    # （'Alpha\n\nBravo\nCharlie' 字面 \n 是合法显示模板）。保护 \n/\r/\t
    # 转义（替换为空格）后再做硬结构判定，避免把真显示文本当结构拦下。
    probe = _PROTECT_ESCAPES.sub(" ", text)
    if is_hard_structural(probe):
        return True
    # 无歧义机器结构（B9 兜底层，本闸门与硬结构判定互补）：值形态
    # 程序集限定类型名（'GameMaster, Assembly-CSharp'）与输入动作映射路径
    # （'PlayerActionsXbox/Move'）在**任何语境**都不可能是玩家显示文本——
    # 反射按名绑定/InputSystem 按名查找，翻译必断绑定（按钮无反应/输入失灵）。
    # 真显示文本不含「段,段」程序集形态（'Hello, world' 第二段是普通英文词，
    # 不是 PascalCase/连字符标识符）也不含「映射段/动作段」路径形态。
    if _ASSEMBLY_QUALIFIED_VALUE.match(text):
        m = _ASSEMBLY_QUALIFIED_VALUE.match(text)
        # 第二段须像程序集/命名空间段（Assembly-CSharp/UnityEngine/
        # mscorlib/System…），防 'Hello, world'（第二段是普通词）误杀
        if m and _ASSEMBLY_SEGMENT.match(m.group("asm")):
            return True
    if _ACTION_MAP_PATH.match(text):
        return True
    if text.casefold() in DISPLAY_WORDS:
        return False
    return is_engine_string_core(probe)

STATUS_PENDING = "pending"
STATUS_TRANSLATED = "translated"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_BLOCKED = "blocked"   # 语义审核终态：重译/再审未收敛，需人工复核


@dataclass
class TextEntry:
    file_id: str
    key_path: str          # 格式内定位路径：json 路径 a/b/3/text、xml /root/x、csv 行号、txt 行号
    original: str
    translation: str = ""
    status: str = STATUS_PENDING
    locked: bool = False
    id: int | None = None
    meta: dict = field(default_factory=dict)   # 格式相关写回元数据
    confidence: str = "medium"
    quality_reasons: tuple[str, ...] = ()


def is_actionable_translation(entry: TextEntry) -> bool:
    """Return whether this entry belongs to an automatic run scope.

    pending 与 failed 都算：翻译失败的条目不永久卡死，下次运行会重试
    （failed 卡死会让「质量门失败原因：untranslated_text N」统计残留）。
    """
    disposition = entry.meta.get("disposition")
    if disposition is not None:
        # Scanned provenance is authoritative.  Legacy rows without this field
        # retain the role-based compatibility path below.
        if str(disposition) != "translate":
            return False
        role = "display"
    else:
        role = str(entry.meta.get("role", "display"))
    confidence = str(entry.meta.get("confidence", entry.confidence))
    if not (entry.status in (STATUS_PENDING, STATUS_FAILED)
            and not entry.locked
            and role not in {"structural", "code", "key"}
            and (confidence != "low"
                 or entry.meta.get("confidence_promoted") is True)):
        return False
    # —— 翻译前最终结构兜底（识别 B 节：结构性文本绝不能进翻译队列）——
    # 本函数是全部队列入口（翻译页待翻译池 / runner run_scope / 概览计数）
    # 的单一权威判定。各提取器（rawstr/typetree/mono/il2cpp/textasset/json…）
    # 已各自分类，但历史上曾出现某条路径误把键/ID/路径/资源名标成
    # role=display、disposition=translate 放行进池（0.37.x 多游戏回归）。
    # 这里在**任何队列接纳前**对原文内容做一次确定性结构终检：命中
    # should_skip（硬结构值 JSON/URL/路径/GUID/程序集名/纯数字，或键风格
    # 标识符 ui_newGame/MENU_PLAY/en）→ 一律拒于队列之外，无论提取层为何
    # 判定。真显示文本（句子/TMP 组合串/TitleCase 按钮词/白名单词）不受
    # 影响——should_skip 对它们恒 False（真实语料 minato/fromivan/hickory/
    # hrana 实测 0 误伤，见 test_models final structural gate 契约）。
    #
    # 性能：正则级判定，仅在建队列时每条目执行一次（run_scope 过滤复用），
    # 非每 token 路径，无热路径放大。
    if _final_structural_backstop(entry):
        return False
    return True


# 审核待处理终态（Phase A 统一落 review_outcome；C6 时代遗留
# review_issue 字段仍兼容）。审校页「待审核」胶囊与概览页「待人工」
# 统计共用此口径（2026-08-15 数字关系统一：此前两处各算各的，显示
# 互相矛盾）。
REVIEW_PENDING_OUTCOMES = frozenset(
    {"NEEDS_REVISION", "BLOCKED", "REVIEW_ERROR"})


def needs_review(meta: dict) -> bool:
    """待审核判定：review_outcome 终态未收敛，或遗留 review_issue。

    与审校页筛选「待审核」同源（入口统一在 models，两页共用）。"""
    return (meta.get("review_outcome") in REVIEW_PENDING_OUTCOMES
            or bool(meta.get("review_issue")))


def entry_from_row(row: dict) -> TextEntry:
    """DB 行 → TextEntry：首页/翻译页/审校页共用的单一口径。

    此前三页各自复制一份解析（meta 字符串/字典兼容），口径漂移是
    「待翻译计数不一致」的根源（#2/#8）：审校页曾用裸 status 计数，
    把 low 置信度留档条目（引擎消息/噪音，不可自动翻译）算进待翻译。
    """
    raw_meta = row.get("meta", {})
    if isinstance(raw_meta, str):
        try:
            meta = json.loads(raw_meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    else:
        meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
    reasons = meta.get("quality_reasons", [])
    return TextEntry(
        file_id=row["file_id"], key_path=row["key_path"],
        original=row["original"], translation=row.get("translation", ""),
        status=row.get("status", "pending"), locked=bool(row.get("locked", 0)),
        id=row.get("id"), meta=meta,
        confidence=str(meta.get("confidence", "medium")),
        quality_reasons=tuple(str(reason) for reason in reasons)
        if isinstance(reasons, list) else (),
    )


@dataclass
class GameContext:
    """游戏语境识别结果（设计文档 §5-10）。"""
    game_name: str = ""
    genre: str = ""
    setting: str = ""
    summary: str = ""
    characters: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    style: str = ""
    translation_notes: list[str] = field(default_factory=list)


@dataclass
class GameProfile:
    game_name: str = ""
    genre: str = ""
    world_setting: str = ""
    tone_notes: str = ""
    prompt_style: str = ""
    source_lang: str = "auto"
    target_lang: str = "zh-CN"
    # 新增：GameContext 字段（与设计文档一致，user-facing 与 model-facing 共享数据）
    context_game_name: str = ""
    context_genre: str = ""
    context_setting: str = ""
    context_summary: str = ""
    context_characters: list[str] = field(default_factory=list)
    context_terms: list[str] = field(default_factory=list)
    context_style: str = ""
    context_translation_notes: list[str] = field(default_factory=list)


@dataclass
class ApiConfig:
    mode: str = "local"           # api / local（默认本地离线——发行版
    # 开箱即本地四模型，无需任何云端配置；在线 API 是可选模式）
    provider: str = "openai"     # openai 兼容 / anthropic 原生
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096
    concurrency: int = 6
    batch_size: int = 40
    timeout: float = 120.0
    local_model_path: str = ""
    local_server_path: str = ""
    local_gpu_layers: int = -1    # -1 = 尽可能全部卸载到 GPU，0 = CPU
    local_context_size: int = 8192
    local_context_auto: bool = False  # 智能上下文（按文本统计自动计算，2026-08-16 用户指令）
    local_port: int = 0           # 0 = 自动选择环回空闲端口
    local_keep_alive: bool = True
    local_concurrency: int = 0  # 0 = GPU 4 / CPU 1 automatic default
    local_batch_size: int = 8   # persistence/progress chunk, not nested workers
    ai_review_enabled: bool = True        # 翻译后自动语义审核（§68 开关）
    ai_review_strategy: str = "balanced"  # fast / balanced / strict → 送审率


@dataclass
class FontConfig:
    enabled: bool = True
    # 2026-08-18 收敛：只保留唯一字体 Noto Serif CJK SC Medium（宋体
    # 中等字重），不再维护多字体/多档位。2026-09-04 D1 根治：默认改
    # 真 TrueType .ttf（CFF OTF 插件部署缺字口口口，问题集 D1）。
    filename: str = "SimplifiedChinese/NotoSerifCJKsc-Medium.ttf"


@dataclass
class GlossaryEntry:
    term: str
    translation: str
    category: str = "术语"       # 人名/地名/专名/术语
    note: str = ""
    id: int | None = None


@dataclass
class TranslateStats:
    total: int = 0
    done: int = 0
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    failed: int = 0
    from_memory: int = 0
    elapsed: float = 0.0    # 本轮 run 耗时（秒），P3 吞吐统计

    @property
    def rate_per_minute(self) -> float:
        """吞吐：已完成条目数 × 60 / 耗时秒（耗时 0 或未完成时为 0）。"""
        if self.elapsed <= 0:
            return 0.0
        return self.done * 60.0 / self.elapsed


@dataclass(frozen=True)
class WriteRejection:
    """One write-ready locator that the writer explicitly declined."""

    locator: str
    reason: str


@dataclass(frozen=True)
class WriteOutcome:
    """Immutable, auditable accounting for one writer run."""

    attempted: int
    written: int
    rejected: tuple[WriteRejection, ...] = ()
    truncated: int = 0          # 写入成功但被固定容量截断的条目数
    logic_reverted: int = 0     # 逻辑审计主动回退（保留原文防断链）——
                                # 终态之一，不是写失败：不触发对象闸门阻断

    def __post_init__(self):
        if self.attempted != self.written + len(self.rejected) + self.logic_reverted:
            raise ValueError(
                "writer outcome must satisfy attempted = written + rejected"
                " + logic_reverted")
        if self.truncated < 0 or self.truncated > self.written:
            raise ValueError(
                "writer outcome truncated must be within [0, written]")
