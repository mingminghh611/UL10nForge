from __future__ import annotations
from collections import Counter
import json
import re

from hanhua.core.engine_strings import (interaction_input_events,
                                        is_interaction_prompt)

# 单 token 英文功能词（介词/连词/助动词/高频副词）——全局强制词对
# 过滤表（2026-08-13 F10）：功能词做强制词对必然误杀自然文本
# （honorplusplus 自动沉淀 ON→关于/on→在/off→关闭 后，incremental-rts
# 'Analytics is ON.'、URL 行 '...on+gnu%2Blinux'、inch-by-inch
# 'Start Ingredients' 全被 glossary_mismatch 误杀——单 token 污染
# 词对教训）。共享定义：agent_memory（沉淀端：功能词不晋升 active）
# 与 quality（检查端：功能词词对不强制）共用，防漂移。
FUNCTION_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by",
    "can", "could", "did", "do", "does", "down", "for", "from", "had",
    "has", "have", "if", "in", "into", "is", "it", "its", "may", "might",
    "no", "nor", "not", "of", "off", "on", "or", "out", "over", "than",
    "the", "then", "this", "to", "too", "under", "up", "very", "was",
    "were", "will", "with", "would", "yes",
    # 2026-08-24 补充常用语法骨架词（键盘噪声误杀修复）：真实英文句的
    # 代词/介词/连词/副词，键盘乱打一个都没有。'plus flex sands powerfull
    # powder is so strong'（is/so）这类开发者自嘲句靠它们识别为真实句子。
    "so", "that", "these", "those", "there", "here", "when", "where",
    "which", "who", "whom", "whose", "why", "how", "what", "because",
    "although", "though", "while", "until", "unless", "since", "once",
    "after", "before", "about", "above", "across", "against", "along",
    "among", "around", "behind", "below", "beneath", "beside", "between",
    "beyond", "during", "inside", "near", "onto", "outside", "through",
    "toward", "towards", "upon", "within", "without", "some", "any",
    "many", "much", "each", "every", "few", "most", "other", "another",
    "both", "either", "neither", "should", "must", "need", "him", "her",
    "his", "my", "our", "their", "your", "me", "us", "them", "he", "she",
    "they", "we", "you", "am", "itself", "himself", "herself", "myself",
    "yourself", "themselves",
})

# 句子语法骨架词（2026-08-24 键盘噪声误杀修复专用，不并入 FUNCTION_WORDS
# 以免影响术语/词对过滤）：真实英文句的代词/介词/连词/情态词是语法骨架，
# 键盘乱打（asdasdasd / fdji ijsdijn）一个都没有。噪声判别时 ≥2 个本集词
# → 真实句子（he was a good frog… 的 was/a/and/at/the；plus flex sands…
# 的 is/so/with）。
_SENTENCE_SKELETON = frozenset(FUNCTION_WORDS) | frozenset({
    "that", "these", "those", "there", "here", "so", "such", "when",
    "where", "which", "who", "whom", "whose", "why", "how", "what",
    "because", "although", "though", "while", "until", "unless", "since",
    "once", "after", "before", "about", "above", "across", "against",
    "among", "around", "behind", "below", "beneath", "beside", "between",
    "beyond", "during", "inside", "near", "onto", "outside", "through",
    "toward", "towards", "upon", "within", "without", "some", "any",
    "many", "much", "each", "every", "few", "most", "other", "another",
    "both", "either", "neither", "should", "must", "need", "let", "get",
    "got", "been", "being", "him", "her", "his", "my", "our", "their",
    "your", "me", "us", "them", "he", "she", "they", "we", "you", "i",
    "am", "my", "all",
})

BB_TAG_PATTERN = re.compile(
    r"\[/?(?:b|i|u|s|color|size|font|url|sprite)(?:=[^\]\r\n]+)?\]",
    re.I,
)
FORMAT_TAG_PATTERN = re.compile(
    r"</?[A-Za-z][^>\r\n]{0,49}>|"
    r"\[/?(?:b|i|u|s|color|size|font|url|sprite)(?:=[^\]\r\n]+)?\]",
    re.I,
)

PLACEHOLDER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("brace",   re.compile(
        r"\{/?[a-zA-Z0-9_.\-]+(?:=[^{}\r\n]*)?(?:,-?\d+)?(?::[^{}\r\n]+)?\}")),
    # {0} {name} {1:0.00} {0,-10:N2} {w=1.5} {/i}（含 Ren'Py 等号值/结束标签）
    ("percent", re.compile(r"%[-+0-9.l]*[a-zA-Z%]")),      # %s %d %1.2f %%
    ("html",    re.compile(r"</?[a-zA-Z][^>]{0,49}>")),    # <b> </b> <color=#fff>
    ("bb",      BB_TAG_PATTERN),                              # [b] [color=#fff]
    ("newline", re.compile(r"\\n")),                       # 字面 \n
    # Undertale 系对话脚本标记：行首 "* " 对话符（模型常整段丢弃，DELTATRAVELER
    # 真实样本）→ 逐字保护；"* (选项)" 的括号是可选样式（既有行为允许去括号）。
    # F13（2026-08-13 interdream 实证）：DELTATRAVELER 文本资源里换行是
    # C# 转义字面 "\n"（两个字符）而非真换行——(?m)^ 只匹配真行首，字面
    # "\n" 后的行首 "* " 漏保（'* ...^10\n* ...' 第二个 '*' 被模型丢弃
    # 未被拦截）。(?:^|\\n) 同时覆盖真行首与字面转义行首。
    ("undertale_bullet", re.compile(r"(?m)(?:^|\\n)\* ")),
    # 计时码 "^NN"（多行对话逐行收尾）→ 模型常丢 "^NN" → 保护。F13：
    # 原模式只匹配 ")^05" 括号形式，interdream 实证形式多样（",^05"、
    # ".^05"、"…^05"、"^05* " 行首接对话符、"?" 后）。前邻非字母数字
    # （逗号/句点/问号/行首）即匹配——"x^10" 数学幂（前邻字母）不受影响。
    ("undertale_timing", re.compile(r"(?<![A-Za-z0-9_])\^[0-9]{1,2}")),
]

_DEV_TEMPLATE_PLACEHOLDER = re.compile(
    r"(?i)^(?:[a-z0-9]+ ){0,4}"
    r"(?:description|name|title|text|content|info|details|dialog|dialogue) here!*$",
)
# 引擎/编辑器默认名占位（开发者未填写字段的默认值，翻译无意义且是哑信号）：
# 'Information text'（Fungus.InfoText 默认）、'Character Name'（Fungus.Character
# 默认角色名）、'New Text'/'New Sprite'/'New Material'（Unity 编辑器默认资源名）——
# 模型对占位名稳定回显或音译，恒败（a-catfiends 实证 2026-09-02：Information
# text/Character Name 被当显示文本进池）。True 显示词（'Information'/'Character'
# 作正式按钮文本）不在此列——本正则要求完整形态匹配「占位标签 + Name/Text 后缀」
# 或裸 'new xxx' 编辑器名，真实 UI 词不含该结构。
_DEFAULT_NAME_PLACEHOLDER = re.compile(
    r"(?i)^(?:information|description|character|player|enemy|item|object|"
    r"dialogue|dialog|message|text|name)\s+(?:text|name|label|title|icon)[']?s?$"
    r"|^new\s+(?:text|sprite|material|game\s*object|script|animation|audio|"
    r"particle|prefab|material|scene|ui|canvas|button|image|text\s*mesh)[\s'']*$"
    r"|^game\s+name[']?s?$|^enter\s+(?:name|title|text)\b",
)

_HTML_OR_BB = re.compile(
    r"^(?:<[^>]+>|\[/?(?:b|i|u|s|color|size|font|url|sprite)(?:=[^\]]+)?\])$",
    re.I,
)
_URL = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9+.-]*://|www\.)\S+$|"
    r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
)
_ONLY_SYMBOL = re.compile(r"^[\W_]+$")
# 星号前缀单词（*shit / *beaner）：TextAsset 脚本里的词表/列表条目
# （baldis resources.assets#71 实证）。模型对 * 前缀短词稳定回显
# （* 被当强调标记），翻译无意义 → 词表条目跳过。星号+空格
# （"* (text)" 对话格式）不匹配。
_STAR_PREFIXED_WORD = re.compile(r"^\*[a-z]{3,}$")
# 引擎富文本控制码（faerie-afterlight 实证：'.^.b' 的 '^b'、'^tr'、
# '^denvis'——'^' + 字母段是引擎样式/命令标记）。剥除后无可译英文词
# 的串在 is_hard_structural 中跳过；含真实内容的（'^denvis' 剥后
# 'denvis'）不误伤（denvis 印尼语内容走正常翻译）。
_ENGINE_CTRL_CODE = re.compile(r"\^[^A-Za-z0-9]{0,2}[A-Za-z]{1,12}")
# 3+ 字母英文词（控制码剥除后的可译语义判定用）
_ENGLISH_WORD_MIN3 = re.compile(r"[A-Za-z]{3,}")
# 混合符号 token：无空格、含至少一个强代码符号（%#&^$@|\）、含字母。
# 匹配随机 token/编码串（'xChDC-Gs%OmaMl+g'）；正常英文句子的强符号
# 都是 '100% sure' 式带空格或有 '=' 成对出现（a=b），不匹配。
# 不含 ! ~（'Kyahaaaaa~!' 日式语气词、'WOW!!!' 是正常文本，误伤
# 实证：unityscript 粒子文本测试）。
_MIXED_SYMBOL_TOKEN = re.compile(
    r"^(?=.*[%#&^$@|\\])(?=.*[A-Za-z])[^\s]+$")
_HAS_LETTER = re.compile(r"[^\W_0-9]")
# 东亚文字探测（CJK 统一表意/假名/谚文）：单字即整词（'你'/'の'/'안'），
# 单字母结构过滤不得误伤（F49 配套规则）
_HAS_EAST_ASIAN = re.compile(r"[㐀-䶿一-鿿豈-﫿"
                             r"぀-ヿ가-힯]")
_STRIP_RICH_TEXT = re.compile(r"<[^>]+>")
# Unity 实例化对象名：frameVertical(Clone) / Player(Clone)(Clone)
_CLONE_SUFFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\(Clone\))+$")
# 资源副本实例名：CreditsVolume (1) Profile（Unity 场景对象命名惯例
# 「名 (编号) 名」，含空格+数字括号；要求全部词 TitleCase——'Press (1)
# to start' 这类交互提示含小写词不匹配。模型对资源名输出解释式垃圾
# （containment 实证：'参考以下翻译：…'），翻译无意义 → 跳过）
_CLONE_NUMBERED = re.compile(
    r"^[A-Z][A-Za-z0-9]*(\s+[A-Z][A-Za-z0-9]*)*\s*\(\d+\)"
    r"(\s+[A-Z][A-Za-z0-9]*)*\s*$")
# markdown 加粗段落行：行首 [ \t]** 且行内无其他星号（无闭合标记）
# ——README/Changelog 文档说明行（\t**All languages are loaded...），
# 开发者文档非游戏文本；'**Bold** text' 含闭合星号不匹配（对话强调
# 正常翻译）。模型对 ** 段内词稳定保留/半翻（containment 实证 4 条
# target_script_mismatch 恒败）→ 跳过
_MD_BOLD_LEAD = re.compile(r"^[ \t]*\*\*[^*]*$")
# 点开头扩展名：.spriteatlas
_DOT_EXTENSION = re.compile(r"^\.[A-Za-z0-9_]{2,12}$")
# F53（adapt-prologue 实证 214 条）：.NET 程序集限定类型名
# 'System.Boolean, mscorlib'（C# 反射 Type.GetType 按名加载的代码结构，
# JSON 字段 returnType 等常见）——至少一边含点（'Hello, world' 无点
# 不误杀），翻译断反射/反序列化
_NET_ASSEMBLY_QUALIFIED_TYPE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_.]*\.[A-Za-z_][A-Za-z0-9_.]*,\s*"
    r"[A-Za-z_][A-Za-z0-9_.]*|[A-Za-z_][A-Za-z0-9_.]*,\s*"
    r"[A-Za-z_][A-Za-z0-9_.]*\.[A-Za-z_][A-Za-z0-9_.]*)$")
