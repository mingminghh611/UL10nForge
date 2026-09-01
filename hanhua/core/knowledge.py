from __future__ import annotations
import datetime as _dt
import json
import re
import sqlite3
import threading
import unicodedata
from pathlib import Path

from hanhua.core.placeholders import should_skip
from hanhua.core.review_failures import failure_pattern
# 注意：不在此处导入 translator 的 builtin_ui_conflict——translator 顶部
# import quality，quality 又 import 本模块（_UPPERCASE_ACTION_VERBS 等），
# 顶层导入 translator 会成环（translator → quality → knowledge →
# translator）。record_review_failure 内延迟导入。

# ────────────────────────────────────────────────────────────────────────
# 知识库：汉化全链路（识别/翻译/写回/质量门）遇到的特殊情况的经验存储。
#
# 与术语库（GlossaryStore）分工：术语库存「专名/术语的译名映射」；
# 知识库存「特殊情况 → 处置规则」，按 domain 分库、kind 细分。
#
# 六库体系（2026-08-11 知识库搭建，§0.4.3，domain 即库）：
#   unity_structure  Unity 结构库（unity_version/resource_type/…）
#   fail_case        失败案例库（note=结构化 JSON，FAIL 编号制）
#   text             文本规则库（形态 kind + 类型 kind，action=translate/keep/skip）
#   component_compat 组件兼容库（text/textmeshpro/dropdown/ui_toolkit/…）
#   quality          翻译质量库（scoring_case/term_consistency/common_error）
#   writeback        写回验证库（writeback_case/test_flow）
# 另有功能域 file（文件形态）/ rule（抽象规则）承载跨场景处置策略。
#
#   domain=text     特殊文本形态（可翻译语义文本的特征模式）
#     kind=spaced_action      间隔动作词（* Y A W N * → * 哈欠 *）
#     kind=uppercase_action   全大写动作指令（TOSS TRASH → 丢垃圾）
#     kind=interaction_prompt 交互提示（Press E to open → 保留按键）
#   domain=file     特殊文件形态（容器/记录布局约束）
#     kind=us_record          #US 字符串（UTF-16 固定码元容量，预算=码元数）
#     kind=il2cpp_string      IL2CPP metadata（UTF-8 变长，链式 dataIndex）
#   domain=rule     抽象规则（跨场景的确定性处置策略）
#     kind=placeholder_restore   译文缺 {n} 补末尾（string.Format 按索引取参）
#     kind=truncation_partial    超容量按字符收尾 + 省略号（部分翻译写回）
#     kind=echo_no_exempt        全大写动作指令回显不豁免（质量门）
#
# 规则 = 匹配特征（pattern）+ 处置策略（action）+ 建议结果（map_to，可选）
# + 来源备注（note）+ 命中证据（hits）。内置种子规则随代码分发（立即生效，
# 确定性、零成本）；跑完每场游戏后 learn() 把「该翻未翻」的新模式自动
# 沉淀入库（幂等），跨游戏持续积累，后续版本可沉淀为内置规则。
# ────────────────────────────────────────────────────────────────────────

# ── 内置种子 1：文本形态识别（确定性，无需查库） ─────────────────────────

# 大写动作指令的动作动词表：全大写短语含这些词 → 判为动作/命令文本，
# 必须翻译（回显=失败）。MEGA CORP / STAR WARS 等真专名不含动作词，
# 不会误命中（专名仍走 proper_name_echo 豁免）。随游戏积累扩充。
_UPPERCASE_ACTION_VERBS = frozenset("""
toss throw pick press push pull use open close enter exit start stop go
skip drop grab take give combine inspect look read eat drink equip swap
craft build break chop mine fish sleep save load quit back next confirm
cancel walk run jump attack defend heal buy sell trade activate deactivate
turn flip drag release catch chase hide sneak shoot aim reload fill empty
pour stack unstack place remove insert attach detach fix repair unlock lock
search examine check view focus zoom pause resume accept decline agree
disagree pay earn win lose fight escape die respawn talk speak shout
scream call listen watch cut dig shovel plant water harvest cook grill
season serve taste smell touch lift carry throw spin rotate shake smash
kick punch hit poke pat clean wash rinse dry iron fold hang wear
""".split())


# ── 写回逻辑层规则（writeback_case 案例转规则，2026-08-11）──────────
# 知识库 writeback_case 域 5 条理论案例（来源：写回资料大全）→ 每条对应
# 一个已实现的可执行规则。写回时 runner 报告「案例规则启用」命中，规则
# 真正落地在代码（UnityEvent 对象信号/逻辑键比较词/长度头自证/占位符
# 恢复），案例记录本身不再只是备忘。
WRITEBACK_CASE_RULES: tuple[dict, ...] = (
    {
        "case": "固定容量池截短译文后字符串尾部带 NUL 导致逻辑判定失灵",
        "rule": "fit_bytes_nul_padding",
        "impl": "writer._fit_bytes（pad 模式容量对齐）+ 译文长度头自证"
                "（logic_audit.verify_string_length_headers）",
    },
    {
        "case": "写回截断把 {0} 占位符切开导致 string.Format 崩溃",
        "rule": "placeholder_preserve",
        "impl": "writer._restore_placeholders / _placeholders_intact"
                "（截断不得破坏占位符，缺失补末尾）",
    },
    {
        "case": "TextAsset 非 UTF-8 内容被 decode-replace 污染",
        "rule": "textasset_encoding_preserve",
        "impl": "writer 写回按源编码/严格校验，不把非 UTF-8 当 UTF-8 重写",
    },
    {
        "case": "替换 prefab/资源后 UnityEvent 事件绑定断裂按钮无反应",
        "rule": "unityevent_binding_preserve",
        "impl": "extractor._UNITYEVENT_SIGNALS 对象信号 → structural 跳过"
                "+ logic_audit.logic_key_evidence 写回回退（reflection 按名绑定键）",
    },
    {
        "case": "代码把显示文本当逻辑键（按钮文字/物品名比较分发）",
        "rule": "logic_key_compare",
        "impl": "logic_audit.LOGIC_COMPARE_WORDS 比较词表 + 代码对象联合"
                "判定（写回自动回退）",
    },
)


def writeback_case_rules() -> tuple[dict, ...]:
    """写回逻辑层规则清单（案例 → 规则 → 实现），供 runner 报告启用状态。"""
    return WRITEBACK_CASE_RULES


def _is_uppercase_action(text: str) -> bool:
    """全大写短语 + 含动作动词 → 大写动作指令（TOSS TRASH / PRESS START）。

    判定：2-5 个全大写词，整串字母全大写，至少一个词是动作动词。
    """
    stripped = str(text).strip()
    if not stripped or stripped.isdigit():
        return False
    words = re.findall(r"[A-Z][A-Z0-9']{1,}", stripped)
    if not 2 <= len(words) <= 5:
        return False
    letters = re.sub(r"[^A-Za-z]", "", stripped)
    if not letters or not letters.isupper():
        return False
    return any(word.casefold() in _UPPERCASE_ACTION_VERBS
               for word in words)


_SPACED_LETTERS = re.compile(r"\b[A-Z](?: [A-Z]\b)+")


def _is_spaced_action(text: str) -> bool:
    """间隔动作词：单字母以空格间隔的全大写词（* Y A W N * / G A S P）。
    字母间有空格 = 文字化动作/音效表现（打哈欠/惊呼），非专名。
    先剥花括号对话标签（{punch=3,2}* Y A W N *{w=3}{x} 的 {punch}/{w=3}/
    {x} 是动画参数不是词——a-catfiends 实证：不剥则判定失效，回显靠
    单字母词判失败、修复链无兜底 → 恒败）。"""
    stripped = re.sub(r"\{[^{}]*\}", " ", str(text)).strip("* \t")
    if len(stripped) < 3 or " " not in stripped:
        return False
    parts = stripped.split()
    return bool(parts and all(
        len(part) == 1 and part.isupper() for part in parts))


def aggregate_spaced_letters(text: str) -> str:
    """聚合间隔字母词：'* Y A W N *' → '* YAWN *'（打字机逐字动画的
    视觉写法）。模型对原形态稳定回显（1.8B 能力边界），聚合后能正确
    翻译且标签原位保留（a-catfiends 实证：'{punch=3,2}* YAWN *{w=3}{x}'
    → '{punch=3,2}* 哎呀 *{w=3}{x}'）。"""
    return _SPACED_LETTERS.sub(lambda m: m.group(0).replace(" ", ""), text)


