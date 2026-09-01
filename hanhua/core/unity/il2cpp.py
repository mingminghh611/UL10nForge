"""v2 IL2CPP 提取：global-metadata.dat 字符串字面量池（写回标记为实验性，长度受限）。"""
from __future__ import annotations
import re
import struct
from pathlib import Path
from typing import Callable

from hanhua.core.extractor import ParsedFile, looks_like_noise_file
from hanhua.core.models import STATUS_SKIPPED, TextEntry
from hanhua.core.unity.extractor import (
    _finalize_skipped_counts, _skipped_sample_entry)
from hanhua.core.placeholders import is_code_identifier, should_skip
from hanhua.core.engine_strings import (
    is_engine_string as _is_engine_string,
    is_strong_interaction_prompt,
)

METADATA_MAGIC = 0xFAB11BAF
_MIN_METADATA_HEADER_SIZE = 0x30
# 只列入已用真实 metadata 验证过的布局；未知版本直接拒绝，不猜 record 尺寸。
# 验证证据：.scratch/diag_il2cpp_records2.py 对 12 个真实 blocked 游戏程序化交叉验证
# （8 字节记录各版本 100% UTF-8 可解码；v39 的 4 字节记录 100% 可解码且 8 字节假说 55% 坏）。
# 每版本：(litOff_pos, litSize_pos, dataOff_pos, dataSize_pos, entry_size, record_mode)
#   record_mode "explicit"：8 字节 <length, dataIndex>，长度显式
#   record_mode "implicit"：4 字节 <dataIndex>，长度 = 下一条 dataIndex 差值（末条到 data 区尾部）
# v39 额外约束：u32@0x10 == 记录数 == litSize / 4（Unity 6 新增字段，三个真实样本全部命中）。
_LAYOUTS = {
    24: (0x08, 0x0C, 0x10, 0x14, 8, "explicit"),
    27: (0x08, 0x0C, 0x10, 0x14, 8, "explicit"),
    29: (0x08, 0x0C, 0x10, 0x14, 8, "explicit"),
    31: (0x08, 0x0C, 0x10, 0x14, 8, "explicit"),
    39: (0x08, 0x0C, 0x14, 0x18, 4, "implicit"),
}
_ALLOWED_CONTROLS = {"\t", "\n", "\r"}
# 引擎/调试字符串特征（真实样本统计：老版 metadata 字符串池含大量
# 反汇编/日志/格式模板）：
# - {0} / {1,6} / {0:x5} 格式占位符 → 代码模板（2328/16541 命中）。
#   #14 实时渲染文本加强：不再无条件跳过——真实样本（254361268a）
#   证明 "HP: {0}/{1}"、"<color=#00FF00>+{0} HP</color>" 是游戏
#   HUD/飘字实时渲染文本，按显示形态细分类（见 _is_display_template）
# - 前导 ≥2 空白 → 调试/反汇编输出（'  .locals '、'   Character:'）
# - 无字母（字符表/数字/纯标点）→ 不可译
_IL2CPP_FORMAT_PLACEHOLDER = re.compile(r"\{[0-9][^}]*\}")
# 显示模板判定（#14）：命中 → 游戏实时渲染显示文本（display/medium
# 可自动翻译）；引擎异常消息（"Invalid token '{0}'…"）与键值模板
# （"value={0}"）不命中 → display/low 留档可见（过滤不是删除；
# 跳过是哑信号——见 memory recognition-silent-miss-lesson）。
# - TMP 富文本标签：<color=#00FF00>+{0} HP</color>（TextMeshPro 飘字）
# - HUD 冒号前缀：HP: {0}/{1}、Playtime: {0}（单短词 + 冒号 + 占位符；
#   "Exception caught: {0}" 前缀多词/动词形态不命中）
# - 数值比值 {0}/{1}：Ammo: {0}/{1}、Potions: {0}/{1}
# - 数值加减 +{0} / -{0}：+{0} 经验、-{0} HP
# - 按键交互动词开头：Press {0} to interact（仅含 {0} 的串才到此层）
_IL2CPP_DISPLAY_TAG = re.compile(r"<[a-zA-Z][a-zA-Z0-9 ]*[=>]")
_IL2CPP_HUD_PREFIX = re.compile(r"^[A-Za-z]{1,16}[:：] ?\{0\}")
_IL2CPP_VALUE_RATIO = re.compile(r"\{0\}\s*/\s*\{1\}")
_IL2CPP_VALUE_DELTA = re.compile(r"[+\-]\s*\{0\}")
_IL2CPP_INTERACTION_TEMPLATE = re.compile(
    r"^(?:press|hold|tap|click|push|hit)\b[^.!?\n]{0,32}\{0\}", re.I)


def _is_display_template(s: str) -> bool:
    """含格式占位符的串是否为游戏实时渲染显示文本（#14）。

    依据真实样本判别：254361268a（HUD/飘字：'Potions: {0}/{1}'、
    '<color=#00FF00>+{0} HP</color>'）vs dcdb50a165（引擎异常：
    "Invalid token '{0}' in input string"、"Can't assign null…"）。
    显示模板具备 TMP 富文本标签 / HUD 冒号前缀 / 数值比值加减值 /
    按键交互动词形态；引擎异常消息与键值模板（"value={0}"）不具备
    → 落入 display/low 留档（过滤不是删除，跳过是哑信号）。
    """
    return bool(
        _IL2CPP_DISPLAY_TAG.search(s)
        or _IL2CPP_HUD_PREFIX.match(s)
        or _IL2CPP_VALUE_RATIO.search(s)
        or _IL2CPP_VALUE_DELTA.search(s)
        or _IL2CPP_INTERACTION_TEMPLATE.match(s))


# ── 引擎日志/异常消息判定（B4 吸收层）─────────────────────────
# il2cpp 引擎字符串（异常消息/调试日志/渲染 Pass/Input System 绑定/
# URP Debug 面板/TMP 处理步骤/着色器路径/构造串/物理按键名）是确定性
# 形态，真实游戏显示文本中几乎不可能完整出现。命中 → skipped（reason=
# engine_log_message + 限量样本留档），不产生 pending——KoiKoi 实证
# 1095 条 pending 全 low（引擎日志污染）→ 自动翻译池空 → 每批 1-2 条
# 慢翻译（识别 B4/E3）。未被命中的留档条目仍是哑信号：宁可多留不可
# 误杀（宁漏勿坏），真实游戏文本（'Koi Koi'/'Boar Deer Butterfly'）
# 必须保留为可译条目。
_ENGINE_QUOTE_JUDGE = re.compile(
    r"""^['"][^'"]{1,60}['"]\s+
        (?:is|are|was|were|has|have|cannot|can't|does not|do not|should not|
           must not|must|not|missing|already|demands?|requires?|
           not found|not valid|not supported|not allowed|not present)""",
    re.I | re.X)
