"""引擎字符串过滤（公共层）：v1 文本提取与 v2 二进制提取共用。

Unity 运行时/模板内容有确定性特征：着色器属性、Input System 绑定、URP 后处理、
TMP 演示文本、字体名、emoji 名等。这些永远不是游戏显示文本。
"""
from __future__ import annotations
from dataclasses import dataclass
import re
from typing import Literal

CORE_MENU_SOURCE_TERMS = frozenset({
    "settings", "quit", "resolution", "sfx", "volume", "resume", "controls",
})

_ENGINE_PROP = re.compile(r"^_[A-Za-z]|^m_[A-Za-z]")
_ENGINE_NAME = re.compile(
    r"UnityEngine|UnityEditor|Unity\.RenderPipelines|Unity\.Addressables|"
    r"Unity\.Services|ShaderGraph|TextMeshPro/|DebugUI|Texture2D_|\.dll$", re.I)

# ── 引擎串三层架构（审计 R2）─────────────────────────────────────────
# 1) ENGINE_STRINGS（确定性层）：无上下文歧义的引擎串——组合词/专名/代码
#    形态，游戏显示文本里几乎不可能完整出现（'face with tears of joy'、
#    'monologuetable'、'arial'）。全局无条件跳过。
# 2) ENGINE_GATED_STRINGS（门控层）：高歧义普通英语单词——输入词
#    （tap/click/press/mouse——'Press' 按钮文本同形）、emoji 字符名
#    （imp/skull/sleeping——'Skull' 物品名/'Imp' 敌人名/'Sleeping' 状态名
#    同形）、后处理效果单词（bloom/vignette）。独立出现时可能是真实显示
#    文本：rawstr 侧（有对象级显示证据系统）交给分类链按证据放行；
#    DLL 侧（无对象级证据，只有验证链）保持无条件拦截。
# 3) _ENGINE_NAMING_PATTERNS（命名猜测层）：编程命名形态（PascalCase/
#    camelCase/snake_case/表名语言变体）。形态是猜测，验证链/显示证据优先。

# 确定性引擎字符串（与 _is_engine_string 的 strip 语义一致，不带首尾空格）
ENGINE_STRINGS = {
    # URP 后处理 Volume 组件/效果（组合词/专有术语，非普通英语单词）
    "lifgammagain", "splittoning", "motionblur", "coloradjustments", "filmgrain",
    "tonemapping", "paniniprojection", "probevolumesoptions", "whitebalance",
    "defaultinputactions", "quaternion", "2dvector", "keyboard&mouse",
    "volume profile",
    "liberation sans", "liberationsans sdf", "screen space", "ambient occlusion",
    "depth of field", "bloom", "vignette", "chromatic aberration", "lut", "color lut",
    "widescreen", "target framerate",
    "graphics quality", "texture quality", "anisotropic filtering",
    "colorlookup", "depthoffield", "lensdistortion",
    "volumetricfogvolumecomponent", "channelmixer", "bloomcomponent", "vignettecomponent",
    "coloradjustmentscomponent", "tonemappingcomponent", "filmgraincomponent",
    "motionblurcomponent", "splittoningcomponent", "whitebalancecomponent",
    "paniniprojectioncomponent", "liftgammagain", "liftgammagaincomponent",
    "probevolumesoptionscomponent",
    "screenspacelensflare", "shadowsmidtoneshighlights", "colorcurves",
    "liberationsans sdf - fallback", "volumeprofile",
    # UGUI 回调/组件
    "ondecrement", "onincrement", "onscrollbarclicked", "panel title",
    "resetdebugmanager", "bitfield", "selectpreviousitem", "selectnextitem",
    "onaction",
    # 数学类型
    "vector4", "vector2", "vector3",
    # TMP 资源/演示
    "tmp settings", "message text", "foldout", "face with tears of joy",
    "default style sheet", "dropcap numbers", "emojione", "emoji one",
    "default sprite asset", "unity sdf", "unity logo", "electronic highway sign",
    "text -", "pts - lorem ipsum", "montserrat", "semibold", "bangers", "oswald",
    "anton", "roboto", "noto sans", "droid sans", "arial", "impact", "times new roman",
    "comic sans", "open sans", "source sans", "lobster", "pacifico", "bebas",
    "cinzel", "playfair", "merriweather", "raleway", "ubuntu", "poppins", "nunito",
    "work sans", "inter", "segoe", "calibri", "cambria", "georgia", "garamond",
    "palatino", "futura", "century", "courier", "verdana", "tahoma", "trebuchet",
    "geneva", "lucida", "monaco", "menlo", "consolas", "dejavu", "liberation",
    # Addressables/Unity 包
    "standalonewindows64", "addressablesmaincontentcatalog",
    "20-7e,a0,200b,2026",
    # Unity Localization 表名
    "monologuetable", "dialoguetable", "uitable", "monologuetable shared data",
    "dialoguetable shared data", "uitable shared data",
    # TMP/EmojiOne 表情名（组合词/专名——'face with tears of joy' 等完整
    # 描述串在游戏显示文本中几乎不出现；普通英语单词如 imp/skull/sleeping
    # 已移入门控层 ENGINE_GATED_STRINGS）
    "stuck out tongue", "zipper mouth", "money mouth", "cold sweat", "eye roll",
    "smile cat", "joy cat", "smiling imp", "face with tears of joy", ".notdef",
}
_ENGINE_STRINGS_LOWER = {s.strip().lower() for s in ENGINE_STRINGS}