# 间隔动作词封闭词典（2026-08-11 实测）：1.8B 对 * SCOFF */* SIGH */
# * YAWN */* GASP * 聚合形态仍稳定回显（只去空格不翻译，VOMITS 偶译
# 「吐出物」质量差）——动作旁白词是封闭词表，确定性直填比依赖模型
# 可靠（模型能力边界兜底，非词级补译能处理的开放文本）。
_SPACED_ACTION_LEXICON: dict[str, str] = {
    "YAWN": "打哈欠", "GASP": "倒吸一口气", "SCOFF": "嗤笑",
    "SIGH": "叹气", "VOMITS": "呕吐", "GROAN": "呻吟", "GROANING": "呻吟",
    "SHIVER": "颤抖", "TREMBLE": "颤抖", "SHUDDER": "战栗",
    "COUGH": "咳嗽", "HICCUP": "打嗝", "SNORE": "打鼾", "PANT": "喘气",
    "PUFF": "喘气", "SNIFF": "抽鼻子", "SNIFFLE": "抽鼻子",
    "WHINE": "呜咽", "WHIMPER": "呜咽", "SOB": "啜泣", "CRY": "哭泣",
    "WEEP": "哭泣", "LAUGH": "大笑", "GIGGLE": "咯咯笑", "CHUCKLE": "轻笑",
    "SCREAM": "尖叫", "SHOUT": "喊叫", "MUMBLE": "嘟囔", "MUTTER": "咕哝",
    "WHISPER": "低语", "HUFF": "哼了一声", "GRUNT": "咕哝一声",
    "HUM": "哼着歌", "HUMMING": "哼着歌", "BURP": "打嗝", "BELCH": "打嗝",
    "SNEEZE": "打喷嚏", "NOD": "点点头", "SHAKE": "摇摇头",
    "STARE": "盯着看", "BLINK": "眨眨眼", "WINK": "眨眨眼",
    "SMILE": "微笑", "FROWN": "皱起眉头", "GRIMACE": "扮鬼脸",
    "SHRUG": "耸耸肩", "HUG": "拥抱", "TAP": "轻敲", "KNOCK": "敲门",
    "THUD": "砰的一声", "BANG": "砰的一声", "CRASH": "哗啦一声",
    "RING": "铃声响起", "BUZZ": "嗡嗡响", "CLICK": "咔嗒一声",
    "TICK": "滴答", "TOCK": "滴答", "POP": "啪的一声",
    "SNAP": "啪的一声", "CRACK": "咔嚓一声", "SQUEAK": "吱呀一声",
    "CREAK": "吱呀一声", "RUSTLE": "沙沙作响", "WHOOSH": "呼的一声",
    "THUMP": "砰的一声", "DRIP": "滴水", "PLOP": "扑通一声",
    "GULP": "咽了口唾沫", "SWALLOW": "咽了口唾沫",
    "SPIT": "啐了一口", "SPUTTER": "结结巴巴", "STAMMER": "结结巴巴",
    "STUTTER": "结结巴巴", "FALTER": "踉跄", "STAGGER": "踉跄",
    "TOTTER": "摇摇晃晃", "WAVER": "动摇", "HESITATE": "犹豫",
    "FLINCH": "缩了一下", "WINCES": "龇牙咧嘴", "WINCE": "龇牙咧嘴",
    "PAUSE": "停顿", "FREEZE": "僵住", "FROZE": "僵住",
    "BOLT": "冲了出去", "DASH": "冲了出去", "RUSH": "冲了出去",
    "MARCH": "大步走", "STOMP": "跺脚", "TROT": "小跑", "RUN": "跑",
    "WALK": "走", "JUMP": "跳起来", "LEAP": "一跃而起", "HOP": "蹦跳",
    "SLIDE": "滑行", "CRAWL": "爬行", "STOOP": "俯身", "BEND": "弯腰",
    "CROUCH": "蹲下", "KNEEL": "跪下", "BOW": "鞠躬", "CURTSY": "行屈膝礼",
    "SALUTE": "敬礼", "WAVE": "挥手", "POINT": "指着", "FINGER": "指着",
    "SHAKE_HEAD": "摇摇头", "NOD_HEAD": "点点头",
    "CHEER": "欢呼", "CLAP": "鼓掌", "APPLAUSE": "鼓掌",
    "WHISTLE": "吹口哨", "BOO": "嘘声", "HISS": "发出嘘声",
    "GROWL": "低吼", "SNARL": "龇牙低吼", "HOWL": "嚎叫", "BARK": "吠叫",
    "MEOW": "喵喵叫", "PURR": "发出呼噜声", "MOO": "哞哞叫",
    "CLUCK": "咯咯叫", "COCK": "喔喔叫", "CROW": "喔喔叫", "TWITTER": "啾啾叫",
    "CHIRP": "啾啾叫", "SQUEAL": "尖叫", "SCREECH": "尖叫", "SHRIEK": "尖叫",
    "YELP": "惊叫", "WALLOW": "打滚", "WRITHE": "打滚", "SQUIRM": "扭动",
    "WRIGGLE": "扭动", "TWITCH": "抽搐", "TIC": "抽搐",
    "SHIVERING": "瑟瑟发抖", "TREMBLING": "瑟瑟发抖",
    "SHUDDERING": "浑身发抖", "QUIVER": "颤抖", "QUAVER": "声音颤抖",
    "WAIL": "嚎啕大哭", "LAMENT": "哀叹", "MOURN": "哀悼",
    "HUFFING": "气喘吁吁", "PUFFING": "气喘吁吁", "WHEEZE": "气喘吁吁",
    "CHOKE": "噎住", "GAG": "干呕", "RETCH": "干呕", "HEAVE": "干呕",
}


def spaced_action_lexicon(word: str) -> str | None:
    """间隔动作词词典查询：'YAWN' → '打哈欠'（未收录返回 None）。"""
    return _SPACED_ACTION_LEXICON.get(str(word).strip().upper())


# 其他语言（非英语）源文本的脚本特征：日文假名（平/片）与带重音拉丁
# 字母（法/意/西/葡等欧洲语言）。游戏多语言打包（同一对象存英/法/意/日
# 四版文本）时，英中模型对日语/重音文本倾向输出**英语译文**（alisa-demo
# 实证 26 条日语/意语 → 准确英语但目标语错误）——质量门须拦截（英文残留），
# 重试走「第一跳英语译文 → 第二跳英译中」双跳，或同对象译例（同 obj
# 兄弟条目的成功译文作参考注入）。
_JAPANESE_KANA_RE = re.compile(r"[぀-ヿㇰ-ㇿ]")
_ACCENTED_LATIN_RE = re.compile(
    r"[À-ÖØ-Þßà-öø-ÿ]")
# 西里尔字母（俄/乌/保语源）：与拉丁/假名正交的独立文字系统，1.8B 模型
# 对俄语源同样倾向输出英语/乱译（containment 实证：клипборд → Klipboard
# 音译、Привет → 解释性垃圾）→ 硬 multilingual 特征，走双跳/兜底。
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
# 罗曼语族（法/意/西/葡）ASCII 功能词：与英语共用拉丁字母，但冠词/介词
# 不同（Chiave di Ferro 的 di、Il cibo 的 Il）。英语中这些词罕见（多为
# 音名/叹词，如 la/si/e）——出现即疑为罗曼语言源文本，须译中文。
# 西语高频词（no/me/te/se/el/los/las/que/es/son/eres/su/sus/mi/mis/
# esta/este/estas/como/cuando/donde/quien/cual/al/ni/o/para/por）补齐
# （containment 实证：'No me veas' 无重音无旧表词，西语未被识别）。
_ROMANCE_FUNCTION_WORDS = frozenset("""
il lo la le les i gli un una di del della dei delle du des au aux
su sul sulla nel nella nello nei negli nelle con per tra fra che chi si
je tu il elle nous vous et mais ou avec en por para entre
da de ne ve ci vi
no me te se su sus mi mis el los las que es son eres esta este estas
como cuando donde quien cual al ni o u pero
""".split())
# 英语也高频的罗曼功能词（否定 no、宾格 me）：单命中不可判 multilingual
# （'No matter what happens' 的 no、'Tell me the truth' 的 me 是英语），
# 需要 ≥2 个罗曼词（'No me veas' = no+me 才是西语）。其余表内词
# （que/es/el/los…）英语罕见，单命中即可。
_ENGLISH_SHARED_ROMANCE_WORDS = frozenset({"no", "me", "te", "el", "o", "ni"})
_ASCII_WORD_RE = re.compile(r"[A-Za-z]{2,}")