# GUID 标识符：GUID:cef3ca5fc32178c449992c58120ccded
_GUID_IDENTIFIER = re.compile(r"^GUID:[0-9a-fA-F]{32}$")
# YarnSpinner 字符串表键（line: 前缀 + FNV 哈希）：对话文本以键引用、
# 真实文本在邻近字符串（count-my-coins 实证：obj=1354 内 214 个键 +
# 对话文本同对象）。键不是玩家可见文本，模型回显恒败
# （untranslated 216 / target_script 15 双形态）→ 跳过
_LINE_HASH_IDENTIFIER = re.compile(r"^line:[0-9a-fA-F]{6,}$")
# C# 编译期插值残留（{nameof(x)} / {typeof(T)}）：日志消息模板字符串
# （YarnSpinner 'Can't save variables to JSON: {nameof(variableStorage)}
# is not set' 实证——运行时字符串不会含未展开的 nameof）→ 跳过
_C_SHARP_INTERPOLATION = re.compile(r"\{nameof\(|\{typeof\(|\{nameof\b")
# 调试 HUD 输出行（(Debug): 前缀：调试面板行标签，非玩家体验文本；
# count-my-coins '(Debug): 1000' 实证：Debug 译成 调试 反而触发
# input_token_mismatch——Debug 被当按键字面量）→ 跳过
_DEBUG_PREFIX_LINE = re.compile(r"^\([Dd]ebug\)\s*:")
# YarnSpinner 编辑器节点边标签（ACTION edge / WAIT edge：节点类型 +
# edge，对话图编辑器 UI，运行时不可见）→ 跳过
_UPPERCASE_EDGE_LABEL = re.compile(r"^[A-Z]{2,} edge$")
# I2 Localization 复数模板占位：{0:p:mine|mines}（运行时按数量展开单复数；
# 翻译会破坏 I2 的 plural 语法，minato 等 I2 游戏真实失败样本）
_I2_PLURAL_BLOCK = re.compile(r"\{[^{}\n]*:p:[^{}\n]*\}")
# IL2CPP 生成的模块调试行：\nmodule.renderOrderPriority: （引擎内部字符串，
# 非游戏文本，翻译必失败；minato global-metadata.dat 真实样本）
_IL2CPP_MODULE_DEBUG = re.compile(r"^module\.[A-Za-z0-9_]+:\s*$")
# 开发者重复占位行：Hello\nHello\nHello\nHello（同一短行重复 ≥4 次，
# 模型必回显，flabby-pizza 真实样本；长行/低重复是真实戏剧文本）
_REPEATED_PLACEHOLDER_LINE = re.compile(
    r"^([^\r\n]{1,16})\n\1(?:\n\1){2,}$")
# Master Audio 插件总线行：\t2810670744\tSoundFX\t\\Default Work Unit\\Master Audio Bus\\
_MASTER_AUDIO_BUS = re.compile(
    r"^[\t ]*\d{6,}[\t ]+[^\r\n]*\\Default[ \t]+Work[ \t]+Unit\\")
# 署名年份行：Darien Gore (Fleebs) 2019 / 3DI70R 2024（人名 + 可选别名 + 年份）；
# Level/Stage 等关卡前缀词 + 年份（Level 2024）不算署名
_CREDIT_YEAR_LINE = re.compile(
    r"^(?!(?:level|stage|chapter|episode|area|zone|round|day|week|wave|"
    r"room|floor|world)\b)(?:[A-Za-z0-9][A-Za-z0-9' -]*"
    r"(?:\([^)]*\))?[\t ]*)(?:19|20)\d{2}$",
    re.I)
# Unity 内部符号：metadata 字符串字面量里的调试符号/程序集引用
# （Unity.Burst.Intrinsics.X86, Unity.Collections.AllocatorManager+SlabAllocator,
#  Unity.Burst, Version=...::DoGetCSRTrampoline() 等——不是游戏文本，
#  模型翻译反而吃逗号/改坏符号，panzershoot/faerie-afterlight 等真实失败样本）
_UNITY_SYMBOL = re.compile(r"^Unity\.[A-Za-z][^\r\n]*$")
_PDB_ALT_PATH = re.compile(r'^PdbAltPath="[^"\r\n]*"$')
# 版本号横幅：\t**\t\tVERSION 0.4.3\t\t**（版本标题保留原文是行业惯例）
_VERSION_BANNER = re.compile(
    r"^[\t ]*\*{1,2}[\t ]*VERSION[ \t]+\d+\.\d+[^\r\n]*$", re.I)
# zalgo 乱码：组合字符叠加的字体艺术文本（翻译必然失败/请求错误）
_COMBINING_MARKS = re.compile(
    r"[̀-ͯ᪰-᫿᷀-᷿"
    r"⃐-⃿︠-︯]")
# 模型正确保留的专名载体（目标脚本/未翻译检查前从语义中移除）：
# 3+ 段路径（User/Blah/Hey/HotelParadiseScreenshot）、域名（itch.io /
# OpenGameArt.com）、@用户名（@zkfie）、版本号（0.4.0beta）、文件扩展名
# （SPOLOUS.exe）、代码标记（[var:ID]）
# 后缀边界用 (?![A-Za-z0-9]) 而非 \b：Python re 的 \b 是 Unicode 词边界，
# 中文（\w）算词字符——'Speedrun.com上的排行榜' 的 com 后紧跟中文时
# \b 不成立 → 域名不剥 → com 被当小写普通词残留误判 target_script_mismatch
# （deepest-sword 实证）。lookahead 只排除 ASCII 词字符继续拼接（comedy
# 的 com 后接字母仍不剥），中文/标点/空白照常构成边界。
SAFE_KEEPERS = re.compile(
    r"https?://[^\s<>'\"]+"
    # 完整 URL（MacOS: https://support.apple.com/... 支持页链接行，
    # incremental-rts 实证：域名分支剥不掉 https 协议段与查询参数，
    # https/en/us 等残留被判小写普通词 → 整行回显误判 untranslated_text）。
    # 支持页链接行模型保留 URL 是正确行为（URL 非可翻译语义文本）。
    r"|[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+){2,}"
    r"|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:com|net|org|io|gg|dev|me|it|ru|de|jp)(?![A-Za-z0-9])"
    # 完整 email 地址（contact@undertowgames.com：@ 前本地部分也要剥，
    # 否则 'contact' 残留被判小写普通词——containment 实证：模型保留
    # 邮箱是正确行为，本地部分不是漏翻）
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"|@[\w.-]+"
    r"|[a-z0-9]+(?:\.[a-z0-9]+)+(?![A-Za-z0-9])"   # 用户名/艺名（yu.una）
    r"|\d+\.\d+[.\d]*[a-z]*(?![A-Za-z0-9])"
    r"|\.[A-Za-z]{2,5}(?![A-Za-z0-9])"            # 文件扩展名（SPOLOUS.exe 的 .exe）
    r"|\[[A-Za-z_][A-Za-z0-9_:#.-]*\]")      # 代码标记（[var:ID]）
# Lorem ipsum 占位文本（minato 真实样本：模型不翻译占位符是正常行为）
_LOREM_IPSUM = re.compile(r"^Lorem ipsum\b", re.I)
# hipster ipsum 占位文本特征词（hipster 风格 lorem ipsum 生成器的词汇，
# 'XOXO keytar glossier mumblecore. Tote bag listicle normcore kinfolk
# kogi hoodie...'：containment level3-6 assets 实证 6 条）。占位文本
# 无真实语义。词表放本文件（而非 quality）：placeholders 无依赖，
# quality 已导入 placeholders，反向导入成环。模型对占位文本行为随机
# ——回显走豁免路径、翻译走中文（如 'XOXO：Keytar风格，更精致、更
# 柔和。'）→ 行数/内容比对恒败。跳过是唯一稳定出口。
_HIPSTER_IPSUM_WORDS = frozenset({
    "keytar", "glossier", "mumblecore", "tote bag", "listicle",
    "normcore", "kinfolk", "kogi", "hoodie", "hashtag", "edison bulb",
    "lo-fi", "keffiyeh", "affogato", "health goth", "flexitarian",
    "enamel pin", "aesthetic", "food truck", "man bun", "lyft", "umami",
    "cardigan", "knausgaard", "narwhal", "mlkshk", "taxidermy",
    "tumeric", "freegan", "slow-carb", "cronut", "shoreditch",
    "vaporware", "pinterest", "fingerstache", "wayfarers", "chambray",
})


def is_hipster_ipsum(text: str) -> bool:
    """hipster ipsum 占位文本：≥4 个特征词命中（子串匹配）。"""
    folded = text.casefold()
    return sum(1 for w in _HIPSTER_IPSUM_WORDS if w in folded) >= 4
# Shell 命令（something-bad-on-the-moon 真实样本：find /var/log -name ... | tar）
_SHELL_COMMAND = re.compile(
    r"^(?:find|tar|ls|grep|sudo|chmod|rm|mkdir|unzip|wget|curl|mv|cp)\b"
    r"[^\r\n]*(\|[^\r\n]*)?$")
# 游戏 jam 署名（roots 真实样本："made in 48h\nfor Ludum Dare 48"，
# 允许前导空白/换行：" \nmade in 48h"）
_JAM_CREDIT = re.compile(r"^[\s]*made in \d+\s*h\b", re.I)
# 对话拟声/感叹词（hickory 实证 2026-09-05：对话行 'tck – er… everything
# is broken.' 被 _KEYBOARD_NOISE 分支 b 误杀——'tck' 是纯辅音 3 连
# （tutting 拟声），'er…' 同理，导致整句真实对话按键盘乱打跳过。拟声词
# （tck/tsk/shh/brr/psst/hmm…）在真实英语里合法存在且常带辅音簇，是
# 内容信号而非噪声信号。出现任一拟声词 → 真实文本语境，不判噪声
# （宁漏勿坏：漏判只多翻一条，误判会漏整条对话）。
_INTERJECTION_WORDS = frozenset({
    "tck", "tsk", "tsktsk", "hmm", "hmmm", "hmmmm", "mmh", "mhm",
    "uhh", "uhm", "uhhuh", "shh", "shhh", "brr", "brrr", "psst", "pss",
    "hss", "tut", "tuttut", "ugh", "ughh", "erm", "err", "huh", "phew",
    "ahh", "ahhh", "hnn", "grr", "grrr", "meow", "woof", "oink", "moo",
    "cluck", "hiss", "zzz", "achoo", "ahem", "aww", "eww", "eek", "oof",
    "yikes", "haha", "hehe", "hihi", "hoho", "mmm", "gah", "argh",
    "aargh", "bleh", "meh", "nyeh", "hng", "hnng", "geez", "whew",
})
# 键盘噪音/乱打文本（开发者测试占位符，真实样本：
# panzershoot "asdasdasd\nasda sdasd"、the-keeper "fdji ijsdijn j jnf oij..."）
# ——无真实单词，模型必然回显，跳过。
# 触发条件（全小写、无中日韩文字、非 URL 方案）：
#  a) 存在 ≥8 字符长词且含重复 3-gram（asdasdasd = 'asd'×3）
#  b) 或存在纯辅音词（jnf/tdr——真实英语几乎不存在无元音词；
#     排除 https/ftp 方案与 www）
_KEYBOARD_NOISE = re.compile(
    r"^(?=[^A-Z㐀-鿿぀-ヿ]*$)"
    r"(?:(?=.*\b[a-z]{8,}\b)(?=.*([a-z]{3}).*\1)"
    r"|(?=.*\b(?!www\b)[bcdfghjklmnpqrstvwxz]{3,}(?!://)\b))"
    r".*$", re.S)