# 门控层：高歧义普通英语单词（独立出现时可能是真实显示文本）
ENGINE_GATED_STRINGS = {
    # Input System 绑定/动作名（'Press' 按钮文本、'Keyboard'/'Mouse' 控制
    # 设置菜单标签同形；组合绑定 'Keyboard&Mouse' 带分隔符是确定性形态，
    # 留在 ENGINE_STRINGS core 层）
    "navigate", "joystick", "gamepad", "touch", "keyboard", "mouse",
    "scrollwheel", "middleclick", "rightclick", "leftclick",
    "trackeddeviceposition", "trackeddeviceorientation", "trackeddirection",
    "pointer", "tap", "click", "press",
    # TMP/EmojiOne 字符名中的普通英语单词（'Skull' 物品名/'Imp' 敌人名/
    # 'Sleeping' 状态名/'Yum!' 对话词同形——审计 R2 实证误跳过）
    "smiley", "wink", "winking", "smirk", "blush", "grinning", "tongue",
    "kissing", "pensive", "weary", "grimacing", "sleeping", "sleepy", "scream",
    "hugging", "thinking", "nerd", "imp", "skull", "poop", "sob", "yum",
    "dizzy", "astonished", "hushed", "sweat", "laughing", "whaaat", "whaaat!",
    # 后处理效果单词（设置菜单 'Bloom' 开关标签同形）
    "bloom", "vignette", "lut",
}
_ENGINE_GATED_LOWER = {s.strip().lower() for s in ENGINE_GATED_STRINGS}
# 前缀匹配引擎串（演示文本等带后缀的确定性内容；_is_engine_string 会先 strip 再匹配）
_ENGINE_PREFIX = ("text -", "pts - lorem ipsum", "bitfield", "default sprite asset")

_ENGINE_NAMING_PATTERNS = [
    # Unity Localization 表键 / 编程命名：无空格、小写开头、含内部大写或下划线
    # （lockedEntrance、ui_newGame、takeTools）
    re.compile(r"^[a-z][a-zA-Z0-9_]*[A-Z][a-zA-Z0-9_]*$"),
    re.compile(r"^[a-z]+_[a-zA-Z0-9_]+$"),
    # PascalCase 数据/类名（FlashlightData、MonologueTable）
    re.compile(r"^[A-Z][a-z]+[A-Z][a-zA-Z0-9]*$"),
    # Localization 表名语言变体（UITable_en / MonologueTable_es / monologue_table_es）：
    # 表名+语言码命名猜测。从确定性桶（_ENGINE_PATTERNS）移入猜测桶——
    # 「已验证 UI setter 消费」的 IL 数据流证据优先于本形态（F26 回归：
    # 放 core 里把 set_text 消费的 UITable_en 也拦截了；而 I2 表键在
    # raw scan 里仍由全量 is_engine_string 拦截，行为不变）。
    re.compile(r"[Tt]able_[a-z]{2}$"),
]