def _is_multilingual_source(text: str) -> bool:
    """原文含日文假名/带重音拉丁字母/西里尔字母/罗曼语功能词 → 非英语源。

    判定独立于目标语：这类原文模型对其默认输出英语是目标语错误
    （质量门拦截 + 双跳修复 + 同对象译例兜底）。
    """
    if (_JAPANESE_KANA_RE.search(text)
            or _ACCENTED_LATIN_RE.search(text)
            or _CYRILLIC_RE.search(text)):
        return True
    words = [w.casefold() for w in _ASCII_WORD_RE.findall(text)]
    hits = [w for w in words if w in _ROMANCE_FUNCTION_WORDS]
    if len(hits) >= 2:
        return True
    return bool(hits and hits[0] not in _ENGLISH_SHARED_ROMANCE_WORDS)


# 语言名（Español/Deutsch/Русский/日本語…）：语言选择器的显示文本保留
# 原名是业界惯例——游戏语言列表从不翻译语言名，回显语言名是合理行为
# （containment level*.assets 'Español' 回显被判 target_script_mismatch
# 真实样本 6 条）。跨游戏通用（任何游戏的语言设置 UI 都含语言名）。
_LANGUAGE_NAMES_CASEFOLD = frozenset(word.casefold() for word in [
    "english", "español", "spanish", "deutsch", "german", "french",
    "français", "japanese", "日本語", "한국어", "korean", "russian",
    "русский", "українська", "ukrainian", "chinese", "中文",
    "简体中文", "繁體中文", "português", "portuguese", "italiano",
    "italian", "polski", "polish", "nederlands", "dutch", "svenska",
    "swedish", "türkçe", "turkish", "ไทย", "thai", "việt",
    "vietnamese", "čeština", "czech", "magyar", "hungarian",
    "ελληνικά", "greek", "עברית", "hebrew", "العربية", "arabic",
    "हिन्दी", "hindi", "bahasa", "indonesian", "norsk", "norwegian",
    "suomi", "finnish", "dansk", "danish", "română", "romanian",
    "български", "bulgarian", "english (us)", "english (uk)"])


def _is_language_name(text: str) -> bool:
    """原文是否语言名（忽略大小写/首尾空白）。"""
    return text.strip().casefold() in _LANGUAGE_NAMES_CASEFOLD


# 语言选项标签直填表（EN→ZH）：「Language: ENGLISH」形态的选项行——
# 1.8B 对选项文本乱译（doog 实证「Language: ENGLISH」→「I AM GOD
# HAND! WAO! 翻译成…」4 次重试稳定乱译 → newline/line_content mismatch
# 恒败）或回显（JAPONÉS）。语言选项标签是封闭集合（标签词 + 语言名），
# 确定性直填不走模型。纯语言名（Español/日本語）保留原名是业界惯例
# （_is_language_name 豁免），本直填只覆盖「标签 + 语言名」组合形态。
_LANGUAGE_OPTION_ZH = {
    "english": "英语", "spanish": "西班牙语", "japanese": "日语",
    "french": "法语", "german": "德语", "italian": "意大利语",
    "portuguese": "葡萄牙语", "russian": "俄语", "korean": "韩语",
    "chinese": "中文", "polish": "波兰语", "dutch": "荷兰语",
    "swedish": "瑞典语", "turkish": "土耳其语", "thai": "泰语",
    "vietnamese": "越南语", "czech": "捷克语", "hungarian": "匈牙利语",
    "greek": "希腊语", "hebrew": "希伯来语", "arabic": "阿拉伯语",
    "hindi": "印地语", "indonesian": "印尼语", "norwegian": "挪威语",
    "finnish": "芬兰语", "danish": "丹麦语", "romanian": "罗马尼亚语",
    "bulgarian": "保加利亚语", "ukrainian": "乌克兰语",
    # 原语语言名（语言选择器里常见自身语言名；拉丁语种为去重音后拼写）
    "日本語": "日语", "한국어": "韩语", "中文": "中文",
    "简体中文": "中文", "繁體中文": "繁体中文",
    "espanol": "西班牙语", "francais": "法语", "deutsch": "德语",
    "italiano": "意大利语", "portugues": "葡萄牙语",
    "nederlands": "荷兰语", "svenska": "瑞典语", "turkce": "土耳其语",
    "polski": "波兰语", "cesky": "捷克语", "magyar": "匈牙利语",
    "dansk": "丹麦语", "norsk": "挪威语", "suomi": "芬兰语",
    "romana": "罗马尼亚语",
    # 非拉丁书写系原语名（NFKD 不转写，直接原形入表）
    "русский": "俄语", "ελληνικά": "希腊语", "עברית": "希伯来语",
    "العربية": "阿拉伯语", "हिन्दी": "印地语", "ไทย": "泰语",
}
_LANGUAGE_LABEL_RE = re.compile(
    r"^(?:language|lang|languagemode|言語|言语|idioma|язык)\s*[:：]?\s*"
    r"(.+?)\s*$", re.I)


def language_option_translation(text: str) -> str | None:
    """语言选项标签确定性直填：'Language: ENGLISH' → '语言：英语'。

    仅覆盖「语言标签 + 语言名」组合形态（标签词可多种语言写法）；
    纯语言名 / 非表内语言名 → None（前者保留原名惯例，后者交模型）。
    语言名查表前 NFKD 去重音（Español→espanol）保证多语言写法命中。
    """
    stripped = text.strip()
    m = _LANGUAGE_LABEL_RE.match(stripped)
    if not m:
        return None
    # NFKD 去重音：Español→espanol（先分解再丢弃组合记号——casefold
    # 不吞组合符，直接 casefold 会留下 n+U+0303 查表失败）
    decomposed = unicodedata.normalize("NFKD", m.group(1).strip())
    name = "".join(ch for ch in decomposed
                   if not unicodedata.combining(ch)).casefold()
    zh = _LANGUAGE_OPTION_ZH.get(name)
    if not zh:
        return None
    return f"语言：{zh}"


# 大写动作指令的机械直译词表（EN→ZH）。用途：learn() 沉淀「该翻未翻」
# 条目时自动生成建议译名（map_to）——重试降级走 native_translate（Hy-MT2
# 无 system prompt 契约），模型看不到知识库规则，译例通过 references 的
# terms 机制带出（"TOSS TRASH translates to 丢垃圾"）。词表是跨游戏通用
# 知识（动作/命令高频词），随游戏积累扩充，非单游戏特判。
_ACTION_VERB_ZH = {
    "toss": "丢", "throw": "扔", "press": "按", "push": "推", "pull": "拉",
    "interact": "交互", "hold": "按住", "use": "使用", "open": "打开",
    "close": "关闭", "enter": "进入",
    "exit": "离开", "start": "开始", "stop": "停止", "go": "出发",
    "skip": "跳过", "drop": "丢弃", "pick": "捡起", "grab": "抓住",
    "take": "拿取",
    "give": "交给", "combine": "组合", "inspect": "检查", "look": "查看",
    "read": "阅读", "eat": "吃掉", "drink": "喝掉", "equip": "装备",
    "swap": "交换", "craft": "制作", "build": "建造", "break": "破坏",
    "chop": "砍伐", "mine": "挖掘", "fish": "钓鱼", "sleep": "睡觉",
    "save": "保存", "load": "读取", "quit": "退出", "back": "返回",
    "next": "下一步", "confirm": "确认", "cancel": "取消", "walk": "行走",
    "run": "奔跑", "jump": "跳跃", "attack": "攻击", "defend": "防御",
    "heal": "治疗", "buy": "购买", "sell": "出售", "trade": "交易",
    "activate": "激活", "deactivate": "停用", "turn": "转动", "flip": "翻转",
    "drag": "拖拽", "release": "释放", "catch": "接住", "chase": "追逐",
    "hide": "躲藏", "sneak": "潜行", "shoot": "射击", "aim": "瞄准",
    "reload": "装弹", "fill": "装满", "empty": "清空", "pour": "倒出",
    "stack": "堆叠", "place": "放置", "remove": "移除", "insert": "插入",
    "attach": "安装", "detach": "分离", "fix": "修复", "repair": "修理",
    "unlock": "解锁", "lock": "锁定", "search": "搜索", "examine": "检查",
    "check": "查看", "focus": "聚焦", "zoom": "缩放", "pause": "暂停",
    "resume": "继续", "accept": "接受", "decline": "拒绝", "agree": "同意",
    "disagree": "不同意", "pay": "支付", "earn": "赚取", "win": "获胜",
    "lose": "失败", "fight": "战斗", "escape": "逃跑", "die": "死亡",
    "respawn": "重生", "talk": "交谈", "speak": "说话", "shout": "喊叫",
    "scream": "尖叫", "call": "呼叫", "listen": "聆听", "watch": "观看",
    "cut": "切割", "dig": "挖掘", "shovel": "铲挖", "plant": "种植",
    "water": "浇水", "harvest": "收获", "cook": "烹饪", "grill": "烧烤",
    "season": "调味", "serve": "端上", "taste": "品尝", "smell": "嗅闻",
    "touch": "触摸", "lift": "举起", "carry": "携带", "spin": "旋转",
    "rotate": "转动", "shake": "摇晃", "smash": "砸碎", "kick": "踢",
    "punch": "出拳", "hit": "击打", "poke": "戳", "pat": "轻拍",
    "clean": "清洁", "wash": "清洗", "rinse": "冲洗", "dry": "晾干",
    "iron": "熨烫", "fold": "折叠", "hang": "挂起", "wear": "穿戴",
    # UI 设置/配置命令动词（F22-3 防过宽：'Adjust spring pressure 调整
    # spring 压力'——Adjust/Change 是祈使命令动词，TitleCase 短语段豁免
    # 不得放行未译的命令动词；与 Press/Open 同类，属操作动词词表）
    "adjust": "调整", "change": "更改", "set": "设置", "select": "选择",
    "choose": "选择", "toggle": "切换", "enable": "启用",
    "disable": "禁用", "move": "移动", "delete": "删除", "add": "添加",
    "switch": "切换", "update": "更新", "edit": "编辑", "modify": "修改",
    "increase": "增加", "decrease": "减少", "reset": "重置", "clear": "清除",
    "apply": "应用", "sort": "排序", "filter": "筛选", "scroll": "滚动",
    "copy": "复制", "paste": "粘贴", "rename": "重命名",
    "configure": "配置", "customize": "自定义", "refresh": "刷新",
    "retry": "重试", "connect": "连接", "disconnect": "断开",
    "import": "导入", "export": "导出", "download": "下载",
    "upload": "上传", "install": "安装", "uninstall": "卸载",
    # 设置项缩写/技术词（0.26 地毯式实证：force-reboot 设置页
    # VSYNC/Vsync 模型稳定回显不翻——learn 单词沉淀需要词表命中）
    "vsync": "垂直同步",
}
# 大写动作短语中常见名词（TOSS TRASH 的 TRASH），补动作词的语义完整
_COMMON_NOUN_ZH = {
    "trash": "垃圾", "axe": "斧头", "ball": "球", "door": "门",
    "key": "钥匙", "box": "箱子", "sword": "剑", "shield": "盾牌",
    "arrow": "箭", "bow": "弓", "potion": "药水", "item": "物品",
    "wood": "木头", "stone": "石头", "food": "食物", "water": "水",
}