# 路径/文件名/版本号等标识符风格值（无空格，点号或斜杠分隔）：如 Unity 程序集名、文件路径
_WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)[^\r\n]+$")
_UNIX_PATH = re.compile(r"^/(?:[^/\r\n]+/)*[^/\r\n]*$")
_EXPLICIT_RELATIVE_PATH = re.compile(r"^\.{1,2}[\\/][^\r\n]+$")
_BACKSLASH_PATH = re.compile(
    r"^(?!.*\\n)\\?[A-Za-z0-9_. -]+(?:\\[A-Za-z0-9_. -]+)+$")
_THREE_SEGMENT_PATH = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+$")
_EXTENSION_PATH = re.compile(
    r"^[A-Za-z0-9_.-]+/(?:[A-Za-z0-9_.-]+/)*"
    r"[A-Za-z0-9_-]+\.[A-Za-z0-9]{1,16}$")
_UNITY_ROOTED_PATH = re.compile(
    r"^(?:Assets|Packages|ProjectSettings|Library|StreamingAssets)[\\/].+$",
    re.I,
)
_INPUT_SYSTEM_BINDING = re.compile(
    r"^<[A-Za-z][A-Za-z0-9_.-]*>/[A-Za-z0-9_./*{}-]+$")
_INPUT_ACTION_IDENTIFIER = re.compile(
    r"^(?:UI/[A-Za-z0-9_.{}*-]+|\*/\{[A-Za-z0-9_.-]+\})$")
# InControl/Rewired 输入插件设备匹配正则（crash-back-in-time 实证 40 条：
# '.*x[\-]*box[ ]*360.*'、'^([xX]iaoji )?Gamesir-G3[svw]?($| [0-9]+.*)'）——
# 运行时按正则匹配手柄设备名，翻译破坏输入映射。真实显示文本不以 '.*'
# 或 '^'+元字符开头（'^' 分支要求首段无空格且含分组/字符类/量词元字符，
# 防 markdown/数学符号误伤）
_INPUT_DEVICE_REGEX = re.compile(
    r"^\.\*[\s\S]*|^\^[^ \r\n]*[()\[\]?$|][\s\S]*")
# 输入系统 API/组件名（微软 XInput/DirectInput 家族 + 常见输入后端）：
# 设备枚举/绑定字符串里孤立出现（ffs-full-game-demo 实证 'xinput'），
# 无品牌词/语境词不命中 _is_input_device_name，此处补孤立 API 名。
# 只收明确 API 形态词（'hid' 是真实英语词 hide 过去式，不收；
# 含版本后缀的 DLL 名不会出现在真实句子中）
_INPUT_API_NAMES = frozenset({
    "xinput", "xinput1_3", "xinput1_4", "xinput9_1_0", "dinput",
    "dinput8", "rawinput", "dxinput", "winmm", "wgl",
})
# 正则表达式串（手柄/设备名匹配模式，'[dD]+ual[ ]*[sS]+ense'——
# DualSense 匹配正则，ffs-full-game-demo 实证）：字符类 [xX] 后跟量词
# +/*/{} 是正则形态特征，真实显示文本不用字符类+量词结构；翻译破坏
# 运行时匹配逻辑。不匹配 markdown 链接 [text](url)（后跟 '(' 非量词）、
# 按键提示 [A]（无量词）
_REGEX_PATTERN = re.compile(
    r"\[[A-Za-z0-9^\\ \t-]+][+*?]|\\[dDwWsS]|\([^)\s]*\)[+*?]")
# 输入设备品牌/型号词（InControl 内置设备数据库跨游戏通用）：设备名
# （ipega media gamepad controller / idroid Snakebyte）与设备说明行
# （Full-sized ipega gamepad. Must be in Gamepad mode…）——翻译破坏
# 按名匹配，且模型对设备专名回显/音译都不稳定。'xbox' 太泛不入表
# （真实文本会含 Xbox），靠 gamepad/joy-con 关键词兜底
_INPUT_DEVICE_BRANDS = (
    "ipega", "idroid", "snakebyte", "gamesir", "8bitdo", "madcatz",
    "3dconnexion", "3drudder", "spacemouse", "spacepilot",
    "spaceexplorer", "shield portable",
)
_INPUT_DEVICE_WORDS = ("gamepad", "game pad", "joy-con", "controller",
                       "bluetooth", "wireless", "joystick", "remote")
# 通用设备名语境词（Rewired HardwareJoystickMap 设备说明行/'Unknown
# Controller' 等）——单独出现不足以判定（'Remote'/'Wheel' 也用于真实
# 游戏文本），须叠加设备专词/品牌词（见 _is_input_device_name 规则 5）。
# ffs-legacy 2026-08-31 实证：SharedResources 'Amazon Fire TV Remote'/
# 'Apple Siri Remote'/'Atari Jaguar Controller'/'Unknown Controller' 全
# 被放行进池。词表只收 Rewired 设备专词（moga/nexus/tv 太泛不入表）。
_INPUT_DEVICE_GENERIC_WORDS = (
    "moga", "fire tv", "siri", "jaguar", "nes", "snes", "n64",
    "gamecube", "genesis", "sega", "atari", "wii u", "xbox 360",
    "guitar", "drum", "turbo", "paddle", "rudder", "accelerator",
    "gamestick",
)
# 设备通用专词（'Unknown Controller' 首词）——单独出现（'Controller'）
# 太泛不入表（真实游戏文本含 Controller），须叠加语境词或专词
_INPUT_DEVICE_PLATFORM_WORDS = (
    "moga", "nimbus", "stadia", "nexus", "shield", "siri", "fire tv",
)
# 硬件元素标签词表（Rewired 映射默认按钮/轴/扳机标签，见 _is_input_
# device_name 规则 6）：元素类型词（stick/axis/trigger/d-pad/wheel/
# yoke/throttle/pedal/lever/hat/rocker/switch/slider/dial/knob/paddle/
# bumper/shoulder/button）+ 方向词（up/down/left/right/in/out）+ 轴
# 端词（x/y）+ 带编号/带方向的模式标签（Left Stick X/Throttle 1 Up/
# D-Pad Left）。命中即设备映射元素名，翻译破坏输入映射（引擎按原名
# 查找轴/按钮）。真实游戏文本（'Press up to jump'/'go left'）无编号/
# 无方向后缀/是动词短语，不命中。
_HW_ELEMENT_LABEL = re.compile(
    r"^(?:"
    r"Stick (?:X|Y|Up|Down|Left|Right)"
    r"|(?:Left|Right) Stick (?:X|Y|Up|Down|Left|Right|Button)"
    r"|Analog (?:Stick|Sticks|Pad)"
    r"|(?:Touchpad|Analog|Joypad|Acceleration|Position) (?:X|Y|Z)"
    r"|D-Pad(?: (?:Up|Down|Left|Right|(?:Up|Down)-(?:Left|Right)))?"
    r"|Axis \d+"
    r"|Button \d+(?: (?:Up|Down|Left|Right))?"
    r"|(?:L|R)(?:1|2|3|4|B|T)"
    r"|(?:Left|Right) (?:Trigger|Bumper|Shoulder|Paddle|Tilt)"
    r"|(?:L|R) (?:Trig|Bumper|Shoulder|Paddle|Tilt)"
    r"|(?:Back|Front|Left|Right|Top|Bottom|Center|Side) Tilt"
    r"|(?:Throttle|Lever|Pedal|Rudder|Hat|Switch|Rocker|Slider|Dial|"
    r"Knob|Wheel|Yoke|Tilt)(?: \d+)?(?: (?:Up|Down|Left|Right|In|Out))?"
    r"|(?:Grip|Hat|Stick|Switch|Touchpad|Wheel|Throttle|Lever|Pedal|Rudder|"
    r"Rocker|Slider|Dial|Knob|Tilt|Yoke|Pad|Thumb|Mini-Stick|Acceleration|"
    r"Joy|Joypad|Rotary|Shifter|POV|H[1-4])(?: \d+)?(?: (?:Up|Down|Left|Right|"
    r"In|Out|Press|Button|Click|Touch|X|Y|L|R|Dn|Center|"
    r"(?:Up|Down|Left|Right)-(?:Left|Right)))?"
    r"|POV(?: (?:Up|Down|Left|Right))? HAT"
    r"(?: (?:Up|Down|Left|Right|Press|(?:Up|Down)-(?:Left|Right)))?"
    r"|(?:Grip|Thumb|Stick|Base|Front|Back|Left|Right|Center|Side|Pinky|"
    r"Index|Middle|Top|Bottom|Action|Aux|Rest|Palm|Ring) (?:Hat|Stick|Switch|"
    r"Index|Middle|Top|Bottom|Action|Aux|Rest|Palm|Ring) (?:Hat|Stick|Switch|"
    r"Slider|Button|Wheel|Lever|Pad|Pedal|Rocker|Knob|Dial|Throttle|Trigger|"
    r"Bumper|Touch|Rest|Position|Thumb)(?: \d+)?(?: (?:Up|Down|Left|Right|"
    r"Press|Click|Fwd|Back|In|Out|"
    r"(?:Up|Down|Left|Right)-(?:Left|Right)))?"
    r"|(?:Grip|Thumb|Stick|Base|Front|Back|Left|Right|Center|Side|Pinky|"
    r"Index|Middle|Top|Bottom) (?:Hat|Stick|Switch|Slider|Button|Wheel|Lever|"
    r"Pad|Pedal|Rocker|Knob|Dial|Throttle|Trigger|Bumper) "
    r"(?:Up|Down|Left|Right|Press|Click|Fwd|Back|In|Out)"
    r"|(?:Gas|Brake|Clutch|Accelerator) Pedal"
    r"|(?:Blue|Green|Red|Yellow|White|Black|Grey|Gray|Orange|Pink|Base|"
    r"Top|Bottom|Front|Back|Center|Action|Aux|Home|Guide) Button \d+"
    r"|(?:Action|Button|Trigger|Throttle|Pedal|Rudder|Hat|Switch|Rocker|"
    r"Slider|Dial|Knob|Wheel|Yoke|Lever|Stick|Shifter|Rotary) "
    r"(?:Top|Bottom|Base|Middle|Center|Front|Back|Left|Right|Side|Index|"
    r"Middle|Pinky|Thumb) (?:Button|Hat|Stick|Switch|Slider|Wheel|Lever|"
    r"Trigger|Pedal|Rocker|Dial|Knob|Paddle|Tilt|Rotary)(?: \d+)?"
    r"(?: (?:Up|Down|Left|Right|In|Out|Press|Click))?"
    r"|(?:Action|Button|Trigger|Throttle|Pedal|Rudder|Hat|Switch|Rocker|"
    r"Slider|Dial|Knob|Wheel|Yoke|Lever|Stick|Shifter|Rotary) "
    r"(?:Top|Bottom) Row \d+"
    r"|(?:Blue|Green|Red|Yellow|White|Black|Grey|Gray|Orange|Pink) "
    r"\((?:X|Y|A|B|Square|Circle|Triangle|Cross|L1|R1|L2|R2|LT|RT|LB|RB|"
    r"Select|Start|Home|Menu|Guide)\)"
    r"|Rotate Yoke(?: (?:Right|Left))?"
    r"|(?:Antenna|Assistant|Capture|Guide|Home|Menu|Select|Start|Touch)"
    r"(?: Button| Pad| Pad Press)"
    r"|(?:Antenna|Assistant|Capture)"
    r"|Accelerator"
    r"|(?:Brake|Clutch|Gas|Lever|Pedal|Rudder|Stick|Throttle|Yoke)"
    r"|(?:Pad|Wheel) (?:Up|Down|Left|Right|Press)"
    r"|(?:Wheel|Pad|Stick|Trigger|Pedal|Lever|Yoke|Throttle|Rudder|Hat|"
    r"Rocker|Switch|Slider|Dial|Knob) Button"
    r"(?: (?:[1-9]\d*|L[1-3]|R[1-3]|Up|Down|Left|Right))?"
    r")$", re.I)
