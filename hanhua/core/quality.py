"""译文在落库和字体语料前必须通过的确定性质量门。"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
from string import punctuation
from typing import Iterable, Literal

from hanhua.core.engine_strings import (PHYSICAL_KEY_NAMES_CASEFOLD,
                                        interaction_action_words,
                                        interaction_input_events,
                                        is_interaction_prompt)
from hanhua.core.models import TextEntry
from hanhua.core.placeholders import (DISPLAY_WORDS, FORMAT_TAG_PATTERN,
                                      FUNCTION_WORDS, SAFE_KEEPERS,
                                      _STRIP_RICH_TEXT, is_hipster_ipsum,
                                      validate_translation)
from hanhua.core.knowledge import (_UPPERCASE_ACTION_VERBS,
                                   _is_spaced_action,
                                   _is_uppercase_action)
from hanhua.core.protected_spans import semantic_target_text
from hanhua.core.review_outcome import review_publishable

_DISPLAY_WORDS_CASEFOLD = {word.casefold() for word in DISPLAY_WORDS}
# 输入设备词（方向盘/手柄/摇杆 HUD 语境标记）：原文含任一设备词 → 输入
# 绑定形态，方向词（left/right/up/down）按方向语义强制检查；普通文本
# （pick the right door）无设备词 → 方向词是普通英语词，自由翻译
# （ffs 2083 失败实证：方向盘输入词对污染全局术语表误杀普通文本）
_INPUT_DEVICE_WORDS = (
    "stick", "button", "hat", "pov", "switch", "trigger", "shoulder",
    "wheel", "throttle", "dial", "shifter", "paddle", "pedal", "lever",
    "knob", "rotary",
)
# 方向词 → 译文必须含的方向中文字（left→左、right→右、up→上、down→下）
_DIRECTION_WORD_ZH = {
    "left": ("左",), "right": ("右",), "up": ("上",), "down": ("下",),
}


def _input_binding_context(original: str) -> bool:
    """原文是否输入绑定语境（设备词或 F22-2 键位绑定后缀 `:xxx`）→
    方向词按输入方向语义检查。'Hat Right' 含设备词 Hat → 语境；
    'pick the right door' / 'Right Tilt' 无设备词 → 非语境（方向词
    自由译：正确的/右侧）。"""
    lower = original.casefold()
    if any(re.search(
            rf"(?<![A-Za-z0-9_]){w}(?![A-Za-z0-9_])", lower)
            for w in _INPUT_DEVICE_WORDS):
        return True
    return bool(re.search(r":([a-z]{2,})\s*$", original, re.I))
# 行首星号 bullet 占位符（undertale_bullet 提取的固定串 "* "）：译文
# 新增 bullet 是模型对星号的规范化（"*SIGH*" → "* sigh *"、" *Added"
# → "* 加空格"），无结构风险——placeholder_mismatch 放行专用
_BULLET_PLACEHOLDER = re.compile(r"^\* $")


@dataclass(frozen=True)
class QualityResult:
    passed: bool
    confidence: Literal["high", "medium", "low"]
    reasons: tuple[str, ...]
    normalized_translation: str


_ENGLISH_WORD = re.compile(r"[A-Za-z]{3,}")
# 独立 ASCII 小写词：\b 按 unicode 词边界（á 等非 ASCII 字母算 \w）→
# 'Stefánsson' 的 ASCII 碎片 'nsson' 不是独立词（前邻 á），不会误判小写
_LOWER_ASCII_WORD = re.compile(r"\b[a-z]+\b")
# lorem ipsum 家族占位文本（游戏开发者填充的假拉丁文本，无真实语义）。
# 标记词：标准 lorem ipsum 及其错拼变体（zero-deaths 'Loem iipsum solar'）。
# 判定：原文含任一标记词 + 所有词都在家族表 → 模型回显是合理行为。
_LOREM_IPSUM_MARKERS = {
    "lorem", "loem", "ipsum", "iipsum", "dolor", "sit", "amet",
    "consectetur", "adipiscing", "labore", "dolore", "incididunt",
}
_LOREM_IPSUM_FAMILY = _LOREM_IPSUM_MARKERS | {
    "elit", "sed", "do", "eiusmod", "tempor", "magna", "aliqua", "ut",
    "enim", "ad", "minim", "veniam", "quis", "nostrud", "exercitation",
    "ullamco", "laboris", "nisi", "aliquip", "ex", "ea", "commodo",
    "consequat", "duis", "aute", "irure", "in", "reprehenderit",
    "voluptate", "velit", "esse", "cillum", "fugiat", "nulla", "pariatur",
    "excepteur", "sint", "occaecat", "cupidatat", "non", "proident",
    "sunt", "culpa", "qui", "officia", "deserunt", "mollit", "anim",
    "id", "est", "laborum",
    # zero-deaths 特有错拼变体
    "solar", "em", "demit", "solo", "demmy", "sorenson",
}
def is_lorem_ipsum_placeholder(text: str) -> bool:
    words = [w.casefold() for w in _ENGLISH_WORD.findall(
        SAFE_KEEPERS.sub(" ", text))]
    if not words or not any(w in _LOREM_IPSUM_MARKERS for w in words):
        if is_hipster_ipsum(text):
            return True
        return False
    if all(w in _LOREM_IPSUM_FAMILY for w in words):
        return True
    # 开发占位混合串："The achievement's description goes here ipsum dolor
    # lorem sit amet..."（说明性前缀 + lorem 词，Incremental RTS 真实样本）
    # → 开发者填充文本，无真实语义，模型回显合理
    return bool(re.search(r"\bgoes? here\b", text, re.I)) and len(words) <= 25


def _max_case_run(text: str) -> int:
    """最大连续同大小写段长（'DeAD' → 1、'Continue' → 7）。"""
    run = best = 0
    prev: str | None = None
    for ch in text:
        if not ch.isalpha():
            run = 0
            prev = None
            continue
        cur = "upper" if ch.isupper() else "lower"
        if cur == prev:
            run += 1
        else:
            run = 1
            prev = cur
        best = max(best, run)
    return best


def _artistic_case_echo(original: str, normalized: str) -> bool:
    """艺术化混排字回显（'DeAD' → 'deAD'，deadbeat 实证）：原文是
    交替大小写的艺术写法（段 ≤2）、译文是其大小写噪声变体（casefold
    相同）→ 回显是正确行为（把艺术写法当普通词翻译成'死亡'是错误）。
    多行逐行对齐检查（'DeAD\\nbEAt' 每行 ≤8 才豁免，0.25.0 修复；
    任一行是普通词如 'Continue' 则整体不豁免）。
    'dead'/'DEAD' 规范形态段长 4 > 2 不豁免，UI 词典词检查仍生效；
    'Continue' TitleCase 段长 7 不豁免，仍判该译未译。"""
    orig_lines = original.splitlines() or [original]
    norm_lines = normalized.splitlines() or [normalized]
    if len(orig_lines) != len(norm_lines):
        # 行结构不一致 → 逐行不可对齐，回退整条判断（行数差即整条结构已变）
        if len(original) > 8 or len(normalized) > 8:
            return False
        orig_lines, norm_lines = [original], [normalized]
    for o, n in zip(orig_lines, norm_lines):
        if len(o) > 8 or len(n) > 8:
            return False
        if o.casefold() != n.casefold():
            return False
        if not (_max_case_run(o) <= 2
                and any(c.isupper() for c in o)
                and any(c.islower() for c in o)):
            return False
    return True


def _ui_check_words(words: list[str]) -> list[str]:
    """专名回显的 UI 词检查词集：多词时跳过末位词（版本后缀形态）。

    'UCLA Gold' 的 Gold 是版本后缀（UCLA Gold 是 Baldis 的版本彩蛋名），
    回显保留合理——Gold 在 UI 词典（金币类 UI 词）曾使专名回显豁免失败
    （baldis 实证）。单词（'SFX'/'Continue'）仍全查，真漏翻照常拦截。
    """
    if len(words) <= 1:
        return words
    return words[:-1]


def is_camel_tech_abbreviation(word: str) -> bool:
    """驼峰技术缩写（VSync/MonoBehaviour/YouTube）：首大写 + 内部混合大小写。

    界面标准术语，保留原文合理（vincent 'VSync: OFF' → 'VSync：关闭'）；形态
    要求首大写 + 内部混合大小写——全大写 SETTINGS/TitleCase Save 不算。
    """
    return (len(word) > 1 and word[0].isupper()
            and any(char.islower() for char in word[1:])
            and any(char.isupper() for char in word[1:]))


def has_independent_lower_word(text: str) -> bool:
    """原文是否存在独立 ASCII 小写词（'iipsum' 是、'Stefánsson' 的 nsson 不是）。

    rich text 标签参数（<color=red> 的 red、<size=50> 的 size）不是语义词——
    NULL 回显曾因 color=red 的 red 被当小写词 → 专名回显豁免失败
    （baldis 实证：'<color=red>NULL NULL…' 的 NULL 是游戏内实体名，保留
    合理）。剥标签后再检查。
    属格尾巴（Playtime's 的 s、don't 的 t）不是独立小写词——'Square Button:
    Jump During Playtime's Jumprope Minigame' 的 s 曾误判小写词 → 译文
    已含中文仍被 untranslated_text 拒（baldis 实证 [6]）。单字母 + 前邻
    撇号 → 撇号缩写的字母碎片，跳过。
    花括号占位符紧邻的单字母（'v{0}' 版本号模板的 v、'{0}' 后接单位字母）
    是格式串载体不是普通词——deepest-sword 实证：版本号模板 'v{0}' 回显
    是正确行为，v 被当独立小写词 → proper_name_echo 豁免失效 →
    target_script_mismatch 恒败。
    """
    cleaned = _STRIP_RICH_TEXT.sub(" ", SAFE_KEEPERS.sub(" ", text))
    for match in _LOWER_ASCII_WORD.finditer(cleaned):
        if (len(match.group(0)) == 1 and match.start() > 0
                and cleaned[match.start() - 1] == "'"):
            continue
        if (len(match.group(0)) == 1
                and ((match.start() > 0
                      and cleaned[match.start() - 1] in "{}")
                     or (match.end() < len(cleaned)
                         and cleaned[match.end()] in "{}"))):
            continue
        return True
    return False
# \u8bd1\u6587\u5f15\u53f7\u5185\u4e13\u540d\u77ed\u8bed\uff08\u6a21\u578b\u7528\u5f15\u53f7\u5305\u88f9\u4e13\u540d\uff1a\u6309\u94ae "Jump During Playtime" \u7684
# \u5f3a\u8c03\u6807\u8bb0\uff09\u2014\u2014\u5f15\u53f7\u5185\u5168 TitleCase \u4e14\u6bcf\u4e2a\u8bcd\u90fd\u5728\u539f\u6587\u51fa\u73b0 \u2192 \u4e13\u540d\u77ed\u8bed\uff0c
# \u52a8\u4f5c\u8bcd/\u82f1\u6587\u6b8b\u7559\u68c0\u67e5\u8c41\u514d\uff08baldis \u5b9e\u8bc1\uff1a'Square Button: Jump During
# Playtime's Jumprope Minigame' \u7684 Jump \u662f\u5c0f\u6e38\u620f\u540d\uff0c\u4e0d\u662f\u52a8\u4f5c\u52a8\u8bcd\uff09\u3002
# \u8981\u6c42\u8bcd\u5728\u539f\u6587\u51fa\u73b0\u9632\u8bef\u8bd1\u653e\u884c\uff08'Jump Along' \u7684 Along \u4e0d\u5728\u539f\u6587 \u2192 \u4e0d\u8c41\u514d\uff09\u3002
_QUOTED_SPAN = re.compile(
    r"[\"\u201c\u201d\u00ab\u00bb\u300c\u300d\u300e\u300f]([^\"\u201c\u201d\u00ab\u00bb\u300c\u300d\u300e\u300f]{1,80})[\"\u201c\u201d\u00ab\u00bb\u300c\u300d\u300e\u300f]")


def _complete_tag_pairs(tags: list[str]) -> bool:
    """缺失标签是否全是完整标签对（<x> 与 </x> 同名成对）。

    模型整体省略彩色强调标签（<color=green>Paused</color> 整对变中文
    引号包裹，baldis 实证 1.8B 稳定行为）→ 样式整对损失、无崩溃风险；
    单个标签缺失（留 <color=red> 丢 </color>）会破坏显示 → 仍需
    self_heal/失败暴露。数据占位符（{0}/{name}）不是 < 开头 → False。
    """
    if not tags or any(not tag.startswith("<") for tag in tags):
        return False
    open_names = [
        tag[1:].split(">")[0].split("=")[0].casefold()
        for tag in tags if not tag.startswith("</")]
    close_names = [
        tag[2:].split(">")[0].casefold()
        for tag in tags if tag.startswith("</")]
    return bool(open_names) and Counter(open_names) == Counter(close_names)


def quoted_proper_terms(translation: str, original: str) -> set[str]:
    """\u8bd1\u6587\u5f15\u53f7\u5185\u5168 TitleCase \u4e13\u540d\u77ed\u8bed\uff08\u6bcf\u4e2a\u8bcd\u90fd\u5728\u539f\u6587\u51fa\u73b0\uff09\u7684\u8bcd\u96c6\u3002

    \u5f15\u53f7\u5185\u5bb9\u542b\u5c0f\u5199\u666e\u901a\u8bcd\uff08\u6309\u94ae "play"\uff09\u2192 \u7a7a\u96c6\uff08\u4e0d\u8c41\u514d\uff09\uff1b
    \u8bcd\u4e0d\u5728\u539f\u6587\uff08'Jump Along' \u8bef\u8bd1\u4e13\u540d\uff09\u2192 \u7a7a\u96c6\uff08\u9632\u8bef\u8bd1\u653e\u884c\uff09\u3002
    \u8c03\u7528\u65b9\u8d1f\u8d23\u91cd\u97f3\u5f52\u4e00\u5316\uff08\u5e26\u91cd\u97f3\u4e13\u540d\u62c6\u788e\u540e\u9996\u5b57\u6bcd\u5224\u5b9a\u5931\u771f\uff09\u3002
    """
    original_terms = {word.casefold()
                      for word in _ENGLISH_WORD.findall(original)}
    quoted: set[str] = set()
    for match in _QUOTED_SPAN.finditer(translation):
        words = _ENGLISH_WORD.findall(match.group(1))
        if not words:
            continue
        title_case = all(word[0].isupper() for word in words)
        no_ui_words = all(word.casefold() not in _DISPLAY_WORDS_CASEFOLD
                          for word in words)
        if (title_case or no_ui_words) and all(
                word.casefold() in original_terms for word in words):
            quoted.update(word.casefold() for word in words)
    return quoted


_CJK = re.compile(
    r"[\u3400-\u9fff\uf900-\ufaff\U00020000-\U0002FA1F]")


def is_chinese_source(text: str) -> bool:
    """\u4e2d\u6587\u6e90\u5224\u5b9a\uff1aCJK \u2265 2 \u4e2a\u4e14\u5360\u5b57\u6bcd \u2265 50%\uff08deadbeat 0.25.0 \u6536\u7d27\uff09\u3002

    \u6e38\u620f\u81ea\u5e26\u4e2d\u6587\u8bed\u8a00\u5305\uff08Language/CH/*.subs\uff09\u539f\u6587\u5373\u4e2d\u6587 \u2192 \u76f4\u63a5\u653e\u884c\u3002
    \u4f46\u539f\u6587\u542b\u5355\u4e2a\u65e5\u6587\u6c49\u5b57\uff08\u6b4c\u8bcd '\u6226\u4e89'\uff09+ \u5927\u91cf\u82f1\u6587\u65f6\u6574\u6761\u88ab\u8bef\u5224\u4e2d\u6587\u6e90
    \u653e\u884c\u662f\u91cd\u5927 bug\uff08\u5b9e\u8bc1\uff1a1719 \u5b57\u7b26\u82f1\u6587\u539f\u6837\u653e\u884c\uff09\u2014\u2014\u6536\u7d27\u4e3a CJK \u2265 2
    \u4e14 CJK \u5360\u5b57\u6bcd\u5b57\u7b26 \u2265 50%\uff0c\u82f1\u6587\u4e3a\u4e3b\u7684\u6b4c\u8bcd\u884c\u4e0d\u518d\u8bef\u653e\u884c\u3002
    \u97e9\u6587\u539f\u6587\uff08\u65e0\u5047\u540d CJK\uff09\u5360\u6bd4\u8db3\u591f\u65f6\u540c\u6837\u653e\u884c\uff1b\u65e5\u6587\u539f\u6587\uff08\u542b\u5047\u540d\uff09\u7531
    _is_multilingual_source \u515c\u5e95\u4e0d\u8d70\u6b64\u8def\u5f84\u3002"""
    cjk = len(_CJK.findall(text))
    if cjk < 2:
        return False
    letters = len(re.findall(r"[A-Za-z\u3400-\u9fff\uf900-\ufaff]",
                             text))
    if letters == 0:
        return True
    return cjk * 2 >= letters
# 解释式垃圾输出：模型把「翻译不了」的词当成提问，输出解释段落而非译文
# （containment 实证：Mierda → '该文本看起来像是随机组合的文字，没有
# 明确的含义。以下是可能的解释：…'；CreditsVolume (1) Profile →
# '参考以下翻译："Volume"可译为"音量"'）。两类：①「译文：」输出包装
# 前缀——正常译文不会以此开头，任何长度都判；②解释特征句式——正常
# 译文不会出现精确短语（「以下是重要信息」不在模式内），但需 ≥20
# 字符防「没有明确的含义」式短句误伤（原文可能是 "has no clear
# meaning" 的描述性文本）
_EXPLANATORY_PREFIX = re.compile(
    r"^(?:translation|translated text|译文|翻译)\s*[:：]", re.I)
_EXPLANATORY_PATTERN = re.compile(
    r"以下是(?:可能的)?(?:解释|翻译)[:：]?"
    r"|参考以下翻译[:：]?"
    r"|该文本看起来像是|这个文本看起来像是|看起来像是随机"
    r"|没有明确的含义"
    # 中置「X 翻译为 Y」解释句式（honorplusplus 实证：'f={0} DEBUG 翻译为
    # DEBUG {1}'——模型对全大写保留词输出解释而非译文；此前只拦前缀/
    # 特定解释短语，中置句式漏网→首译垃圾直接过质量门→写回截断丢占位符
    # 才被拒）。20 字符门槛（调用处）已防短文本误伤；正常译文句子中
    # 不会出现「翻译为」这个翻译动作的元描述词。
    r"|\w+\s*翻译为\s*\S+", re.I)


def _has_illegal_controls(value: str) -> bool:
    return any((ord(char) < 0x20 and char not in "\t\n\r")
               or 0x7F <= ord(char) <= 0x9F for char in value)


def _newline_events(value: str) -> tuple[str, ...]:
    names = {r"\n": "literal", "\r\n": "crlf", "\r": "cr", "\n": "lf"}
    return tuple(names[match.group(0)]
                 for match in re.finditer(r"\\n|\r\n|\r|\n", value))


def _line_content_topology(value: str) -> tuple[bool, ...]:
    return tuple(bool(part.strip())
                 for part in re.split(r"\\n|\r\n|\r|\n", value))


def _blank_line_compression(original: str, normalized: str) -> bool:
    """译文换行结构是否等于「原文删除 ≤4 个空行」：
    模型压缩连续空行（\n\n\n→\n\n）是稳定行为，中文排版无视觉差异
    （mimic-search 两处压缩累计 3、interdream 1）。只允许删除空行
    （strip 后为空的行），不允许删除/新增/移位内容行或空行位置。
    """
    source = _line_content_topology(original)
    target = _line_content_topology(normalized)
    if len(target) > len(source) or source == target:
        return False
    skipped = 0
    j = 0
    for line in source:
        if j < len(target) and target[j] == line:
            j += 1
        elif line is False:
            skipped += 1
            if skipped > 4:
                return False
        else:
            return False
    return j == len(target) and skipped > 0


def _interaction_prompt_merge_compression(original: str, normalized: str) -> bool:
    """交互提示「对象名 + 按键动作行」双行原文的译文合行豁免。

    Flabby Pizza 实证：'Dish\\nG - to throw'（对象名行 + 按键动作行）在
    反馈重译时被模型合并成单行「盘子/容器 G 扔掉」——机械门 newline_
    mismatch + line_content_mismatch 恒定拦截，正确译文被 BLOCKED 留人工
    （对象名+按键提示共 4 条全部阻断）。按键提示的双行是 UI 提示的排版
    （对象名 / 按键动作），合并成单行是排版损失、无运行时崩溃风险（非
    占位符/数据换行）。

    豁免条件从严（宁漏勿坏）：
    - 原文必须是交互提示（is_interaction_prompt）且非空行恰好 2 行；
    - 第 2 行（按键动作行）含按键字面量事件（G/E…）；第 1 行是对象名；
    - 译文非空行恰好 1 行（确实合行）；
    - 译文在该按键字面量**之前**还有非空内容（对象名已翻译，未丢）——
      这排除「G 投掷」这类把对象名整行丢弃的坏译文（内容丢失仍判失败，
      由 untranslated_text 承接）。
    """
    if not is_interaction_prompt(original):
        return False
    src_lines = [ln for ln in re.split(r"\\n|\r\n|\r|\n", original)
                 if ln.strip()]
    if len(src_lines) != 2:
        return False
    key_events = [ev.value for ev in interaction_input_events(original)
                  if ev.kind == "literal_glyph"]
    if not key_events:
        return False
    # 按键字面量必须在第 2 行（对象名行之后）才符合「对象名 + 按键动作」
    # 形态；按键在第 1 行（如 'E: 打开' 前导提示）不是本豁免目标。
    if not any(k in src_lines[1].casefold()
               for k in (ke.casefold() for ke in key_events)):
        return False
    dst_lines = [ln for ln in re.split(r"\\n|\r\n|\r|\n", normalized)
                 if ln.strip()]
    if len(dst_lines) != 1:
        return False
    # 译文必须在按键字面量之前还有内容（对象名已翻译）；按键丢失由
    # input_token_mismatch 承接，本豁免不覆盖。
    for key in key_events:
        pos = re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])",
            dst_lines[0], re.I)
        if pos is not None and pos.start() > 0:
            return True
    return False


def _normalize_translation(original: str, translation: str) -> str:
    """Trim model wrappers while restoring source boundary line breaks."""
    core = re.sub(r"[ \t]+(?=\r?$)", "", translation.strip(), flags=re.M)
    leading = re.match(r"^(?:\r\n|\r|\n)*", original).group(0)
    trailing = re.search(r"(?:\r\n|\r|\n)*$", original).group(0)
    return leading + core + trailing


def _input_token_events(value: str, source_tokens: tuple[str, ...]) -> tuple[str, ...]:
    if not source_tokens:
        return ()
    alternatives = sorted(
        {token.casefold(): token for token in source_tokens}.values(),
        key=len, reverse=True,
    )
    pattern = re.compile(
        r"(?<![A-Za-z0-9])(?:" +
        "|".join(re.escape(token) for token in alternatives) +
        r")(?![A-Za-z0-9])",
        re.I,
    )
    return tuple(match.group(0).casefold() for match in pattern.finditer(value))


def _glossary_pairs(glossary: Iterable) -> list[tuple[str, str]]:
    pairs = []
    for item in glossary:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            source, target = item[0], item[1]
        elif isinstance(item, dict):
            source, target = item.get("term"), item.get("translation")
        else:
            source = getattr(item, "term", None)
            target = getattr(item, "translation", None)
        if isinstance(source, str) and source and isinstance(target, str) and target:
            pairs.append((source, target))
    return pairs


# 术语命中正则缓存（2026-08-19 翻译性能修复）：source_term_applies 是
# 翻译热路径——_build_item 每条目 × 每术语一次（300 术语 × 万条目 =
# 300 万次），re.escape + pattern 拼接 + re.search 每次重建正则对象
# （实测纯开销 11s/万条目）。缓存 pattern 编译结果 + 按原文缓存词元集：
# 真实调用形态是「同一原文 × N 术语」（_build_item 循环），词元集只算
# 一次；术语首词元不在原文词元集 → 不可能命中，跳过正则（综合实测
# 万次调用 0.1s，此前 11s）。
_TERM_PATTERN_CACHE: dict[str, re.Pattern] = {}
_TERM_HAS_ALNUM: dict[str, bool] = {}
_TERM_PATTERN_CACHE_LIMIT = 4096
_TOKEN_SPLIT_RE = re.compile(r"[^A-Za-z0-9_]+")
# 原文 → casefold 词元 frozenset（LRU 近似：超限清空——原文条目数万级，
# 词元集很小（几十字节/条），4096 条上限内存可忽略）
_SOURCE_TOKENS_CACHE: dict[str, frozenset[str]] = {}
_SOURCE_TOKENS_CACHE_LIMIT = 4096


def _cached_term_pattern(term: str) -> re.Pattern | None:
    """术语 → 预编译整词边界正则（非字母数字术语返回 None）。"""
    pat = _TERM_PATTERN_CACHE.get(term)
    if pat is None:
        if not _TERM_HAS_ALNUM.get(term, True):
            return None   # 已知纯 CJK/符号术语，非正则路径
        if not re.search(r"[A-Za-z0-9_]", term):
            _TERM_HAS_ALNUM[term] = False
            if len(_TERM_HAS_ALNUM) > _TERM_PATTERN_CACHE_LIMIT:
                _TERM_HAS_ALNUM.clear()
            return None
        pat = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", re.I)
        if len(_TERM_PATTERN_CACHE) < _TERM_PATTERN_CACHE_LIMIT:
            _TERM_PATTERN_CACHE[term] = pat
    return pat


def _cached_source_tokens(source_text: str) -> frozenset[str]:
    """原文 → casefold 词元集（同原文 N 术语循环只算一次）。"""
    tokens = _SOURCE_TOKENS_CACHE.get(source_text)
    if tokens is None:
        tokens = frozenset(
            tok.casefold()
            for tok in _TOKEN_SPLIT_RE.split(source_text) if tok)
        if len(_SOURCE_TOKENS_CACHE) < _SOURCE_TOKENS_CACHE_LIMIT:
            _SOURCE_TOKENS_CACHE[source_text] = tokens
        else:
            _SOURCE_TOKENS_CACHE.clear()   # 近似 LRU：清空重来
            _SOURCE_TOKENS_CACHE[source_text] = tokens
    return tokens


def source_term_applies(term: str, source_text: str) -> bool:
    """Match alphanumeric glossary terms as complete source tokens.

    2026-08-19 性能修复：pattern 预编译缓存 + 原文词元集缓存 + 词元
    预筛（术语首词元不在原文词元集 → 跳过正则）。语义与旧实现完全
    一致（同一正则；预筛是必要条件剪枝——首词元不在则整词必不在）。"""
    term = term.strip()
    if not term:
        return False
    pat = _cached_term_pattern(term)
    if pat is None:
        return term.casefold() in source_text.casefold()
    first_token = _TOKEN_SPLIT_RE.split(term)[0]
    if first_token and first_token.casefold() not in _cached_source_tokens(
            source_text):
        return False
    return bool(pat.search(source_text))


def _in_filename_segment(term: str, source_text: str) -> bool:
    """词对 source 是否命中文件名/路径段（player-diagnostics.txt 的
    Player）→ 文件名是标识符不是可翻译语义文本，词对不适用。
    incremental-rts 实证：'Export player-diagnostics.txt with recent
    logs...' 译文「导出包含最新日志…的 player-diagnostics.txt 文件」
    保留文件名被 (Player→玩家) 词对误杀 glossary_mismatch。检测：
    token 所在连续词段（[A-Za-z0-9_.-]+）含 '-' 或 '_' 且以
    '.'+扩展名（1-8 字母）结尾——'player-diagnostics.txt' 段含连
    字符 + .txt 扩展名。防误伤：普通短语（fight-or-flight 不以 .ext
    结尾）不受影响。"""
    lower = source_text.casefold()
    term_cf = term.casefold()
    start = 0
    while True:
        m = re.search(rf"(?<![A-Za-z0-9_]){re.escape(term_cf)}(?![A-Za-z0-9_])",
                      lower[start:], re.I)
        if not m:
            return False
        i = start + m.start()
        j = start + m.end()
        seg_start, seg_end = i, j
        while seg_start > 0 and (
                source_text[seg_start - 1].isalnum()
                or source_text[seg_start - 1] in "._-"):
            seg_start -= 1
        while seg_end < len(source_text) and (
                source_text[seg_end].isalnum()
                or source_text[seg_end] in "._-"):
            seg_end += 1
        seg = source_text[seg_start:seg_end]
        if re.search(r"\.\w{1,8}$", seg) \
                and ("-" in seg or "_" in seg or seg_start > 0):
            return True
        start = j


def _label_context_match(term: str, source_text: str) -> bool:
    """单 token 词对子串命中处是否为标签语境：命中处左/右邻是标点、
    数字、行首或行尾（'miss: 999' 的 miss 右邻冒号是 HUD 计数标签；
    'TIME' 单独一行是计时器 UI 标签）→ 词对适用。命中处前后都是
    字母词（'time to take on' 的 time 左邻行首右邻字母）是自然句
    普通词 → 词对不适用——译文意译（是时候/工夫）不该被词对
    （TIME→时间）误杀（goodmorning 实证：'you're all ready -'
    'time to take on the day!' 完美译文被 (TIME→时间) 子串误判
    glossary_mismatch）。"""
    term_cf = term.casefold()
    lower = source_text.casefold()
    start = 0
    while True:
        m = re.search(rf"(?<![A-Za-z0-9_]){re.escape(term_cf)}(?![A-Za-z0-9_])",
                      lower[start:], re.I)
        if not m:
            return False
        i = start + m.start()
        j = start + m.end()
        # 富文本标签内部（<size=120%> 的 size / </size>）：标签属性与
        # 标签名是标记语言不是可翻译文本（incremental-rts '<size=120%>
        # <b>Weapon:</b></size>' 实证：(size→大小) 词对命中标签内
        # size 误杀译文「武器：」——与 F10b 花括号占位符同类：非语义
        # 文本段豁免词对）。判据：token 前最近的 '<' 到 token 之间
        # 无 '>'（未闭合标签段内），且右侧有 '>' 闭合——'<b>Weapon:'
        # 的 Weapon 在标签内容里（前有 '>' 闭合 <b>），照常适用词对。
        lt = source_text.rfind("<", 0, i)
        gt = source_text.find(">", j)
        if (lt != -1 and gt != -1 and lt < i
                and ">" not in source_text[lt:i]):
            return False  # 富文本标签内部（标记语言，非可翻译文本）
        if source_text[i:i + 1].isupper():
            # 全大写句子（'IT'S LOCKED.'——Morfosi 64 条同因全灭实证）：
            # 全大写文本每个词都大写，大写不携带 UI 形态信息——"句中
            # 大写 = 菜单词"判据失效（LOCKED 被判句中 TitleCase →
            # (Locked→锁定) 词对误杀译文「它被锁住了。」）。判据：命中
            # 处所在句子（最近句尾标点之后）无小写字母、且命中处右侧
            # 紧跟句尾标点（.！？…）→ 喊话式自然句 → 走通用判定（句尾
            # 标点豁免自然句）；'NEW GAME'/'TIME 5' 无句尾标点仍按
            # 标签处理。
            tail_pos = min(
                (p for p in (source_text.find(c, j)
                             for c in ".!?。！？…")
                 if p != -1), default=-1)
            head_pos = max(
                (p for p in (source_text.rfind(c, 0, i)
                             for c in ".!?。！？…")
                 if p != -1), default=-1)
            if not (tail_pos != -1 and not any(
                    c.islower() for c in source_text[head_pos + 1:tail_pos])):
                # 大写命中分两种（inch-by-inch 实证）：非句子首词大写
                # （'Open Settings menu' 的 Settings 左邻有词 Open）是 UI
                # 菜单词形态 → 词对适用；句子首词大写（'Time for some
                # science!' 的 Time 在行首）只是英文句子首词大写规则 →
                # 右邻（跳空白）是小写字母词则豁免（自然句），右邻标点/
                # 行尾/数字才适用（'TIME' 单行计时器标签 / 'Time:' /
                # 'Time 5' 计数）。
                # 富文本标签透明化（2026-08-13 F11）：'<b>Full version on
                # Steam</b>' 的 Full 语义上是句子首词，但左邻是 '>'（标签
                # 结束）→ 当前扫描判为非句首 → 词对 (full→完整的) 误杀
                # 译文「全版本」。句子首词判定应跳过标签段（'>' 回跳最近
                # '<'）——标签是装饰层，不影响语境判定（VICTORY 实证）。
                left = source_text[:i]
                k = i - 1
                while k >= 0:
                    c = left[k]
                    if c.isspace():
                        k -= 1
                        continue
                    if c == ">":
                        # 富文本标签结尾：跳过整个标签段（</b>、<size=120%>）
                        lt2 = left.rfind("<", 0, k)
                        if lt2 != -1:
                            k = lt2 - 1
                            continue
                    break
                sentence_head = (k < 0) or (left[k] in ".!?。！？…")
                if not sentence_head:
                    return True  # 左邻有词：句中 TitleCase（UI 菜单词形态）
                # 右邻跳过空白取首个字符（'Time for' 的 Time 后是空格）
                after = source_text[j:].lstrip(" \t\r\n")[:1] \
                    if j < len(source_text) else ""
                if after and after.isalpha() and after.islower():
                    return False  # 句子首词大写 + 右邻小写词 = 自然句
                return True  # 句首 + 右邻标点/行尾/数字 = 标签
        before = source_text[i - 1] if i > 0 else ""
        after = source_text[j] if j < len(source_text) else ""
        if not before or not after:
            return True  # 行首/行尾（'TIME' 单独一行是计时器标签）
        if (before in punctuation or after in punctuation):
            # 标点邻接——但句尾标点（!.?）是句子正常结束不是标签标记
            # （'...at this size!' 的 size 右邻感叹号是自然句句尾，
            #  词对 (size→大小) 误杀译文「这种规模」——inch-by-inch
            #  实证）；占位符花括号（{health}）是变量边界不是标签
            # 标记（incremental-rts 'Increase unit HP by {health}'
            #  译文「生命值」被 (HEALTH→健康) 误杀——占位符内词
            #  不是可翻译语义文本）；冒号/逗号/括号等才是 HUD 标签
            # 标记（'miss: 999'）。
            if after in ".!?。！？":
                return False
            if before in "{}" or after in "{}":
                return False  # 占位符边界
            return True  # 标点邻接（'miss:' 的 miss 右邻冒号是 HUD 标签）
        if (before.isdigit() or after.isdigit()):
            return True  # 数字邻接（'Time 5' 类计数标签）
        start = j
    return False


def _is_lyric_like(text: str) -> bool:
    """歌词文本特征：含假名/汉字（日文歌词）或括号音乐标记/重复结构
    （(Guh)/(Let it die)/Sha la la）。普通词术语在歌词中常被押韵词/
    拟声词误命中——'Miss, hit' 的 Miss 是'小姐'非'未命中'（deadbeat
    歌词实证），歌词整体豁免普通词术语检查（keep 型与专名豁免仍生效）。"""
    if len(text) < 200:
        return False
    if re.search(r"[぀-ヿ一-鿿]", text):
        return True
    return bool(re.search(r"\((?:[A-Za-z ,'!?]{2,20})\)", text))


def _dialogue_script_like(text: str) -> bool:
    """Undertale 系对话脚本特征（interdream/DELTATRAVELER 实证）：
    行首 "* " 对话符（后续为括号或大写字母——markdown 列表 "* item"
    小写开头不受影响）、"^NN" 计时码、全大写喊话（≥3 词 + 句子标点，
    'WHAT'S YOUR NAME?' / 'WOULD YOU THREE LIKE TO PLAY UNO?'——
    Undertale 系统提示/喊话风格，无 "* " 前缀也无计时码）。
    对话文本是叙事自由语义语言，普通词词对强制必然误杀意译——
    (PLACE→地点) 杀「那个地方」、(NAME→名称) 杀「你叫什么名字？」、
    (Time→时间) 杀「时光/时机」、(Play→播放) 杀「玩 UNO」、
    (SHOOT→射击) 杀感叹词「哦，天哪」（全部 interdream 实证）。
    与 _is_lyric_like 同族：自由语义文本豁免普通词术语检查。
    keep 型（target==source 保留映射）与多词短语词对（语义确定性
    高）不受影响。"""
    if re.search(r"(?<![A-Za-z0-9_])\^[0-9]{1,2}", text):
        return True
    for line in text.splitlines():
        if re.match(r"^\* [A-Z(]", line):
            return True
    # 字面 "\n"（C# 转义）也当词分隔——'WOULD YOU THREE \nLIKE TO PLAY
    # UNO?' 的 '\nLIKE' 若不替换会因小写 n 使 isupper() 判 False
    stripped = re.sub(r"\\n", " ", text.strip())
    return bool(stripped and len(stripped.split()) >= 3
                and stripped.isupper()
                and re.search(r"[?!。！？]", stripped))


def _glossary_proper_phrase(term: str, source_text: str) -> bool:
    """术语在原文中的出现处邻接 TitleCase 词 → 术语是专名的一部分，与
    术语表的普通词含义无关。deadbeat 'Miss Fire Spitting' 角色名实证：
    Miss 邻接 Fire（TitleCase）→ 与 (miss, 未命中) 术语无关 → 豁免；
    'Slash key' 的 Slash 右邻 key（小写）→ 不豁免（触发术语确定性修复）。
    """
    term = term.strip()
    if not term:
        return False
    # UI 词典词（Settings/Save/Options…在 DISPLAY_WORDS）：显示文本，
    # 邻接 TitleCase 词不构成专名证据——'Open Settings menu' 的 Settings
    # 左邻 Open（普通动词），Settings 仍是 UI 术语，译文必须含其译名
    # （test_translation_term_missing_target_still_fails 固化）→ 不豁免
    if term.casefold() in _DISPLAY_WORDS_CASEFOLD:
        return False
    # 专名形态术语（Moon Key 等 TitleCase 组合）：自身即专名短语，
    # 邻接 TitleCase 词不构成豁免——'Use Moon Key to open...' 的 Moon
    # 左邻 Use（句首动词），译文偏离译名（月之钥匙 vs 月光钥匙）仍是
    # 真 drift（test_quality_rejects_untranslated_english_and_glossary_
    # drift 固化）。豁免只适用于普通词形态术语（小写 miss 在 'Miss
    # Fire Spitting' 专名中被 TitleCase 化 → 与 (miss, 未命中) 无关）
    if re.fullmatch(r"[A-Z][a-z'-]*(?:\s+[A-Z][a-z'-]*)*", term):
        return False
    for m in re.finditer(
            rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
            source_text, re.I):
        before = re.match(r"([A-Z][a-z'-]*)\s*$",
                          source_text[:m.start()])
        after = re.match(r"^\s*([A-Z][a-z'-]*)",
                         source_text[m.end():])
        if before or after:
            return True
    return False


def _glossary_verb_usage(term: str, source_text: str) -> bool:
    """术语词在原文中处于动词用法（前邻 to 不定式或助动词）→ 该出现与
    术语表的标签/判定含义无关。doubleshake "shouldn't be hard to miss"
    的 miss 是"错过/遗漏"（译文"遗漏"正确），术语表 (miss, 未命中) 是
    音游 HUD 判定标签（deadbeat 'miss: 999' 实证）——动词用法豁免；
    'miss: 999' / 'Slash key' 前邻是冒号/行首 → 不豁免，术语照常生效。
    口语助动词缩写（gonna/wanna/gotta/lemme/dunno/oughta/ain't）同属
    动词用法（field-hospital-web 叙事文本 'are gonna miss him dearly'
    的 miss=想念 实证：译文"想念"被 (miss, 未命中) 误杀）。
    守卫与 _glossary_proper_phrase 一致：UI 词典词（Settings）与专名
    形态术语（Moon Key）不适用此豁免（防把真术语漂移当动词放过）。
    """
    term = term.strip()
    if not term:
        return False
    if term.casefold() in _DISPLAY_WORDS_CASEFOLD:
        return False
    if re.fullmatch(r"[A-Z][a-z'-]*(?:\s+[A-Z][a-z'-]*)*", term):
        return False
    for m in re.finditer(
            rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
            source_text, re.I):
        before = re.search(
            r"(?i)(?<![A-Za-z])(?:to|can|could|will|would|shall|should|"
            r"may|might|must|do|does|did|not|never|don'?t|won'?t|"
            r"couldn'?t|shouldn'?t|wouldn'?t|am|is|are|was|were|be|been|"
            r"gonna|wanna|gotta|lemme|dunno|oughta|ain'?t|"
            r"I'?m|I|you|he|she|we|they|it|me|him|her|us|them)\s+$",
            source_text[:m.start()])
        if before:
            return True
    return False


_LOG_TEMPLATE_RE = re.compile(
    r"^[A-Z]{3,}:\s*[a-z]{2,}\s*=\s*\{[0-9]+\}")


# 日期/数字格式模板（yyyy-MM-dd HH:mm:ss、{0:F2}、0123456789+-.eE）：
# string.Format/ToString 的格式参数，不是显示语义文本——回显是正确
# 行为（force-reboot 实证 3 条 PlayFab 日期格式回显被误判
# untranslated_text）。字符类只含格式符（数字/冒号/连字符/斜杠/花括号/
# 时间字母）——普通词含字母（Time: / value={0} 的 value）不匹配；
# 纯字母串（help）不匹配。
_FORMAT_TEMPLATE_RE = re.compile(
    r"^(?:[0-9{}:\-./,_%#+TZzHhMmSsFfKkyd ]{1,})+$")


def _is_format_template(text: str) -> bool:
    stripped = str(text).strip()
    if not stripped or stripped.isalpha():
        return False
    return bool(_FORMAT_TEMPLATE_RE.match(stripped))


# 键名强制保留表（casefold）：输入提示里这些键位是字面量，译文必须保留
# 键名原文——译成中文（RMB→「人民币」force-reboot 实证）玩家找不到键。
# 两类排除：
# ① 有可靠中文通称的键（回车/空格/退格/删除是标准译法）；
# ② 兼作普通英语词的键名（control/pause/return/break/insert/home/end/
#    escape——escape=逃跑/逃脱 普通词义，'escape the room' 误杀风险；
#    goodmorning 实证 Camera Control）——译文「控制」正确，强制保留
#    会误杀。只保留专有键名形态（Esc 的强制保留由按键序列检查
#    input_token_mismatch + 中文通称豁免承接）。
# RMB/LMB/MMB 保留原文最安全（「右键」与「人民币」无法可靠区分，
# 宁可失败不写错——失败只留原文，写错是误导）。
_KEY_LABEL_CASEFOLD = frozenset(
    PHYSICAL_KEY_NAMES_CASEFOLD - {
        "enter", "return", "space", "tab", "backspace", "delete",
        "control", "pause", "break", "insert", "home", "end",
        "escape",
    })

# 键名中文通称（headache 实证 2026-08-12）：'PRESS SPACE TO RESTART'
# →「按空格键进行重启」、「PRESS ESCAPE TO GO BACK」→「按 ESC 键以
# 返回」——按键名译成中文标准通称/简写是正确翻译（玩家能识别），
# 按键字面量/键名保留检查却要求译文中含英文键名原文 → 误杀。
# 本表只放「中文通称与键名一一对应」的键（空格=space、回车=enter），
# 无歧义；普通词义（空间/移动）不含——'free space' 的「空间」不豁免
# SPACE 字面量要求，shift 的「移动」同理（shift 不在本表）。
_KEY_ZH_ALIASES: dict[str, tuple[str, ...]] = {
    "space": ("空格",),
    "escape": ("esc", "退出键"),
    "esc": ("esc", "退出键"),
    "rmb": ("右键", "鼠标右键", "右击"),
    "lmb": ("左键", "鼠标左键", "左击"),
    "mmb": ("中键", "鼠标中键"),
    "enter": ("回车",),
    "return": ("回车",),
    "backspace": ("退格",),
    "delete": ("删除键",),
    "del": ("删除键",),
    "control": ("ctrl", "控制键"),
    "ctrl": ("ctrl", "控制键"),
    "tab": ("制表键",),
}


def is_log_template(text: str) -> bool:
    """调试日志模板串：'MEMORY: cur = {0}MB, max = {1}MB' / 'CHANNELS:
    real = {0}, total = {1}'——Unity Debug.Log 格式串（全大写标签 + 冒号 +
    小写变量赋值 + 占位符）。变量名（cur/real/max/total）是脚本标识符
    无语义、日志行仅调试可见 → 模型保留变量名（译文含中文）或整行回显
    都是合理行为（final-shot 实证 ×2）。形态防过宽：标签必须全大写
    （普通 UI 'Score = {0}' 不满足）、变量必须小写且带 = {n} 赋值模板
    ——防 'LEVEL: 1' 类真 UI 文本误豁免。
    """
    return bool(_LOG_TEMPLATE_RE.match(text or ""))


# 法语特征字符（重音拉丁字母：法语 é/è/à/ç/œ 等；西语 ó/ñ、德语 ä 等
# 同样覆盖——外语重音即说明非英语语境，英语术语表双关词都不适用）
_FRENCH_ACCENT_CHARS = set("àâäéèêëîïôöùûüçñÿœ")
# 法语功能词（高频介词/代词/系动词，'c'est encore vous' 实证）
_FRENCH_MARKER_WORD = re.compile(
    r"(?i)\b(c'est|qu'est|les|des|une|un|vous|nous|je|tu|ma|mes|ses|"
    r"est|sont|il|elle|chez|avec|pour|dans|sur)\b")


def _french_marker(source_text: str) -> bool:
    """原文是否法语特征文本（重音字母或法语功能词）→ 英语借词术语
    （encore→安可）不适用（faerie-afterlight 实证 9 条法语对话：
    'Hé, c'est encore vous !' 的 encore 是法语「又/再」日常副词，不是
    演出「安可」）。防过宽：英语原文 'Encore! Encore!' 无法语特征 →
    术语表照常生效（test_english_encore_still_checked 固化）。"""
    return (bool(_FRENCH_ACCENT_CHARS & set(source_text))
            or _FRENCH_MARKER_WORD.search(source_text) is not None)


def _glossary_keep_echo(original: str, translation: str, glossary) -> bool:
    """保留型术语回显豁免：glossary 中 keep 型术语（source==target
    casefold，署名专名/缩写保留映射）全部覆盖原文、译文无中文且与原文
    字母序列一致（大小写变体）→ 回显是模型对术语的保留，不是漏翻
    （'hiss pop collection' → 'Hiss Pop Collection'，222am 实证：署名
    专名，模型 TitleCase 化回显保留合理）。与 proper_name_echo 的区别：
    原文可含小写普通词（hiss/pop/collection 全是小写词），身份由
    glossary keep 规则证明，不需要 proper_name role。"""
    if _CJK.search(translation):
        return False
    keep_sources = [
        str(source) for source, target in _glossary_pairs(glossary)
        if str(source).strip()
        and str(target).strip()
        and str(source).strip().casefold() == str(target).strip().casefold()
        and source_term_applies(str(source), original)]
    if not keep_sources:
        return False
    letters_src = re.sub(r"[^A-Za-z]", "", original).casefold()
    letters_dst = re.sub(r"[^A-Za-z]", "", translation).casefold()
    if letters_src != letters_dst:
        return False
    # keep 术语覆盖全部原文词：剥离术语后无剩余字母才算"原文全是保留术语"
    rest = original
    for term in sorted(keep_sources, key=len, reverse=True):
        rest = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
            " ", rest, flags=re.I)
    return not re.search(r"[A-Za-z]", rest)



# ── 数字一致性（Q1 P2 缺口）：数字是数据不是文风，数值与百分比标记
# 必须保留。「造成 15 点伤害」（原文 50）、「提升 10」（原文 10%）、
# 「得分：15」（原文 1.5）都是数字失真——写回进游戏就是错误数值。
# 允许等价转换：50→五十/五十点、10%→百分之十、3.5→三点五、100→一百。
_CN_NUM_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_NUM_UNITS = {"十": 10, "百": 100, "千": 1000,
                 "万": 10000, "亿": 100000000}
_CN_NUM_CHARS = "零〇一二两三四五六七八九十百千万亿"
# 提取顺序：千分位（1,500）→ 普通小数 → % 允许空格分离（"10 %"）→
# 百分之中文百分比 → X折/X成（五折=50%）→ X点Y小数 → 半 → 普通中文数字
# 阿拉伯数字+万亿乘数（50万=500000、1.5亿=1.5e8）必须排在裸数字前——
# 否则 "50" 先匹配、万被拆走（fake-it 实证：'200.000 readers'→'20万
# 读者'、'500.000'→'50万' 被 numeric_mismatch 误杀，AI 审核已通过）。
_NUMBER_TOKEN_RE = re.compile(
    r"\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*%"
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?"
    r"|\d+(?:\.\d+)?\s*%"
    r"|\d+(?:[.,]\d+)?\s*[万亿]"
    r"|\d+(?:\.\d+)?"
    rf"|百分之[{_CN_NUM_CHARS}]+"
    rf"|[{_CN_NUM_CHARS}]+[折成]"
    rf"|[{_CN_NUM_CHARS}]+点[{_CN_NUM_CHARS}]+"
    r"|半"
    rf"|[{_CN_NUM_CHARS}]+"
)
# 欧式千分位：点分组（200.000 = 二十万，法国/德语区习惯；fake-it 的
# French ContentFr 字段实证）。仅当小数部分恰为 3 位一组时才判千分位，
# 普通 3.5 / 1.25 小数不受影响。
_DOT_GROUPED_RE = re.compile(r"\d{1,3}(?:\.\d{3})+")
# 阿拉伯数字 + 万/亿 乘数后缀
_CN_MULT_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*([万亿])$")
# 英文乘数词（million/billion）与数字组合：1.5 million = 150万。
# 词必须紧贴数字且独立成词（millionaire 之类不算）。
_EN_MULT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(million|billion)\b", re.I)


def _parse_cn_number(text: str) -> int | float | None:
    """中文数字 → 数值：支持 0~亿级组合、零占位、『点』小数。"""
    if not text:
        return None
    whole, _, frac = text.partition("点")
    value = 0.0
    section = 0.0
    digit = 0
    for ch in whole:
        if ch in _CN_NUM_DIGITS:
            digit = _CN_NUM_DIGITS[ch]
        elif ch in _CN_NUM_UNITS:
            unit = _CN_NUM_UNITS[ch]
            if unit >= 10000:
                section = (section + digit) * unit
                value += section
                section = 0.0
            else:
                section += (digit or 1) * unit
            digit = 0
        else:
            return None
    value += section + digit
    if frac:
        for i, ch in enumerate(frac):
            if ch not in _CN_NUM_DIGITS:
                return None
            value += _CN_NUM_DIGITS[ch] * 10 ** -(i + 1)
    return value


_ASCII_ALNUM = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
# 'X of Y' 结构（'Wave 3 of 5' / 'Level 2 of 8'）：总量数字（分母 Y）
# 是范围说明，译文「第 3 波」省略总量是常见合理译法——Y 标记 soft
_OF_DENOM_RE = re.compile(
    r"\d+(?:\.\d+)?\s+of\s+(\d+(?:\.\d+)?)", re.I)

# leetspeak/梗/自嘲原文判定（审核维度 11 同源）：原文含多个明显错拼、
# 网络梗或故意不通的词时，数字一致性从「语义数据强制」降级为「软可省略」
# ——梗文本里形近数字（4=for）与普通数量数字无法从表层可靠区分，宁可从宽
# 放行（漏检只留原文，不误杀正确梗译文）。只认「明确错拼/梗词」命中的
# 数量，不用「像不像英语单词」的启发式（宁漏勿坏：启发式会误伤正常文本）。
_LOW_QUALITY_SOURCE_RE = re.compile(
    r"\b(?:supa|loife|sequl|ware|gud|gr8|kewl|noob|rofl|lol|tho|plz|pls|"
    r"wan|wanna|gonna|cuz|dunno|gimme|gotta|lemme|u|ur|4real|2b|2f|4ever"
    r"|nuc|m8|bro)\b", re.I)
# leetspeak 短语：数字紧贴英文词（4 real、2 fast）——网络梗对字母的
# 形近替代（4=for、2=to）。与普通语义数字（Deal 4 damage）表层不可分，
# 故只作为「低质量」信号之一计数（须另有其他梗/错拼词才判定低质量），
# 不单独豁免。'N of M' 分母（3 of 5）是正常结构，不算 leetspeak 信号。
_LEETSPEAK_PHRASE_RE = re.compile(
    r"\b\d+\s+(?!(?:of|in|on|to)\b)[A-Za-z]{2,}\b", re.I)


def _is_low_quality_source(original: str) -> bool:
    """原文是否明显低质量（梗/自嘲/错拼泛滥）。

    信号 = 独立梗/错拼词命中数 + leetspeak 短语（数字+词）命中数；
    ≥2 个信号判定低质量（Ice Age Baby Adventure 实证：supa/loife/
    sequl/ware/i wan/4 real 都是故意错拼的自嘲文本）。单信号（如普通
    文本 'Deal 4 damage'）不判定，语义数字仍强制保留（宁漏勿坏：启发式
    误判会杀掉正确译文）。
    """
    text = str(original or "")
    if not text:
        return False
    signals = set(_LOW_QUALITY_SOURCE_RE.findall(text))
    signals |= {m.group(0).casefold()
                for m in _LEETSPEAK_PHRASE_RE.finditer(text)}
    return len(signals) >= 2


def _number_tokens(text: str) -> list[tuple[float, bool, bool]]:
    """数字 token 序列（值, 是否百分比, 分母 soft）——原文/译文同口径。

    紧贴 ASCII 字母/下划线的 token 是标识符成分（text0、3D、0x1F、
    v1.2.3、MP3）——技术键名/格式代号，数字无独立数据语义，不参与
    强制。中文前缀不豁免（「有百分之五十」是自然语义数字）。
    """
    soft_spans = [m.start(1) for m in _OF_DENOM_RE.finditer(text)]
    # 英文乘数词（1.5 million）合并为单一 token（=150万），防止数字
    # 部分单独被匹配成 1.5 而乘数词被丢弃（fake-it 数字族根因 C15）
    en_mult_spans: dict[int, tuple[float, bool, bool]] = {}
    for m in _EN_MULT_RE.finditer(text):
        start, end = m.span()
        if (start > 0 and text[start - 1] in _ASCII_ALNUM) \
                or (end < len(text) and text[end] in _ASCII_ALNUM):
            continue
        value = float(m.group(1).replace(",", "."))
        unit = 1e6 if m.group(2).lower() == "million" else 1e9
        en_mult_spans[start] = (value * unit, False, False)
    tokens: list[tuple[float, bool, bool]] = []
    for match in _NUMBER_TOKEN_RE.finditer(text):
        start, end = match.span()
        if start in en_mult_spans:
            # 整段数字+乘数词一起吞掉（match 只覆盖数字部分）
            tokens.append(en_mult_spans.pop(start))
            continue
        if (start > 0 and text[start - 1] in _ASCII_ALNUM) \
                or (end < len(text) and text[end] in _ASCII_ALNUM):
            continue
        raw = match.group(0)
        soft = start in soft_spans  # '3 of 5' 的分母 5 → 可省略
        mult_m = _CN_MULT_RE.match(raw)
        if raw.endswith("%"):
            tokens.append((float(raw.rstrip(" %")), True, soft))
        elif mult_m:
            # 50万 = 500000 / 1.5亿 = 1.5e8（阿拉伯数字 × 中文乘数）
            value = float(mult_m.group(1).replace(",", "."))
            unit = 10000.0 if mult_m.group(2) == "万" else 1e8
            tokens.append((value * unit, False, soft))
        elif "百分之" in raw:
            value = _parse_cn_number(raw[3:])
            if value is not None:
                tokens.append((float(value), True, soft))
        elif raw == "半":
            tokens.append((0.5, False, soft))
        elif raw.endswith(("折", "成")):
            value = _parse_cn_number(raw[:-1])
            if value is not None:
                tokens.append((float(value) * 10, True, soft))
        elif "点" in raw:
            value = _parse_cn_number(raw)
            if value is not None:
                tokens.append((float(value), False, soft))
        else:
            value = _parse_cn_number(raw)
            if value is None:
                # 欧式千分位点分组（200.000 = 二百）仅当整段命中才换算，
                # 否则按普通小数（3.5 保持 3.5，不得当 3 千分位）
                if _DOT_GROUPED_RE.fullmatch(raw):
                    value = float(raw.replace(".", ""))
                else:
                    value = float(raw.replace(",", ""))
            tokens.append((float(value), False, soft))
    return tokens


def _numeric_mismatch(original: str, normalized: str) -> bool:
    """数字一致性检查：原文每个数字 token 必须按序在译文中可匹配
    （数值相同、百分比标记相同）。'X of Y' 分母 soft 可省略（「第 3
    波」省略总量是合理译法）；译文多出的数字不追究（"1-2"→「一到二」
    两边都有；"Press 1 or 2" 的序号回显在两侧一致）。

    低质量原文（梗/自嘲/错拼，如 '4 real now' 的 4=for）→ 数字软可
    省略（Ice Age Baby Adventure 实证：梗文本形近数字被当语义数据会让
    正确译文 numeric_mismatch 恒败 → 重译再被同一门拒 → BLOCKED）。
    """
    src_tokens = _number_tokens(original)
    if not src_tokens:
        return False
    low_quality = _is_low_quality_source(original)
    dst_tokens = _number_tokens(normalized)
    pos = 0
    for value, pct, soft in src_tokens:
        start_pos = pos
        found = False
        while pos < len(dst_tokens):
            d_value, d_pct, _ = dst_tokens[pos]
            pos += 1
            if d_value == value and d_pct == pct:
                found = True
                break
        if not found:
            if soft or low_quality:
                # soft token（'3 of 5' 分母）或低质量原文的数字可省略——
                # 搜索时吞掉的 dst token 可能正是后续硬 token 需要的，
                # 必须回退 pos 防止把硬数字误当缺失（'4 real… 3 gems'
                # 译文「…3 颗宝石」若前一个缺席 token 不回退会把 3 吞掉）
                pos = start_pos
            else:
                return True
    return False


def validate_translation_quality(
    entry: TextEntry,
    translation: str,
    glossary: Iterable = (),
    *,
    check_placeholders: bool = True,
) -> QualityResult:
    normalized = (_normalize_translation(entry.original, translation)
                  if isinstance(translation, str) else "")
    reasons = []
    if not normalized:
        reasons.append("empty_translation")
    if _has_illegal_controls(normalized):
        reasons.append("illegal_control")
    if (_EXPLANATORY_PREFIX.search(normalized)
            or (_EXPLANATORY_PATTERN.search(normalized)
                and len(normalized) >= 20)):
        reasons.append("explanatory_prefix")
    if ("```" in normalized
            or (normalized.startswith(("- ", "* "))
                and not normalized.rstrip().endswith((" -", " *"))
                # "* (You felt...)" 选项文案风格不是 markdown 列表——按原文判定
                and not entry.original.lstrip().startswith(("* (", "- ("))
                # 原文本身以 -/* 开头（"-Love, Sean" 签名、"- Quality Settings -"
                # 装饰标题）→ 译文的 "- " 是原文破折号的延续，不是 markdown 列表
                and not entry.original.lstrip().startswith(("-", "*")))):
        reasons.append("markdown_wrapper")
    if check_placeholders and normalized:
        placeholders_ok, missing_ph, extra_ph = validate_translation(
            entry.original, normalized)
        if not placeholders_ok:
            # 缺失占位符全是完整标签对（<color=green>Paused</color> 整对
            # 丢失、模型用引号替代彩色强调）→ 样式整对损失无崩溃风险、
            # 译文已含中文 → 不算 mismatch（baldis 实证：1.8B 对彩色强调
            # 词的稳定行为是引号替代）。字面 \n（C# 转义换行）缺失同样
            # 放宽：模型把它输出为真实换行/并入相邻行是等价行为（格式
            # 标记非数据，测试实证 '{0}kg\n£{1:0.00}' 首译缺失 \n）。数据
            # 占位符 {0}/{name} 缺失仍判失败（运行时展开会崩溃/显示错误）；
            # extra（模型新增）不在 missing 内、仍由校验失败暴露。
            non_escape_missing = [
                ph for ph in missing_ph if not ph.startswith("\\")]
            # 行首星号规范化：原文 "*SIGH*" / " *Added"（星号紧接词）→
            # 译文 "* sigh *" / "* 加空格"（模型把星号规范为强调/列表
            # 标记）。extra 全为 bullet 且无缺失 → 样式规范化无结构
            # 风险，放行（containment Changelog 4 条实证）；无缺失且
            # extra 非 bullet → 模型新增占位符仍判失败
            extra_all_bullet = bool(extra_ph) and all(
                _BULLET_PLACEHOLDER.fullmatch(ph) for ph in extra_ph)
            if not (missing_ph and _CJK.search(normalized)
                    and (not non_escape_missing
                         or _complete_tag_pairs(non_escape_missing))):
                if not (not missing_ph and extra_all_bullet):
                    reasons.append("placeholder_mismatch")
    src_tags = FORMAT_TAG_PATTERN.findall(entry.original)
    dst_tags = FORMAT_TAG_PATTERN.findall(normalized)
    if src_tags != dst_tags:
        missing_tags = list((Counter(src_tags) - Counter(dst_tags)).elements())
        if not (missing_tags and _CJK.search(normalized)
                and _complete_tag_pairs(missing_tags)):
            reasons.append("rich_text_mismatch")
    if (_newline_events(entry.original) != _newline_events(normalized)
            and not _blank_line_compression(entry.original, normalized)
            and not _interaction_prompt_merge_compression(
                entry.original, normalized)
            # 歌词豁免：引擎单行存储超长歌词（无 \n），模型按句分行输出
            # 是歌词的自然渲染（deadbeat 歌词实证）——分行非结构破坏，
            # 判失败会丢弃完整中文译文。非歌词文本原文单行译文多行仍判。
            and not _is_lyric_like(entry.original)):
        reasons.append("newline_mismatch")
    if (_line_content_topology(entry.original)
            != _line_content_topology(normalized)
            and not _blank_line_compression(entry.original, normalized)
            and not _interaction_prompt_merge_compression(
                entry.original, normalized)
            and not _is_lyric_like(entry.original)):
        reasons.append("line_content_mismatch")
    input_tokens = tuple(
        event.value for event in interaction_input_events(entry.original)
        if event.kind == "literal_glyph"
    )
    source_input_events = tuple(token.casefold() for token in input_tokens)
    # 译文按键序列须包含原文按键序列（子序列语义：顺序一致、允许译文出现
    # 原文按键外的额外字面量 —— "Press 1 for Chapter 1" 的章节号 1、
    # "A: " 说话人标记 A 出现在译文中不算破坏按键顺序）
    target_input_events = _input_token_events(normalized, input_tokens)
    pos = 0
    for token in source_input_events:
        # 键名中文通称豁免（headache 实证 2026-08-12）：'PRESS SPACE'→
        # 「按空格键」、'PRESS ESCAPE'→「按 ESC 键」——按键名译成中文
        # 标准通称/简写是正确翻译，字面量序列检查要求英文键名原文而误杀。
        if any(alias in normalized.casefold()
               for alias in _KEY_ZH_ALIASES.get(token, ())):
            continue
        while pos < len(target_input_events) and target_input_events[pos] != token:
            pos += 1
        if pos == len(target_input_events):
            reasons.append("input_token_mismatch")
            break
        pos += 1
    original_words = _ENGLISH_WORD.findall(entry.original)
    translated_words = _ENGLISH_WORD.findall(normalized)
    if is_interaction_prompt(entry.original):
        action_words = interaction_action_words(entry.original)
        # 原文中被识别为按键的字面量（"press z or enter" 的 enter 是按键不是动词）
        # 或物理键名动作词（enter/return/space…交互提示中多为按键）——
        # 译文保留按键名是正确行为 → 从动作词检查中豁免
        key_tokens = {
            event.value.casefold()
            for event in interaction_input_events(entry.original)
            if event.kind == "literal_glyph"
        }
        key_tokens |= ({word.casefold() for word in action_words}
                       & PHYSICAL_KEY_NAMES_CASEFOLD)
        # 译文引号内专名短语（按钮 "Jump During Playtime"）→ 动作词在
        # 引号内且短语在原文出现 → 是专名短语不是动作残留（baldis 实证：
        # 'Square Button: Jump During Playtime's Jumprope Minigame' 的
        # Jump 是 Jump During Playtime 小游戏名，模型引号包裹保留合理）
        quoted_terms = quoted_proper_terms(normalized, entry.original)
        if any(
            word.casefold() not in key_tokens
            and word.casefold() not in quoted_terms
            and re.search(
                rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])",
                normalized, re.I)
            for word in action_words):
            reasons.append("untranslated_text")
    # 源词剥离专名载体（域名 itch.io / @用户名 / 版本号）后仍无小写词
    # → 专名回显合理（one-thousand-acts-of-decency 真实样本：
    #   "@_domeDev\ndomedev.itch.io" 回显是作者署名，不算未翻译）
    source_words = _ENGLISH_WORD.findall(
        SAFE_KEEPERS.sub(" ", entry.original))
    # 驼峰技术缩写回显豁免（VSync/MonoBehaviour）：译文全部残留词都是原文
    # 含有的驼峰缩写 → 保留原文合理（vincent 'VSync: OFF' 真实样本）
    source_terms_cf = {word.casefold() for word in original_words}
    camel_echo = (
        bool(translated_words)
        and all(is_camel_tech_abbreviation(word) and word.casefold() in source_terms_cf
                for word in translated_words))
    # 小写词用独立词检查（'Stefánsson' 的 ASCII 碎片 nsson 不是小写普通词）；
    # lorem ipsum 占位文本回显是合理行为（zero-deaths 'Loem iipsum solar'）
    # 知识库特殊文本：全大写动作指令（TOSS TRASH）与间隔动作词（* Y A W N *）
    # 是可翻译语义文本，回显一律判失败（不依赖小写词/UI 词典——大写形态
    # 指令既无小写词又常不在 UI 词典，曾被 proper_name_echo 当专名豁免）
    special_action = _is_uppercase_action(entry.original) or _is_spaced_action(
        entry.original)
    # 知识库规则：大写动作指令的译文不得残留原动作动词——"TOSS 垃圾" 是
    # 半翻译（TOSS 是动作动词，必须译成中文"丢"）。回显（无中文）已被
    # untranslated_text 拦截；此检查补充「有中文但残留动作动词」的场景。
    # 判失败触发重试：native 降级路径带 knowledge 译例后模型输出"丢垃圾"。
    # 引号豁免与 untranslated_text 对齐（headache 实证：'PRESS E TO
    # INTERACT' 译「点击"PRESS E"以进行互动」——引号内是模型引用 UI 提示
    # 原文（按钮字面量），不算动作残留）：剥离引号内容后检查引号外是否
    # 仍有裸露动作词——引号外双写残留（「点击"PRESS E"… PRESS」）仍拦。
    if special_action and _CJK.search(normalized):
        quoted_stripped = _QUOTED_SPAN.sub(" ", normalized)
        for word in re.findall(r"[A-Z][A-Z0-9']{1,}", entry.original):
            if (word.casefold() in _UPPERCASE_ACTION_VERBS
                    and re.search(
                        rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])",
                        quoted_stripped, re.I)):
                reasons.append("action_word_residue")
                break
    # 键名强制保留检查：原文含物理键名（Shift/RMB/Esc…）时译文必须保留
    # 该键名——输入提示是键位字面量，译成中文（RMB→「人民币」force-reboot
    # 实证 4 次且被记忆沉淀成词对）让玩家找不到键，且污染词对跨游戏误杀
    # 正确译文（goodmorning 实证）。有中文通称的键（回车/空格/退格/删除）
    # 不强制；RMB/LMB/MMB 保留原文最安全——「右键」与「人民币」无法可靠
    # 区分，宁可判失败（失败只留原文，写错是误导）。纯回显（无中文）由
    # untranslated_text 拦截，本检查只管「有中文但键名被译掉」。
    #
    # 键名兼作普通英语名词（shift/alt/esc…）时，必须区分「键位绑定」与
    # 「普通词义」——Flabby Pizza 实证：'night shift/day shift' 的 shift
    # 是「班次」普通名词，译文「夜班/日班」完全正确却被误杀。判别：键名
    # 作为交互提示的字面量输入事件出现（interaction glyph），或源文该词
    # 首字母大写（专有键拼写 Shift/RMB/Esc）→ 判定为键位绑定才强制保留；
    # 源文小写普通名词（night shift）→ 键名检查跳过，由 untranslated_text
    # 与其它规则承接。
    if _CJK.search(normalized):
        glyph_key_tokens = frozenset(
            event.value.casefold()
            for event in interaction_input_events(entry.original)
            if event.kind == "literal_glyph")
        for key in _KEY_LABEL_CASEFOLD:
            key_match = re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])",
                entry.original, re.I)
            if not key_match:
                continue
            binding_context = (
                key in glyph_key_tokens
                or key_match.group(0)[:1].isupper())
            if not binding_context:
                # 小写普通名词形态（night shift）→ 非键位绑定，键名检查跳过
                continue
            if (not re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])",
                    _STRIP_RICH_TEXT.sub(" ", normalized), re.I)
                    and not any(
                        alias in normalized.casefold()
                        for alias in _KEY_ZH_ALIASES.get(key, ()))):
                reasons.append("key_name_mistranslated")
                break
    # 数字一致性（Q1 P2）：原文含数字（含百分比）时译文必须保留相同
    # 数值——数字是玩家可验证的数据（伤害/价格/概率/坐标），失真进
    # 游戏就是错误。允许中文等价（50→五十/五十点、10%→百分之十）。
    # 日志/格式模板豁免：模板数字多为格式说明（%d）与索引（wave 3），
    # 且 _is_log_template/_is_format_template 内的数字本来可自由改
    # 写（译文的 %d 不换数字）；占位符 {0} 的 0 两侧一致不受影响。
    if (not is_log_template(entry.original)
            and not _is_format_template(entry.original)
            and _numeric_mismatch(entry.original, normalized)):
        reasons.append("numeric_mismatch")
    # 全大写 ≤3 字母缩写回显（MAX/SFX/UI/OK）：单 token 缩写是界面
    # 标准术语，1.8B 模型稳定回显（count-my-coins 'SFX' 实证；proper_name
    # echo 侧已有同规则 1847 行，本门补一致；driftapocalypse 'MAX' ×3
    # 实证重试耗尽仍回显）。要求原文与译文残留词全为 ≤3 全大写缩写，
    # 防 'GAME OVER'/'TOSS TRASH' 类多词回显误放行（动作指令
    # special_action 不豁免）。
    short_abbr_echo = (
        translated_words
        and not special_action
        and all(len(w) <= 3 and w.isupper() for w in translated_words))
    if (original_words and translated_words and not _CJK.search(normalized)
            and semantic_target_text(entry.original, entry.original)
            and not is_lorem_ipsum_placeholder(entry.original)
            and not camel_echo
            and not _artistic_case_echo(entry.original, normalized)
            and not _glossary_keep_echo(entry.original, normalized, glossary)
            and not short_abbr_echo
            and not is_log_template(entry.original)
            and not _is_format_template(entry.original)
            # 低质量原文（梗/自嘲/错拼，supa/loife/sequl/ware…）回显豁免：
            # 原文本就是故意错拼的「broken English」，模型保留原文是合理
            # 行为（come-back 实证：'supa mario in real loife' 回显被
            # untranslated_text 拒 → 强制重译再回显 → BLOCKED 留人工，
            # 与审核维度 11 低质量豁免对齐——审核端已放行，质量门不应
            # 再拦）。真可翻译句子（非低质量）回显仍判失败。
            and not _is_low_quality_source(entry.original)
            and (has_independent_lower_word(entry.original)
                 or special_action
                 or any(
                    word.casefold() in _DISPLAY_WORDS_CASEFOLD
                    for word in _ui_check_words(source_words)))):
        # 纯品牌/署名串（Playstation、Xbox）模型保留原文是合理行为，不算未翻译
        # （传原文自身：从原文中移除其保护术语后仍有内容才算未翻译；
        #  原文全为专名形态（Crash Bandicoot/Roquette/Profiler 无小写词、
        #  不在 UI 词典）时模型回显也是合理行为；'Continue'/'SFX' 在 UI
        #  词典 → 回显仍判失败；glossary keep 术语组成的原文（hiss pop
        #  collection）TitleCase 化回显是模型保留术语 → 豁免）
        reasons.append("untranslated_text")
    pairs = _glossary_pairs(glossary)
    # 精确词对优先：原文精确匹配某词对 source 时，只查该词对——子串
    # 词对会把正确译文误判 glossary_mismatch（force-reboot 实证：
    # ENTER NAME→输入姓名 被独立词对 (NAME→名称) 子串命中判失败；
    # (FOX→狐狸) 子串命中专名 FOXYPAW 同样误伤）。原文精确命中表示
    # 该词对就是本条目的权威译名，其它子串词对语义不相关，跳过。
    source_cf = entry.original.strip().casefold()
    exact_pairs = [
        (s, t) for s, t in pairs
        if s.strip().casefold() == source_cf]
    if exact_pairs:
        pairs = exact_pairs
    # 键名词对豁免：source 是物理键名（RMB→人民币、SHIFT→移位、
    # SPACE→空间）→ 键名不该被翻译，该词对是错误沉淀（force-reboot
    # 实证：RMB 译「人民币」通过质量门被记忆沉淀 active）——跳过检查，
    # 正确译文保留键名不判失败（goodmorning 'Camera Control - Shift +
    # RMB' 实证被误杀）。用全键名集而非强制表：space 因有中文通称被
    # 排除出强制表，但 (SPACE→空间) 词对同样是污染（headache 实证：
    # happy-cat-tavern 的 UI 词 Space→空间 learn 沉淀 → PRESS SPACE
    # 译文「按空格键」被 glossary_mismatch 误杀）。
    pairs = [
        (s, t) for s, t in pairs
        if s.strip().casefold() not in PHYSICAL_KEY_NAMES_CASEFOLD]
    # 功能词/单字母词对豁免（2026-08-13 F10）：功能词（on/off/in…）
    # 是句子功能成分，做全局强制必然误杀自然文本（incremental-rts
    # 'Analytics is ON.' → 译文「已开启」被 (ON→关于) 误杀；URL 内
    # on token 同样误杀）——与沉淀端（agent_memory 功能词不晋升
    # active）对齐：功能词词对一律不做强制（沉淀端已挡新入库，
    # 本处兜底存量数据）。单字母 ASCII 词对（'<b>' 标签的 b 被审核
    # 错误提取成词对 → 整句译文当词对翻译）同样无强制价值，跳过。
    # 高频普通词（miss/health）不在此过滤：按钮动词（save/play）与
    # 语境变体词（miss/health）同在 C5 审核拒绝表，但 quality 端
    # 强制有价值（漏翻拦截）——语境误杀由 _label_context_match
    # 与占位符边界豁免处理（'miss: 999' 标签强制保留、
    # '{health}' 占位符内豁免）。
    pairs = [
        (s, t) for s, t in pairs
        if s.strip().casefold() not in FUNCTION_WORDS
        and not (len(s.strip()) == 1 and s.strip().isascii()
                 and s.strip().isalpha())]
    # 方向词单 token 词对（F11，2026-08-13）：left/right/up/down 做
    # 全局强制必然误杀自然副词（incremental-rts '(buy factories on
    # the right)' 译文「请在右侧购买工厂」被 (RIGHT→对) 误杀——
    # force-reboot 沉淀的方向词单字对，正是「方向/设备词单字对是
    # 污染源」教训的落实缺口：方向词在普通文本可自由译（正确的/
    # 右侧/上面），方向语义由 _input_binding_context 专用检查负责
    # （输入绑定语境强制查方向字），词对层不做全局强制。
    pairs = [
        (s, t) for s, t in pairs
        if s.strip().casefold() not in _DIRECTION_WORD_ZH]
    for source, target in pairs:
        # 对话脚本豁免（F13，2026-08-13 interdream 实证）：Undertale 系
        # 对话（行首 "* " / "^NN" 计时码 / 全大写喊话）是叙事自由语义
        # 文本——(PLACE→地点) 杀「那个地方」、(NAME→名称) 杀「你叫
        # 什么名字？」、(Time→时间) 杀「时光/时机」、(Play→播放) 杀
        # 「玩 UNO」、(SHOOT→射击) 杀感叹「哦，天哪」。单 token 普通
        # 词词对强制必然误杀意译——keep 型（target==source 保留映射）
        # 与多词短语词对（语义确定性高）仍强制。
        if (" " not in source.strip()
                and target.strip().casefold() != source.strip().casefold()
                and _dialogue_script_like(entry.original)):
            continue
        # 文件名/路径段豁免（F11）：player-diagnostics.txt 的 Player
        # 是文件名的一部分（incremental-rts 实证：'Export player-
        # diagnostics.txt...' 译文保留文件名被 (Player→玩家) 误杀
        # glossary_mismatch——文件名是标识符不是可翻译语义文本）。
        # 检测：token 所在连续段含 '-'/'_' 且以 .+扩展名结尾。
        if _in_filename_segment(source, entry.original):
            continue
        # 单 token 词对子串命中：仅标签语境检查——普通词词对（TIME→时间、
        # NAME→名称）子串命中自然句必然误杀意译（goodmorning 'time to
        # take on' 译文「是时候」实证）→ 非标签语境跳过；多词短语词对
        # （ENTER NAME）与精确命中（fix-26 已收窄 pairs）不受影响。
        if (" " not in source.strip()
                and _label_context_match(source, entry.original) is False):
            continue
        # 大小写不敏感：自动沉淀的专名保留映射（KRAPOS→KRAPOS）与模型
        # 回显的 TitleCase 变体（Krapos）是同一词形态变体——大小写敏感
        # 检查把合法专名回显误判 glossary_mismatch（count-my-coins
        # 'Krapos' 实证：learn 时保留检测 casefold，quality 检查却没
        # casefold，自相矛盾）
        if (source_term_applies(source, entry.original)
                and target.casefold() not in normalized.casefold()):
            # 保留型术语（term→term 原样，learn_proper_names 自动沉淀的
            # 专名/缩写保留映射）：模型把该词翻译成中文是合理行为——
            # "FPS" 译成"帧率"优于强制保留（backrooms 实证：自动沉淀
            # FPS→FPS 后质量门拒绝更忠实的「输入自定义帧率...」）。
            # 仅当译文无中文翻译（纯回显/丢失）时保留型术语仍判失败。
            if (target.strip().casefold() == source.strip().casefold()
                    and _CJK.search(normalized)):
                continue
            # 法语原文豁免：重音字母/法语功能词说明原文是法语（或含法语
            # 段）→ 英语术语表双关词不适用（faerie-afterlight 实证 9 条：
            # 'Hé, c'est encore vous !' 的 encore=又/再，术语 (encore, 安可)
            # 是演出借词含义；'I miss my father' 的 miss=想念由动词豁免
            # 处理）。防过宽：英语原文（'Encore! Encore!'）无法语特征 →
            # 术语照常生效
            if _french_marker(entry.original):
                continue
            # 专名邻接豁免：术语在原文中邻接 TitleCase 词 → 术语是专名
            # 的一部分，与术语表普通词含义无关（deadbeat 'Miss Fire
            # Spitting' 角色名的 Miss 邻接 Fire，误命中 (miss, 未命中)；
            # 'Slash key' 的 Slash 右邻 key 小写 → 不豁免，触发术语
            # 确定性修复）
            if _glossary_proper_phrase(source, entry.original):
                continue
            # 动词用法豁免：术语词在原文中是动词用法（前邻 to/助动词，
            # "shouldn't be hard to miss" 的 miss=错过）→ 与术语表的
            # 标签含义无关（doubleshake d_scrap14 实证：译文「遗漏」
            # 语义正确被 (miss, 未命中) 误判 glossary_mismatch）；
            # 'miss: 999' 标签格式前邻冒号 → 不豁免
            if _glossary_verb_usage(source, entry.original):
                continue
            # 歌词语境豁免：歌词文本整体豁免普通词术语检查（押韵词/
            # 拟声词与术语表无关，'Miss, hit' 实证；keep 型与专名豁免
            # 已在上方处理，此处只豁免普通词术语）
            if _is_lyric_like(entry.original):
                continue
            reasons.append("glossary_mismatch")
            break
    # 方向语义检查（输入绑定语境）：原文含方向词（left/right/up/down）+
    # 译文有中文 → 译文必须含对应方向字。1.8B 在 HUD 方向指令上把方向词
    # 译成「正确/抬起/按住」类语义错（ffs 'Hat Right'→'正确' 实证），
    # 术语表 (Right, 右拨片) 曾拦截但误杀普通文本（'pick the right door'
    # → '正确的门'）——改为仅输入绑定语境检查：无设备词/键位后缀的普通
    # 文本方向词自由翻译（right=正确的/右边），不查方向字。
    if _input_binding_context(entry.original) and _CJK.search(normalized):
        missing = [
            zh
            for word, zh in _DIRECTION_WORD_ZH.items()
            if re.search(
                rf"(?<![A-Za-z0-9_]){word}(?![A-Za-z0-9_])",
                entry.original, re.I)
            and not any(c in normalized for c in zh)]
        if missing:
            reasons.append("direction_mismatch")
    max_chars = entry.meta.get("max_chars")
    if (type(max_chars) is int and max_chars > 0 and len(normalized) > max_chars):
        # 超长不判失败：译文质量合格只是物理容量放不下——写回端 _fit_bytes
        # 按容量收尾 + 省略号（部分翻译）。判失败会把好译文整体丢弃、游戏
        # 里只剩原文（taxes 'I did ' 实证 9 字符译文 vs 6 码元容量）。
        # 超出的量记入 meta，写回报告与人工校对可见。
        entry.meta = dict(entry.meta)
        entry.meta.setdefault("length_over_budget", len(normalized) - max_chars)
    ordered = tuple(dict.fromkeys(reasons))
    confidence = entry.confidence if entry.confidence in {"high", "medium", "low"} else "medium"
    return QualityResult(not ordered, confidence, ordered, normalized)


def is_write_ready(status: str, translation: str, meta) -> bool:
    """只有已验、非低置信（或经人工提升）且审核终态可发布的译文可自动写回。

    两道防线：
    1. 机械质量门：status=translated、译文非空、quality_passed=True、
       置信度非 low（或人工提升）；
    2. 审核发布门（Phase A，2026-08-13）：review_outcome 显式终态非
       APPROVED/APPROVED_MINOR 拒绝；旧字段 review_blocked/review_error/
       need_revision/need_retranslate 或 review_level=MAJOR/CRITICAL 拒绝。
       ——MAJOR/CRITICAL、审核错误、未收敛条目一律不可写回。
    """
    if status != "translated" or not translation:
        return False
    if isinstance(meta, str):
        try:
            import json
            evidence = json.loads(meta or "{}")
        except (json.JSONDecodeError, TypeError):
            return False
    elif isinstance(meta, dict):
        evidence = meta
    else:
        return False
    if evidence.get("quality_passed") is not True:
        return False
    if evidence.get("confidence", "medium") != "low" \
            or evidence.get("confidence_promoted") is True:
        return review_publishable(evidence)