# 机械直译跳过的功能词（冠词/介词/连词），如 OPEN THE DOOR 的 THE
_ACTION_SKIP_WORDS = frozenset("""
the a an up down off on in out into onto to of with and from for at by
it its my your our their this that these those me you we they
""".split())


# 单设置词 token（JUMP/Vsync/jump：纯字母数字词，无空格/标点）
_SINGLE_WORD_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9']{1,39}$")


def _is_single_lexicon_word(text: str) -> bool:
    """单个词且命中动作/名词词表 → 单词机械直译候选（JUMP → 跳跃）。

    0.26 地毯式实证：force-reboot 设置页 JUMP/VSYNC/Vsync 全大写/TitleCase
    键名模型稳定回显——_is_uppercase_action 要求 2-5 词短语，单词形态不
    沉淀译例，重试耗尽恒败。词表词（动作动词/设置名词）是跨游戏通用
    语义，单个词回显即「该翻未翻」，learn 沉淀译例后 native 降级
    references 带出（"JUMP translates to 跳跃"）模型照做。
    """
    stripped = str(text).strip()
    if not _SINGLE_WORD_TOKEN.match(stripped):
        return False
    table = {**_ACTION_VERB_ZH, **_COMMON_NOUN_ZH}
    return stripped.casefold() in table


def translate_uppercase_action(text: str) -> str | None:
    """大写动作指令的机械直译（词表逐词映射，全部命中才返回）。

    "TOSS TRASH" → "丢垃圾"、"OPEN THE DOOR" → "打开门"；含词表外
    单词（如专名）→ None（不兜底，避免机械翻译弄出错误专名）。
    供 learn() 生成 map_to 建议译名。
    """
    stripped = str(text).strip()
    words = re.findall(r"[A-Z][A-Z0-9']{1,}", stripped)
    if not words:
        return None
    table = {**_ACTION_VERB_ZH, **_COMMON_NOUN_ZH}
    parts = []
    for word in words:
        if word.casefold() in _ACTION_SKIP_WORDS:
            continue
        zh = table.get(word.casefold())
        if zh is None:
            return None
        parts.append(zh)
    return "".join(parts) if parts else None


# ── 内置种子 2：抽象规则/文件知识（跨场景处置策略，随代码分发） ──────────

# 六库 domain 命名（2026-08-11 知识库体系搭建统一，§0.4.3）：
#   unity_structure  Unity 结构库（kind: unity_version/resource_type/…）
#   fail_case        失败案例库（note=结构化 JSON，FAIL 编号制）
#   text             文本规则库（形态 kind + 文本类型 kind）
#   component_compat 组件兼容库（text/textmeshpro/dropdown/ui_toolkit/…）
#   quality          翻译质量库（scoring_case/term_consistency/common_error）
#   writeback        写回验证库（writeback_case/test_flow）
#   file / rule      文件知识域与抽象规则域（跨场景处置策略，保留）