_ENGINE_PATTERNS = [
    re.compile(r"^;"),                                              # ;Gamepad
    re.compile(r"[;&].*[;&]"),                                      # Keyboard&Mouse;Gamepad 组合绑定
    re.compile(r"^[0-9a-fA-F]{32}$"),                               # 32 位哈希
    re.compile(r"^[0-9a-fA-F]{40}$"),                               # 40 位哈希
    re.compile(r"\bto\b.*[-–—]\s*(vertical|horizontal|diagonal|radial)$", re.I),
    re.compile(r"^[0-9A-Fa-f][0-9A-Fa-f, -]+$"),                    # 字符区间表 20-7E,A0,2026
    re.compile(r"\bsdf$", re.I),                                    # TMP 字体名 … SDF
    re.compile(r"^(?:<[^>]{1,60}>)+[^\w]?$"),                       # 整行纯 TMP 富文本标签
    re.compile(r"^(smiling face|grinning face|face with|slightly smiling|"
               r"rolling on the floor|thinking face|winking face|kissing face|"
               r"pensive face|confused face|flushed face|disappointed face|"
               r"worried face|angry face|pouting face|crying face|loudly crying|"
               r"frowning face|weary face|tired face|grimacing face|lying face|"
               r"relieved face|neutral face|expressionless face)", re.I),  # emoji 字符名
    re.compile(r"^[A-Za-z]+ \([a-z]{2,3}\)$"),                      # 语言名 English (en)
    re.compile(r"table shared data$", re.I),                        # Localization 表名 XxxTable Shared Data
    # HTTP 协议状态行（websocket-sharp.dll 网络库内部串，非游戏文本）
    re.compile(r"^HTTP/\d(?:\.\d)? \d{3} [A-Za-z][A-Za-z ]*$", re.I),
    # Input System 序列化绑定路径（<Keyboard>/z、<Mouse>/position、<Gamepad>/leftStick）。
    # 设备路径是引擎语法，翻译后 InputSystem 反序列化/查找绑定失败 → 按键全部无反应
    # （morfosigame 实证：Proceed/SkipCutscene 动作被译后点击与跳过失效）。
    re.compile(r"^<[A-Za-z0-9_.]+>/(?:[A-Za-z0-9_./-]+)?$"),
    # Input System interactions 触发方式串（Press(behavior=2)、Hold()、Tap()）：
    # 运行时按名字解析交互，翻译必然破坏触发条件。
    re.compile(r"^(?:press|hold|tap|slowtap|multitap|doubletap|"
               r"pressandrelease|pressdelay|presspoint)\s*\(.*\)$", re.I),
    # Timeline 动画资源 displayName（"AnimationPlayableAsset of Recorded"）
    re.compile(r"^animationplayableasset of\b", re.I),
    # Timeline 轨道 displayName（Animation Track (1) 带编号形式——轨道重名自动加序号，
    # 翻译后字符串结构破坏且按名查找失败，morfosigame 实证被拆成 '动画轨道'+' (1)'）
    re.compile(r"^(?:Activation|Animation|Audio|Control|Group|Marker|Playable|"
               r"Signal|Cinemachine) Track(?:\s*\(\d+\))?$", re.I),
    re.compile(r"^version=0\.0\.0\.0, culture=neutral", re.I),      # 程序集限定名尾部
    # Unity Shader 路径名（Shader.Find 查找键）：Hidden/ 前缀是引擎内置
    # 隐藏 shader 的惯例（Hidden/Post FX/FXAA 等后处理链）。翻译后
    # Shader.Find 找不到 → 材质空 → 渲染崩溃（tiiny-ragdoll 实证：
    # '隐藏/后期处理/FXAA' → PostProcessing OnRenderImage 每帧抛异常
    # → 启动卡死）。仅 Hidden/ 前缀（引擎惯例最确定，防过宽）。
    re.compile(r"^Hidden/", re.I),
    # FMOD Studio 事件路径（event:/Bank/Event、event:/Music/GlobalMusic）：
    # RuntimeManager 运行时按路径字符串查找并加载音频事件，翻译后事件
    # 路径断裂 → 音效/音乐全部静默（give-me-strength 实证 184 条被译成
    # 「事件：/音乐/全球音乐」——FMOD 查找失败静默无声，哑破坏）。
    # event:/ 前缀是 FMOD 序列化字符串的确定性形态（显示文本几乎不会以
    # 此开头），全局无条件跳过。
    re.compile(r"^event:/", re.I),
]

_DISPLAY_WORD = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿА-Яа-я]{2,}")
_SENTENCE_PUNCT = re.compile(r"[.!?。！？]$")
_CODE_QUALIFIED = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)+[A-Za-z_][A-Za-z0-9_]*$")
_INTERACTION_ACTION = (
    r"open|interact|use|continue|pick\s*up|talk|enter|exit|close|read|"
    r"unlock|activate|inspect|grab|drop|hide|show|jump|crouch|sprint|run|"
    r"reload|fire|shoot|attack|aim|block|dodge|pause|select|confirm|cancel|"
    r"equip|consume|throw|climb|descend|drive|take|put|move|begin|insert|break"
)
_ACTION_OBJECT_WORD = r"[A-Za-z0-9][A-Za-z0-9'_-]*"
_IMPERATIVE_ACTION_CLAUSE = (
    rf"(?P<action>(?:{_INTERACTION_ACTION})\b"
    rf"(?:[ \t]+{_ACTION_OBJECT_WORD}){{0,12}})"
)
_ACTION_VERB_PREFIX = re.compile(rf"^(?:{_INTERACTION_ACTION})\b", re.I)
_ACTION_DETERMINERS = {"a", "an", "the", "your", "my", "this", "that"}
_ACTION_FUNCTION_WORDS = {
    *_ACTION_DETERMINERS,
    "in", "on", "into", "from", "with", "through", "at", "to",
    "down", "up", "out", "away", "back", "not",
}
_SECONDARY_AUXILIARIES = {
    "am", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "has", "have", "had", "will", "would",
    "could", "should", "must", "may", "might", "won't", "can't", "cannot",
    "doesn't", "didn't", "isn't", "wasn't", "weren't", "aren't",
    "couldn't", "wouldn't", "shouldn't", "hasn't", "haven't", "hadn't",
    "don't",
}
# Finite result verbs seen after an already complete action complement.  Keep
# ``can`` out of the auxiliary set because Unity prompts commonly name oil cans.
_SECONDARY_FINITE_PREDICATES = {
    "fell", "broke", "went", "gone", "came",
    "ran", "got", "failed", "fails", "appears", "disappears", "vanishes",
    "breaks", "falls", "opens", "closes", "shut", "shows", "displays",
    "times", "crashes", "expires", "collapses", "stops", "succeeds",
    "hangs", "freezes", "froze", "jams", "dies",
}
_SECONDARY_PARTICIPLE_MODIFIERS = {"fallen", "broken", "frozen"}
_AMBIGUOUS_NOMINAL_FINITE = {"remains", "errors", "ends", "works", "returns"}
_AMBIGUOUS_NOMINAL_MODIFIERS = {
    "fallen", "frozen", "system", "loose", "collected", "tax",
}
_ACTION_ADVERBS = {
    "again", "hard", "once", "twice", "today", "tomorrow", "yesterday",
    "now", "soon", "later", "already", "yet", "forever", "here", "there",
    "repeatedly", "unexpectedly", "suddenly", "briefly", "eventually",
    "initially", "previously", "currently",
}
_INTERACTION_PROMPT = re.compile(
    rf"(?:\b(?:press|hold|tap|click|push)\b\s+"
    rf"(?:\[[^\]\r\n]+\]|\([^\)\r\n]+\)|<[^>\r\n]+>|"
    rf"[A-Za-z0-9]+(?:[ \t]+[A-Za-z0-9]+){{0,2}}?)"
    rf"(?:[ \t]+key)?[ \t]+(?:to[ \t]+)?(?:{_INTERACTION_ACTION})\b)"
    rf"|(?:^[ \t]*(?:\[[^\]\r\n]+\]|\([^\)\r\n]+\)|[A-Za-z0-9]+)"
    rf"[ \t]*[-:：][ \t]*(?:to[ \t]+)?(?:{_INTERACTION_ACTION})\b)"
    rf"|(?:(?:按下?|长按|点击|轻触)[ \t]*"
    rf"(?:\[[^\]\r\n]+\]|\([^\)\r\n]+\)|[A-Za-z0-9]+)[ \t]*键?[ \t]*"
    rf"(?:以便|来|以)?[ \t]*(?:打开|互动|交互|继续|拾取|对话|进入|退出|关闭|阅读|解锁|激活|检查))",
    re.I | re.M,
)
_INTERACTION_ACTION_WORD = re.compile(
    rf"\b(?:press|hold|tap|click|push|{_INTERACTION_ACTION})\b", re.I)