# 设备名中的普通功能词（说明句 "Must be in Gamepad mode (hold X + Home)"
# 有句子结构，但已含品牌词+gamepad 关键词被上一分支覆盖；此处是纯
# 品牌词+型号的专名形态判定）
_INPUT_DEVICE_FUNCTION_WORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with",
    "by", "you", "your", "must", "hold", "press", "use", "how", "mode",
    "full", "micro", "sized", "mini", "pro", "classic", "wireless",
})


def _is_input_device_name(text: str) -> bool:
    """输入插件设备名/设备说明/硬件元素标签 → 结构跳过（Rewired/
    InControl 家族：DeviceInfo、HardwareJoystickMap、TemplateMap）。

    六种形态（任一命中）：
    1. 冒号品牌 ID：idroid:con（InControl 设备 ID brand:model 形态）
    2. 品牌词 + 设备语境词（gamepad/controller/bluetooth/wireless）：
       'ipega media gamepad controller'、'Micro ipega controller…'
    3. 品牌词 + 括号型号标识：(R)/(M1)/(Mode 1)/(2015 model)
       （'idroid:con Snakebyte (M1)'）或纯品牌专名（'idroid Snakebyte'）
    4. 括号型号标识 + 设备语境词（'Nvidia Shield … Controller (2015 model)'、
       'Joy-Con (R)'——joy-con 既是品牌也是语境词）
    5. 设备通用专词 + 语境词（'Amazon Fire TV Remote'——Rewired 设备名
       说明，ffs-legacy 实证 2026-08-31）：fire tv/siri/jaguar/nes/…
       + remote/controller/gamepad；或 Unknown + Controller（通用默认
       设备配置）
    6. 硬件元素标签（'Left Stick X'/'Throttle 1 Up'/'D-Pad Left'/'Axis 0'/
       'Brake'/'Gas Pedal'——Rewired 映射默认按钮/轴标签）：无品牌词、
       无设备语境词，是硬件元素专名形态（编号/方向/轴端词 + 硬件词），
       命中即跳过（翻译破坏输入映射的按名查找）
    """
    low = text.casefold()
    if re.fullmatch(r"[a-z]{2,20}:[a-z]{2,20}", low):
        return True
    has_suffix = bool(re.search(
        r"\((?:r|m\d|mode \d|19\d\d|20\d\d(?: model)?)\)$", low))
    has_brand = any(b in low for b in _INPUT_DEVICE_BRANDS)
    has_context = any(k in low for k in _INPUT_DEVICE_WORDS)
    if has_brand and has_context:
        return True
    if has_suffix and (has_context or has_brand):
        return True
    if has_brand:
        words = re.findall(r"[a-z]+", low)
        if words and all(
                w not in _INPUT_DEVICE_FUNCTION_WORDS for w in words):
            return True
    # 规则 5：设备通用专词/说明行。'Unknown Controller' 无品牌词——
    # 通用默认设备配置（Rewired 兜底映射）。'Controller' 单独出现太泛
    # （真实文本含 Controller），须叠加专词/语境词/品牌词才命中。
    has_generic = any(k in low for k in _INPUT_DEVICE_GENERIC_WORDS)
    has_platform = any(k in low for k in _INPUT_DEVICE_PLATFORM_WORDS)
    if has_generic and (has_context or has_platform):
        return True
    if low.startswith("unknown ") and has_context:
        return True
    # 双语境词互证（'Wireless Controller'——wireless+controller 都是
    # 设备语境词，无品牌词）：Rewired 通用设备名。整串判定防真实句
    # 误伤（'The wireless controller needs batteries' 含 the/needs）。
    if (len([k for k in _INPUT_DEVICE_WORDS if k in low]) >= 2
            and re.fullmatch(r"[a-z]+(?: [a-z-]+)+", low)
            and not any(w in low for w in ("the ", " a ", " and ", " is ",
                                           " to ", " of ", " in ", " for "))):
        return True
    # 品牌词与语境词带下划线/横线分隔（'G3_ Android/DI mode'——Rewired
    # 设备说明行模式标记）。'Stadia Controller'/'Wireless Controller' 是
    # 带语境词的通用设备名；'Saitek Pro Flight Yoke'/'Mad Catz Micro
    # C.T.R.L.R' 是品牌词 + 型号专名。全部整串判定，防真实句误伤。
    if has_context and (
            has_brand or has_platform or has_generic
            or re.search(r"[_-]?\b(?:saitek|ch |ch products|mad catz|"
                         r"buffalo|steelseries|stadia|insten|logitech|"
                         r"fanatec|hori|elecom|moga|nexus|fire tv|siri|"
                         r"atari|jaguar|gamecube|nes30|snes30|f30|fc30|"
                         r"n30|sn30|8bitdo|xiaoji|gamesir)\b", low)):
        return True
    # 无语境词/语境词为空的设备专名行（'Saitek Pro Flight Yoke'/
    # 'CH Eclipse Yoke'/'8Bitdo NES30 Pro'/'Logitech G25'）：品牌词 +
    # 型号/功能词专名。整串判定（^…$）防真实句误伤（'The car brake
    # is broken' 含 the/is 不命中）。要求 ≥2 个词——裸品牌单词
    # （'shield'/'sony'/'atari'）是常见英语词/公司名，单出现不足以
    # 判定设备（游戏文本 'equip a shield' 会误伤）。
    if len(low.split()) >= 2 and re.fullmatch(
            r"(?:(?:saitek|ch products|ch|mad catz|buffalo|steelseries|"
            r"stadia|insten|logitech|fanatec|hori|horipad|elecom|8bitdo|"
            r"xiaoji|gamesir|moga|nexus|fire tv|siri|atari|nintendo|sony|"
            r"microsoft|amazon|apple|google|nvidia)\b"
            r"[\s\S]*)"
            r"|[\s\S]*(?:saitek|mad catz|8bitdo|logitech|fanatec|hori|"
            r"horipad|elecom|gamesir|gge[0-9]+)\b[\s\S]*", low, re.I):
        return True
    # 规则 6：硬件元素标签。只对整串判定（'Brake' 全串命中；句中
    # 'apply the brake' 不命中——正则 ^…$ 全串匹配）。孤立元素标签
    # （无品牌/语境词）命中即设备映射键。全大写串（SELECT 按钮文本）
    # 与全小写裸词（'brake'/'shield'/'start' 普通英语词，游戏文本
    # 常见）是显示形态，不是硬件标签——Rewired 映射标签为 TitleCase/
    # mixed（'Left Stick X'/'Brake'）。TitleCase 裸硬件词（'Brake'）
    # 仍是映射键（ffs-legacy obj 实证：孤立 'Brake'/'Clutch' 是 Rewired
    # 元素标签）。普通功能词（'Menu'/'Select'/'Start'——DISPLAY_WORDS
    # 成员或常见按钮文本）不在标签词表内，返回非结构（该翻）：
    # test_raw_string_entries_inputsystem_actions_skipped_in_map_object
    # 契约锚点——它们由 is_input_system_object 对象级信号整体跳过。
    if text.isupper() or text.islower():
        return False
    if _HW_ELEMENT_LABEL.fullmatch(text.strip()):
        return True
    return False# 版本占位/模板串（v?.??：版本号占位符——InControl 固件版本正则截断
# 或版本格式模板，crash-back-in-time level0 实证；? 是占位信号，真实
# 版本号 v2.5 无 ? 不命中）
_INPUT_VERSION_TEMPLATE = re.compile(r"^[vV]?[0-9?]+\.[0-9?]+\?+$|^[vV]?\?[0-9?]*\.[0-9?]+$")
# C# 日志拼接模板尾部（'CustomController device instance GUID: sourceId = '
# ——Rewired 设备实例日志前缀，'=' 是拼接点，无显示价值）
_GUID_LOG_TEMPLATE = re.compile(r"\bGUID:\s*[A-Za-z]+\s*=\s*$")
# C# 日志/错误模板句：整句以「词: 」或「= 」拼接点结尾（'The address is
# not found in the Scene GUID to Address Map. Address: '——crusty-proto
# Eflatun.SceneReference.dll 实证：'Address: ' 是 code 续行拼接点；正常
# 玩家文本以句号/叹号/问号结尾。'Press: ' 短 UI 提示 <20 字符不命中）
# 2026-08-24 come-back 实证：'the mission is simple:' 是真实任务目标句，
# 结尾的 simple 是全小写普通词——拼接点标签必须是代码形态（TitleCase
# 'Address'、驼峰 'sourceId'、含数字）才拦截；全小写普通词冒号结尾是
# 自然叙述（'the mission is simple:'），放行。
_LOG_TEMPLATE_TAIL = re.compile(
    r"(?:[A-Z][A-Za-z0-9]*:|\w*[A-Z][A-Za-z0-9]*:|\w*\d\w*:|\w+\s*=)\s*$")
# 首尾空白片段串（' to JSON. '：字符串表拆分出的无完整语义片段，
# crash-back-in-time 实证——YarnSpinner 错误模板 'Can't save variables
# to JSON.' 的尾部碎片；译文更长时写回被容量截断 → object 闸门 WARN）。
# 含 CJK 排除（中文 padding 串 ' 继续 ' 是真实 UI 文本）
_WHITESPACE_PADDED_FRAGMENT = re.compile(r"^\s+\S[\s\S]*\s+$")
# Rewired 输入动作绑定路径（'Game/Jump[/Keyboard/x,/Keyboard/upArrow]'、
# 'Win Menu/Up[/Keyboard/upArrow,/Keyboard/leftArrow]'——deadbeat 实证：
# MonoBehaviour 里 ActionName/Binding 序列化，运行时按字符串解析输入
# 映射，翻译破坏绑定）。形态：路径/路径 + [/设备/键,...]（[ 后紧跟 /、
# 逗号分隔多绑定）；真实显示文本的 [ 后是内容不是设备路径，不命中
_INPUT_BINDING_PATH = re.compile(
    r"^[A-Za-z0-9_ -]+/[A-Za-z0-9_ -]+"
    r"\[/[A-Za-z0-9_+-]+/[A-Za-z0-9_+-]+"
    r"(?:,/[A-Za-z0-9_+-]+/[A-Za-z0-9_+-]+)*\]$")
# 全大写编码/加密串（'NIIVMMSEGAROTME…' 2567 字符无空格全大写——
# deadbeat 实证：对象内嵌编码数据，翻译请求超模型槽位恒败）。真实
# 全大写英文句有空格不命中、全大写词/缩写长度 <32 不命中；判定还
# 要求 ≥8 种不同字符（'A'×100 重复填充串不命中——既有测试反例）
_UPPERCASE_ENTROPY = re.compile(r"^[A-Z0-9]{32,}$")
# 无完整词碎片（' e   i t'、'r wr TE'——deadbeat 实证：对象内嵌的
# 字母噪声，所有词 ≤2 字符、≥3 段、含首空白或全大写词段；无翻译
# 语义。'Hi hi hi' 类 TitleCase 语气词无全大写段不命中，走正常翻译）
_FRAGMENT_NOISE = re.compile(r"^[\sA-Za-z]{5,12}$")


def _is_fragment_noise(text: str) -> bool:
    """无完整词碎片 → 结构跳过（deadbeat ' e   i t'/'r wr TE' 实证）。"""
    s = text.strip()
    if not s or not _FRAGMENT_NOISE.fullmatch(text) or len(s) < 3:
        return False
    words = [w for w in re.split(r"\s+", s) if w]
    if len(words) < 3 or any(len(w) >= 3 for w in words):
        return False
    return text[0].isspace() or any(
        w and w.isupper() and len(w) >= 2 for w in words)