_ENGINE_PAREN_HEAD = re.compile(r'^\([^)]{0,50}\)')
_ENGINE_HEX_FMT = re.compile(r'0x\{|\\u\{|\\U\{')
_ENGINE_FMT_ONLY_HEAD = re.compile(r'^\{0')
_ENGINE_ERRWORD = re.compile(
    r'\b(?:cannot be|can not be|can\'t|cannot|must not|must be|should not|'
    r'is not supported|not supported|is not allowed|not allowed|is not valid|'
    r'not valid|not found|not present|does not exist|not exist|not implemented|'
    r'is not|are not|was not|were not|has not|have not|does not|do not|'
    r'could not|would not|failed|failure|error|invalid|undefined|'
    r'null reference|out of range|out of memory|exception|denied|missing|'
    r'unable|insufficient|illegal|malformed|obsolete|deprecated|overflow|'
    r'underflow|corrupt|unavailable|already been|already added|already bound|'
    r'already contains|already exists|too small|too big|too many|too large|'
    r'exceeds|exceeded|attempted|attempting|trying to|requires|required to|'
    r'needs to|should have|mismatch|does not support|should not be|'
    r'cannot be called|not be called|is not a|are not a|must not be|'
    r'can not be|null or empty|has no name|must implement|can not|'
    r'is null|cannot be null|can not be null|is read-only|is of a fixed size|'
    r'was probably not|not be blitted|not allocated|not dynamic|'
    r'belongs to a different domain|can only be stopped|only be stopped|'
    r'not be called on|is not dynamic|not be null)\b', re.I)
# 引擎日志特征词（无显式 errword 但形态确定）
# B4 补充（2026-09-02，KoiKoi 实证）：A buffer must be provided /
# Coroutine container not configured / No receiver for uri / Maximum
# event size is / Only one XR display is supported 等异常消息被该正则吸收
# （errword 已含 is read-only/cannot be null 等），B4 吸收层在模板分类
# 之前——KoiKoi 102 条 pending 仍全 low 是**宁漏勿坏**的保守结果
# （识别宁严勿漏：宁可多留不可误杀真实文本）。此层收紧会误杀真实显示
# 模板（HP: {0}/{1} 等），故不在此处扩正则，留档条目由人工/后续扫描
# 处理。
_ENGINE_LOGLINE = re.compile(
    r'\b(?:unexpected|expected|sequence contains|path is empty|list is empty|'
    r'path is too long|null key|short read|no module|has never been assigned|'
    r'is no longer valid|reserved by the system|this instance is read only|'
    r'actual value was|value was|values are|straddling|would overrun|'
    r'is beyond array size|smaller than lowLimit|greater than highLimit|'
    r'has been closed|has been reset|must complete|may not be used|'
    r'is positioned before|not be called twice|call Convert|call Encoder|'
    r'no factory that accept|no registered factory|multiple base layouts|'
    r'returned null when invoked|is not supported in universal|'
    r'is reserved to|reserved to CreateWrapper|dont know how to convert|'
    r'may not be properly initialized|one of the paths contains|'
    r'is a directory|is a null|invalid data|is not a valid|'
    r'cannot be created|no data is available|not available|'
    r'does not have|does not contain|does not support|can not be|'
    r'more than (?:byte|ushort|int|long)\.|no assembly|no action|'
    r'no map for|no memberinfo|no shader for|no valid rank|no assembly id|'
    r'no assembly information|unsupported (?:dropPosition|virtualizationMethod|'
    r'rendertargetmode|pipeline)|not be called on|no longer valid|'
    r'value not initialized|no data is available|no module|'
    r'cannot be created|not exist|does not exist|no map|'
    r'could not be found|not found|not supported|not allowed|not valid|'
    r'not present|not implemented|not allocated|not dynamic|not be null)\b', re.I)
_ENGINE_PASS = re.compile(
    r'^(?:Additional|Universal|URP|Built-in|Screen|Global|Local|Forward\+?|'
    r'Deferred|GBuffer|DepthOnly|DepthNormals|Draw|Copy|Clear|Final|HDR|XR|'
    r'Color Lut|Transparent|Capture|Volumetric|Reflection|Post|Lit)\s+'
    r'[A-Za-z0-9 ]*?(?:Pass|Blit|Buffer|Atlas|Map|Mode|Settings|Prepass|Setup|'
    r'Upload|Complete|DebugView|Luminance|Pipeline|Info|Preset|Mesh|Camera|'
    r'Occlusion|Mirror|Fog|Shadowmap|Wireframe|Grid|Line|Culling|Tile|Z-Bin)'
    r'(?:\s+Pass)?$')
# 不带裸 "Display"/"Touch" 前缀（会误杀真文本 'Display'/'Touch'），
# 只留组合词（Display Index / Touch Tap…）
_ENGINE_RENDER_FIELD = re.compile(
    r'^(?:Albedo|Anisotropic|Ambient|Animation|Any Key|Caps Lock|Capture|'
    r'Clip|Click Count|Context Menu|Debug Menu|Default|Delta|Display Index|'
    r'Dropdown List|Hue Tolerance|Value Range|Vertex|Volume|Validation|'
    r'Down Arrow|Up Arrow|Page Up|Page Down|Context|Input: |Begin |End |'
    r'Frame|Clear|Update (?:Bindings|Hierarchy|Layout|Rendering|Style|ViewData)'
    r'|Input System|Cinemachine|Daydream HMD|Editor Camera)\s*(?:[A-Z].*)?$')
_ENGINE_CSS_ENUM = re.compile(r'^[a-z][a-z-]*(\s*\|\s*[a-z][a-z-]*)+$')
_ENGINE_CSS_TERM = re.compile(r'^(?:flex|none|auto|stretch-to-fill|scale-and-crop|'
    r'scale-to-fit|nowrap|wrap|wrap-reverse|repeat-x|repeat-y|space-between|'
    r'space-around|space-evenly|center|start|middle|end|upper-left|middle-left|'
    r'lower-left|upper-center|middle-center|lower-center|upper-right|'
    r'middle-right|lower-right|padding-box|content-box|border-box|visible|hidden|'
    r'relative|absolute|normal|italic|bold|bold-and-italic|column|row|'
    r'column-reverse|row-reverse|cover|contain|scroll|ease[^ ]*|'
    r'length-percentage|max-content|min-content|auto-fit|auto-fill)')
_ENGINE_KV_FMT = re.compile(
    r'^(?:[a-zA-Z][a-zA-Z0-9_.]*[{=]\s*\{0\})|(?:bool\d*\(|float\d*\(|uint\d*\(|'
    r'int\d*\(|double\d*\()|^typeof\(|^button\{0\}')
_ENGINE_DATE_CULTURE = re.compile(
    r'^(?:dddd|yyyy|h:mm tt|ISO-8859|collation\.|Expected hex|Expected \{0x|'
    r'Gregorian Calendar|Lucida Grande)')
_ENGINE_CORE_MSG = re.compile(
    r'^(?:generic args|not a generic|index \+ count|index \+ length|'
    r'method arguments|method return|index < lower bound|length < 0|'
    r'id attribute|mode attribute|url attribute|routine is null|'
    r'null resource|callback parameter|native handle|eventPtr|'
    r'custom styles is null|context\.currentElement|'
    r'Assembly qualifed|Use of unassigned|Count not parse|'
    r'Inconsistent state during|Indices allocated|Vertices allocated|'
    r'Collection is of a fixed|Collection is read-only|'
    r'Enumeration already finished|GCHandle value belongs|'
    r'GameObject parameter|Cant be Guid|Can not add properties|'
    r'Can not call MakeByRef|Creating renderers|Color attachment|'
    r'Depth attachment|Content of previous|Converting PrimitiveValue|'
    r'Copying bitfields|Coordinate outside|Coroutines can only|'
    r'CreateClipFromPlayableAsset|Created Texture|Daydream HMD|'
    r'DefaultDimensionForChannel|Derived classes must|Device has no|'
    r'Disk full|Don\'t know how to convert|Duplicate device|'
    r'Empty usage entry|Expected a|Expected an|Expecting|'
    r'File name:|GetUVChannel called|History buffer has been|'
    r'Incompatible Delegate|Incorrect length|Index should|'
    r'InsertChildControl|Input System not yet|Interlocked.CompareExchange|'
    r'InvalidOperationException|Key {0}|Layout override|'
    r'Cannot advance|Cannot add|Cannot begin|Cannot cast|Cannot change|'
    r'Cannot convert|Cannot create|Cannot delete|Cannot find|'
    r'Cannot set|Cannot open|Cannot perform|Cannot seek|'
    r'Can\'t|Couldn\'t|Cannot|Array type can not|Array spec cannot)')