_COMMON_NAMED_PHYSICAL_INPUT = (
    r"d-pad[ \t]+(?:up|down|left|right)|page[ \t]+(?:up|down)|"
    r"(?:arrow[ \t]+(?:up|down|left|right)|(?:up|down|left|right)[ \t]+arrow)|"
    r"(?:caps|num|scroll)[ \t]+lock|print[ \t]+screen|"
    r"backspace|delete|insert|home|end|enter|return|tab|space|esc(?:ape)?"
)
_PHYSICAL_INPUT_COMPONENT = (
    rf"(?:{_COMMON_NAMED_PHYSICAL_INPUT})|"
    r"(?:left|right)[ \t]+(?:shift|ctrl|control|alt)|"
    r"mouse[0-9]+|f[0-9]+|shift|ctrl|control|alt|"
    r"numpad[ \t]+[A-Za-z0-9+_-]+|"
    r"(?-i:[A-Z][A-Z0-9_-]{1,23})|[A-Za-z0-9]"
)
_PHYSICAL_INPUT_CHORD = (
    rf"(?:{_PHYSICAL_INPUT_COMPONENT})"
    rf"(?:[ \t]*\+[ \t]*(?:{_PHYSICAL_INPUT_COMPONENT}))+"
)
_PHYSICAL_BINDING_COMPONENT_PATTERN = (
    r"(?:ctrl|control|shift|alt|esc(?:ape)?|backspace|delete|insert|"
    r"home|end|enter|return|tab|space|pageup|pagedown|"
    r"mouse[0-9]+|f[0-9]+|l[0-9]+|r[0-9]+|lb|rb)"
)
_PHYSICAL_BINDING_CHORD = (
    rf"(?:{_PHYSICAL_BINDING_COMPONENT_PATTERN})"
    rf"(?:[_-](?:{_PHYSICAL_BINDING_COMPONENT_PATTERN}))+"
)
_D_PAD_BINDING = re.compile(
    r"d-pad[ \t]+(?:up|down|left|right)", re.I)