_HAS_CJK_CHAR = re.compile(r"[㐀-鿿豈-﫿]")
_ASSET_FOLDER = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9 &_.-]{0,71})?"
    r"(?:Assets|Materials|Presets)/$",
    re.I,
)
_DANGLING_FORMAT_SUFFIX = re.compile(
    r"^[^\w\s]+(?:</[A-Za-z][^>\r\n]{0,49}>)+$")
# .NET 日期/时间格式串（HH:mm dd MMMM, yyyy 等）：翻译破坏格式语义
# （a-catfiends Fungus.dll us#49189 实证：模型回显恒败）。token 段间由
# 分隔符连接（: 空格 , / - .），不匹配则为普通文本（'May the 4th' 等）。
_DATETIME_FORMAT = re.compile(
    r"^(?:HH|hh|mm|MM|MMMM|MMM|dd|yyyy|yy|ss|tt|zzz)"
    r"(?:[: ,./\-]+(?:HH|hh|mm|MM|MMMM|MMM|dd|yyyy|yy|ss|tt|zzz))+$")
# C# format 字符串转义大括号：{{ / }} 是 string.Format 的转义写法，
# 常与 {0} 占位符共存于代码常量模板（a-catfiends Unity.ProBuilder.dll
# us#32180 实证：'{0} : {1}\nCPAPI:{{"cmd":"Watch" "name":"{0}"}}'——多行
# 含字母绕过了单行纯符号/纯占位符检测，模型翻译恒败）。显示文本几乎
# 不会含 {{，命中即代码/数据模板。
_ESCAPED_BRACES = re.compile(r"\{\{|\}\}")
# 颜色表条目：HTML/CSS 色名列表（ProBuilder 材质/顶点着色 UI 的数据表，
# 无翻译价值，模型对专有名词回显恒败）。固定标注格式：
# 'Gray (HTML/CSS Gray)'、'Green (HTML/CSS Color)'、'Air Force Blue (USAF)'
_COLOR_TABLE_ENTRY = re.compile(
    r"\(HTML(?:/CSS)?(?: [A-Z][A-Za-z]+)?\)|\(USAF\)")
# 纯富文本标签串：整串都是 {tag} 序列（Fungus/UGUI 样式模板拆分出的
# 标签行，a-catfiends resources.assets obj1292 实证：'{customName}'、
# '{/customName}'、'{color=blue}'、'{audio=AudioTag}'——模型回显合理，
# 但翻译无意义，且写回因无变化被静默过滤造成统计虚高）。对话文本
# 含真实内容（'{punch=3,2}* Y A W N *{w=3}{x}'）不命中锚定模式。
_PURE_TAG_SEQUENCE = re.compile(r"^(?:\{[^}\r\n]*\})+$")
_QUALIFIED = re.compile(r"^[a-zA-Z0-9_]+([.\-][a-zA-Z0-9_]+)+$")
# .NET 程序集全名：Namespace.Type, Version=x.y.z, Culture=neutral, PublicKeyToken=null
# （Addressables catalog m_AssemblyName 真实值，project-arrhythmia 失败样本）
_ASSEMBLY_REF = re.compile(
    r"^[^,]+(?:,[^,]+)*,\s*Version=\d[\w.]*(?:,\s*[A-Za-z]+=[\w.]+)*$")
# 协议相对 URL：//host/path（A* 库版权文件真实值，morfosigame 失败样本）
_PROTOCOL_RELATIVE_URL = re.compile(r"^//[A-Za-z0-9][^\r\n]*$")
# InputAction 绑定路径：前缀多段路径 + 方括号绑定段
# （swallow-the-sea level0 真实值：SwallowControls/MousePosition[/Mouse/position]）。
# 带空格或单段前缀的显示文本（Save[/b]、Credits [More]）不受影响
_BRACKETED_PATH = re.compile(
    r"^[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+(?:\[[^\s\[\]]+\])+$")
# CLI 参数（无空格、- 开头）：--platform=Windows（Burst 命令记录真实值）
_CLI_ARG = re.compile(r"^--?[A-Za-z][^\s]*$")
# base64 序列化数据（Addressables catalog m_BucketDataString 真实值）
_BASE64 = re.compile(r"^[A-Za-z0-9+/]{16,}={0,2}$")
# credit/署名行：- from X / by X 结尾 / created by X 开头 / ©版权行
# （真实失败样本：CREDITS.txt 逐行、level0 的 Created by Sam Hogan）
_CREDIT_ATTRIBUTION = re.compile(
    r"(?i:^created\s+by\s+[A-Z0-9]|"
    r"[-:：]\s*(?:from|by)\s+[A-Z0-9]|"
    r"\sby\s+[A-Z][a-zA-Z0-9'.]*(?:\s+[A-Z][a-zA-Z0-9'.]*){0,3}$|"
    # 本地化署名（Chinese Localization by: gugu subtitle group：语言 +
    # Localization/Translation + by/of + 署名，containment ReadMe 实证
    # ——署名方非大写字头也能匹配，模型把组名当普通词残留恒败）
    r"(?:localization|translation)\s+(?:by|of)\s*:?\s+[A-Za-z]|"
    r"©|(?i:\bcopyright\b)[^\d]{0,40}\d{4})")
# TMP SDF 字体资产名（Signed Distance Field 字体）：X SDF Y / X SDF 形状
# （真实失败样本：ComicsCarToon SDF Zesty、roquetteplain SDF Bonus）
_SDF_FONT = re.compile(r"(?i:\bSDF\b)")
# 语言文件键码（§m_quit ### / §e1_credits_1 ###：§ 前缀菜单/对话键 +
# ' ###' 空值分隔符，butterflies 真实样本 97 条——localization 键值模板
# 的键且值缺失 → 无译义内容，模型回显恒败）
_SECTION_KEY = re.compile(r"^§[a-zA-Z0-9_]+ ###$")
# 语言代码目录标记（EN/ / DE/：双语 TextAsset 的语种分隔行，butterflies 样本）
_LANG_CODE_WITH_SLASH = re.compile(r"^[a-zA-Z]{2}/$")
# 多行键位映射（"k\nm\n/\nh"：键盘快捷键组合提示，每行恰好 1 个字符，
# butterflies 真实样本 4 条）——无译义内容，模型回显恒败
_SINGLE_CHAR_KEYMAP_LINES = re.compile(r"^(?:[^\r\n])(?:\n[^\r\n])+$")
# XXXX 占位名（XXXX t'a：游戏内未命名角色/玩家的占位名，XXXX 是标准
# 名字占位符）→ 保留原文合理
_XXXX_PLACEHOLDER_NAME = re.compile(r"^XXXX(?: [A-Za-z]+(?:'[a-z]+)?)?$")
# credit 名单对齐行：双无空格 token 多空格分隔（kangaroovindaloo    qubodup /
# pcaeldries          RICHERlandTV：制作人名单两列对齐，无译义）
_CREDIT_ALIGNED = re.compile(r"^[A-Za-z0-9]+ {2,}[A-Za-z0-9]+$")
# 音乐合作名单（Highraiser ft. inkoutlines, MC Cruel Addict：ft. =
# featuring 合作标签，游戏音乐/音效署名行）
_FT_CREDIT = re.compile(r"(?i:\bft\.)")
# 人名+引号昵称署名（Sam Lynch ("InnocentSam")：制作人员名单的作者
# 名+昵称，无句子结构；模型保留人名合理，containment sharedassets7
# TextAsset 实证 2 条被判 glossary_mismatch 恒败）→ 署名跳过
_PERSON_WITH_NICKNAME = re.compile(
    r'^[A-Z][A-Za-z\'\-]+(?: [A-Z][A-Za-z\'\-]+)*\s+'
    r'[\(\[]["\'“”「」『』]'
    r'[^"\'“”「」『』]{1,40}'
    r'["\'“”「」『』][\)\]]\s*$')
# 斜杠分隔的作者/团队名单（Turtle Sandwich/Catnipbuddy：无句子虚词的
# TitleCase 名单行，containment credits 实证——制作组名翻译无意义，
# 且模型对名单回显/音译都不稳定）。要求斜杠任一侧 ≥2 词：UI 双选项
# 是单侧单词（Click/Tap、Load/Save、Audio/Video——test_slashes_inside_
# display_text_are_not_paths 固化），2 词名单（Sam Hogan / Kyuppin）
# 无小写词走 proper_name_echo 回显放行，无需 credit 跳过
_SLASH_NAME_LIST = re.compile(
    r"^(?:[A-Z][A-Za-z'.-]*\s+)+[A-Z][A-Za-z'.-]*\s*/\s*"
    r"[A-Z][A-Za-z'.-]*(?:\s+[A-Z][A-Za-z'.-]*)*$"
    r"|^[A-Z][A-Za-z'.-]*\s*/\s*"
    r"(?:[A-Z][A-Za-z'.-]*\s+)+[A-Z][A-Za-z'.-]*$")
# 本地化署名行（Russian   -   Nattakara：语言名 + 连字符 + 译者名，
# containment ReadMe/TextAsset 本地化清单实证——语言名引导模型把
# 译者名音译成该语言字母，目标脚本错误恒败；署名保留原文合理）。
# 语言名词表开头：'Press - Start'（按键 UI 文本）不是署名
_LANG_CREDIT_LINE = re.compile(
    r"^(?:english|russian|chinese|japanese|korean|french|german|"
    r"spanish|portuguese|italian|polish|dutch|swedish|turkish|"
    r"ukrainian|vietnamese|thai|indonesian|norwegian|finnish|danish|"
    r"czech|hungarian|greek|romanian|bulgarian|arabic|hebrew|hindi"
    r"|español|deutsch|français|русский|日本語|中文)\s+-\s+"
    r"[A-Za-z][A-Za-z' .-]*$", re.I)
# JSON 数组字面量残留行（null, / true, / false,：kv 语言文件逐行提取
# 的 JSON 数组空槽，无译义——containment ES/sceneStrings.subs 'null,'
# 实证 19 条）
_JSON_LITERAL_LINE = re.compile(
    r"^(?:null|true|false|nil|none),?\s*$", re.I)
# JSON 数组标识符字符串残留行（"chara_guard",：带引号的标识符 + 逗号，
# 是 JSON 数组元素（角色键）——containment ES/sceneStrings.subs 实证
# 23 条。引号内无空白无转义（对话文本引号内必有空格）→ 不会误伤）
_JSON_IDENTIFIER_STRING_LINE = re.compile(
    r'^"[A-Za-z0-9_][A-Za-z0-9_$./:-]*",?\s*$')
# 星号包裹的全大写标注（*SIGH* / *SIGH* Now...：音效/情绪标注，SFX
# 字幕键位——模型稳定回显小写变体（* sigh *），翻译无意义且小写
# 残留判失败恒败（containment SCP-035 实证 6 条）。星号强调的真实
# 指令（*Attention* 需翻译）是驼峰/TitleCase 或含小写词，不匹配）
_ASTERISK_CAPS_LABEL = re.compile(
    r"^\*[A-Z]{2,}\*(?:\s+[^\r\n]*)?$")
# 普通句子标记：credit 形状的行若含这些虚词仍是可翻译句子。
# 注意不含单字母 a——标题/选项（Option A、A* star）中的 A 不是虚词。
# 句末标点也是句子标记：'dropped by bosses.'（句号结尾）是真实句子，
# 署名行形态无句号（'A game by Kyuppin'）——by 归属分支只拦截无标点行
# （审计 R5 实证：is_credit_like 把普通句子当署名跳过，对象值证据随之丢失）。
_SENTENCE_MARKERS = re.compile(
    r"(?i:\b(?:the|an|of|for|and|with|to|in|on|is|are|was|were|"
    r"it|we|you|your|our|this|that|have|has|had|will|would|can|could|"
    r"should|not|no|be|been)\b|[.!?。！？]$)")