_ENGINE_GPU = re.compile(r'^[A-Za-z0-9. ()-]*\(TM\)[A-Za-z0-9 .()/-]*$')
_ENGINE_MATERIAL = re.compile(
    r'^(?:Black|White|Dark|Dry|Fresh|Green|Blue|Red|Yellow|Grey|Gray|'
    r'Worn|Wet|Light|Heavy|Soft|Hard|Deep|Bright|Pale|Rich|Vivid|'
    r'Muddy|Dirty|Clean|Smooth|Rough|Grainy|Polished|Matt|Glossy|Satin)\s+'
    r'[A-Za-z]+(?: [A-Za-z]+){0,2}$')
_ENGINE_PROFILER = re.compile(
    r'^(?:CPU|GPU|Render|Begin|End|Frame|UI|URP|XR|Input|Touch|'
    r'Animation|Particle|Physics|Audio|Video|Timeline)\s+[A-Z]')
_ENGINE_UNKNOWN = re.compile(r'^Unknown\s+[A-Za-z]')
_ENGINE_TYPE_MSG = re.compile(r'^Type\s+[A-Za-z{]')
_ENGINE_INPUT_FIELD = re.compile(
    r'^(?:Display Index|Touch Tap|Touch Position|Touch Pressure|Touch Radius|'
    r'Touch Delta|Touch Start)\s+[A-Za-z]')
_ENGINE_WORDS = re.compile(
    r'^(?:Any Key|Caps Lock|Context Menu|Debug Menu|Dropdown List|'
    r'Click Count|Display Index|Default Value|Value Range Min|Value Range Max|'
    r'Hue Tolerance|Clip Parameters|Validation Preset|Volume Info|'
    r'Button \{0\}|Page Up|Page Down|Down Arrow|Up Arrow|Left Arrow|Right Arrow)')
_ENGINE_QUOTE_TAIL = re.compile(
    r'\b(?:denied|not found|not valid|not supported|is null|must not be|'
    r'not allowed|already exists|does not exist|exceeded|missing)\b$', re.I)
# Input System 绑定名 / 输入设备 / 物理键
_ENGINE_INPUT_BINDING = re.compile(
    r'^(?:Primary Touch(?:[ A-Za-z]|$)|Scroll (?:Up|Down|Left|Right|Wheel|Lock)$|'
    r'Numpad [0-9]$|Numpad Enter$|Middle Button$|Print Screen$|'
    r'System Normal$|Oculus (?:HMD|Remote)$|PLAYSTATION\(R\)[0-9] Controller$|'
    r'Mouse [XY]$|Radius [XY]$|Left (?:Alt|Button|Control|Shift|System|Windows)$|'
    r'Right (?:Alt|Button|Control|Shift|System|Windows)$)')
# TMP 处理步骤（profiler 面板名）
_ENGINE_TMP_PREFIX = re.compile(
    r'^TMP(?: Calculate| Compute| Generate| Handle| Layout| Lookup| Parse|'
    r' Save| Add| Set)[ A-Za-z&]*$|^TMP GenerateText - Phase [IVX]+$')
# URP/内置着色器路径
_ENGINE_SHADER_PATH = re.compile(
    r'^(?:Universal Render Pipeline/|Text/Mobile/|Hidden/|Custom/)')
# URP/渲染 Debug 面板项
_ENGINE_DEBUG_PANEL = re.compile(
    r'^(?:Rendering Debug|Lighting Debug Mode|Lighting Debug Modes|'
    r'Lighting Features|Material Validation|Material Validation Mode|'
    r'Material Filters|Material Override|Overdraw Mode|Pixel Validation|'
    r'Pixel Validation Mode|Pixel Range Settings|TAA Debug Mode|'
    r'Stencil Volume|Motion Vector Pass|Additional Wireframe Modes?|'
    r'Map Size|Map Overlays|Max Luminance|Min Luminance|Max Value|Min Value|'
    r'Saturation Tolerance|Generate HDR DebugView CIExy|Main Shadowmap|'
    r'Main Light Shadowmap|Set Additional Shadow Globals|Set GBuffer Globals|'
    r'Set Global Copy Color|Set Global Copy Depth|Set Main Shadow Globals|'
    r'Setup Additional Shadows|Setup Camera Parameters|Setup Global Depth|'
    r'Setup Light Constants|Setup Main Shadowmap|Sort Render Passes|'
    r'Copy Color$|Copy Depth$|RenderGraph Resources|OnRenderObject Callback Pass|'
    r'NativeRenderPass[ A-Za-z]*|Target Color$|Present limited$|'
    r'Stencil Id:\{0\}|Interpolated Value$|Track Parameters$|'
    r'New Concrete$|Old Concrete$|Shared UI Mesh$|'
    r'StylePropertyAnimation Update$|Specify a list of supported pipeline$|'
    r'Resize To Fit$|LineBreaking (?:Following|Leading) Characters$|'
    r'NameInfo Pool$|SerObjectInfo Pool$|ObjectReader Object Stack$|'
    r'ValueType Fixup Stack$|VFX Process Camera$|Max Overdraw Count$|'
    r'Metallic Settings$|Setup Global Copy Depth$|Navigating to specific array element$|'
    r'Negative (?:bitOffset|byteOffset|sizeInBits)$|Null create delegate$|'
    r'Iterated beyond end$)')
# 构造串（Input System 处理器/向量数学 ToString）
_ENGINE_CTOR_STR = re.compile(
    r'^(?:AxisDeadzone|Clamp|InvertVector2|InvertVector3|Normalize|Scale|'
    r'ScaleVector2|ScaleVector3|StickDeadzone|RectOffset|RGBA|float2x2|float4x4|'
    r'System\.ReadOnlySpan|System\.Span|UsagePage|Synchronize with|parsing)')
# 非拉丁文字符表/日历（希伯来/阿拉伯历法名）
_ENGINE_SCRIPT_RTL = re.compile(r'^[֐-׿]|^[؀-ۿ]')
# CSS 语法片段
_ENGINE_CSS_SYNTAX = re.compile(r'^\[ <length-percentage>|^\[StyleSelectorPart')
# 数学构造串（Normalize(min={0},max={1})…）
_ENGINE_MATH_FMT = re.compile(r'^[A-Za-z]+\([a-zA-Z]+=\{\d')
# 含占位符的引擎格式模板（KoiKoi 实证补充）：带引号占位符/异常形态词
_ENGINE_FMT_QUOTE_PH = re.compile(r"""['"][{]\d+""")
_ENGINE_FMT_MSG_WORD = re.compile(
    r"(?:violation on path|fault on path|Win32 IO returned|"
    r"doesn't exist in the shader|zero-size state buffer|"
    r"generic parameter\(s\)|RTHandle\.Initialize|RenderGraphTexture_|"
    r"Unused Layer|Parameter name:|Object name:|User #\{|"
    r"XRSystem setup|borders \{1\} are overridden|"
    r"is modifying the child of another control|"
    r"was null\.|Nested quantifier|declared:|created:|"
    r"inherit from \{0\}|Unrecognized escape|state block|"
    r"should only be called once|\{0:D2\}|0\.00 MB)", re.I)