_PHYSICAL_BINDING_COMPONENT = re.compile(
    _PHYSICAL_BINDING_COMPONENT_PATTERN,
    re.I,
)
# 物理按键名（casefold）：交互提示中这些词通常作为按键出现
# （"press z or enter" 的 enter 是按键不是动词），译文保留按键名是正确行为。
# 注意 enter/return/space 等同时是动作词——按语境区分（见 quality.py）。
PHYSICAL_KEY_NAMES_CASEFOLD = {
    "escape", "esc", "enter", "return", "space", "tab", "backspace",
    "delete", "del", "insert", "home", "end", "pageup", "pagedown",
    "shift", "ctrl", "control", "alt", "capslock", "numlock",
    "scrolllock", "printscreen", "prtsc", "pause", "break",
    # 鼠标键（force-reboot 实证：RMB 被译「人民币」且被记忆沉淀成词对，
    # 跨游戏误杀正确译文——键名必须可识别，见 quality._KEY_LABEL_CASEFOLD）
    "rmb", "lmb", "mmb",
    *{f"f{i}" for i in range(1, 13)},
}
_LITERAL_GLYPH = (
    r"'[^'\r\n]{1,24}'|\[[^\]\r\n]{1,24}\]|"
    r"\([^\)\r\n]{1,24}\)|<[^>\r\n]{1,24}>|"
    rf"(?:{_PHYSICAL_INPUT_CHORD})|"
    rf"(?:{_PHYSICAL_BINDING_CHORD})|"
    rf"(?:{_COMMON_NAMED_PHYSICAL_INPUT})|"
    r"(?:left|right)[ \t]+(?:shift|ctrl|control|alt)|"
    r"mouse[0-9]+|f[0-9]+|shift|ctrl|control|alt|"
    r"[A-Za-z0-9](?![A-Za-z0-9+_-])"
)
_PREFIX_LITERAL_EVENT = re.compile(
    rf"\b(?:press|hold|tap|push|click)\b[ \t]*"
    rf"(?P<token>{_LITERAL_GLYPH})(?![A-Za-z0-9+_-])",
    re.I,
)
_PREFIX_NAMED_LITERAL_EVENT = re.compile(
    r"(?i:\b(?P<command>press|hold|tap|push|click)\b)[ \t]*"
    r"(?P<token>[A-Z][A-Z0-9+_-]{1,23}|(?i:numpad)[ \t]+[A-Za-z0-9+_-]+)"
    r"(?=[ \t]*(?:(?i:key)\b)?(?:[ \t]+(?i:to|then|on)\b|[,;]|$))"
)
_CHINESE_LITERAL_EVENT = re.compile(
    rf"(?:按下?|长按|轻触|点击)[ \t]*(?P<token>{_LITERAL_GLYPH})"
    rf"(?![A-Za-z0-9+_-])",
    re.I,
)
_LEADING_LITERAL_EVENT = re.compile(
    rf"^[ \t]*(?P<token>{_LITERAL_GLYPH})(?![A-Za-z0-9+_-])"
    r"[ \t]*[-:：][ \t]*(?P<action>[^\r\n]+)",
    re.I | re.M,
)
_ARTICLE_KEY_EVENT = re.compile(
    rf"\b(?:press|hold|tap|push|click)\b[ \t]+the[ \t]+"
    rf"(?P<token>{_COMMON_NAMED_PHYSICAL_INPUT})"
    r"(?:[ \t]+(?:key|button))?\b",
    re.I,
)
_STRONG_PREFIX_PROMPT = re.compile(
    rf"\b(?:press|hold|tap|push|click)\b[ \t]*"
    rf"(?:{_LITERAL_GLYPH})(?![A-Za-z0-9+_-])"
    rf"(?:[ \t]+key)?[ \t]+to[ \t]+"
    + _IMPERATIVE_ACTION_CLAUSE,
    re.I,
)
_STRONG_ARTICLE_KEY_PROMPT = re.compile(
    rf"\b(?:press|hold|tap|push|click)\b[ \t]+(?:the[ \t]+)?"
    rf"(?:{_COMMON_NAMED_PHYSICAL_INPUT})"
    r"(?:[ \t]+(?:key|button))?\b",
    re.I,
)
_LONG_ARTICLE_KEY_INSTRUCTION = re.compile(
    rf"(?:^|[.!?。！？][ \t]+)"
    rf"(?:(?:when|once|after|before|to)\b[^.!?。！？]{{0,48}},[ \t]*|"
    rf"(?:then|please)[ \t]+){_STRONG_ARTICLE_KEY_PROMPT.pattern}"
    rf"(?:[ \t]+on[ \t]+your[ \t]+keyboard)?"
    rf"(?=$|[.!?。！？])",
    re.I,
)
_STRONG_ARTICLE_KEY_ACTION_PROMPT = re.compile(
    rf"{_STRONG_ARTICLE_KEY_PROMPT.pattern}[ \t]+to[ \t]+"
    + _IMPERATIVE_ACTION_CLAUSE,
    re.I,
)
_LONG_ARTICLE_KEY_ACTION_INSTRUCTION = re.compile(
    rf"(?:^|[.!?。！？][ \t]+)"
    rf"(?:(?:when|once|after|before|to)\b[^.!?。！？]{{0,48}},[ \t]*|"
    rf"(?:then|please)[ \t]+){_STRONG_ARTICLE_KEY_ACTION_PROMPT.pattern}"
    rf"(?=$|[.!?。！？])",
    re.I,
)
_LONG_LITERAL_ACTION_INSTRUCTION = re.compile(
    rf"(?:^|[.!?。！？][ \t]+)"
    rf"(?:(?:when|once|after|before|to)\b[^.!?。！？]{{0,48}},[ \t]*|"
    rf"(?:then|please)[ \t]+){_STRONG_PREFIX_PROMPT.pattern}"
    rf"(?=$|[.!?。！？])",
    re.I,
)
_INTERACTION_DIAGNOSTIC_CONTEXT = re.compile(
    r"\b(?:message|prompt|state|event)\b[^.!?。！？]{0,64}"
    r"\b(?:(?:was|were|is|are)[ \t]+)?"
    r"(?:missing|not[ \t]+(?:displayed|shown)|observed|failed)\b",
    re.I,
)
# 方括号包围的交互动作标签（seijunDROP 实证 2026-09-01：'[PICK UP]'——
# 游戏把「拾取物品」提示以 [动作] 形态硬编码在 IL2CPP 字面量里）。方括号
# 标签是输入/交互提示的确定性形态（[E] 键位、[PICK UP] 动作、[OPTIONS]
# 菜单项——游戏常用它标注 UI 按键位与交互提示）。剥去方括号后是 2+ 词
# 动作短语（PICK UP/OPEN DOOR/PUSH CART）或白名单 UI 词（OPTIONS/EXIT）。
# 与 _INTERACTION_PROMPT 区分：后者是「按键 → 动作」完整提示
# （'Press E to interact'），本规则是「动作本身被括号标注」的短标签
# （'[PICK UP]'）。不做成完整动词白名单（防过宽），只命中无空格歧义
# 的括号动作形态。真实显示文本 '[2026.09.01]' 类日期/数字括号无动作词，
# 不命中。
_BRACKET_ACTION_LABEL = re.compile(
    r"^\[[A-Za-z0-9][^\]\r\n]{1,39}\]$")