def _is_full_value_path_or_binding(text: str) -> bool:
    """Match a complete path/binding after protecting embedded rich tags."""
    if _INPUT_SYSTEM_BINDING.fullmatch(text):
        return True
    without_rich_tags = FORMAT_TAG_PATTERN.sub("", text).strip()
    return bool(
        _URL.fullmatch(without_rich_tags)
        or _WINDOWS_PATH.fullmatch(without_rich_tags)
        or _UNIX_PATH.fullmatch(without_rich_tags)
        or _EXPLICIT_RELATIVE_PATH.fullmatch(without_rich_tags)
        or _BACKSLASH_PATH.fullmatch(without_rich_tags)
        or _THREE_SEGMENT_PATH.fullmatch(without_rich_tags)
        or _EXTENSION_PATH.fullmatch(without_rich_tags)
        or _UNITY_ROOTED_PATH.fullmatch(without_rich_tags)
    )

# ── 键名识别（Localization 表键/字典键/标识符，绝不能翻译） ──────────────────
# 无空格的标识符形态（3–64 字符）：LOCALIZATION 表键（ui_newGame、MENU_PLAY）、
# 对象名（UITable_en）、程序标识符（FlashlightData）、语言代码（en/ru/zh）等。
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{2,63}$")
_LOCALE_CODE = re.compile(r"^[a-z]{2}$")   # en/ru/zh/ja… 语言代码
# 单词式写法（TitleCase / ALL-CAPS，可含单连字符）：CREDITOS、Settings、V-SYNC
# 是显示文本（任意语言的 UI 标签），不是键——键采用 snake/camel/下划线等编程命名。
_WORD_CASE = re.compile(r"^[A-Z]+(?:-[A-Z]+)*$|^[A-Z][a-z]+(?:-[A-Z][a-z]+)*$")
# B26（dead-catch 实证）：单个单词 + 句尾点号 = 自然语言短句（对话行
# 'Listen.'/'Alright.'/'Good.'/'listen.'）。_IDENTIFIER 把句尾点当标识符
# 字符导致整词被误判键风格；键名/标识符不会以句号结尾。多段点名
# （Assets.Scripts.Foo）中间带点不匹配此形态，仍按键处理。
_SENTENCE_WORD = re.compile(r"^[A-Za-z][a-z]{1,15}\.$")

# 显示单词白名单：标识符形态但确实是游戏显示文本（UI 标签/短对话）。
# 仅这些无空格单词允许翻译；其余标识符一律视为键名跳过。
# 注意：白名单单词若作为键使用（SharedData 键列表），由对象级键列表规则覆盖（见 unity/extractor）。
DISPLAY_WORDS = {
    # 确认/导航
    "ok", "yes", "no", "on", "off", "go", "hi", "hey", "hello", "bye",
    "goodbye", "thanks", "thank", "sorry", "welcome", "wait", "back", "next",
    "prev", "enter", "exit", "leave", "return", "cancel", "confirm", "accept",
    # 交互/按钮高频词（'Press Start'/'Click to continue'/'Tap to play'——
    # 与 InputAction 绑定名同形，由 is_input_system_object 对象信号先拦；
    # R2 实证 'PressButton'+'Press' 按钮文本需要白名单放行）
    "press", "click", "tap",
    "apply", "close", "open", "skip", "retry", "continue", "start", "stop",
    "pause", "resume", "restart", "reset", "default", "backtomenu",
    # 菜单/UI
    "menu", "mainmenu", "newgame", "loadgame", "savegame", "settings", "options",
    "language", "volume", "audio", "video", "graphics", "quality", "screen",
    "window", "fullscreen", "sound", "music", "brightness", "sensitivity",
    "controls", "keyboard", "mouse", "gamepad", "controller", "resolution",
    "vsync", "v-sync", "credits", "help", "instructions", "pause", "paused",
    "loading", "waiting",
    "ready", "locked", "unlocked", "failed", "success", "victory", "defeat",
    "gameover", "difficulty", "easy", "normal", "hard", "nightmare", "beginner",
    "expert", "custom", "high", "medium", "low", "max", "min", "auto", "manual",
    # 通用动作/名词
    "new", "play", "save", "load", "quit", "use", "talk", "buy", "sell", "shop",
    "map", "quest", "item", "inventory", "attack", "defend", "heal", "flee",
    "run", "walk", "jump", "read", "look", "take", "give", "drop", "hold",
    "left", "right", "up", "down", "win", "lose", "dead", "hide", "show",
    "toggle", "enable", "disable", "delete", "warning", "danger", "help",
    "score", "level", "wave", "round", "time", "health", "mana", "stamina",
    "energy", "ammo", "money", "gold", "coins", "online", "offline",
    "singleplayer", "multiplayer", "coop", "pvp", "chat", "friend", "party",
    "guild", "lobby", "2d", "3d",
    # F50（hickory/dcdb50a165/a61ae49375 实证 2026-09-05）：UI 碎片词——
    # '2F'（楼层，m_text/roomName 双证）、'x2'/'2x'（倍数标记，m_Text 4 游戏实证）、
    # '2H'（时长标签 12 连）、'1P'/'2P'（人数）。F49 单字母规则把它们当
    # 「无成词内容」拦截，但它们是玩家可读的 UI 短语。写回安全层独立于
    # 识别（immutable/回退不受影响），下游仍有 is_key_style_identifier +
    # actionable 终检 + AI 候选层兜底，白名单放行不破宁漏勿坏。
    "1f", "2f", "3f", "4f", "5f", "6f", "7f", "8f", "9f",
    "0x", "1x", "2x", "3x", "4x", "5x", "6x", "7x", "8x", "9x",
    "x1", "x2", "x3", "x4", "x5",
    "1h", "2h", "3h", "4h", "6h", "8h", "12h",
    "1p", "2p", "3p", "4p",
}

# JSON 字段名视为键字段（值不翻译）：Key/ID/GUID/Hash/Ref/语言代码等
_KEY_FIELD_NAMES = {
    "key", "id", "guid", "gid", "hash", "ref", "refid", "m_key", "keyid",
    "key_id", "keyname", "idname", "locale", "lang", "language", "culture",
    "region", "country", "tag", "type", "category", "class", "kind", "section",
    "group", "index", "order", "flag", "state", "mode", "status",
    # 渲染/样式引用字段（containment subtitles.jsonc 实证 333 条）：
    # "color": "classd" / "sound": "door_open" 是枚举/资源名，翻译（译成
    # 中文）断字幕着色/音效查找。key 风格判定（_IDENTIFIER 无空格）会
    # 把这些短枚举当「显示单词」放行进池，模型再译成中文 → 引用断链。
    "color", "colour", "sound", "sfx", "music", "song", "material",
    "sprite", "icon", "image", "prefab", "scene", "layer", "animation",
    "anim", "style", "shader", "font", "texture", "camera", "light",
    "audio", "clip", "model", "mesh", "effect", "particle", "controller",
    # Addressables catalog 结构字段（Unity 序列化名）：值分别是资源地址/程序集名/
    # 加载器类型，翻译必然破坏资源加载（catalog.json 真实失败样本 21 条）
    "m_address", "m_assetpath", "m_internalid", "m_providerid",
    "m_assemblyname", "m_objecttype", "m_typename", "m_type",
    "m_sceneproviderdata", "m_instanceproviderdata",
    "m_internalids", "m_bucketdatastring", "m_entrydatastring",
    "m_keydatastring", "m_extradatastring",  # base64 键/额外数据（interdream request_error 样本）
}


def is_key_style_identifier(text: str) -> bool:
    """键风格标识符 → 永不翻译。

    判定：标识符形态且「不是单词式写法」且「不是显示单词」。
    - 键：ui_newGame、MENU_PLAY、phone_call_01、UITable_en、en/ru 语言代码
    - 显示值（允许翻译）：CREDITOS / Settings / V-SYNC（单词式写法，任意语言）、
      start / menu（显示单词白名单）
    """
    s = text.strip()
    if _LOCALE_CODE.match(s):
        return True                       # en/ru/zh… 语言代码
    # F41（bottle-cracks 实证）：省略号结尾 = 进行中状态/输入占位文本
    # （'Leaving...'/'Connecting...'/'Username...'）——'...' 不是标识符
    # 点号，是省略号形态（_IDENTIFIER 把点当标识符字符导致误匹配）
    if s.endswith("..."):
        return False
    # B26（dead-catch 实证）：单词 + 句尾点号（'Listen.'/'Alright.'/'Good.'）
    # 是自然语言短句——对话行 dialogueLines[N].text 的常见形态。键名
    # 不会以句号结尾，多段限定名（Assets.Scripts.Foo）不匹配此形态。
    if _SENTENCE_WORD.match(s):
        return False
    if not _IDENTIFIER.match(s):
        return False
    if _WORD_CASE.match(s):
        return False                      # CREDITOS / Settings / V-SYNC → 显示文本
    if s.islower() and s.isalpha():
        # 纯小写纯字母单词（shower/city/bedroom/eggs）→ 显示文本。键名
        # 几乎总带分隔符（ui_newGame/MENU_PLAY/phone_call_01）或混合
        # 大小写（lockedEntrance）；无分隔符纯小写是自然语言单词形态。
        # DISPLAY_WORDS 白名单覆盖不了全部常见场景词（222am 实证：
        # shower/city/bedroom/eggs/ladder/mug 20 条音效/场景标签被当键
        # 跳过）——形态规则治本，白名单治标
        return False
    if s.lower() in DISPLAY_WORDS:
        return False                      # start / menu / ok → 显示文本
    return True                           # ui_newGame / MENU_PLAY → 键


def is_code_identifier(text: str) -> bool:
    """代码字符串池（DLL #US / IL2CPP metadata / 配置资源）标识符 → 键，永不翻译。

    代码池中的无空格 ASCII 标识符（Bold / WASD / Move / Fire / Unity / Enum）
    是枚举名、Input 绑定名、引擎名、UI 控件名——游戏代码按原名查找，
    翻译必然破坏功能。单词式写法也**不**放行（与 is_key_style_identifier 相反：
    Bundle 表值可能是显示文本，代码池字面量几乎都是标识符）。
    """
    s = text.strip()
    if _LOCALE_CODE.match(s):
        return True                       # en/ru/zh… 语言代码
    return bool(_IDENTIFIER.match(s))


def looks_like_key_field(field_name: str) -> bool:
    """JSON 字段名是否为键字段（其值不应翻译）。"""
    n = field_name.strip().lower()
    if n in _KEY_FIELD_NAMES:
        return True
    return n.startswith("key_") or n.endswith(("_key", "_id", "_guid", "_hash", "_ref"))


def extract_placeholders(text: str) -> list[str]:
    """按出现顺序返回文本中的全部占位符。"""
    found: list[tuple[int, int, int, str]] = []
    for pattern_index, (_, pat) in enumerate(PLACEHOLDER_PATTERNS):
        for m in pat.finditer(text):
            found.append((m.start(), m.end(), pattern_index, m.group(0)))
    found.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in found]


_LITERAL_NEWLINE = re.compile(r"\\n")
_OPENING_TAG = re.compile(
    r"<[A-Za-z][^>\r\n]{0,49}>|"
    r"\[(?:b|i|u|s|color|size|font|url|sprite)(?:=[^\]\r\n]+)?\]",
    re.I,
)
_CLOSING_TAG = re.compile(
    r"</[A-Za-z][^>\r\n]{0,49}>|\[/(?:b|i|u|s|color|size|font|url|sprite)\]",
    re.I,
)


def _placeholder_spans(text: str) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, int, str]] = []
    for pattern_index, (_, pat) in enumerate(PLACEHOLDER_PATTERNS):
        for m in pat.finditer(text):
            found.append((m.start(), m.end(), pattern_index, m.group(0)))
    found.sort(key=lambda item: (item[0], item[1], item[2]))
    return [(start, end, text) for start, end, _idx, text in found]