BUILTIN_RULES: tuple[dict, ...] = (
    # text：文本形态
    {"domain": "text", "kind": "spaced_action",
     "pattern": "字母单字空格间隔的全大写词（* Y A W N *）",
     "action": "translate",
     "map_to": "中文动作/音效词（* 哈欠 *），保留星号与格式",
     "note": "seed:规则11-间隔动作词文字化表现（a-catfiends 实证 6 条回显）"},
    {"domain": "text", "kind": "uppercase_action",
     "pattern": "全大写短语含动作动词（TOSS TRASH / PRESS START）",
     "action": "translate",
     "map_to": "中文动作/命令短语（丢垃圾）",
     "note": "seed:知识库首案（taxes 实证 2 条 TOSS TRASH 回显被专名豁免）"},
    {"domain": "text", "kind": "interaction_prompt",
     "pattern": "Press/按 + 按键 + 动作（Press E to open）",
     "action": "translate_keep_tokens",
     "map_to": "按 E 打开（保留按键字面量）",
     "note": "seed:交互提示——按键字面量必须原样保留"},
    {"domain": "text", "kind": "multilingual_source",
     "pattern": "原文含日文假名或带重音拉丁字母（法/意/西/葡…）",
     "action": "translate",
     "map_to": "中文（模型常误译为英语，须以中文为目标语：双跳或同对象译例）",
     "note": "seed:多语言打包游戏（alisa-demo 实证 26 条日语/意语 → 准确英语但目标语错误）"},
    {"domain": "text", "kind": "platform_name",
     "pattern": "小写平台/网站名（itch=itch.io、discord、steam 等）出现在 "
                "on/at + 平台名 + page/store/链接语境",
     "action": "keep_source",
     "map_to": "保留平台名原文 + 译其余（'itch page' → 'itch 页面'；模型把 "
               "itch 当普通词直译「痒页面」是稳定误译，backrooms 实证）",
     "note": "seed:独立游戏平台名（itch.io）跨游戏高频，直译破坏语境辨识"},
    # file：特殊文件形态
    # file：特殊文件形态
    {"domain": "file", "kind": "us_record",
     "pattern": "DLL #US 字符串记录（压缩前缀+UTF-16LE+标志字节）",
     "action": "capacity_fixed",
     "map_to": "容量=码元数；预算 max_chars=码元；超限截断+省略号",
     "note": "seed:#US 固定码元容量（taxes 'I did ' 实证 max_chars 字节/码元错位）"},
    {"domain": "file", "kind": "il2cpp_string",
     "pattern": "IL2CPP global-metadata 字符串池",
     "action": "capacity_variable",
     "map_to": "UTF-8 变长，dataIndex 链式更新，顺序配对验证",
     "note": "seed:IL2CPP 变长写回（v39 链式 dataIndex）"},
    # rule：抽象规则
    {"domain": "rule", "kind": "placeholder_restore",
     "pattern": "译文缺失原文的 {n} 占位符",
     "action": "restore_to_end",
     "map_to": "缺失占位符补到译文末尾（string.Format 按索引取参位置无关）",
     "note": "seed:模型漏 {n} 是稳定行为，机械补回避免 reject 丢好译文"},
    {"domain": "rule", "kind": "truncation_partial",
     "pattern": "译文超容量",
     "action": "partial_write",
     "map_to": "按字符收尾+省略号，部分翻译写入且不阻断发布",
     "note": "seed:截断=容量内最优解，不因 1 条截断拖垮整场写回"},
    {"domain": "rule", "kind": "echo_no_exempt",
     "pattern": "知识库文本规则命中但译文回显原文",
     "action": "fail_untranslated",
     "map_to": "回显一律判失败并重试（不得当专名豁免）",
     "note": "seed:全大写动作指令/间隔动作词回显不得豁免"},
    # ── 六库蓝图：unity_structure（Unity 结构与资源定位库） ──
    {"domain": "unity_structure", "kind": "unity_version",
     "pattern": "Unity 2018-2019：AssetBundle 常见、Text 组件多、Localization 少",
     "action": "info",
     "map_to": "资源结构简单但兼容旧格式；2021+：Addressables 增加、TMP 大量、"
               "Localization Package 普及 → 文本分散、写回复杂",
     "note": "seed:六库1-先判断 Unity 版本再选提取/写回方案"},
    {"domain": "unity_structure", "kind": "resource_type",
     "pattern": "TextAsset（配置/对话/JSON/CSV）",
     "action": "info",
     "map_to": "优先直接替换文本内容；MonoBehaviour 检查序列化字段；"
               "AssetBundle 备份+重建（直接改可能破坏结构）",
     "note": "seed:六库1-资源类型决定处理方式"},
    # ── 六库蓝图：text（文本类型与处理规则库，文本规则库） ──
    {"domain": "text", "kind": "debug",
     "pattern": "Debug 日志/调试输出文本",
     "action": "skip",
     "map_to": "不翻译（玩家不可见，翻译无价值且可能破坏日志语义）",
     "note": "seed:六库3-调试文本不翻译"},
    {"domain": "text", "kind": "code",
     "pattern": "无空格大写驼峰（PlayerController）→ 类名/代码标识符特征",
     "action": "skip",
     "map_to": "不翻译（代码按原名查找，翻译破坏功能）；"
               "Attack Damage +10% 等游戏文本才翻译",
     "note": "seed:六库3-代码文本 vs 游戏文本的判断规则"},
    # ── 六库蓝图：component_compat（Unity 组件兼容库） ──
    {"domain": "component_compat", "kind": "text",
     "pattern": "Unity UI Text 组件中文乱码",
     "action": "replace_font",
     "map_to": "默认字体不支持中文 → 替换 Font 为中文支持字体",
     "note": "seed:六库4-后台成功游戏失败的第一类原因"},
    {"domain": "component_compat", "kind": "textmeshpro",
     "pattern": "TMP 文本中文显示方块（□）",
     "action": "rebind_font_asset",
     "map_to": "TMP Font Asset 缺中文字形 → 生成中文 Atlas + 重新绑定 Font Asset",
     "note": "seed:六库4-TMP 是 2021+ 重点组件"},
    {"domain": "component_compat", "kind": "dropdown",
     "pattern": "Dropdown 选项翻译成功但列表仍英文",
     "action": "patch_data_source",
     "map_to": "显示文本已改但数据源未替换 → 修改 Option List 数据源",
     "note": "seed:六库4-不是所有文本都直接改字符串"},
    {"domain": "component_compat", "kind": "ui_toolkit",
     "pattern": "UI Toolkit 文本（UXML/USS/Localization Table）",
     "action": "locate_source",
     "map_to": "文本来源在 UXML/USS/Localization Table，先定位再替换",
     "note": "seed:六库4-UI Toolkit 与 UGUI 文本存放位置不同"},
    # ── 六库蓝图：quality（翻译质量库） ──
    {"domain": "quality", "kind": "scoring_case",
     "pattern": "翻译质量评分：语义 40 + 上下文 30 + 中文自然 20 + 术语统一 10",
     "action": "info",
     "map_to": "Critical Strike Chance 错译『关键打击机会』=40 分；"
               "『暴击率』=95 分——目标是游戏本地化而非机器翻译",
     "note": "seed:六库5-质量评分标准"},
    {"domain": "quality", "kind": "common_error",
     "pattern": "多义词按游戏语境判断：Charge→冲锋/蓄力/费用（非充电）、"
                "Skill Tree→技能树（非技能树木）",
     "action": "context_judge",
     "map_to": "翻译后自检：是否符合上下文/游戏习惯/无歧义/不与既有术语冲突",
     "note": "seed:六库5-常见翻译错误需上下文判断"},
    {"domain": "quality", "kind": "term_consistency",
     "pattern": "技能/装备/成就等游戏术语",
     "action": "translate_consistent",
     "map_to": "首次翻译后全局统一（Health→生命值，不得再出现 血量/生命/HP值）",
     "note": "seed:六库5-术语统一是汉化品质核心（原六库3 迁移）"},
    # ── 六库蓝图：writeback（写回与运行验证库） ──
    {"domain": "writeback", "kind": "test_flow",
     "pattern": "写回后运行验证流程：启动→主菜单→设置→新游戏→核心玩法→"
                "暂停菜单→存档→退出",
     "action": "verify",
     "map_to": "各环节逐项检查：游戏启动正常/菜单正常/文本显示正常",
     "note": "seed:六库6-统一验证流程，不同游戏同样覆盖"},
    {"domain": "writeback", "kind": "writeback_case",
     "pattern": "写回后游戏黑屏",
     "action": "runtime_patch",
     "map_to": "Bundle 结构损坏 → 改用运行时替换方案，而非直接改 Bundle",
     "note": "seed:六库6-写回失败记录（黑屏=结构损坏信号）"},
)