# 方括号动作标签内的动作词根：_INTERACTION_ACTION 的动词形态（去掉
# 'pick\s*up' 的补语要求——'[PICK]' 单动作词同样真实）+ 补充 push 按键
# 动词。从开头匹配 + \b 边界防误伤（'[BREAD]' 含 read 子串不命中；
# 'read' 是动作词根，但 ^ 开头匹配保证 BREAD 不被 search 误判）。
_BRACKET_ACTION_ROOT = re.compile(
    rf"^(?:{_INTERACTION_ACTION.replace(r'pick\s*up', 'pick')}|push)\b",
    re.I)
_CODE_ACTION = re.compile(
    r"(?:(?:[A-Za-z_][A-Za-z0-9_]*\.)+[A-Za-z_][A-Za-z0-9_]*|"
    r"(?:get|set)_[A-Za-z_][A-Za-z0-9_]*|m_[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\(\)|\[[0-9]+\])?"
)
_SEMANTIC_INPUT_EVENT = re.compile(
    r"\b(?:press|hold|tap|push|click)\b[ \t]+(?P<any_key>any[ \t]+key)\b|"
    r"(?P<right_click>\bright[ \t]+click\b)|"
    r"(?P<button>\b(?:square(?:/x/y)?|x|y)[ \t]+button\b)",
    re.I,
)
_SEMANTIC_PROMPT = re.compile(
    r"\b(?:press|hold|tap|push|click)\b[ \t]+"
    r"(?:any[ \t]+key|(?:square(?:/x/y)?|x|y)[ \t]+button)\b|"
    r"\bright[ \t]+click\b(?=[ \t]+(?:with|to)\b|[ \t]*[-:：])|"
    r"\b(?:square(?:/x/y)?|x|y)[ \t]+button[ \t]*[-:：][ \t]*\S",
    re.I,
)


@dataclass(frozen=True)
class InputEvent:
    kind: Literal["literal_glyph", "semantic_input"]
    value: str


def _normalize_input_value(value: str) -> str:
    normalized = " ".join(value.strip().split())
    if ((normalized[0], normalized[-1])
            in {("[", "]"), ("(", ")"), ("<", ">"), ("'", "'")}):
        return normalized[1:-1].strip()
    return normalized


def is_code_action_binding(text: str) -> bool:
    """Return whether a glyph-action row targets a code symbol, not display text."""
    match = _LEADING_LITERAL_EVENT.fullmatch(text.strip())
    return bool(match and _CODE_ACTION.fullmatch(match.group("action").strip()))


def is_physical_binding_identifier(text: str) -> bool:
    """Return whether a bare value is a physical-key binding identifier."""
    stripped = text.strip()
    if _D_PAD_BINDING.fullmatch(stripped):
        return True
    parts = re.split(r"[_-]", stripped)
    return (len(parts) > 1
            and all(_PHYSICAL_BINDING_COMPONENT.fullmatch(part) for part in parts))


def interaction_input_events(text: str) -> tuple[InputEvent, ...]:
    """Parse typed input events from a user-visible interaction prompt."""
    stripped = text.strip()
    positioned: list[tuple[int, InputEvent]] = []
    semantic_spans: list[tuple[int, int]] = []
    literal_spans: list[tuple[int, int]] = []
    for match in _SEMANTIC_INPUT_EVENT.finditer(stripped):
        group = next(name for name, value in match.groupdict().items()
                     if value is not None)
        start, end = match.span(group)
        semantic_spans.append((start, end))
        positioned.append((start, InputEvent("semantic_input", match.group(group))))
    for pattern in (_ARTICLE_KEY_EVENT, _PREFIX_LITERAL_EVENT, _CHINESE_LITERAL_EVENT,
                    _LEADING_LITERAL_EVENT):
        for match in pattern.finditer(stripped):
            action = match.groupdict().get("action")
            if action is not None and _CODE_ACTION.fullmatch(action.strip()):
                continue
            start, end = match.span("token")
            if any(start < semantic_end and semantic_start < end
                   for semantic_start, semantic_end in semantic_spans):
                continue
            literal_spans.append((start, end))
            positioned.append((start, InputEvent(
                "literal_glyph", _normalize_input_value(match.group("token")))))
    for match in _PREFIX_NAMED_LITERAL_EVENT.finditer(stripped):
        command_start, command_end = match.span("command")
        if any(command_start < semantic_end and semantic_start < command_end
               for semantic_start, semantic_end in semantic_spans):
            continue
        start, end = match.span("token")
        if any(start < other_end and other_start < end
               for other_start, other_end in semantic_spans + literal_spans):
            continue
        positioned.append((start, InputEvent(
            "literal_glyph", _normalize_input_value(match.group("token")))))
    positioned.sort(key=lambda item: item[0])
    return tuple(event for _, event in positioned)