def self_heal_format_tags(original: str, translation: str) -> str:
    """确定性修复译文的占位符缺口与闭合标签乱序（无模型调用）。

    仅两类修改，不引入原文没有的标记：
    1. **缺口补全**：译文占位符序列是原文序列的子序列（无 extra）→ 缺失
       占位符按原文顺序插回原位置（a-catfiends 丢 {w=0.5}、interdream 丢
       </color>——模型漏写标记是稳定行为，语义译文本身正确）。
    2. **闭合重排**：译文占位符 multiset 与原文相等但顺序不同 → 开标签
       顺序一致时，把闭合标签序列重排为原文顺序（the-keeper 的
       </b></color> 逆序——内容正确只是闭合顺序颠倒）。

    模型新增占位符（extra）或顺序彻底破坏时原样返回（仍由判定失败暴露）。
    """
    src_spans = _placeholder_spans(original)
    dst_spans = _placeholder_spans(translation)
    src_texts = [s[2] for s in src_spans]
    dst_texts = [d[2] for d in dst_spans]
    if src_texts == dst_texts:
        return translation
    if (Counter(src_texts) == Counter(dst_texts)
            and _OPENING_TAG.findall(original) == _OPENING_TAG.findall(translation)
            and _CLOSING_TAG.findall(original) != _CLOSING_TAG.findall(translation)):
        # 闭合标签 multiset 相同且开标签顺序一致 → 重排闭合标签为原文顺序
        closing_iter = iter(_CLOSING_TAG.findall(original))
        return _CLOSING_TAG.sub(lambda match: next(closing_iter), translation)
    # 缺口补全：贪心子序列匹配（译文占位符 = 原文子序列，无 extra）
    missing_idx: list[int] = []
    dst_to_src: dict[int, int] = {}
    i = 0
    for di, dtext in enumerate(dst_texts):
        while i < len(src_texts) and src_texts[i] != dtext:
            missing_idx.append(i)
            i += 1
        if i >= len(src_texts):
            return translation
        dst_to_src[di] = i
        i += 1
    while i < len(src_texts):
        missing_idx.append(i)
        i += 1
    if not missing_idx:
        return translation
    # 字面 \n 缺口不补：换行结构缺失必须由 multiline repair 重建分隔符，
    # 自愈插入会改变行拓扑（补到相邻占位符前 → 空行压缩豁免误放行）
    missing_idx = [i for i in missing_idx
                   if not _LITERAL_NEWLINE.fullmatch(src_texts[i])]
    if not missing_idx:
        return translation
    if missing_idx and len(missing_idx) >= len(dst_texts):
        # 译文保留的占位符 ≤ 缺失量 → 通常结构锚点不足（模型全丢标签时
        # 补全会把标记堆到末尾、位置全错）→ 交 protected/multiline
        # repair 重建。例外：缺失是 src 尾部连续缺口且末尾缺失占位符
        # 本身位于原文末尾（句末的 {w=3}{x}/</color>）→ append 恢复的
        # 是原文原位置，可确定性补全——1.8B 稳定漏写句末标签
        # （a-catfiends 丢 '{w=3}{x}' 留 '{punch=3,2}' 实证：丢 2 留 1
        # 被原限制拒绝，好译文被弃、placeholder_mismatch 恒败）。
        # 'Press <color=red>E</color> to continue' 的 </color> 后还有
        # 文本 → 非原文末尾 → 仍拒绝（append 会拉长样式范围，须重试）。
        if not dst_texts:
            return translation
        last_missing = missing_idx[-1]
        tail_contiguous = (
            last_missing == len(src_texts) - 1
            and all(missing_idx[k] == missing_idx[k - 1] + 1
                    for k in range(1, len(missing_idx)))
            and src_spans[last_missing][1] == len(original))
        if not tail_contiguous:
            return translation
    # 每个缺失占位符插到「其后第一个已匹配译文占位符」之前（末尾缺口 → append）
    by_pos: dict[int, list[str]] = {}
    for mi in missing_idx:
        nxt = next((di for di, si in dst_to_src.items() if si > mi), None)
        pos = dst_spans[nxt][0] if nxt is not None else len(translation)
        by_pos.setdefault(pos, []).append(src_texts[mi])
    for pos in sorted(by_pos, reverse=True):
        translation = (translation[:pos] + "".join(by_pos[pos])
                       + translation[pos:])
    return translation


def validate_translation(original: str, translation: str) -> tuple[bool, list[str], list[str]]:
    """译文必须保留占位符次数与顺序。返回 (是否通过, 缺失, 多余)。"""
    src = extract_placeholders(original)
    dst = extract_placeholders(translation)
    missing_counts = Counter(src) - Counter(dst)
    extra_counts = Counter(dst) - Counter(src)
    missing = []
    extra = []
    for placeholder in src:
        if missing_counts[placeholder] > 0:
            missing.append(placeholder)
            missing_counts[placeholder] -= 1
    for placeholder in dst:
        if extra_counts[placeholder] > 0:
            extra.append(placeholder)
            extra_counts[placeholder] -= 1
    # F12（2026-08-13 incremental-rts 实证 row207）：模型把 {health}
    # 本地化成中文变量名（{伤害}/{速度惩罚}）并在尾部重复原文占位符
    # 堆叠（{health}{damage}...）——brace 模式只匹配 ASCII，中文
    # 花括号段逃过提取 → Counter 恰好相等。译文所有 {..} 花括号段
    # 必须都是原文占位符（变量名是代码标识符，本地化破坏运行时替换，
    # 变量名不属于可翻译语义文本——与 F10b/F11 同族：非语义段）；
    # 孤立花括号（'健康} HP' 的 '}'——{health} 被拆碎）是占位符
    # 破碎信号。
    brace_dst = re.findall(r"\{[^{}\r\n]*\}", translation)
    brace_src = set(re.findall(r"\{[^{}\r\n]*\}", original))
    # 只补非 ASCII 段：ASCII 花括号段已被 brace 模式提取进 extra_counts
    # （'拿起{item}物品' 的 {item}——避免重复计）。中文段（{伤害}）
    # brace 模式漏提——变量名本地化破坏运行时替换。
    cjk_brace_extra = [
        b for b in brace_dst
        if b not in brace_src and not b.isascii()]
    if cjk_brace_extra:
        extra += cjk_brace_extra
    if src and translation.count("{") != translation.count("}"):
        missing.append("<lone-brace>")  # 占位符破碎（健康} HP）
    return (src == dst and not cjk_brace_extra and not (
        src and translation.count("{") != translation.count("}"))), missing, extra


def is_credit_like(text: str) -> bool:
    """署名/版权反模式（软猜测规则）：制作者署名/版权行。

    'A game by Kyuppin' / 'made in 48h' / 'Created by Sam Hogan' /
    '© 2021 Some Studio' 等。用于 is_hard_structural 的署名分支；但
    **确定性显示证据**（typetree m_Text 等 UI 字段）中的署名是真实
    显示文本（lilys-day-off level13 结局画廊实证：'A game by Kyuppin'
    被此规则降级跳过）——extractor 降级闸门据此做证据分层：确定性
    显示条目不被此软猜测降级，只被硬结构规则降级。
    """
    s = text.strip()
    if not s or len(s) > 90:
        return False
    if re.match(r"(?i:^created\s+by\s+[A-Z0-9])", s):
        return True              # created by X（短行，对话不会以此开头）
    if (_CREDIT_ATTRIBUTION.search(s) or _CREDIT_ALIGNED.match(s)
            or _FT_CREDIT.search(s)) and not _SENTENCE_MARKERS.search(s):
        return True              # credit/署名/版权行（无句子虚词）
    if _CREDIT_YEAR_LINE.match(s) and not _SENTENCE_MARKERS.search(s):
        return True              # 人名 + 年份署名行（Darien Gore (Fleebs) 2019）
    if (_SLASH_NAME_LIST.match(s) or _LANG_CREDIT_LINE.match(s)) \
            and not _SENTENCE_MARKERS.search(s):
        return True              # 作者/团队名单（Turtle Sandwich/Catnipbuddy）/
                                 # 本地化署名行（Russian - Nattakara）
    if _PERSON_WITH_NICKNAME.match(s):
        # 剥掉括号昵称（昵称内容可含 The/of 等虚词，Tom ('The Cat')）后
        # 主体只剩纯名字——再查句子虚词防对话行（He said ("What?")）
        if not _SENTENCE_MARKERS.search(_PERSON_WITH_NICKNAME.sub("", s)):
            return True          # 人名+昵称署名（Sam Lynch ("InnocentSam")）
    return bool(_JAM_CREDIT.match(s))   # 游戏 jam 署名（made in 48h）