# 完整句子形态引擎异常消息的前缀词（非占位符、句号结尾判定用）：
# 真实游戏显示文本几乎不以这些前缀词开头（"Index was outside…" /
# "The argument was out of range." / "An exception was thrown"）。
_ENGINE_SENTENCE_HEAD = re.compile(
    r"^(?:The |An |A |Only |Cannot |Can't |Could not |Couldn't |"
    r"Please |You |Index |Array |Invalid |Not |Value |"
    r"Object |Operation |Specified |Argument |Cannot convert |"
    r"Nullable |Out of |Zero |This operation|Stream |String |"
    r"At least one|All rows|More than one|No |Unknown |"
    r"Collection |Culture |Guid |Dashes |End of |Mapping |"
    r"Non-|Positive |Requested |Rethrow |Some platforms|"
    r"Either the |Enumeration |Event |Frame |GarbageCollector |"
    r"Hashtable's |Hour, |IAsyncResult |Incomplete |Index and |"
    r"JNI: |Larger than|Maximum |MemberInfo |Mesh |Method may |"
    r"More than|Must specify|No Era|No EventSystem|No Hanafuda|"
    r"Non existent|Not a |Not enough|Number |Object contains|"
    r"Object reference|Object synchronization|Offset and |"
    r"One or more|Only Base|Only FieldInfo|Only cameras|"
    r"Only directional|Only one|Only readonly|Only single |"
    r"Only the |Opacity |Operations that|Overlays |Override |"
    r"Panel has no|Parent of |Passed in |Please assign |"
    r"Quantifier |Queue |RTHandle |Released |Renderer at |"
    r"RenderGraph: |Runtime cursors|Select which|Set the size|"
    r"Sprite was changed|Stack |Stencil changes|Stream was |"
    r"String reference|Task\.ContinueWith|Task: |The added |"
    r"The binary data|The calling thread|The camera: |"
    r"The event |The field handle|The given |The index |"
    r"The key |The keys |The name can|The number of|"
    r"The operation |The property handle|The rendering pipeline|"
    r"The semaphore |The serialization|The source |The specified |"
    r"The stream |The sum of |The supplied |The task |"
    r"The tasks |The timeout |The type |The UTC |The wait |"
    r"There can be only one|This operation|Thread tracking|"
    r"Thread was |Time is |TimeSpan |Total |TwoPaneSplitView|"
    r"TypedReference|UIR |Unclosed |Unimplemented|Unrecognized |"
    r"Unterminated |Uri already|Use of |Use the |"
    r"Validate a |Validate using|Value has |ValueFactory |"
    r"Visual |Waithandle |Warning: |When supplying|Xform |"
    r"Year, |You can only|You must specify)", re.I)
# 无显式 errword 但确定的引擎日志（KoiKoi 实证补充）
_ENGINE_SENTENCE_LOG = re.compile(
    r'^(?:A context property did not approve the candidate context for activating the object|'
    r'Nested animation tracks should never be asked to create a graph directly|'
    r'Only point, spot and directional shadow casters are supported in universal pipeline|'
    r'VisualElementAsset has a RuleIndex but no inlineStyleSheet|'
    r'far clip plane|near clip plane|field of view|'
    r'itemHeight, item-height|long item support|'
    r'Unmatched \'\]\' while parsing generic argument assembly name|'
    r'Unrecognized escape sequence \\\\\{0\}|'
    r'Value should range from \{0\} to \{1\}, but was \{2\}|'
    r'[a-zA-Z ]* \{0\} [a-zA-Z ]* (?:buffer|path|was null|doesn.t exist|no longer valid)$)')


def _is_engine_log_message(s: str) -> bool:
    """引擎异常/日志消息判定（B4 吸收层）。

    引擎字符串是确定性形态（异常语义词/渲染 Pass/Input System 绑定/
    调试面板/TMP 处理步骤/着色器路径/构造串/物理按键名），真实游戏显示
    文本中几乎不可能完整出现。命中的条目绝不应进翻译池（识别 B4：
    il2cpp 引擎字符串污染 KoiKoi 1095 条 pending 全 low → 自动翻译池
    空）。未被命中的留档条目仍是哑信号——宁可多留不可误杀（宁漏勿坏），
    真实游戏文本（'Koi Koi'/'Boar Deer Butterfly'）必须保留为可译条目。
    """
    st = s.strip()
    if not st:
        return False
    # 含格式占位符的串优先：显示模板（HUD 冒号/比值/加减值/富文本）是
    # 真实游戏渲染文本，绝不能被引擎判定吸收（HP: {0}/{1} 等）。引擎
    # 格式模板带引号占位符（"'{0}'"）或异常形态词（violation/parameter
    # name/was null…）才吸收——真实显示模板两者皆不具备。
    if _IL2CPP_FORMAT_PLACEHOLDER.search(st):
        if _is_display_template(st):
            return False
        if (_ENGINE_FMT_QUOTE_PH.search(st)
                or _ENGINE_FMT_MSG_WORD.search(st)
                or _ENGINE_KV_FMT.match(st)):
            return True
        # 引擎异常消息含显式 errword（"Invalid token '{0}'…" 的 Invalid）
        if _ENGINE_ERRWORD.search(st) or _ENGINE_LOGLINE.search(st):
            return True
        # 其余含占位符串：不在此层判定（流入后续分类——格式模板 → low
        # 留档 / 显示模板 → medium 可自动翻译）
        return False
    if _ENGINE_QUOTE_JUDGE.match(st) or _ENGINE_PAREN_HEAD.match(st):
        return True
    if _ENGINE_HEX_FMT.search(st) or _ENGINE_FMT_ONLY_HEAD.match(st):
        return True
    if _ENGINE_ERRWORD.search(st) or _ENGINE_LOGLINE.search(st):
        return True
    # 无占位符的完整句子形态异常消息（"Index was outside…"）：句号结尾
    # + 引擎前缀词。带占位符的定位串（'Type {0} NameID {1} InstanceID {2}'
    # 'XR Pass {0} Cull {1}'）被误判 display/low 留档（KoiKoi 实证）——
    # 识别是宁漏勿坏（宁可多留不可误杀），此处不再收紧，让它们流回
    # 留档而非吸收（防真实显示模板误杀）。
    # 多词完整句子形态（非占位符）：句号结尾 + 引擎前缀词（The/An/Only/
    # Cannot/Please/You/Index/Array/Invalid/Not/Value/…）= 异常消息主形态
    # （"Index was outside the bounds of the array."）——真实游戏显示文本
    # 在资源而非 metadata 字面量，句子形态只是「可能」而非证据，吸收
    # 宁漏勿坏（宁可多留不可误杀：完整真实对话句通常以动词/人称开头，
    # 非此前缀词，且无句号）。
    if (st[-1] == "."
            and _ENGINE_SENTENCE_HEAD.match(st)):
        return True
    if (_ENGINE_PASS.match(st) or _ENGINE_RENDER_FIELD.match(st)
            or _ENGINE_CSS_ENUM.match(st) or _ENGINE_CSS_TERM.match(st)
            or _ENGINE_KV_FMT.match(st) or _ENGINE_DATE_CULTURE.match(st)
            or _ENGINE_CORE_MSG.match(st) or _ENGINE_GPU.match(st)
            or _ENGINE_MATERIAL.match(st) or _ENGINE_PROFILER.match(st)
            or _ENGINE_UNKNOWN.match(st) or _ENGINE_TYPE_MSG.match(st)
            or _ENGINE_INPUT_FIELD.match(st) or _ENGINE_WORDS.match(st)
            or _ENGINE_INPUT_BINDING.match(st) or _ENGINE_TMP_PREFIX.match(st)
            or _ENGINE_SHADER_PATH.match(st) or _ENGINE_DEBUG_PANEL.match(st)
            or _ENGINE_CTOR_STR.match(st) or _ENGINE_SCRIPT_RTL.match(st)
            or _ENGINE_CSS_SYNTAX.match(st) or _ENGINE_MATH_FMT.match(st)
            or _ENGINE_SENTENCE_LOG.match(st)):
        return True
    if _ENGINE_QUOTE_TAIL.search(st):
        return True
    return False