class KnowledgeStore:
    """知识条目库（SQLite，跨项目共享）。多库 = domain 分库聚合。"""

    def __init__(self, db_path: str | Path):
        self.db = Path(db_path)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def init_schema(self):
        with self._lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL,
                kind TEXT NOT NULL,
                pattern TEXT NOT NULL,
                action TEXT NOT NULL DEFAULT '',
                map_to TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                hits INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT '',
                game TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                UNIQUE(domain, kind, pattern)
            );""")
            # 旧库兼容升级：2026-08-11 前只有 domain/kind/pattern/action/
            # map_to/note/hits 七列——缺溯源/时间列则 ALTER 补齐（不重建）
            cols = {r["name"] for r in self.conn.execute(
                "PRAGMA table_info(knowledge_items)")}
            for col, ddl in (
                    ("source", "TEXT NOT NULL DEFAULT ''"),
                    ("game", "TEXT NOT NULL DEFAULT ''"),
                    ("created_at", "TEXT NOT NULL DEFAULT ''"),
                    ("updated_at", "TEXT NOT NULL DEFAULT ''"),
                    # #43 阶段 A（重构指令 §7 置信度/§17 生命周期/§8 来源）：
                    # 加列全带 DEFAULT——旧行自动获得保守值（AI 生成
                    # 0.6 / verified / priority 0），零迁移脚本
                    ("confidence", "REAL NOT NULL DEFAULT 0.6"),
                    ("status", "TEXT NOT NULL DEFAULT 'verified'"),
                    ("priority", "INTEGER NOT NULL DEFAULT 0"),
                    ("source_ref", "TEXT NOT NULL DEFAULT ''"),
                    ("usage_count", "INTEGER NOT NULL DEFAULT 0"),
                    ("success_count", "INTEGER NOT NULL DEFAULT 0")):
                if col not in cols:
                    self.conn.execute(
                        f"ALTER TABLE knowledge_items ADD COLUMN {col} {ddl}")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_domain_kind_hits"
                " ON knowledge_items(domain, kind, hits)")
            self.conn.commit()

    @staticmethod
    def _now() -> str:
        return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── 写入 ──

    #: 生命周期合法状态（重构指令 §17-18：candidate→verified→trusted→
    #: locked→deprecated；不删除旧知识，deprecated 保留历史）
    VALID_STATUS = frozenset(
        {"candidate", "verified", "trusted", "locked", "deprecated"})

    #: 来源类型（重构指令 §8：知识必须有来源，禁止「不知道哪来的」）
    VALID_SOURCES = frozenset(
        {"seed", "manual", "auto", "official", "imported",
         "human_corrected", "review_confirmed"})

    def upsert(self, domain: str, kind: str, pattern: str, *,
               action: str = "", map_to: str = "", note: str = "",
               hits: int = 1, source: str = "", game: str = "",
               confidence: float = 0.6, status: str = "verified",
               priority: int = 0, source_ref: str = "") -> bool:
        """幂等入库：已存在则 hits+1 并刷新来源备注/时间，返回是否新增。

        source: seed/manual/auto（内置种子/人工沉淀/自动学习）或
        official/imported/human_corrected/review_confirmed；game: 沉淀
        来源游戏（可空）。#43 阶段 A 扩展：confidence（置信度，人工确认
        1.0 / 人工修改 0.95 / 审核通过 0.85 / AI 生成 0.6）、status
        （生命周期）、priority（0-10 优先级）、source_ref（来源文本/文件/
        时间细节，可追溯）。所有扩展参数有默认值——旧调用零改动。"""
        now = self._now()
        if status not in self.VALID_STATUS:
            status = "verified"
        if source and source not in self.VALID_SOURCES:
            source = "auto"
        with self._lock:
            row = self.conn.execute(
                "SELECT id, game FROM knowledge_items"
                " WHERE domain=? AND kind=? AND pattern=?",
                (domain, kind, pattern)).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO knowledge_items"
                    "(domain, kind, pattern, action, map_to, note, hits,"
                    " source, game, created_at, updated_at, confidence,"
                    " status, priority, source_ref)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (domain, kind, pattern, action, map_to, note, hits,
                     source, game, now, now, confidence, status, priority,
                     source_ref))
                self.conn.commit()
                return True
            prev_game = row["game"]
            self.conn.execute(
                "UPDATE knowledge_items SET hits=hits+?, note=?,"
                " game=?, updated_at=? WHERE id=?",
                (max(1, hits), note, game if game else prev_game, now,
                 row["id"]))
            self.conn.commit()
            return False

    def list_by_domain(self, domain: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM knowledge_items WHERE domain=?"
                " ORDER BY priority DESC, hits DESC, id", (domain,))]

    def list_all(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM knowledge_items"
                " ORDER BY domain, kind, priority DESC, hits DESC")]

    def delete(self, domain: str, kind: str, pattern: str) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM knowledge_items"
                " WHERE domain=? AND kind=? AND pattern=?",
                (domain, kind, pattern))
            self.conn.commit()

    # ── #43 阶段 A：生命周期 + 冲突检测 ──

    def set_status(self, domain: str, kind: str, pattern: str,
                   status: str) -> bool:
        """知识生命周期流转（重构指令 §17-18）：不删除旧知识，deprecated
        保留历史。合法状态 candidate/verified/trusted/locked/deprecated；
        未知状态拒绝（不静默吞掉调用方错误）。"""
        if status not in self.VALID_STATUS:
            return False
        with self._lock:
            cur = self.conn.execute(
                "UPDATE knowledge_items SET status=?, updated_at=?"
                " WHERE domain=? AND kind=? AND pattern=?",
                (status, self._now(), domain, kind, pattern))
            self.conn.commit()
            return cur.rowcount > 0

    def detect_conflicts(self, domain: str, kind: str, pattern: str,
                         action: str) -> list[dict]:
        """术语/规则冲突检测（重构指令 §9）：同 pattern 不同 action 的
        已存在条目——不允许静默共存，返回冲突清单供上层提示用户
        preferred / forbidden 取舍。pattern 相同而 action 不同 =
        同一原文有两种译法/处置。"""
        if not action:
            return []
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, action, map_to, note, source, confidence,"
                " status FROM knowledge_items"
                " WHERE domain=? AND kind=? AND pattern=?"
                " AND action!=? AND action!=''",
                (domain, kind, pattern, action)).fetchall()
            return [dict(r) for r in rows]

    def mark_used(self, domain: str, kind: str, pattern: str,
                  success: bool = True) -> None:
        """使用计数（可观测性 §19）：检索命中 +1，成功（审校通过/人工
        确认沿用）另计 success_count。"""
        with self._lock:
            self.conn.execute(
                "UPDATE knowledge_items SET usage_count=usage_count+1,"
                " success_count=success_count+? WHERE domain=? AND kind=?"
                " AND pattern=?", (1 if success else 0, domain, kind,
                                   pattern))
            self.conn.commit()

    def close(self):
        with self._lock:
            self.conn.close()


class KnowledgeBase:
    """知识库聚合入口：内置种子规则（BUILTIN_RULES）+ 持久库。

    识别/翻译/质量门/写回各阶段按需查询——文本形态用零成本确定性
    识别函数（_is_uppercase_action / _is_spaced_action），抽象规则
    与文件知识通过 describe/list 供给报告、文档与人工查阅。
    """

    def __init__(self, db_path: str | Path | None = None):
        self.store: KnowledgeStore | None = None
        if db_path is not None:
            self.store = KnowledgeStore(db_path)
            self.store.init_schema()

    # ── text 域：文本形态匹配（翻译/质量门用） ──

    def match_text(self, text: str) -> list[dict]:
        """文本形态识别：内置确定性函数 + 持久库精确原文对照。"""
        rules: list[dict] = []
        if _is_spaced_action(text):
            rules.append({
                "domain": "text", "kind": "spaced_action",
                "pattern": "spaced_uppercase", "action": "translate",
                "map_to": "", "note": "内置：字母间隔全大写=动作/音效表现",
            })
        if _is_uppercase_action(text):
            rules.append({
                "domain": "text", "kind": "uppercase_action",
                "pattern": "uppercase_verb_phrase", "action": "translate",
                "map_to": "", "note": "内置：全大写含动作动词=动作/命令文本",
            })
        if _is_multilingual_source(text):
            rules.append({
                "domain": "text", "kind": "multilingual_source",
                "pattern": "kana_or_accented_latin", "action": "translate",
                "map_to": "", "note": "内置：含假名/重音拉丁=其他语言，须译中文",
            })
        if self.store is not None:
            for row in self.store.list_by_domain("text"):
                # #43 阶段 A：deprecated 知识退役不参与检索（保留历史，
                # 重构指令 §17-18 生命周期）
                if row.get("status") == "deprecated":
                    continue
                if row["kind"] in {"spaced_action", "uppercase_action"}:
                    continue  # 形态识别已覆盖，持久库只存精确原文对照
                try:
                    if re.search(row["pattern"], text, re.I):
                        rules.append(row)
                except re.error:
                    continue
        return rules

    def requires_translation(self, text: str) -> bool:
        """命中「必须翻译」规则（回显不得豁免）→ True。"""
        return any(r["action"] == "translate"
                   for r in self.match_text(text))

    # ── prompt 注入 ──

    def format_for_prompt(self, limit: int = 0) -> str:
        """翻译阶段注入的规则文本：内置形态规则短句 + 持久库最新
        limit 条精确对照（map_to 有值才注入，避免膨胀）。"""
        lines = [
            "[特殊文本] * Y A W N * 等字母间隔全大写词是动作/音效表现，"
            "须译为中文动作词并保留星号（如 * Y A W N * → * 哈欠 *）",
            "[特殊文本] TOSS TRASH / PRESS START 等全大写短语是动作/命令"
            "文本（含动作动词），每个词都须译成中文"
            "（如 TOSS TRASH → 丢垃圾、PRESS START → 按开始），"
            "不得保留任何英文单词；人名/地名等专名才保留原文",
        ]
        if self.store is not None:
            rows = self.store.list_by_domain("text")
            if limit > 0:
                rows = rows[-limit:]
            for row in rows:
                if not row["map_to"]:
                    continue
                if row.get("status") == "deprecated":
                    continue  # #43 阶段 A：退役知识不注入 prompt
                lines.append(
                    f"[特殊文本] “{row['pattern']}”应译为“{row['map_to']}”"
                    f"（{row['kind']}）")
        return "\n".join(lines)

    # ── 学习：跑完一场后从「该翻未翻」条目沉淀 ──

    def learn(self, entries, source_game: str,
              names: set[str] | None = None) -> tuple[int, int]:
        """从「该翻未翻」回显条目（译文==原文）提取模式入库。

        两类都是真实漏翻证据：质量门通过但回显（曾被专名豁免的 TOSS
        TRASH）、质量门拒绝的 untranslated_text 回显（重试仍回显，模型
        惯性——taxes 实证 2 条）。纯专名回显（在专名清单/无动作词）
        不学习。返回 (新增条数, 命中数)。
        """
        if self.store is None:
            return 0, 0
        names = names or set()
        learned = hits = 0
        for e in entries:
            if not e.translation:
                continue
            rejected = set(e.meta.get("quality_reasons", ()))
            if e.translation == e.original:
                # 回显：质量门通过但回显（曾被专名豁免）或拒绝 → 都学
                if not (rejected & {"untranslated_text", "action_word_residue"}
                        or e.meta.get("quality_passed")):
                    continue
            else:
                # 非回显：仅 action_word_residue 拒绝（半翻译残留英文）
                # 学习——其余失败与知识库形态无关
                if "action_word_residue" not in rejected:
                    continue
            original = str(e.original)
            if not original.strip() or original in names:
                continue
            # 结构键/代码串/专名载体（§ 键码、路径、URL…）不是「该翻未翻」
            # 的可译文本——学习会污染知识库（butterflies 实证：§m_language_en
            # ### 的 en 是语言代码后缀，被罗曼功能词误判成 multilingual_source
            # 入库，反向把结构键送入翻译）
            if should_skip(original):
                continue
            if _is_spaced_action(original):
                learned += self.store.upsert(
                    "text", "spaced_action", original, action="translate",
                    note="自动学习：间隔动作词回显",
                    source="auto", game=source_game)
                hits += 1
            elif _is_uppercase_action(original):
                # map_to 由动作词表机械直译生成：重试降级（native_translate
                # 无 system prompt）时作为 references 译例带出，模型照做
                learned += self.store.upsert(
                    "text", "uppercase_action", original, action="translate",
                    map_to=translate_uppercase_action(original) or "",
                    note="自动学习：大写动作指令回显",
                    source="auto", game=source_game)
                hits += 1
            elif _is_multilingual_source(original):
                # 其他语言源回显（法语 Clé en Fer 等模型不认识）→ 沉淀形态
                # 规则；译例需人工沉淀或同对象译例机制（batch_translator
                # 的 _obj_reference_pairs），模型对完全回显无机械直译来源
                learned += self.store.upsert(
                    "text", "multilingual_source", original, action="translate",
                    note="自动学习：其他语言源文本回显（含假名/重音字母）",
                    source="auto", game=source_game)
                hits += 1
            elif _is_single_lexicon_word(original):
                # 单词词表机械直译（JUMP/Vsync → 跳跃/垂直同步）：词表命中
                # 的单设置词回显，map_to 由词表生成——重试降级 references
                # 带出译例（force-reboot 实证：全大写键名 1.8B 稳定回显）
                zh = ({**_ACTION_VERB_ZH, **_COMMON_NOUN_ZH}
                      .get(original.casefold(), ""))
                if zh:
                    learned += self.store.upsert(
                        "text", "single_lexicon_word", original,
                        action="translate", map_to=zh,
                        note="自动学习：单词词表命中回显",
                        source="auto", game=source_game)
                    hits += 1
        return learned, hits

    def format_reference_pairs(self) -> list[tuple[str, str]]:
        """知识库译例对照（pattern → map_to），并入 glossary references。

        native_translate（Hy-MT2 官方单段 prompt）用 terms 机制注入——
        source 命中原文即带出 "TOSS TRASH translates to 丢垃圾"，重试时
        模型看到具体译例，而非只有抽象规则。
        """
        if self.store is None:
            return []
        pairs = []
        for row in self.store.list_by_domain("text"):
            if row["map_to"]:
                pairs.append((str(row["pattern"]), str(row["map_to"])))
        return pairs

    # ── fail_case 域：失败案例库（六库蓝图 2，FAIL 标准格式） ──

    _FAIL_TYPES = frozenset(
        {"提取", "识别", "分类", "翻译", "写回", "显示", "崩溃", "审核"})
    _FAIL_KEYS = ("fail_no", "game", "env", "issue", "phenomenon",
                  "root_cause", "solution", "impact", "fixed_version",
                  "fail_type")

    def record_case(self, *, game: str, fail_type: str, problem: str,
                    root_cause: str, fix: str, symptom: str = "",
                    impact: str = "", version: str = "",
                    environment: str = "Unity",
                    source: str = "manual") -> bool:
        """失败案例入库（fail_case 域，FAIL-编号标准格式，note 结构化 JSON）。

        案例即「经验大脑」的长期积累：下次发现同类失败 → search_cases
        检索历史 → 复用已验证的修复方案，而非重新追查。
        fail_type ∈ 提取/识别/分类/翻译/写回/显示/崩溃。幂等（同问题
        同游戏不重复）。note 为 JSON：
        {fail_no, game, env, issue, phenomenon, root_cause, solution,
        impact, fixed_version, fail_type}（§0.4.3 结构化字段）。
        source: manual（knowledge_seed 人工）/ auto（runner 闭环自动）。"""
        if self.store is None:
            return False
        if fail_type not in self._FAIL_TYPES:
            fail_type = "翻译"
        existing = self.store.list_by_domain("fail_case")
        number = len(existing) + 1
        note = json.dumps({
            "fail_no": f"FAIL-{number:05d}", "game": game,
            "env": environment, "issue": problem, "phenomenon": symptom,
            "root_cause": root_cause, "solution": fix,
            "impact": impact, "fixed_version": version,
            "fail_type": fail_type,
        }, ensure_ascii=False)
        return bool(self.store.upsert(
            "fail_case", fail_type, problem, action="apply_fix", note=note,
            source=source, game=game))

    def record_review_failure(self, failure: dict, *,
                              source: str = "auto") -> bool:
        """审核失败结构化入库（fail_case 域，review_failure_v1 schema）。

        Phase B-5（审计 P1-7）：CRITICAL/MAJOR 语义错译与 REVIEW_ERROR
        管线错误进入失败案例闭环——错误译文/正确译文/错误类型/审核理由
        结构化留档；match_case/search_keyword 解析 note JSON 的 original
        字段可按原文召回同类失败作反例（KnowledgeRetrieval 接入点）。

        幂等（pattern=game:locator）：同条目重审只 hits+1 并刷新 note，
        不产生重复案例。收敛与未收敛均记录；correct_translation 是否
        填充由 reviewer 构建时保证（仅终态 APPROVED 系）。"""
        if self.store is None:
            return False
        # BUILTIN 冲突门禁（2026-09-01 记忆库/知识库污染系统性根治）：
        # 失败案例的 correct_translation 若与内置 UI 权威冲突（如
        # Disabled→残疾人士）→ 留空——坏译名不得成为可召回的正确例。
        # 幂等（pattern=game:locator）不变，仅净化内容。延迟导入避免
        # 模块级成环（translator ↔ knowledge，见顶部注释）。
        from hanhua.core.translator import builtin_ui_conflict
        if builtin_ui_conflict(failure.get("original", ""),
                               failure.get("correct_translation", "")):
            failure = {**failure, "correct_translation": ""}
        pattern = failure_pattern(failure)
        if not pattern:
            return False
        note = json.dumps(failure, ensure_ascii=False)
        return bool(self.store.upsert(
            "fail_case", "审核", pattern, action="apply_fix", note=note,
            source=source, game=str(failure.get("game") or "")))

    def migrate_legacy_notes(self) -> tuple[int, int]:
        """旧库 fail_case note 迁移：FAIL-|键:值| 管道格式 → 结构化 JSON。

        2026-08-11 前 record_case 写管道字符串（游戏/环境/问题/现象/根因/
        解决/影响范围/修复版本/失败类型字段齐全，只差结构化）——原地升级
        不丢 pattern/hits。返回 (迁移数, 已新格式数)。幂等可重复执行。"""
        if self.store is None:
            return 0, 0
        migrated = already = 0
        with self.store._lock:
            rows = self.store.list_by_domain("fail_case")
            for r in rows:
                note = str(r["note"])
                if note.startswith("{"):
                    already += 1
                    continue
                fields = self.parse_case_note(note)
                if not fields.get("issue"):
                    continue
                new_note = json.dumps(fields, ensure_ascii=False)
                self.store.conn.execute(
                    "UPDATE knowledge_items SET note=? WHERE id=?",
                    (new_note, r["id"]))
                migrated += 1
            self.store.conn.commit()
        return migrated, already

    def solve(self, problem: str, limit_each: int = 3) -> dict[str, list[dict]]:
        """跨库联动检索（§0.4.1 六库关系链）：一个问题的全部答案。

        按 domain 分组返回相关条目——结构方案（unity_structure）、历史
        案例（fail_case）、判定规则（text）、组件方案（component_compat）、
        质量规则（quality）、写回方案（writeback）。同一拆词打分逻辑
        （整串 +3、拆词 +1、中文 2 字滑窗、同分按 hits）。"""
        keys = [problem]
        for run in re.findall(r"[一-鿿]{2,}", problem):
            keys.append(run)
            keys += [run[i:i + 2] for i in range(len(run) - 1)]
        keys += [w.casefold() for w in
                 re.findall(r"[A-Za-z]{3,}", problem)]
        keys = list(dict.fromkeys(k for k in keys if k))
        out: dict[str, list[dict]] = {}
        for lib in self.SIX_LIBRARIES:
            scored: list[tuple[int, int, dict]] = []
            for row in self.list_knowledge(domain=lib):
                hay = (f"{row.get('pattern', '')}|{row.get('map_to', '')}"
                       f"|{row.get('note', '')}").casefold()
                score = 0
                if problem.casefold() in hay:
                    score += 3
                for k in keys:
                    if k in hay:
                        score += 1
                if score > 0:
                    scored.append(
                        (score, int(row.get("hits", 0) or 0), row))
            scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
            out[lib] = [r for _, _, r in scored[:limit_each]]
        return out

    def renumber_cases(self) -> int:
        """fail_case fail_no 唯一化：按 id 升序重编 FAIL-00001 起连续编号。

        旧库编号错位（len(existing)+1 与 upsert 刷新 note 叠加导致 FAIL-
        00067 重复 48 次）——结构化迁移时一并修复编号唯一性（不丢内容）。
        返回重编号条数。幂等：编号已连续唯一则 0。"""
        if self.store is None:
            return 0
        rows = sorted(self.store.list_by_domain("fail_case"),
                      key=lambda r: r["id"])
        changed = 0
        with self.store._lock:
            for idx, r in enumerate(rows, start=1):
                try:
                    fields = json.loads(r["note"])
                except ValueError:
                    continue
                want = f"FAIL-{idx:05d}"
                if fields.get("fail_no") != want:
                    fields["fail_no"] = want
                    self.store.conn.execute(
                        "UPDATE knowledge_items SET note=? WHERE id=?",
                        (json.dumps(fields, ensure_ascii=False), r["id"]))
                    changed += 1
            self.store.conn.commit()
        return changed

    @staticmethod
    def parse_case_note(note: str) -> dict:
        """解析 fail_case note：新版 JSON 直接返回；旧版 FAIL-|键:值| 管道
        格式兼容解析（迁移脚本用）。"""
        if note.startswith("{"):
            try:
                return json.loads(note)
            except ValueError:
                pass
        # 旧格式：FAIL-00067|游戏:x|环境:y|问题:z|…（2026-08-11 前）
        fields = {k: "" for k in ("fail_no", "game", "env", "issue",
                                  "phenomenon", "root_cause", "solution",
                                  "impact", "fixed_version", "fail_type")}
        for part in note.split("|"):
            part = part.strip()
            if not part:
                continue
            if part.startswith("FAIL-"):
                fields["fail_no"] = part.split(" ")[0]
                continue
            if ":" not in part:
                continue
            key, _, value = part.partition(":")
            key_map = {"游戏": "game", "环境": "env", "问题": "issue",
                       "现象": "phenomenon", "根因": "root_cause",
                       "解决": "solution", "影响范围": "impact",
                       "修复版本": "fixed_version", "失败类型": "fail_type"}
            k = key_map.get(key.strip())
            if k:
                fields[k] = value.strip()
        return fields

    def search_cases(self, fail_type: str | None = None,
                     keyword: str | None = None) -> list[dict]:
        """检索失败案例库：按失败类型和/或关键词过滤（案例检索复用）。

        关键词匹配 pattern（问题）或 note（结构化 JSON 全文，含游戏/根因/
        解决/影响范围等所有字段——历史案例命中即提示复用已有方案）。"""
        if self.store is None:
            return []
        rows = self.store.list_by_domain("fail_case")
        if fail_type:
            rows = [r for r in rows if r["kind"] == fail_type]
        if keyword:
            k = keyword.casefold()
            rows = [r for r in rows
                    if k in r["pattern"].casefold()
                    or k in r["note"].casefold()]
        return rows

    # ── 六库统一查询接口（§0.4.3：按 domain/kind 查询 + 命中统计） ──

    SIX_LIBRARIES = ("unity_structure", "fail_case", "text",
                     "component_compat", "quality", "writeback")

    def list_knowledge(self, domain: str | None = None,
                       kind: str | None = None) -> list[dict]:
        """按库（domain）和/或子类（kind）查询知识条目，hits 降序。

        合并内置种子（BUILTIN_RULES）+ 持久库；domain 省略 = 全部六库。
        返回条目含 hits（累计命中证据）——命中统计即「哪条知识最常用」。"""
        rows = list(BUILTIN_RULES)
        if self.store is not None:
            rows += self.store.list_all()
        if domain:
            rows = [r for r in rows if r["domain"] == domain]
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        return sorted(rows, key=lambda r: r.get("hits", 0), reverse=True)

    def search_keyword(self, keyword: str,
                       domains: tuple[str, ...] | None = None) -> list[dict]:
        """跨库全文检索：pattern/map_to/note（含 fail_case JSON 解析字段）
        任一处含关键词即命中，hits 降序。

        用途：新游戏遇问题 → 一次检索所有相关历史知识（结构方案/失败案例/
        组件兼容/质量规则），而非只查失败案例库（§0.4.1 六库联动）。"""
        k = keyword.casefold()
        rows = self.list_knowledge()
        if domains:
            rows = [r for r in rows if r["domain"] in domains]
        out = []
        for r in rows:
            fields = (str(r.get("pattern", "")), str(r.get("map_to", "")),
                      str(r.get("note", "")))
            if any(k in f.casefold() for f in fields):
                out.append(r)
        return out

    def match_case(self, problem: str, fail_type: str | None = None,
                   limit: int = 3) -> list[dict]:
        """失败案例智能复用：按问题短语拆词对 fail_case 全量打分检索。

        runner 闭环遇到失败模式时调用——同模式历史案例直接给出已验证的
        解决方案与修复版本，而非重新追查（§0.4.2 经验大脑复用）。

        打分：整串命中 +3，拆词命中（≥2 字中文词 / ≥3 字母英文词）+1；
        中文连续串加 2 字滑窗（「标签值格式串」→ 标签/值格/格式）——
        无分词依赖也能按语义单元命中；同分按 hits（累计命中证据）排序，
        特异词（slash）不被高频词（回显/判失败）淹没。"""
        if self.store is None:
            return []
        keys = [problem]
        zh_runs = re.findall(r"[一-鿿]{2,}", problem)
        for run in zh_runs:
            keys.append(run)
            keys += [run[i:i + 2] for i in range(len(run) - 1)]
        keys += [w.casefold() for w in
                 re.findall(r"[A-Za-z]{3,}", problem)]
        keys = list(dict.fromkeys(k for k in keys if k))
        scored: list[tuple[int, int, dict]] = []
        for row in self.store.list_by_domain("fail_case"):
            if fail_type and row["kind"] != fail_type:
                continue
            hay = (f"{row['pattern']}|{row['map_to']}|{row['note']}"
                   ).casefold()
            score = 0
            if problem.casefold() in hay:
                score += 3
            for k in keys:
                if k in hay:
                    score += 1
            if score > 0:
                scored.append((score, int(row.get("hits", 0)), row))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [r for _, _, r in scored[:limit]]

    def library_stats(self) -> dict[str, dict]:
        """六库命中统计：每库条数（内置种子 + 持久库）与总 hits。

        供 CLI 验证与报告（六库可查询 + 种子就位检查 §0.4.5）。"""
        rows = self.list_knowledge()
        stats: dict[str, dict] = {}
        for lib in self.SIX_LIBRARIES:
            lib_rows = [r for r in rows if r["domain"] == lib]
            kinds: dict[str, int] = {}
            for r in lib_rows:
                kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
            stats[lib] = {
                "count": len(lib_rows),
                "hits": sum(int(r.get("hits", 0)) for r in lib_rows),
                "kinds": kinds,
            }
        return stats

    def game_stats(self) -> dict[str, dict]:
        """按游戏统计沉淀（持久库 game 字段）：每游戏条数与 hits。

        排查进度可视化：哪些游戏沉淀最多经验（经验最丰富），哪些还没有
        （新游戏无先验，识别/翻译依赖内置规则）。"""
        if self.store is None:
            return {}
        stats: dict[str, dict] = {}
        for r in self.store.list_all():
            game = r.get("game") or ""
            if not game:
                continue
            entry = stats.setdefault(game, {"count": 0, "hits": 0,
                                            "domains": {}})
            entry["count"] += 1
            entry["hits"] += int(r.get("hits", 0))
            entry["domains"][r["domain"]] = \
                entry["domains"].get(r["domain"], 0) + 1
        return dict(sorted(stats.items(),
                           key=lambda kv: kv[1]["hits"], reverse=True))

    # ── 全库视图（报告/文档/人工查阅） ──

    def describe(self) -> list[dict]:
        """种子 + 持久库合并视图（rule/file 域供报告与人工查阅）。"""
        return list(BUILTIN_RULES) + (self.store.list_all() if self.store else [])

    def close(self):
        if self.store is not None:
            self.store.close()
            self.store = None