def is_hard_structural(text: str) -> bool:
    """Return whether *text* is structural regardless of display provenance."""
    s = text.strip()
    if not s or len(s) < 2:
        return True
    # F41（bottle-cracks 实证）：显示词白名单豁免——'v-sync'/'fullscreen'
    # 等设置项文本命中 URL/版本号等结构形态（v-sync 是 UI 设置标签，
    # 该翻「垂直同步」），白名单是显式显示词证据，优先于结构形态猜测
    # （与 a-catfiends 白名单优先于资源猜测的证据分层同语义）
    if s.casefold() in DISPLAY_WORDS:
        return False
    # F49（fromivan 实证 2026-09-01）：孤立单字母碎片——'n۶?'（1 个 ASCII
    # 字母 + 阿拉伯-印度数字 U+06F6 + '?'）是二进制/专利残留串，被
    # display_evidence_tier 当句子放行（U+06F6 是 \w，'_DISPLAY_WORD'
    # 的 [A-Za-z…]{2,} 把 'n'+U+06F6 凑成词）误判 pending 进池翻译。
    # 'F1'/'A1'/'x7' 等「单字母 + 数字/符号」串同样无成词内容。真实显示
    # 文本必有 ≥2 字母（Mr./OK）或东亚整字（'你' 单字即词）；东亚文字
    # （CJK/假名/谚文）单字即整词，不误伤。数字（含阿拉伯-印度数字）
    # isalpha()=False 不计字母。
    if not _HAS_EAST_ASIAN.search(s) and (
            sum(1 for c in s if c.isalpha()) == 1):
        return True
    if _NET_ASSEMBLY_QUALIFIED_TYPE.match(s):
        return True                  # F53：.NET 程序集限定类型名（反射键）
    if s.isdigit():
        return True
    if s.startswith(("{", "[")):
        # JSON 序列化字符串（引擎把结构化数据序列化后存成字符串）。
        # 能解析成 JSON 就是数据而非人读文本；翻译会破坏 JSON 语法致游戏崩溃。
        try:
            json.loads(s)
            return True
        except Exception:  # noqa: BLE001 - 非 JSON（对话以 {/[ 开头很常见）
            pass
    if _DEV_TEMPLATE_PLACEHOLDER.match(s):
        # 开发者模板占位（"beast description here" / "Option description here!!!"）：
        # 内容未填写的占位字符串，翻译无意义（真实语料漏检样本）
        return True
    if _DEFAULT_NAME_PLACEHOLDER.match(s):
        # 引擎/编辑器默认名占位（'Information text'/'Character Name'/'New Sprite'）：
        # 字段默认值不是显示文本（Fungus 组件默认名实证）
        return True
    if _URL.match(s) or _ONLY_SYMBOL.match(s) or _HTML_OR_BB.match(s):
        return True
    if _DANGLING_FORMAT_SUFFIX.fullmatch(s):
        return True
    if _DATETIME_FORMAT.match(s):
        return True                  # .NET 日期/时间格式串（HH:mm dd MMMM, yyyy）
    if _ESCAPED_BRACES.search(s):
        return True                  # C# format 转义 {{/}} → 代码/数据模板
    if _COLOR_TABLE_ENTRY.search(s):
        return True                  # 颜色表条目（Gray (HTML/CSS Gray) 等）
    if _PURE_TAG_SEQUENCE.fullmatch(s):
        return True                  # 纯 {tag} 序列（Fungus 样式模板标签行）
    if (_INPUT_ACTION_IDENTIFIER.fullmatch(s)
            or _ASSET_FOLDER.fullmatch(s)
            or _ASSEMBLY_REF.fullmatch(s)
            or _PROTOCOL_RELATIVE_URL.fullmatch(s)
            or _BRACKETED_PATH.fullmatch(s)
            or _CLI_ARG.fullmatch(s)):
        return True
    if (_INPUT_DEVICE_REGEX.match(s)
            or _is_input_device_name(s)
            or _INPUT_VERSION_TEMPLATE.match(s)
            or _GUID_LOG_TEMPLATE.search(s)):
        return True                  # 输入插件设备正则/设备名/版本占位/GUID 日志模板
    if s.casefold() in _INPUT_API_NAMES:
        return True                  # 输入系统 API/组件名（xinput 等，ffs 实证）
    if _REGEX_PATTERN.search(s):
        return True                  # 正则表达式串（字符类+量词形态，ffs 实证）
    if _INPUT_BINDING_PATH.match(s):
        return True                  # Rewired 输入动作绑定路径（翻译破坏绑定解析）
    if _UPPERCASE_ENTROPY.match(s) and len(set(s)) >= 8:
        return True                  # 全大写编码/加密串（超模型槽位恒败）
    if _is_fragment_noise(text):
        return True                  # 无完整词字母碎片（对象内嵌噪声）
    if len(s) >= 20 and _LOG_TEMPLATE_TAIL.search(s):
        return True                  # C# 日志拼接模板句（'Address: ' 尾部拼接点）
    if (text != s and _WHITESPACE_PADDED_FRAGMENT.match(text)
            and not _HAS_CJK_CHAR.search(text)
            and len(text) <= 48):
        return True                  # 首尾空白片段串（字符串表拆分碎片）
    # 代码注释行（// 前缀）：C#/JS 风格注释不是游戏文本（baldis 实证：
    # resources.assets TextAsset 脚本里 '//        word:replacement:
    # notCaseSensitive' 注释行被模型当文本翻译成乱语）。要求 // 后跟
    # 空白（//host/path 协议相对 URL、//server/share UNC 路径无空白，
    # 已由 _PROTOCOL_RELATIVE_URL/URL 分支处理，不重复拦截）。
    if s.startswith("//") and (len(s) == 2 or s[2].isspace()):
        return True
    # 混合符号 token：无空格、含强代码符号（%#&^$@!|\~）与字母、长度 ≥8
    # 的串多为随机会话 token/加密串/编码数据（baldis 实证：
    # 'xChDC-Gs%OmaMl+g' 模型回显恒败）。'%' 等强符号在正常英文句中
    # 极少独立成串（'100% sure' 有空格不匹配），base64 已单列判定。
    # 先剥 rich text 标签：<color=#fff> 的 # 颜色码不是 token 符号
    if (len(s) >= 8 and _MIXED_SYMBOL_TOKEN.match(
            _STRIP_RICH_TEXT.sub("", s))
            and not _URL.match(s)):
        return True
    # 剥掉富文本标签（<color=...>）后只有数字与符号 → 纯装饰/字符画（▓ 颜色条）
    if not _HAS_LETTER.search(_STRIP_RICH_TEXT.sub("", s)):
        return True
    if _CLONE_SUFFIX.match(s):
        return True                  # Unity 实例化对象名 frameVertical(Clone)
    if _CLONE_NUMBERED.match(s):
        return True                  # 资源副本实例名 CreditsVolume (1) Profile
    if _DOT_EXTENSION.match(s):
        return True                  # 点开头扩展名 .spriteatlas
    if _GUID_IDENTIFIER.match(s):
        return True                  # GUID:xxxxxxxx 资源标识符
    if _LINE_HASH_IDENTIFIER.match(s):
        return True                  # YarnSpinner 字符串表键（line:hash）
    if _C_SHARP_INTERPOLATION.search(s):
        return True                  # C# 插值残留（{nameof(} 日志模板）
    if _DEBUG_PREFIX_LINE.match(s):
        return True                  # 调试 HUD 输出行（(Debug): 前缀）
    if _UPPERCASE_EDGE_LABEL.match(s):
        return True                  # YarnSpinner 节点边标签（ACTION edge）
    if _MASTER_AUDIO_BUS.match(s):
        return True                  # Master Audio 总线行（插件内部音频路径）
    if _I2_PLURAL_BLOCK.search(s):
        return True                  # I2 复数模板（{0:p:mine|mines} 运行时展开）
    if _IL2CPP_MODULE_DEBUG.match(s):
        return True                  # IL2CPP 模块调试行（module.renderOrderPriority:）
    if _REPEATED_PLACEHOLDER_LINE.match(s):
        return True                  # 开发者重复占位行（Hello×4）
    if _UNITY_SYMBOL.match(s) or _PDB_ALT_PATH.match(s):
        return True                  # Unity 内部符号/PDB 调试路径
    if _VERSION_BANNER.match(s):
        return True                  # 版本号横幅（VERSION 0.4.3 保留原文）
    zalgo = _COMBINING_MARKS.findall(s)
    if zalgo and len(zalgo) >= len(_HAS_LETTER.findall(s)):
        return True                  # zalgo 乱码（组合字符 ≥ 字母数）
    if _LOREM_IPSUM.match(s) or is_hipster_ipsum(s):
        return True                  # lorem/hipster 占位文本（模型不翻占位符是正常行为）
    if _SHELL_COMMAND.match(s):
        return True                  # Shell 命令（find/tar/rm…不是游戏文本）
    if _KEYBOARD_NOISE.match(s):
        # F41（bottle-cracks 实证）：含白名单词的短语（'show fps'——
        # show 是显示词）是设置项文本，不是键盘噪声（asdasdasd /
        # fdji ijsdijn 无白名单词）
        if any(w.casefold() in DISPLAY_WORDS for w in s.split()):
            pass
        # 2026-08-24（Ice Age Baby Adventure 实证）：噪声判别只看「长词
        # + 重复 3-gram」，误杀真实英文句——'he was a good frog and was
        # good at protecting the crünch'（was/a/and/at/the）与 'if you
        # want you can have a ride in my spaceship'（if/can/have/a/in）
        # 是开发者自嘲对话，却被当键盘乱打跳过（92 条跳过里 8 条）。真实
        # 句子的功能词（was/a/and/the/if/can…）是语法骨架，键盘乱打
        # （asdasdasd / fdji ijsdijn）一个功能词都没有。≥2 个功能词 →
        # 真实句子，不是噪声（宁漏勿坏：漏判只多翻一条，误判会漏整条对话）。
        elif sum(1 for w in s.split() if w.casefold() in FUNCTION_WORDS) >= 2:
            pass
        # hickory 实证：对话拟声词（tck/shh/psst…）是内容词不是噪声
        elif any(w in _INTERJECTION_WORDS for w in
                 re.findall(r"[a-z]+", s.casefold())):
            pass
        else:
            return True
    # 引擎富文本控制码串（faerie-afterlight 实证：'.^.b'×178、'^tr'、
    # '^denvis'——'^' 前缀字母段是引擎样式/命令标记（GameMaker/类
    # RichText 控制码），剥除后无可译英文词 → 结构跳过。要求剥除后
    # 无 ≥3 字母连续段（'x^2 + y^2' 剥后 x/y 单字母 → 不误伤）。
    if (_ENGINE_CTRL_CODE.search(s)
            and not _ENGLISH_WORD_MIN3.search(
                _ENGINE_CTRL_CODE.sub("", s))):
        return True
    if _STAR_PREFIXED_WORD.match(s):
        return True                  # 星号前缀词表条目（*shit：脚本示例词）
    if _SECTION_KEY.match(s):
        return True                  # § 键码（§m_quit ###：语言文件键值模板键）
    if _JSON_LITERAL_LINE.match(s) or _JSON_IDENTIFIER_STRING_LINE.match(s):
        return True                  # JSON 数组残留行（null, / "chara_guard",）
    if _ASTERISK_CAPS_LABEL.match(s):
        return True                  # 星号包裹全大写标注（*SIGH*：音效标注）
    if _LANG_CODE_WITH_SLASH.match(s):
        return True                  # 语言代码目录标记（EN/ / DE/）
    if _SINGLE_CHAR_KEYMAP_LINES.match(s):
        return True                  # 多行键位映射（k\nm\n/\nh 快捷键提示）
    if _XXXX_PLACEHOLDER_NAME.match(s):
        return True                  # XXXX 占位名（XXXX t'a：未命名角色名）
    if _BASE64.fullmatch(s) and any(char.isdigit() for char in s):
        return True                  # base64 序列化数据（catalog m_BucketDataString）
    if s.startswith("UEsDB") and _BASE64.fullmatch(s):
        return True                  # base64 编码的 ZIP 包（TextAsset 序列化数据，
                                     # PK\x03\x04 魔数；Morfosi level5 str/0 实证——
                                     # 此前 '=' 填充符不在 _BASE64 字符集，fullmatch
                                     # 失败漏网，模型整段回显恒败）
    if len(s) <= 48 and _SDF_FONT.search(s):
        return True                  # TMP SDF 字体资产名（对话不会含 SDF 词）
    if _MD_BOLD_LEAD.match(s):
        return True                  # markdown 加粗段落行（\t** 无闭合）
    if is_credit_like(s):
        return True                  # 署名/版权行（软猜测，见 is_credit_like）
    path_text = s
    if is_interaction_prompt(s):
        for event in interaction_input_events(s):
            if event.kind == "semantic_input":
                path_text = path_text.replace(event.value, "", 1)
    if (_is_full_value_path_or_binding(path_text)
            or _QUALIFIED.match(s)):   # 路径/程序集名/版本号等标识符
        return True
    return False


def should_skip(text: str) -> bool:
    """无需翻译的文本：hard structural 值或无 display provenance 的键风格值。"""
    if is_hard_structural(text):
        return True
    s = text.strip()
    if is_key_style_identifier(s):     # 键风格标识符（ui_newGame / MENU_PLAY / en）
        return True
    return False


# ── 视觉小说/对话脚本命令行（通用规则，Yarn/Naninovel/Ink 源脚本） ──
# .yarn 源文件的 <<if>>/<<set>>/<<jump>> 命令块、=== 节点头、.ink 的
# -> 跳转行、Naninovel 的 @command 行——按名引用/控制流结构，翻译破坏
# 运行时解析。带引号对话内容的命令行（@speak Actor: "text"）不拦截
# （内容是可译文本，由调用方按行提取后模型保留引号结构）。
_VN_COMMAND_LINE = re.compile(
    r"^(?:"
    r"<<.*>>"                       # Yarn 命令块 <<if $var>> / <<jump Node>>
    r"|===.*==="                     # Ink 节点头 === knot ===
    r"|@[A-Za-z_][A-Za-z0-9_]*\s*$"  # Naninovel 无参数命令 @stop / @goto
    r"|@[A-Za-z_][A-Za-z0-9_]*\s+[A-Za-z_][\w.]*\s*$"  # @goto Label 无引号参数
    r")$")


def is_vn_command_line(text: str) -> bool:
    """Yarn/Naninovel/Ink 源脚本的纯命令行（无对话内容）。

    Yarn 选择行 `-> Go left` 不拦截——显示给玩家的选项文本本身可译
    （翻译保留 -> 前缀）。带引号的对话命令行不拦截。
    """
    s = text.strip()
    if not s or '"' in s:
        return False
    return bool(_VN_COMMAND_LINE.match(s))