# 控制符/≥2 空白开头 = 调试输出（'\ndepth: '、'  .locals '、字符表片段）
_IL2CPP_LEADING_WS = re.compile(r"^[\t\r\n]|^[ \t]{2,}")
_MIN_LITERAL_LEN = 3
# 识别 L3：metadata 字符串区（header 0x18/0x1C，Il2CppDumper 跨
# v24-v31 交叉验证的稳定布局）= 类型名/方法名/namespace/字段名全集。
# 字面量与字符串区成员相等是「反射/代码引用键」的确定性证据（typeof/
# GetMethod 参数等运行时按名查找），证据强度高于 is_code_identifier 的
# 形态正则——所以判跳过时细分 reason，且优先于 engine_morph 的长度猜测。
# v39（Unity 6）字符串区偏移未验证（Il2CppDumper 6.7.46 不支持），
# 不启用——待真实样本校准（评估报告 L3 同款措辞）。
_STRING_POOL_VERSION_OK = frozenset({24, 27, 29, 31})
_MAX_POOL_ENTRIES = 2_000_000  # 防畸形区段死循环的上限（正常池远小于此）

# 兼容引用：保留名称，值 = 各版本记录字节数。
SUPPORTED_LITERAL_RECORD_SIZES = {v: cfg[4] for v, cfg in _LAYOUTS.items()}


def find_metadata_file(game_dir: str | Path) -> Path | None:
    """il2cpp_data/Metadata/global-metadata.dat。"""
    game_dir = Path(game_dir)
    for p in game_dir.rglob("global-metadata.dat"):
        return p
    return None


def _has_illegal_controls(text: str) -> bool:
    return any(
        (ord(ch) < 0x20 and ch not in _ALLOWED_CONTROLS)
        or 0x7F <= ord(ch) <= 0x9F
        for ch in text
    )