def is_engine_string_core(s: str) -> bool:
    """确定性引擎串核心判定（无编程命名形态猜测）：着色器属性、序列化
    引用、程序集限定名、已知引擎字符串、哈希、绑定路径、富文本标签、
    表情名、语言名 (en)、HTTP 状态行、Timeline/Interaction 形态。

    与 is_engine_string 的区别：不含「编程命名」形态猜测（驼峰/PascalCase/
    小写下划线——lockedEntrance/FlashlightData 类）。PascalCase 形态对
    **无结构上下文**的 raw scan 是类名猜测，但对结构化格式 value 位置
    会误伤罗马音台词等真实显示文本（doog 实证 'FeeNGAh' 等 hololive
    罗马音歌词被引擎串判定跳过）——格式化文本降级用本核心判定。
    """
    s2 = s.strip()
    if _ENGINE_PROP.match(s2) or _ENGINE_NAME.search(s2):
        return True
    low = s2.lower()
    if low in _ENGINE_STRINGS_LOWER or low.startswith(_ENGINE_PREFIX):
        return True
    return any(p.search(s2) for p in _ENGINE_PATTERNS)


def is_engine_string_gated(s: str) -> bool:
    """门控层引擎串判定：高歧义普通英语单词（输入词/emoji 字符名/后处理
    效果单词）精确匹配。

    调用方决定门控策略：
    - DLL 侧（mono_dll）无对象级显示证据系统，只有 IL 验证链——gated 词
      在未验证时无条件拦截（绑定名/枚举名形态，'press'/'mouse' 独立 ldstr
      大多是代码串）；
    - rawstr 侧（extractor）有对象级显示证据（句子/交互提示/白名单词/
      控件信号）——gated 词不在此层拦截，流入分类链按证据放行（审计 R2：
      'Skull' 物品名/'Imp' 敌人名/'Sleeping' 状态名曾被全局词表误跳过）。
    """
    return s.strip().lower() in _ENGINE_GATED_LOWER


def is_engine_string(s: str) -> bool:
    """引擎内部字符串判定：着色器属性、序列化引用、程序集限定名、已知引擎字符串。"""
    if is_engine_string_core(s):
        return True
    s2 = s.strip()
    return any(p.search(s2) for p in _ENGINE_NAMING_PATTERNS)


def display_evidence_tier(text: str) -> str:
    """统一显示证据档位判定（审计 R3 收敛三套阈值）：sentence / phrase /
    word / none。

    历史问题：_has_sentence_shape（≥10 字符含空格即句子）、has_display_
    text_evidence（≥3 词）、_SENTENCE_PUNCT 三套阈值互不一致——同一文本
    在不同路径判定不同（'Player Idle'/'White Flash' 等 2 词引擎配置名被
    10 字符规则误判句子放行；'New Game'/'Load game' 等 2 词按钮文本卡
    在 3 词阈值下）。收敛为单一权威函数，调用方按需取档：
    - sentence：句末标点或 ≥3 词——真实句子/对话/教程形态，可独立放行；
    - phrase：2 词（无标点）——按钮/菜单短文本（'New Game'/'Press
      Start'），需对象级证据配合放行（组件对象/UI 控件/白名单）；
    - word：单词（无空格）——需白名单/对象级证据配合放行；
    - none：无语言内容（空串；纯代码形态由调用方先行排除）。
    2 词短语（'Player Idle'/'Arrow Keys'/'Grass Shader'）不凭文本形态
    放行——它们与 'Press Start'/'New Game' 文本层面不可区分，只能靠
    对象级证据（所在对象是引擎配置还是 UI 元素）区分。
    """
    stripped = text.strip()
    if not stripped:
        return "none"
    words = _DISPLAY_WORD.findall(stripped)
    if _SENTENCE_PUNCT.search(stripped) or len(words) >= 3:
        return "sentence"
    if len(words) >= 2:
        return "phrase"
    return "word"


def has_display_text_evidence(text: str) -> bool:
    """Return whether a raw Unity string has strong user-visible language evidence."""
    stripped = text.strip()
    if not stripped or is_engine_string(stripped) or _CODE_QUALIFIED.fullmatch(stripped):
        return False
    if is_interaction_prompt(stripped):
        return True
    return display_evidence_tier(stripped) == "sentence"


def is_interaction_prompt(text: str) -> bool:
    stripped = text.strip()
    if is_code_action_binding(stripped):
        return False
    return bool(
        any(event.kind == "literal_glyph"
            for event in interaction_input_events(stripped))
        or _SEMANTIC_PROMPT.search(stripped)
        or _INTERACTION_PROMPT.search(stripped)
    )


def _is_safe_imperative_match(match: re.Match[str] | None) -> bool:
    if match is None:
        return False
    action = str(match.groupdict().get("action") or "")
    verb = _ACTION_VERB_PREFIX.match(action)
    if verb is None:
        return False
    complement = re.findall(_ACTION_OBJECT_WORD, action[verb.end():])
    entity_count = 0
    entities: list[str] = []
    determined_phrase = False
    for index, token in enumerate(complement):
        normalized = token.casefold()
        if normalized in _ACTION_DETERMINERS:
            determined_phrase = True
            continue
        if normalized in _ACTION_FUNCTION_WORDS:
            continue
        later_entity = any(
            later.casefold() not in _ACTION_FUNCTION_WORDS
            and later.casefold() not in _ACTION_ADVERBS
            and not later.casefold().endswith("ly")
            for later in complement[index + 1:]
        )
        is_contextual_can = (
            normalized == "can" and entity_count > 0
            and index + 1 < len(complement)
            and complement[index + 1].casefold() == "not"
        )
        is_proper_name = (
            normalized in {"may", "will"}
            and token[:1].isupper()
            and entity_count == 0
        )
        is_determined_will = (
            normalized == "will" and determined_phrase
            and entity_count == 0 and not later_entity
        )
        if ((normalized in _SECONDARY_AUXILIARIES
             and not is_proper_name and not is_determined_will)
                or is_contextual_can):
            return False
        if normalized in _AMBIGUOUS_NOMINAL_FINITE:
            is_nominal = (
                not later_entity
                and ((determined_phrase and entity_count == 0)
                     or (entity_count > 0 and entities[-1]
                         in _AMBIGUOUS_NOMINAL_MODIFIERS))
            )
            if is_nominal:
                entity_count += 1
                entities.append(normalized)
                continue
            return False
        if normalized in _SECONDARY_FINITE_PREDICATES:
            return False
        looks_like_participle = (
            normalized in _SECONDARY_PARTICIPLE_MODIFIERS
            or (len(normalized) > 4 and normalized.endswith("ed"))
        )
        if looks_like_participle:
            if entity_count == 0 and later_entity:
                entity_count += 1
                entities.append(normalized)
                continue
            return False
        entity_count += 1
        entities.append(normalized)
    return True


def is_strong_interaction_prompt(text: str) -> bool:
    """Return only interaction evidence safe without UI call provenance."""
    stripped = text.strip()
    if is_code_action_binding(stripped):
        return False
    if re.match(r"^(?:debug|error|warning|failed|unable|exception)\b", stripped,
                re.I):
        return False
    if _INTERACTION_DIAGNOSTIC_CONTEXT.search(stripped):
        return False
    # 方括号交互动作标签（seijunDROP '[PICK UP]'）：括号动作 + 白名单动作词
    # /UI 词 → 强交互证据。'[E]' 等纯键位括号（_PREFIX_LITERAL_EVENT 已覆盖
    # 形态）不重复拦截，由括号动作词判定。剥去括号后是真实动作短语
    # （PICK UP / OPEN DOOR）或白名单 UI 词（OPTIONS/EXIT）才命中。
    bracket_match = _BRACKET_ACTION_LABEL.fullmatch(stripped)
    if bracket_match:
        inner = stripped[1:-1].strip()
        inner_words = inner.split()
        # 懒加载：placeholders.py 从本模块导入（循环依赖），DISPLAY_WORDS
        # 只在命中括号形态时才解析。2+ 词动作短语由动作词根判定
        # （PICK UP/OPEN DOOR/PUSH CART）；单词括号（[PICK]/[OPTIONS]/
        # [EXIT]/[PLAY]）需动作词根或白名单 UI 词才放行（纯键位 [E]/[B2]/
        # [WASD] 无动作词不命中）。
        from hanhua.core.placeholders import DISPLAY_WORDS
        has_action = any(
            _BRACKET_ACTION_ROOT.match(w) or w.casefold() in DISPLAY_WORDS
            for w in inner_words)
        if has_action:
            return True
    bare_command = stripped.rstrip(" .!?。！？")
    has_prefix_input = bool(
        _PREFIX_LITERAL_EVENT.fullmatch(bare_command)
        or _PREFIX_NAMED_LITERAL_EVENT.fullmatch(bare_command)
        or _CHINESE_LITERAL_EVENT.fullmatch(bare_command))
    sentence_marks = sum(stripped.count(mark) for mark in ".!?。！？")
    prefix_action = _STRONG_PREFIX_PROMPT.fullmatch(bare_command)
    article_action = _STRONG_ARTICLE_KEY_ACTION_PROMPT.fullmatch(bare_command)
    long_action = (
        _LONG_ARTICLE_KEY_ACTION_INSTRUCTION.search(stripped)
        or _LONG_LITERAL_ACTION_INSTRUCTION.search(stripped)
    )
    long_instruction = (
        len(stripped) >= 40
        and sentence_marks >= 2
        and bool(_LONG_ARTICLE_KEY_INSTRUCTION.search(stripped)
                 or _is_safe_imperative_match(long_action)))
    return bool(
        _is_safe_imperative_match(prefix_action)
        or _is_safe_imperative_match(article_action)
        or _STRONG_ARTICLE_KEY_PROMPT.fullmatch(bare_command)
        or has_prefix_input
        or _SEMANTIC_PROMPT.match(stripped)
        or (_LEADING_LITERAL_EVENT.match(stripped)
            and _INTERACTION_PROMPT.match(stripped))
        or long_instruction
    )


def interaction_input_tokens(text: str) -> tuple[str, ...]:
    """Return literal keyboard/button tokens that an interaction prompt must retain."""
    return tuple(event.value for event in interaction_input_events(text)
                 if event.kind == "literal_glyph")


def interaction_action_words(text: str) -> tuple[str, ...]:
    """Return only known interaction verbs that must not survive untranslated."""
    return tuple(match.group(0) for match in _INTERACTION_ACTION_WORD.finditer(text))