def parse_string_literals(raw: bytes) -> list[tuple[int, int, int]]:
    """严格解析已验证版本的字面量池 → [(dataIndex, length, dataOffset)]。

    v24/v27/v29/v31 使用 8 字节 <length, dataIndex> 显式长度记录；
    v39 使用 4 字节 <dataIndex> 记录，长度由下一条差值隐含（末条到 data 区尾部）。
    """
    if len(raw) < _MIN_METADATA_HEADER_SIZE:
        return []
    magic, version = struct.unpack_from("<II", raw, 0)
    if magic != METADATA_MAGIC:
        return []
    layout = _LAYOUTS.get(version)
    if layout is None:
        return []
    (lit_off_pos, lit_size_pos, data_off_pos, data_size_pos,
     entry_size, record_mode) = layout
    lit_off, lit_table_size = struct.unpack_from("<II", raw, lit_off_pos)
    data_off, data_size = struct.unpack_from("<II", raw, data_off_pos)
    if ((lit_table_size and lit_off < _MIN_METADATA_HEADER_SIZE)
            or (data_size and data_off < _MIN_METADATA_HEADER_SIZE)):
        return []
    if (lit_table_size % entry_size != 0
            or lit_off + lit_table_size > len(raw)):
        return []
    if data_off > len(raw) or data_size > len(raw) - data_off:
        return []
    lit_table_end = lit_off + lit_table_size
    data_end = data_off + data_size
    if (lit_table_size and data_size
            and max(lit_off, data_off) < min(lit_table_end, data_end)):
        return []
    out: list[tuple[int, int, int]] = []
    if record_mode == "implicit":
        # v39：4 字节 dataIndex 记录，长度 = 下一条差值（末条到 data 区尾部）。
        # Unity 6 在 header 0x10 处新增「记录数」字段，必须与 litSize/4 一致。
        if version == 39:
            declared = struct.unpack_from("<I", raw, 0x10)[0]
            if declared != lit_table_size // entry_size:
                return []
        if lit_table_end > len(raw) or (lit_table_end - lit_off) % entry_size:
            return []
        count = (lit_table_end - lit_off) // entry_size
        indexes = struct.unpack_from(f"<{count}I", raw, lit_off)
        for i, data_index in enumerate(indexes):
            end = indexes[i + 1] if i + 1 < count else data_size
            length = end - data_index
            if length < 0 or data_index > data_size:
                return []
            out.append((data_index, length, data_off + data_index))
    else:
        occupied_ranges: list[tuple[int, int]] = []
        for i in range(lit_table_size // entry_size):
            pos = lit_off + i * entry_size
            if pos + entry_size > lit_table_end or pos + entry_size > len(raw):
                return []
            length, data_index = struct.unpack_from("<II", raw, pos)
            if data_index > data_size or length > data_size - data_index:
                return []
            out.append((data_index, length, data_off + data_index))
            if length:
                occupied_ranges.append((data_index, data_index + length))
        occupied_ranges.sort()
        if any(current_start < previous_end
               for (_, previous_end), (current_start, _) in
               zip(occupied_ranges, occupied_ranges[1:])):
            return []
    valid: list[tuple[int, int, int]] = []
    for data_index, length, data_pos in out:
        if length == 0:
            continue
        try:
            raw[data_pos:data_pos + length].decode("utf-8")
        except UnicodeDecodeError:
            continue
        valid.append((data_index, length, data_pos))
    return valid


def _metadata_string_pool(raw: bytes) -> frozenset[str]:
    """字符串区标识符全集（识别 L3）；布局非法/版本未验证 → 空集。

    解析失败一律降级为空集——分类链保持现状，解析失败不改变既有判定
    （与 L6 `_script_class_of` 同模式）。自校验：偏移/大小在界内、
    逐条 NUL 终结、strict UTF-8 可解码、总数有上限。
    """
    if len(raw) < _MIN_METADATA_HEADER_SIZE:
        return frozenset()
    magic, version = struct.unpack_from("<II", raw, 0)
    if magic != METADATA_MAGIC or version not in _STRING_POOL_VERSION_OK:
        return frozenset()
    str_off, str_size = struct.unpack_from("<II", raw, 0x18)
    if not str_size or str_off < _MIN_METADATA_HEADER_SIZE:
        return frozenset()
    if str_off + str_size > len(raw):
        return frozenset()
    blob = raw[str_off:str_off + str_size]
    names: set[str] = set()
    cursor = 0
    for _ in range(_MAX_POOL_ENTRIES):
        if cursor >= str_size:
            break
        end = blob.find(b"\x00", cursor)
        if end < 0:
            return frozenset()   # 区段尾部无 NUL → 不是字符串数组布局
        try:
            names.add(blob[cursor:end].decode("utf-8"))
        except UnicodeDecodeError:
            return frozenset()
        cursor = end + 1
    else:
        return frozenset()       # 超上限 → 畸形区段，不启用
    return frozenset(names)


def _independent_pool_records(raw: bytes) -> list[tuple[int, int, int]] | None:
    """第二套独立字符串池读取器（交叉验证用，不共享 parse 的防御逻辑）。

    直接按 header 字段硬读记录区：explicit 逐条 <length, dataIndex>，
    implicit 差分（末条 = data_size - dataIndex）。不依赖 occupied_ranges
    重叠防御、不做 UTF-8/非零长过滤——返回记录区全部条目
    [(data_index, length, data_pos)]；布局非法返回 None。

    用途：写回前的「同源盲区」防御——提取/重开验证与 parse 共用同一套
    解析代码，若对布局有系统性误解，自证失效。独立读取器以不同代码路径
    重新推导全部记录，任何解析偏移/截断/防御误放行都会被交叉核对捕获。
    """
    if len(raw) < _MIN_METADATA_HEADER_SIZE:
        return None
    magic, version = struct.unpack_from("<II", raw, 0)
    if magic != METADATA_MAGIC:
        return None
    layout = _LAYOUTS.get(version)
    if layout is None:
        return None
    (lit_off_pos, lit_size_pos, data_off_pos, data_size_pos,
     entry_size, record_mode) = layout
    lit_off, lit_size = struct.unpack_from("<II", raw, lit_off_pos)
    data_off, data_size = struct.unpack_from("<II", raw, data_off_pos)
    if not lit_size or not data_size:
        return None
    if lit_off + lit_size > len(raw) or data_off + data_size > len(raw):
        return None
    out: list[tuple[int, int, int]] = []
    if record_mode == "implicit":
        count = lit_size // entry_size
        if not count or lit_off + count * 4 > len(raw):
            return None
        indexes = struct.unpack_from(f"<{count}I", raw, lit_off)
        for i, data_index in enumerate(indexes):
            end = indexes[i + 1] if i + 1 < count else data_size
            if data_index > data_size or end < data_index:
                return None
            out.append((data_index, end - data_index, data_off + data_index))
    else:
        for i in range(lit_size // entry_size):
            pos = lit_off + i * entry_size
            if pos + entry_size > len(raw):
                return None
            length, data_index = struct.unpack_from("<II", raw, pos)
            if data_index > data_size or length > data_size - data_index:
                return None
            out.append((data_index, length, data_off + data_index))
    return out


def _cross_validate_pool(raw: bytes) -> bool:
    """parse_string_literals 与独立读取器交叉核对。

    规则：独立读取器全部条目中「非零长且 strict UTF-8 可解码」的子集
    必须与 parse 的 valid 结果逐条一致（顺序与数量一致）。两套代码路径
    独立推导，任一偏移/过滤错误即不一致。
    """
    parsed = parse_string_literals(raw)
    independent = _independent_pool_records(raw)
    if independent is None:
        return False
    expect: list[tuple[int, int, int]] = []
    for data_index, length, data_pos in independent:
        if length == 0:
            continue
        try:
            raw[data_pos:data_pos + length].decode("utf-8")
        except UnicodeDecodeError:
            continue
        expect.append((data_index, length, data_pos))
    return expect == parsed


def metadata_data_layout(raw: bytes) -> tuple[int, int, str] | None:
    """(data_off, data_size, record_mode)；解析失败返回 None。

    供写回侧把「提取时记录的 file_offset」换算成 data_index，以及断言
    写回范围。与 parse_string_literals 共用同一版本白名单。
    """
    if len(raw) < _MIN_METADATA_HEADER_SIZE:
        return None
    magic, version = struct.unpack_from("<II", raw, 0)
    layout = _LAYOUTS.get(version)
    if layout is None:
        return None
    (_lit_off_pos, _lit_size_pos, data_off_pos, data_size_pos,
     _entry_size, record_mode) = layout
    data_off = struct.unpack_from("<I", raw, data_off_pos)[0]
    data_size = struct.unpack_from("<I", raw, data_size_pos)[0]
    if not data_size or data_off >= len(raw) or data_size > len(raw) - data_off:
        return None
    return data_off, data_size, record_mode


def patch_metadata_strings(raw: bytes, changes: dict[int, bytes]) -> bytes:
    """按 data_index 原位替换字面量数据,并同步修复长度语义(尾部 NUL 修复)。

    explicit(v24/27/29/31):记录区 <length> 字段更新为译文实际字节数,数据原位;
    运行时按记录长度读取 = 译文,不再带尾部 NUL 填充。剩余容量区域保持原字节
    (不被任何记录引用,运行时按更新后的 length 读取)。

    implicit(v39):没有 length 字段,每条长度由「下一条 dataIndex 差值」决定
    (末条 = data_size - dataIndex)——收缩后若不前移后续记录,运行时读到的
    长度仍是旧值,尾部残留照样进字符串。全部记录连续紧凑排列到数据区头部
    (记录间零间隙,每条差分长度 = 下一条差值 = 实际字节数),并同步改小
    header 的 dataSize 字段(= 新总长)——末条差分以 data_size 为锚,若不
    改小,空洞(原数据区尾部)会被末条差分吞进字符串(尾部 NUL 复现);
    改小后空洞落在数据区声明之外,不被任何记录引用。数据区之后的物理字节
    原位保留,其他区段按各自显式 offset 定位,不受 dataSize 影响。
    全部 dataIndex 链式更新,记录数不变。

    explicit(v24/27/29/31):记录 <length> 字段显式,数据原位覆盖,无差分
    问题,dataSize/header 一律不动。两种模式都不改记录数/其他区偏移。
    """
    if not changes:
        return raw
    if len(raw) < _MIN_METADATA_HEADER_SIZE:
        raise ValueError("metadata 文件过短,无法补丁")
    magic, version = struct.unpack_from("<II", raw, 0)
    if magic != METADATA_MAGIC:
        raise ValueError("非 global-metadata.dat(magic 不匹配)")
    layout = _LAYOUTS.get(version)
    if layout is None:
        raise ValueError(f"不支持的 metadata 版本: {version}")
    (lit_off_pos, lit_size_pos, data_off_pos, data_size_pos,
     entry_size, record_mode) = layout
    lit_off, lit_table_size = struct.unpack_from("<II", raw, lit_off_pos)
    data_off, data_size = struct.unpack_from("<II", raw, data_off_pos)
    if not lit_table_size or not data_size:
        raise ValueError("metadata 字面量表或数据区为空")
    records = parse_string_literals(raw)
    if not records:
        raise ValueError("metadata 字面量池无法解析,拒绝补丁")
    # 同源盲区防御：提取/重开验证与 parse 共用同一套解析，若对布局有
    # 系统性误解则自证失效——写回前用第二套独立读取器交叉核对
    if not _cross_validate_pool(raw):
        raise ValueError("metadata 字面量池交叉验证不一致,拒绝补丁")
    by_index = {data_index: (length, data_pos)
                for data_index, length, data_pos in records}
    for data_index, payload in changes.items():
        if data_index not in by_index:
            raise ValueError(f"data_index {data_index} 不在字面量记录表中")
        if len(payload) > by_index[data_index][0]:
            raise ValueError(
                f"译文 {len(payload)} 字节超过容量 "
                f"{by_index[data_index][0]}（data_index={data_index}）")
    blob = bytearray(raw)
    if record_mode == "explicit":
        # 记录区全部条目按 data_index 索引——绝不能用 valid 记录序号推算
        # 记录区位置：parse 的 UTF-8/非零长过滤会破坏「序号 ↔ 记录区位置」
        # 的对应（cosl 实证：15170 条目 1 条被过滤 → length 字段写错条目
        # → 重开区间重叠）。同一 data_index 可能有多条记录（空字符串），
        # 全部同步更新 length 字段。
        pos_of: dict[int, list[tuple[int, int]]] = {}
        for i in range(lit_table_size // entry_size):
            pos = lit_off + i * entry_size
            length, data_index = struct.unpack_from("<II", blob, pos)
            pos_of.setdefault(data_index, []).append((pos, length))
        for data_index, payload in changes.items():
            _length, data_pos = by_index[data_index]
            blob[data_pos:data_pos + len(payload)] = payload
            # explicit 记录 = <length, dataIndex>：length 在记录起点。
            # 同一 data_index 可有多条记录（空字符串 length=0 与实数据
            # 共享偏移，运行时读空）——只更新 length>0 的实际条目：空
            # 记录保持 0 才不新增区间重叠（parse 防御检查不误伤），且
            # 运行时语义不变（空记录仍读空字符串）。
            for pos, rec_len in pos_of.get(data_index, ()):
                if rec_len > 0:
                    struct.pack_into("<I", blob, pos, len(payload))
    else:
        # implicit：记录区全部条目（含 parse 过滤掉的空/非 UTF-8 记录）
        # 参与紧凑重建——valid 过滤掉的条目若被丢下，记录区残留旧
        # dataIndex，dataSize 缩小后越界（minato 实证：末 2 条残留
        # 528463/528466 > 新 dataSize → 重开解析整体拒绝）。
        # 全部记录连续紧凑到数据区头部（零间隙），差分长度含空洞原样
        # 搬运（运行时读取语义不变），末条差分以 data_size 为锚 → 同步
        # 改小 dataSize 字段，空洞（原数据区尾部）落在数据区声明之外，
        # 清零保持确定性。全部 dataIndex 链式更新，记录数不变。
        count = lit_table_size // entry_size
        indexes = struct.unpack_from(f"<{count}I", blob, lit_off)
        new_indexes: dict[int, int] = {}
        cursor = 0
        for i, data_index in enumerate(indexes):
            end = indexes[i + 1] if i + 1 < count else data_size
            length = end - data_index
            payload = changes.get(data_index)
            new_len = len(payload) if payload is not None else length
            new_indexes[data_index] = cursor
            if payload is not None:
                blob[data_off + cursor:data_off + cursor + new_len] = payload
            else:
                blob[data_off + cursor:data_off + cursor + new_len] = (
                    raw[data_off + data_index:data_off + end])
            cursor += new_len
        if cursor > data_size:
            raise ValueError("metadata 数据区溢出：紧凑重建超出 data_size")
        blob[data_off + cursor:data_off + data_size] = (
            b"\x00" * (data_size - cursor))
        struct.pack_into("<I", blob, data_size_pos, cursor)
        for i, data_index in enumerate(indexes):
            struct.pack_into("<I", blob, lit_off + i * entry_size,
                             new_indexes[data_index])
    _assert_diff_whitelist(
        raw, blob, record_mode=record_mode, lit_off=lit_off,
        lit_table_size=lit_table_size, data_off=data_off,
        data_size=data_size, data_size_pos=data_size_pos,
        entry_size=entry_size, changes=changes, by_index=by_index,
        cursor=cursor if record_mode == "implicit" else None)
    return bytes(blob)


def _assert_diff_whitelist(raw: bytes, blob: bytearray, *, record_mode: str,
                           lit_off: int, lit_table_size: int, data_off: int,
                           data_size: int, data_size_pos: int, entry_size: int,
                           changes: dict[int, bytes],
                           by_index: dict[int, tuple[int, int]],
                           cursor: int | None = None) -> None:
    """写回差异白名单：patch 前后逐字节 diff，所有差异必须落在合法变更
    范围内——header 其他字段、其他区段（方法名表/类表等游戏逻辑所在）
    零字节被碰（「不影响游戏」的硬保证）。

    explicit：允许差异 = 被改记录的数据段 [data_pos, data_pos+len(payload))
    + 记录区 length 字段（记录起点 4 字节，仅 length>0 条目——与补丁
    逻辑同判据，空记录不更新）。
    implicit：允许差异 = 数据区 [data_off, data_off+cursor)（紧凑重建）
    + dataSize 字段 + 记录区全部条目（链式更新）。
    """
    patched = bytes(blob)
    if len(patched) != len(raw):
        raise ValueError(f"写回改变了文件长度 {len(raw)} -> {len(patched)}")
    if record_mode == "explicit":
        allowed: set[int] = set()
        for data_index, payload in changes.items():
            _length, data_pos = by_index[data_index]
            allowed.update(range(data_pos, data_pos + len(payload)))
            for i in range(lit_table_size // entry_size):
                pos = lit_off + i * entry_size
                length, di = struct.unpack_from("<II", raw, pos)
                if di == data_index and length > 0:
                    allowed.update(range(pos, pos + 4))
        bad = [i for i in range(len(raw))
               if raw[i] != patched[i] and i not in allowed]
    else:
        if cursor is None:
            raise ValueError("implicit 白名单需要 cursor")
        # 数据区允许范围 = 整个 [data_off, data_off+data_size)：紧凑重建
        # 搬移全部记录 + 空洞（原数据区尾部）清零覆盖数据区全部字节；
        # 白名单的意义是锁定「数据区/记录区/dataSize 之外零字节被碰」
        intervals = [
            (data_off, data_off + data_size),
            (data_size_pos, data_size_pos + 4),
            (lit_off, lit_off + lit_table_size),
        ]
        bad = [i for i in range(len(raw))
               if raw[i] != patched[i]
               and not any(a <= i < b for a, b in intervals)]
    if bad:
        raise ValueError(
            f"写回差异越出白名单 {len(bad)} 处（首例 0x{bad[0]:x}），"
            "文件被意外改动，拒绝")


def extract_metadata_strings(path: str | Path, file_id: str | None = None,
                             progress_cb: Callable | None = None) -> ParsedFile:
    """提取 metadata 字符串字面量 → ParsedFile。"""
    p = Path(path)
    fid = file_id or str(p).replace("\\", "/")
    raw = p.read_bytes()
    entries: list[TextEntry] = []
    skipped: dict[str, int] = {}  # R5 静默跳过留档（哑识别可见化）
    # 识别 L3：字符串区标识符全集（类型名/方法名/namespace 名）——
    # 字面量与它相等是反射/代码引用键的确定性证据；解析失败 → 空集
    # 降级（分类链保持现状）
    metadata_strings = _metadata_string_pool(raw)
    for data_index, length, data_pos in parse_string_literals(raw):
        if data_pos + length > len(raw):
            # R5：记录越界（池损坏/解析器边界）静默跳过留档
            skipped["literal_oob"] = skipped.get("literal_oob", 0) + 1
            continue
        try:
            s = raw[data_pos:data_pos + length].decode("utf-8")
        except UnicodeDecodeError:
            skipped["decode_failed"] = skipped.get("decode_failed", 0) + 1
            continue
        if _has_illegal_controls(s):
            # R5/L1：非法控制字符静默跳过留档（计数 + 限量样本）
            skipped["illegal_controls"] = skipped.get("illegal_controls", 0) + 1
            sample = _skipped_sample_entry(
                fid, f"skip/meta#{data_index}", s, kind="il2cpp",
                reason="illegal_controls",
                count=skipped["illegal_controls"])
            if sample:
                entries.append(sample)
            continue
        # 代码池严格键检测：无空格标识符是枚举名/绑定名，绝不翻译
        if should_skip(s) or is_code_identifier(s) or _is_engine_string(s):
            # R5/L1：代码标识符/引擎串静默跳过留档（计数 + 限量样本）
            skipped["code_identifier"] = skipped.get("code_identifier", 0) + 1
            sample = _skipped_sample_entry(
                fid, f"skip/meta#{data_index}", s, kind="il2cpp",
                reason="code_identifier",
                count=skipped["code_identifier"])
            if sample:
                entries.append(sample)
            continue
        # 识别 L3：确定性反射键——字面量 == metadata 字符串区成员（类型名/
        # 方法名/namespace/字段名）。is_code_identifier 是形态正则猜测，这里
        # 是集合命中的事实证据（typeof/GetMethod 参数等运行时按名查找键），
        # 优先于 engine_morph 的长度猜测（证据分层：确定性 > 形态）。
        if s in metadata_strings:
            skipped["reflection_key"] = skipped.get("reflection_key", 0) + 1
            sample = _skipped_sample_entry(
                fid, f"skip/meta#{data_index}", s, kind="il2cpp",
                reason="reflection_key", count=skipped["reflection_key"])
            if sample:
                entries.append(sample)
            continue
        # 引擎/调试形态：反汇编/日志输出、字符表 → 不产生条目
        # （真实样本 16541 条中 65% 属此类，minato/seijunDROP v24 池）
        if (_IL2CPP_LEADING_WS.match(s) or len(s) < _MIN_LITERAL_LEN
                or not any(ch.isalpha() for ch in s)):
            # R5/L1：引擎/调试形态静默跳过留档（计数 + 限量样本）
            skipped["engine_morph"] = skipped.get("engine_morph", 0) + 1
            sample = _skipped_sample_entry(
                fid, f"skip/meta#{data_index}", s, kind="il2cpp",
                reason="engine_morph", count=skipped["engine_morph"])
            if sample:
                entries.append(sample)
            continue
        # 引擎日志/异常消息判定（B4 吸收层）：il2cpp 引擎字符串（异常
        # 消息/调试日志/渲染 Pass/Input System 绑定/URP 面板/TMP 处理步骤/
        # 着色器路径/物理按键名）是确定性形态，真实游戏显示文本中几乎
        # 不可能完整出现。命中 → skipped（reason=engine_log_message + 限量
        # 样本留档），不产生 pending——KoiKoi 实证 1095 条 pending 全
        # low（引擎日志污染）→ 自动翻译池空 → 每批 1-2 条慢翻译。吸收层
        # 在模板细分类之前（含占位符的引擎消息也吸收），仅放行真实游戏
        # 文本（'Koi Koi'/'Boar Deer Butterfly' 等）。
        if _is_engine_log_message(s):
            skipped["engine_log_message"] = skipped.get(
                "engine_log_message", 0) + 1
            sample = _skipped_sample_entry(
                fid, f"skip/meta#{data_index}", s, kind="il2cpp",
                reason="engine_log_message",
                count=skipped["engine_log_message"])
            if sample:
                entries.append(sample)
            continue
        # 含格式占位符的模板串：#14 实时渲染文本加强——旧逻辑无条件
        # 跳过（注释称「游戏显示文本不具备这些形态」，真实样本
        # 254361268a 证明错误：HUD/飘字模板被哑跳过）。细分类：
        # 显示模板 → display/medium 可自动翻译（质量门禁放行）；
        # 引擎异常消息/键值模板 → display/low 留档可见，不浪费模型
        # 调用（批量引擎消息不进自动翻译）。
        if _IL2CPP_FORMAT_PLACEHOLDER.search(s):
            if _is_display_template(s):
                status, confidence, role, disposition, reason = (
                    "pending", "medium", "display", "translate",
                    "il2cpp_display_template")
            else:
                status, confidence, role, disposition, reason = (
                    "pending", "low", "display", "translate",
                    "il2cpp_format_template")
            entries.append(TextEntry(
                file_id=fid, key_path=f"meta#{data_index}",
                original=s, status=status,
                meta={
                    "kind": "il2cpp", "file_offset": data_pos, "length": length,
                    "confidence": confidence, "role": role,
                    "disposition": disposition, "reason": reason,
                }))
            continue
        # 剩余字面量分类。真实样本验证（minato/seijunDROP 老版池）：
        # 池内容几乎全是引擎字符串（异常消息/属性名/系统库字符表），游戏
        # 显示文本在资源而非代码字面量——句子形态只是「可能」而非证据。
        # - 交互提示形态 → display/medium（可翻译）
        # - 句子形态 → display/low（留档可见、不可自动翻译——质量门禁
        #   is_actionable_translation 要求 confidence≠low）
        # - 其余（词/短语）→ structural/low 留档（「过滤不是删除」）
        interaction = is_strong_interaction_prompt(s)
        # 句末标点策略（宁漏勿坏）：句号结尾 = 引擎异常消息主流形态
        # （"Index was outside the bounds of the array."），不进句子池；
        # 感叹/问号结尾 = 真实游戏对话情绪句（"Let's play another round!"），
        # 剥离后判定字母/数字结尾放行为可译。只对 !? 放松，句号保持
        # 既有结构跳过（真实显示文本在资源而非 metadata 字面量）。
        _sentence_core = s.rstrip(" \t\n\r!?。！？…·")
        sentence_like = (" " in s and s[0].isalpha()
                         and s[-1] not in ".。"
                         and _sentence_core and _sentence_core[-1].isalnum())
        if interaction:
            status, confidence, role, disposition, reason = (
                "pending", "medium", "display", "translate",
                "il2cpp_interaction_prompt")
        elif sentence_like:
            status, confidence, role, disposition, reason = (
                "pending", "low", "display", "translate",
                "il2cpp_sentence")
        else:
            status, confidence, role, disposition, reason = (
                STATUS_SKIPPED, "low", "structural", "structural",
                "il2cpp_literal")
        entries.append(TextEntry(
            file_id=fid, key_path=f"meta#{data_index}",
            original=s, status=status,
            meta={
                "kind": "il2cpp", "file_offset": data_pos, "length": length,
                "confidence": confidence, "role": role,
                "disposition": disposition, "reason": reason,
            }))
    for e in entries:
        if e.status == "pending" and (should_skip(e.original) or is_code_identifier(e.original)):
            e.status = STATUS_SKIPPED
    # 样本计数回写：限量样本的 skipped_count 是累计值，报告聚合需
    # 真实总数（消费端按 (file_id, reason, obj) 取 max）
    _finalize_skipped_counts(entries, skipped)
    noise = looks_like_noise_file(entries)
    return ParsedFile(fid, str(p), "v2_il2cpp", entries, "utf-8", "\n",
                      {"kind": "il2cpp"}, noise, skipped)
