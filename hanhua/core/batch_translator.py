from __future__ import annotations
import hashlib
import json
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import replace
from typing import Callable

from hanhua.core.agent_memory import context_key_of
from hanhua.core.engine_strings import (interaction_input_events,
                                        interaction_input_tokens)
from hanhua.core.knowledge import (_ACTION_VERB_ZH, _JAPANESE_KANA_RE,
                                   _is_language_name, _is_multilingual_source,
                                   _is_spaced_action, _is_uppercase_action,
                                   aggregate_spaced_letters,
                                   language_option_translation)
from hanhua.core.placeholders import (DISPLAY_WORDS, SAFE_KEEPERS,
                                      self_heal_format_tags)
from hanhua.core.local_model import sanitize_exception
from hanhua.core.models import (TextEntry, TranslateStats, STATUS_FAILED,
                                STATUS_TRANSLATED, is_actionable_translation)
from hanhua.core.protected_spans import (protected_slot_parts,
                                         semantic_target_text)
from hanhua.core.prompts import build_batch_user_prompt
from hanhua.core.quality import (_CJK, _EXPLANATORY_PATTERN,
                                 _EXPLANATORY_PREFIX, _glossary_keep_echo,
                                 _glossary_pairs, _is_format_template,
                                 _is_lyric_like, _ui_check_words,
                                 PHYSICAL_KEY_NAMES_CASEFOLD,
                                 QualityResult,
                                 has_independent_lower_word,
                                 is_camel_tech_abbreviation,
                                 is_chinese_source,
                                 is_log_template,
                                 is_lorem_ipsum_placeholder,
                                 quoted_proper_terms,
                                 source_term_applies,
                                 validate_translation_quality)
from hanhua.core.translator import (BUILTIN_UI_REFERENCES,
                                    BUILTIN_UI_SOURCE_TERMS, BaseClient,
                                    ServiceUnavailableError,
                                    extract_json_array,
                                    extract_json_array_fallback,
                                    merge_translation_references,
                                    strip_prompt_echo,
                                    translate_source_directive)


# 译文残留英文检测（target_script_mismatch）：
# 连续短语 = 两个 3+ 字母英文词之间有非字母非中文的间隔（明确半翻）。
# 注意间隔必须「非空且非纯字母」——否则单词 Escape/YouTube 会被正则回溯
# 拆成 Esc+ape 假匹配。'Escape会退出游戏'（间隔只有中文）不算短语。
_ENGLISH_PHRASE = re.compile(
    r"[A-Za-z]{3,}[^A-Za-z㐀-鿿豈-﫿]+[A-Za-z]{3,}")
# 原文引号内片段（内嵌引文/铭文/题词，如 "To the house of ..."）：译文
# 保留其原文是正确行为（alisa-demo 实证同一引文的三语言版译文都被误判
# 英文残留）→ 引号内容中的英文词在译文出现时豁免
_QUOTE_CONTENT = re.compile(
    r"[\"“”«»「」『』]([^\"“”«»「」『』]{1,80})[\"“”«»「」『』]")
_ENGLISH_WORD = re.compile(r"[A-Za-z]{3,}")
# 标签-值格式串（'slash: 999' / 'encore 1'——deadbeat 实证：HUD 计数
# 显示模板「标签: 值」或「标签 值」，模型仅做大小写规范化回显
# （'Slash: 999'）→ 词级补译的 TitleCase 检查把它当专名跳过 → 按原文
# 形态恢复标签整词补译；空格分隔格式（encore 1）同属 HUD 标签；艺术
# 大小写（'eNCORE 1'）是标签原样（deadbeat 实证）——标签首字符大小写
# 不设限，提取后补译/译例替换大小写不敏感）
_LABEL_VALUE_FORMAT = re.compile(r"^([A-Za-z]{2,16})(?:: ?| )(\d+)$")
# 重音拉丁字母 → ASCII 一对一词符映射（长度不变，索引对齐保持）：
# _ENGLISH_WORD 是纯 ASCII 正则，带重音专名会被拆成碎片（"Pulsomètre" →
# "Pulsom"+"tre"），碎片 "tre" 是小写普通词 → 误判英文残留（alisa-demo
# 实证：法语设备名 Pulsomètre 保留在译文被判 target_script_mismatch）。
# 语义英文词提取前归一化 → 重音专名成完整词走 TitleCase 豁免；
# 非 ASCII 字母检查仍用归一化前的语义串（假名/西里尔残留照常拒绝）
_ACCENT_TO_ASCII = str.maketrans(
    "àáâãäåçèéêëìíîïñòóôõöùúûüýÿßøæœðþ"
    "ÀÁÂÃÄÅÇÈÉÊËÌÍÎÏÑÒÓÔÕÖÙÚÛÜÝØÆŒÐÞ",
    "aaaaaaceeeeiiiinooooouuuuyyssaoet"
    "AAAAAACEEEEIIIINOOOOOUUUUYOAEDT")
_AT_USER = re.compile(r"@[\w.-]+")
# @用户名紧邻的 2-4 字母显示名（"fie (@zkfie" 末尾的 fie）→ 作者名豁免
_DISPLAY_NAME_BEFORE_AT = re.compile(r"[A-Za-z]{2,4}(?=\s*[\(\s,]*$)")
# UI 词典词（casefold）：模型保留这些词 = 半翻失败；大写专名（Windows/CBS）豁免
_DISPLAY_WORDS_CASEFOLD = {word.casefold() for word in DISPLAY_WORDS}
# 问候语：译文首行保留英文问候（Hello, there. / Hi!）是本地化惯例，
# 其余已译为中文时豁免（mimic-search/soul-delivery 真实样本）
_GREETING_WORDS = {"hello", "hi", "hey"}
# 内置 UI 术语（BUILTIN_UI_SOURCE_TERMS）：模型回显 = 未翻译（SFX/Quit/Volume…）
_BUILTIN_UI_TERMS_CASEFOLD = {
    str(term).casefold() for term in BUILTIN_UI_SOURCE_TERMS}
# Q1 语义门：BUILTIN_UI_REFERENCES 精确对照（source casefold → 权威中文
# 译名）。原文精确命中 source 时，译文必须包含参考译名——'Resume'→'简历'
# 这类「有中文、占位符齐」的形式门全过错误被此门拦截（审计 Q1：参考译文
# 只进 prompt 不进质量门；Q2：错误译文进记忆 + 一致性锚定复制污染）。
# 子串匹配宽容合理变体（'继续游戏' 含 '继续'；'itch 页面' 含 'itch' 保留
# 平台名），同时拦下不相关译文（'简历' 不含 '继续'）。
_BUILTIN_UI_EXACT = {
    source.strip().casefold(): target
    for source, target in BUILTIN_UI_REFERENCES}
# 英语功能词（冠词/介词/连词/代词/be 动词等）：原文 TitleCase 形态
# （句子开头 "The End is near" 的 The）不是专名——译文小写残留（"the End"）
# 是真实半翻，不得走小写化专名豁免（baldis 实证的是 Bossfight→bossfight
# 这种真专名，the/save 这类普通词残留必须仍判失败）
_ENGLISH_FUNCTION_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "nor", "so", "yet", "for",
    "of", "in", "on", "at", "to", "from", "by", "with", "without",
    "over", "under", "is", "are", "was", "were", "be", "been", "being",
    "am", "it", "its", "this", "that", "these", "those", "you", "your",
    "my", "our", "we", "they", "he", "she", "his", "her", "their",
    "i", "me", "us", "them", "him", "do", "does", "did", "not", "no",
    "off", "out", "up", "down", "if", "as", "than", "then", "into",
    "onto", "upon", "after", "before", "when", "where", "while", "who",
    "what", "which", "why", "how", "there", "here", "all", "any",
})
# 译文空段（\n\n 空行）：多行文本的段整体漏译证据——换行合并兜底只放行
# 「合并」不放行「段丢失」（测试实证：Second → 空行必须失败）
_EMPTY_SEGMENT = re.compile(r"\n[ \t]*\n")
# 键盘噪音重复 3-gram（sdfsdfsdfsdfsdfsdf = 'sdf'×6：开发者乱打测试串）
_KEYBOARD_NOISE_3GRAM = re.compile(r"([a-z]{3}).*\1")
# 文件引用词（readme/changelog/license/credits：文件名/章节名，游戏中
# 出现时保留原文是合理行为——'Fixed name in readme' 的 readme 引用
# README 文件，与普通名词漏翻（ram）性质不同）
_FILE_REFERENCE_WORDS = frozenset(
    {"readme", "changelog", "license", "credits"})


# 测试噪音块：原文中的无空格字母块 + 乱串形态（重复 3-gram / 罕见辅音
# 连缀）→ 整块是开发者乱串（doubleshake 实证：测试文本
# 'aksjdhashd/asdlajsdhasjkdh/asdasdasd' 与可译句同条目）。其子串
# （asd ⊂ asdasdasdasd）同属噪音段，模型保留是正确行为。
_NOISE_BLOCK_RE = re.compile(r"[A-Za-z]+")
# 罕见辅音连缀：3+ 连续辅音且含 j/q/z/k——英语真实词中 j/q/z 不参与
# 辅音连缀、k 仅双连（sk/ck），3+ 连出现即乱串形态（aksjdhashd 的
# ksjdh、asdlajsdhasjkdh 的 jsdh）；length 的 ngth/spring 的 spr 等
# 真实组合不含这些字母，照常判漏翻
_RARE_CONSONANT_RUN = re.compile(
    r"[bcdfghjklmnpqrstvwxyz]*[jqzk][bcdfghjklmnpqrstvwxyz]*")
# 连字符专名（Loam-arino）：小写段是专名的一部分，模型保留不是漏翻
# （doubleshake 'Hi, Loam-arino!' → 嗨，Loam-arino！实证）


def _noise_blocks(original: str) -> list[str]:
    """原文中的测试噪音块：≥8 字符 + 重复 3-gram（sdfsdfsdfsdfsdfsdf）
    或 ≥6 字符 + 罕见辅音连缀（aksjdhashd）→ 开发者乱串块。"""
    blocks = _NOISE_BLOCK_RE.findall(original)
    return [b for b in blocks
            if (len(b) >= 8 and _KEYBOARD_NOISE_3GRAM.search(b))
            or (len(b) >= 6 and any(
                _RARE_CONSONANT_RUN.fullmatch(run)
                for run in re.findall(r"[bcdfghjklmnpqrstvwxyz]{3,}", b)))]


def _kept_word_plausible(original: str, word: str) -> bool:
    """模型保留的原文词是否「非普通英语词漏翻」→ 放行合理。

    形态特征（满足任一）：
    - 键盘噪音：≥8 字符 + 重复 3-gram（sdfsdfsdfsdfsdfsdf 开发者乱串）
    - 罕见辅音连缀：≥6 字符 + 3+ 连续辅音含 j/q/z/k（aksjdhashd 类
      一次性乱串——无重复 3-gram 但辅音组合是英语没有的）
    - 噪音块子串：词是原文乱串块（asdasdasdasd）的一段（asd）
    - 连字符专名段：词是原文连写专名（Loam-arino）的一部分（arino）
    - 命令/参数语法：原文中词紧跟 [（playsub [character] 的命令名）或
      词在方括号内（[identifier] 的参数占位符）
    - 文件引用词（readme/changelog/license/credits）

    普通英语词（ram/ragdoll/name/length/spring）不具备上述特征 → 仍判
    漏翻失败（test_common_word_leftovers_still_target_script_mismatch
    固化）。
    """
    w = word.casefold()
    if len(w) >= 8 and _KEYBOARD_NOISE_3GRAM.search(w):
        return True
    if len(w) >= 6 and any(
            _RARE_CONSONANT_RUN.fullmatch(run)
            for run in re.findall(r"[bcdfghjklmnpqrstvwxyz]{3,}", w)):
        return True
    if any(w in b.casefold() for b in _noise_blocks(original)):
        return True
    # 连字符专名段：词是原文连写专名的一段（Loam-arino 的 arino、
    # 词-词 任意一段，大小写不敏感）
    if (re.search(rf"(?<![A-Za-z])[A-Za-z]+-{re.escape(word)}(?![A-Za-z])",
                  original, re.I)
            or re.search(rf"(?<![A-Za-z]){re.escape(word)}-[A-Za-z]+(?![A-Za-z])",
                         original, re.I)):
        return True
    if w in _FILE_REFERENCE_WORDS:
        return True
    if re.search(r"\b" + re.escape(word) + r"\s*\[", original, re.I):
        return True                              # 命令名后跟参数（playsub [x]）
    return re.search(
        r"\[[^\]]*" + re.escape(word) + r"[^\]]*\]", original, re.I) is not None
# 聊天/控制台命令（"/kick" 引号包裹 或 /give 独立词）：游戏命令保留原文是
# 正确行为（Slendergus 真实样本）→ 从英文残留判定中移除
_SLASH_COMMAND = re.compile(
    r"[\"']/[A-Za-z][A-Za-z0-9_/-]*[\"']"
    r"|(?:^|(?<=\s))/[A-Za-z][A-Za-z0-9_-]*")


def _entry_id(e: TextEntry) -> str:
    return f"{e.key_path}@{e.file_id}"


def _replace_word_first(text: str, word: str, replacement: str) -> str:
    """整词边界替换第一次出现（词级补译单词替换用——裸 replace 会把
    'the' 替换进 other/these 的子串）。"""
    return re.sub(rf"\b{re.escape(word)}\b", replacement, text, count=1)


def _is_lyric_source(text: str) -> bool:
    """歌词/韵律文本特征：超长 + 歌词形态。1.8B 模型对歌词稳定续写/
    截断而非完整翻译——纯英文歌词续写英文（'Tonight...' 2677 字符
    实证）、含假名歌词只译首句（'(Three, two, one) わたし...' 实证，
    常规路径多语言双跳也救不回）→ 歌词专用路径（中文引导+限长+高
    重复惩罚）逐句翻译。判定双分支：
    - ≥1000 纯西文无换行（Tonight 类，无标记纯歌词）
    - ≥500 含歌词形态（假名/汉字或括号音乐标记，Guh/Three 类）"""
    if len(text) < 500:
        return False
    if _is_lyric_like(text):
        return True
    return (len(text) >= 1000
            and "\n" not in text
            and not re.search(r"[一-鿿぀-ヿ가-힯]", text))


def _split_translation_segments(text: str) -> tuple[str, list[str], list[str]]:
    """拆分为可逐段翻译的片段（返回 (前缀, 片段, 分隔符)）。

    换行优先（保留换行符与字面 \\n）；无换行的长单段文本按句子边界拆
    （保留标点后空白）—— 长 prompt 超出 ctx 时模型回显原文是稳定行为
    （untranslated_text），短句回显概率极低，拆句逐段翻译后拼接。
    空段（空行/纯空白）并入**前一个**非空段的分隔符原位保留，不单独
    发请求；前导空白作为前缀返回（挂在译文开头）。
    """
    parts = re.split(r"(\\n|\r\n|\r|\n)", text)
    if len(parts) > 1:
        prefix = ""
        segments: list[str] = []
        separators: list[str] = []
        for i in range(0, len(parts) - 1, 2):
            piece, separator = parts[i], parts[i + 1]
            if piece.strip():
                segments.append(piece)
                separators.append(separator)
            elif segments:
                separators[-1] += piece + separator
            else:
                prefix += piece + separator
        if parts[-1].strip():
            segments.append(parts[-1])
            separators.append("")
        elif segments:
            separators[-1] += prefix + parts[-1]
        elif not segments:
            prefix += parts[-1]
        return prefix, segments, separators
    pieces = re.split(r"(?<=[.!?。！？])(\s+)", text)
    prefix = ""
    segments, separators = [], []
    for i in range(0, len(pieces), 2):
        piece = pieces[i]
        separator = pieces[i + 1] if i + 1 < len(pieces) else ""
        if piece.strip():
            segments.append(piece)
            separators.append(separator)
        else:
            prefix += piece + separator
    return prefix, segments, separators


def _auto_translatable(entry: TextEntry) -> bool:
    return is_actionable_translation(entry)


# Q3 失败原因分类体系：reason → 策略类别（审计报告 Q3——16 个失败 reason
# 平铺无分类，失败条目每轮全量重跑同一条链、无 attempt 预算，「同样的问题
# 反复出现」的机制根源是原因不驱动策略）。
# - request：API/网络级失败（request_error）——外部瞬时可恢复，重试预算高；
# - model_behavior：模型输出质量失败（回显/漏译/格式错/占位符破坏）——重试
#   大概率复败（小模型稳定行为 + Q2 记忆锚定放大），预算低；耗尽后条目保持
#   失败状态不再重试，报告按类别可见「该翻未翻」而非每轮重复烧 token；
# - content_inherent：条目内容不可译——单次验证即放弃。
# 质量门 reason（quality.py 全部）默认归 model_behavior；只有显式列出的
# 才归其他类别。content_inherent 无直接 reason 映射——由
# _record_failure_attempt 按原文内容判定赋值（Q3 C1 死代码修复）。
FAILURE_CATEGORIES = {
    "request_error": "request",
    "content_inherent": "content_inherent",
}
# 每类别 attempt 预算（meta["attempt_count"] 跨轮累计，store 持久化）
_MAX_ATTEMPTS = {"request": 3, "model_behavior": 2, "content_inherent": 1}

# C2 规则版本戳：attempt 预算与「规则修复→重跑」工作流的协调。
# 预算耗尽条目永久不进 run_scope，而历史上 F22 类豁免修复恰恰依赖跨轮
# 重跑验证（faerie run1→run2 35 条转成功、doog 4 条转成功）——预算无
# 版本戳时，规则升级不会重置，被旧规则误判锁死的条目无法自我验证修复。
# 版本从质量门/修复链核心函数字节码自动派生：函数体变化 → co_code/
# co_consts 变化 → 版本变化 → 预算自动清零；注释/文档字符串变化不触发
# （不反映行为）。_attempt_exhausted 见版本不符即放行，等价于「规则
# 修复后定向重跑自动生效」。
_RULES_VERSION_CACHE: int | None = None


def _rules_version() -> int:
    global _RULES_VERSION_CACHE
    if _RULES_VERSION_CACHE is None:
        digest = hashlib.sha256()
        for fn in (_inherent_untranslatable, _auto_translatable,
                   BatchTranslator._apply_quality,
                   validate_translation_quality):
            code = fn.__code__
            digest.update(code.co_code)
            digest.update(repr(code.co_consts).encode("utf-8"))
        # 预算/分类常量变化同样属于规则升级
        digest.update(repr(_MAX_ATTEMPTS).encode("utf-8"))
        digest.update(repr(FAILURE_CATEGORIES).encode("utf-8"))
        _RULES_VERSION_CACHE = int.from_bytes(digest.digest()[:4], "little")
    return _RULES_VERSION_CACHE


def _inherent_untranslatable(text: str) -> bool:
    """内容不可译判定：原文无任何自然语言内容（英文词/汉字/日文假名）。

    Q3 C1 死代码修复的赋值路径——FAILURE_CATEGORIES/_MAX_ATTEMPTS 声明
    了 content_inherent 类别但 16 个失败 reason 无一会映射到它，类别
    从上线起永不触发。字幕分隔线 '-----'、纯装饰符号串、纯数字/版本号
    等条目（提取侧 role=display 放行）失败后任何重试都不会产出有意义
    译文，白烧 token → 首次失败即归此类、单次验证后不再重试。
    保守边界：带 3+ 字母词（'https://…'、'© 2024 Game Studio'）视为
    有可译内容，归 model_behavior 重试——宁可多试一次，不可误判可译
    文本为不可译。
    """
    if not text:
        return False
    return not (_ENGLISH_WORD.search(text)
                or _CJK.search(text)
                or _JAPANESE_KANA_RE.search(text))


def _clear_review_state(meta: dict) -> None:
    """重译成功写入前清旧审核终态（#9：重译是新的开始）。

    审核阻断（review_outcome=BLOCKED/NEEDS_REVISION、review_blocked 等
    残留）会让发布门 fail-closed 拒绝重译成功的译文——用户「重试失败/
    标记为待翻译」后重译成功仍不可写回，失败文本无法自己处理。与
    manual_correction 的 _REVIEW_STATE_CLEAR 同一清理语义；quality 门
    字段由调用方随后重写。
    """
    for field in ("review_outcome", "review_blocked", "review_error",
                  "need_revision", "need_retranslate", "review_level",
                  "review_reason", "review_suggestion", "review_error_kind",
                  "review_blocked_rounds", "rejected_candidate",
                  "quality_reasons", "review_issue"):
        meta.pop(field, None)


def _record_failure_attempt(entry: TextEntry, reason: str) -> None:
    """Q3 失败记账：attempt_count 跨轮累计 + failure_category 分类。

    幂等安全：同一 entry 同轮多次失败（repair 路径）只递增一次——
    以 meta 中的 attempt_mark 防重复计账；跨轮（新 run）自然继续累计。
    """
    meta = dict(entry.meta)
    mark = meta.get("_attempt_mark")
    current = id(entry)
    if mark == current:
        return
    meta["_attempt_mark"] = current
    attempts = int(meta.get("attempt_count", 0)) + 1
    meta["attempt_count"] = attempts
    category = FAILURE_CATEGORIES.get(reason, "model_behavior")
    if category == "model_behavior" and _inherent_untranslatable(entry.original):
        # 内容不可译：原文无自然语言（纯符号/数字/URL 等），重试必复败
        category = "content_inherent"
    meta["failure_category"] = category
    # C2：记账时挂当前规则版本——规则升级后旧预算自动失效
    meta["_rules_version"] = _rules_version()
    entry.meta = meta


def _attempt_exhausted(entry: TextEntry) -> bool:
    """Q3 预算判定：attempt_count 达到类别上限 → 不再进入翻译链。

    C2：预算挂规则版本戳——meta 中版本与当前规则版本不符（规则已升级）
    视为未耗尽，旧预算自动清零失效（「规则修复→定向重跑」自动生效）。
    """
    meta = entry.meta
    if meta.get("_rules_version") != _rules_version():
        return False
    attempts = int(meta.get("attempt_count", 0))
    category = meta.get("failure_category", "model_behavior")
    return attempts >= _MAX_ATTEMPTS.get(category, 2)


class BatchTranslator:
    """批量翻译引擎：记忆命中 → 分批并发 → 占位符校验 → 结果落库。

    容错：批量 JSON 解析失败时降级逐条并发重试（短超时），且每条完成即回调进度，
    避免"整批解析失败 → 串行 25 条 × 长超时"导致的长时间无进度卡死。
    """

    FALLBACK_TIMEOUT = 45.0   # 降级逐条时的单条超时

    def __init__(self, client: BaseClient, batch_size: int = 25, concurrency: int = 3,
                 memory=None, model: str = "", lang: str = "→zh-CN",
                 system_prompt: str = "", placeholder_check: bool = True,
                 glossary=(), glossary_force=(), cancellation_event=None,
                 agent_memory=None, agent_game: str = "",
                 context_store=None, context_game: str = "",
                 vector_recall=None, knowledge=None,
                 service_restart: Callable | None = None):
        # F42（8morelives 实证）：服务死亡回调——本地 llama-server 长任务
        # 中偶发被终止，连接类失败快速抛 ServiceUnavailableError，批量层
        # 连续失败 ≥2 批时调回调（调用方重新 ensure_running 拉起服务），
        # 后续批在新服务上继续（不丢失已翻译进度）
        self.service_restart = service_restart
        self.client = client
        # 服务端实际可用上下文（2026-08-14 用户实证：--ctx-size 6144
        # 实际 2048——llama-server 在 KV 显存不足时启动自动降级
        # （--parallel 3 → 每槽 6144/3）；客户端按配置组装 prompt 必超
        # 限被拒。探测一次，组装前按实际预算；探测失败回退配置值。
        cfg = getattr(client, "config", None)
        cfg_ctx = int(getattr(cfg, "local_context_size", 0) or 0) or 8192
        probe = getattr(client, "probe_context_size", None)
        self.actual_ctx = probe() if callable(probe) else None
        if not self.actual_ctx or self.actual_ctx <= 0:
            self.actual_ctx = cfg_ctx
        self.batch_size = max(1, batch_size)
        self.concurrency = max(1, concurrency)
        self.memory = memory
        self.model = model
        self.lang = lang
        self.system_prompt = system_prompt
        self.placeholder_check = placeholder_check
        # glossary = 强制 + 参考注入 + 精确直填全用途词对；glossary_force
        # = 仅质量门强制（glossary_mismatch 判定）的词对子集。
        # 经验记忆参考档（reference_pairs）设计即「参考而非强制」——
        # 并入 glossary 使其成为硬规则（Morfosi 64 条同因全灭实证：
        # ('Locked','锁定') 把自然句 "IT'S LOCKED." 判为标签语境强制），
        # GUI/runner 只把术语库 active + 知识库译例传入 glossary_force，
        # 记忆词对保留参考注入与精确直填（打破回显死循环）但不强制。
        # 不传 glossary_force（或传空）→ 回退全量 glossary（旧行为）。
        self.glossary = tuple(glossary)
        self._glossary_force = (
            tuple(glossary_force) if glossary_force else self.glossary)
        self.cancellation_event = cancellation_event
        # 经验记忆（AgentMemory，跨游戏）：高置信短语直接应用 +
        # 质量门通过译文沉淀证据。agent_game 用于记忆溯源（来源游戏）。
        self.agent_memory = agent_memory
        self.agent_game = agent_game
        # 游戏语境库（翻译 C6，阶段 2）：同游戏同指纹精确命中直填 +
        # 跨游戏相似语境注入 prompt 参考（多义词消歧 Resume=继续）。
        # context_game 是当前翻译项目的游戏名（语境库按游戏隔离）。
        self.context_store = context_store
        self.context_game = context_game or agent_game
        # 向量检索（阶段 4）：相似去重（≥0.95 复用译文）+ 相似召回
        # （≥0.8 注入参考）。服务不可用由 VectorRecall 内部降级为空。
        self.vector_recall = vector_recall
        # 知识库（KnowledgeBase 实例，2026-08-14 用户要求：按文本检索
        # 命中注入，不再全量拼 system_prompt——全量注入膨胀上下文且
        # 稀释注意力；_build_item 按原文 match_text 精确命中才注入）。
        # 不传（runner/旧调用方）→ 无知识注入，行为与旧版一致。
        self.knowledge = knowledge
        self.references = merge_translation_references(self.glossary)
        # Q1 语义门对照表。三条件：
        # 1) source 必须内置于参考表（_BUILTIN_UI_EXACT）——用户自定义术语
        #    不设语义门；
        # 2) 用户 glossary 覆盖的 source 显式排除——merge 虽已用用户 pair
        #    替换内置 pair，但用户 pair 的 source 仍在 _BUILTIN_UI_EXACT
        #    键集合内，不排除就会按用户译文强检查，误伤自由翻译；
        # 3) 只对**单术语**生效（source 无空白）——复合短语（'Interact
        #    hold' → '交互（长按）'）合理变体多（'交互保持'/'按住交互'），
        #    硬子串门会误杀自由翻译；单术语按钮标签（Resume/Quit/Settings）
        #    是固定 UI 词，权威译名要求严格才合理。
        merged_builtin = _BUILTIN_UI_EXACT
        user_sources = set()
        for item in self.glossary:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                source = item[0]
            elif isinstance(item, dict):
                source = item.get("term")
            else:
                source = getattr(item, "term", None)
            if isinstance(source, str) and source.strip():
                user_sources.add(source.strip().casefold())
        self.builtin_ui_exact = {
            source.strip().casefold(): target
            for source, target in self.references
            if (source.strip().casefold() in merged_builtin
                and " " not in source.strip()
                and source.strip().casefold() not in user_sources)}
        # 术语/知识/记忆词对精确索引（casefold→译文）：单全大写键名
        # （JUMP/Vsync）1.8B 带译例仍稳定回显（force-reboot 16 条恒败
        # 实证）→ _chat_each 词对精确命中时确定性直填，打破回显死循环。
        # learn 沉淀的 single_lexicon_word、AgentMemory 词对、人工术语
        # 全在此表；质量门复查兜底词对污染（不合规 → 拒绝走模型链）。
        self._glossary_exact: dict[str, str] = {}
        # 内置 UI 引用并入精确直填表（2026-08-14 用户实证：play 反复译
        # 「播放」——prompt 注入/Q1 语义门只能拦截标记失败，重试仍可能
        # 复败；原文精确命中内置引用源时直接落权威译文，零模型调用且
        # 质量门必然通过）。保留型（target==source，itch）跳过——直填
        # 即回显，无意义。用户词对在下方循环后写入，覆盖内置（用户
        # 意愿优先）。
        for _src, _tgt in self.builtin_ui_exact.items():
            if _tgt.casefold() != _src:
                self._glossary_exact[_src] = _tgt
        for item in self.glossary:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                src, tgt = item[0], item[1]
            elif isinstance(item, dict):
                src, tgt = item.get("term"), item.get("translation")
            else:
                src = getattr(item, "term", None)
                tgt = getattr(item, "translation", None)
            if (isinstance(src, str) and src.strip()
                    and isinstance(tgt, str) and tgt.strip()
                    # 键名 source 不直填（headache 实证：SPACE→空间 污染
                    # 词对在记忆库 active，原文 "SPACE" 精确命中会直填
                    # 「空间」——键名不该被翻译，跳过直填走模型链，质量门
                    # 全键名词对豁免已拦截该词对本身）
                    and src.strip().casefold()
                    not in PHYSICAL_KEY_NAMES_CASEFOLD):
                self._glossary_exact[src.strip().casefold()] = tgt
        self._stop = threading.Event()
        self._metrics_lock = threading.Lock()
        self._consistency_lock = threading.Lock()
        self._requests = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._consistent_translations: dict[tuple[str, str], str] = {}
        # 同对象（asset_file+obj）已成功译文：多语言打包游戏（同一对象存
        # 英/法/意/日四版文本）与对话序列对象中，兄弟条目的成功译文是
        # 重试时的译例来源（alisa-demo 实证：Clé en Fer 回显 → 注入同 obj
        # "Iron Key translates to 铁钥匙" → 模型输出「铁钥匙」）。
        self._obj_results: dict[str, list[tuple[str, str]]] = {}
        self._obj_lock = threading.Lock()
        # 2026-08-19 emit_stats 条件重算标志：本轮产生过失败（置 failed
        # 的任何路径）才全量扫 entries 统计 failed——连续成功批零遍历
        # （万级条目 × 数百批的 O(N) 降 O(1)，见 emit_stats 注释）。
        self._failures_dirty = True

    def stop(self):
        self._stop.set()
        if self.cancellation_event is not None:
            self.cancellation_event.set()

    def _is_cancelled(self) -> bool:
        return (self._stop.is_set()
                or (self.cancellation_event is not None
                    and self.cancellation_event.is_set()))

    def run(self, entries: list[TextEntry], progress_cb: Callable | None = None,
            context_window: int = 1,
            force_retry_exhausted: bool = False) -> TranslateStats:
        self._stop.clear()
        cancelled = self.cancellation_event
        self._failures_dirty = True   # 首次 emit 必须统计一次基线 failed
        with self._metrics_lock:
            self._requests = 0
            self._input_tokens = 0
            self._output_tokens = 0
        with self._consistency_lock:
            self._consistent_translations.clear()
        # Q3 attempt 预算：失败条目跨轮累计 attempt_count，预算耗尽不再
        # 重跑同一条链（保留失败状态供报告审计）——消灭「每轮全量重跑
        # 同一条链」的反复失败。
        # C2：预算挂规则版本戳（规则升级自动清零）；force_retry_exhausted
        # 是显式「修复后定向重跑」开关——任何修复（即使规则未变）后
        # 用户确认强制重跑预算耗尽条目。
        run_scope = [entry for entry in entries
                     if _auto_translatable(entry)
                     and (force_retry_exhausted
                          or not _attempt_exhausted(entry))]
        stats = TranslateStats(total=len(run_scope))
        started_at = time.perf_counter()

        def finalize_elapsed():
            """所有返回路径统一记录耗时（P3 吞吐统计）。"""
            stats.elapsed = time.perf_counter() - started_at
        changed: list[TextEntry] = []
        new_memory: list[tuple] = []

        def flush():
            if self.memory:
                self.memory.batch_update_translation_results(changed)
                changed.clear()
                self.memory.batch_add_memory(new_memory)
                new_memory.clear()
            if self.agent_memory is not None:
                self.agent_memory.flush()

        def emit_stats():
            """实时上报进度（2026-08-14 卡顿优化：去掉每批 O(N) 重算）。

            done 由各成功路径增量累加（记忆命中/直填/批量/降级全覆盖，
            run_scope 无 pre-translated 条目——is_actionable 只认 pending/
            failed，与重算语义严格等价），直接使用；failed 保留全量重算
            （C4 预算耗尽的历史 failed 条目不在 run_scope 也不在本轮
            changed，增量会漏计——chips 显示口径含它们，进度必须一致）。

            2026-08-19 再优化：failed 全量重算改为**条件重算**——只有
            本轮实际产生过失败（含记忆拒绝置 failed 的路径）才扫
            entries（纯 status 判断）；连续成功批零遍历。万级条目 ×
            数百批的场景，绝大多数批（全部成功）从 O(N) 降到 O(1)。
            条件标志由各失败路径置位（_mark_* 家族统一在
            _note_local_failure 记账）。
            """
            with self._metrics_lock:
                stats.requests = self._requests
                stats.input_tokens = self._input_tokens
                stats.output_tokens = self._output_tokens
            if progress_cb is None:
                # 无监听者零成本（#13 卡顿实证：runner 无 UI 场景每批
                # O(N) 全扫 run_scope 白算）
                return
            if self._failures_dirty:
                # C4：failed 按 entries 全量统计——预算耗尽的条目不在
                # run_scope（本轮不再尝试），但记忆拒绝置 failed 后必须
                # 统计可见，否则又是「该翻未翻」的黑洞。
                stats.failed = sum(
                    1 for entry in entries if entry.status == STATUS_FAILED)
                self._failures_dirty = False
            progress_cb(replace(stats))

        # 1) 翻译记忆命中（工作记忆，会话级缓存）
        hits: dict[str, str] = {}
        if self.memory:
            pending = [e for e in entries if _auto_translatable(e)]
            hits = self.memory.get_memory_hits([e.original for e in pending], self.model, self.lang)
            for e in pending:
                if e.original in hits:
                    good = self._apply_quality(e, hits[e.original])
                    e.status = STATUS_TRANSLATED if good else STATUS_FAILED
                    if good:
                        e.status = STATUS_TRANSLATED
                        stats.done += 1
                        stats.from_memory += 1
                    else:
                        self._failures_dirty = True
                        rejected = list(e.quality_reasons)
                        self.memory.remove_memory(e.original, self.model, self.lang)
                        e.translation = ""
                        e.quality_reasons = ()
                        e.meta["memory_rejected_reasons"] = rejected
                        e.meta.pop("quality_passed", None)
                        e.meta.pop("quality_reasons", None)
                        # C4：预算已耗尽的条目不落 pending 黑洞——它已不在
                        # run_scope，永久 pending 不 fail 不 translated，
                        # 统计不可见（只算进 pending 计数）；直接置 failed
                        # 保留拒绝原因供报告审计。
                        # 补记 stats.failed（#13：emit_stats 无监听者时不再
                        # 全扫重算 failed——此前预算耗尽计数只靠重算兜底）
                        e.status = (
                            STATUS_FAILED if _attempt_exhausted(e)
                            else "pending")
                        if e.status == STATUS_FAILED:
                            stats.failed += 1
                    changed.append(e)
            flush()
            if progress_cb:
                progress_cb(replace(stats))

        # 1b) 经验记忆直接应用（AgentMemory 高置信短语，混合运用高档）。
        # 门槛（agent_memory.direct_applications 内）：多词短语 + 证据≥3
        # + 零拒绝 + 跨游戏（或人工）。工作记忆命中优先；直接应用的
        # 译文仍过质量门复查——拒绝则反馈降级（rejects+1 → 退休）。
        if self.agent_memory is not None:
            pending = [e for e in entries if _auto_translatable(e)]
            roles = {e.original: str(e.meta.get("role", ""))
                     for e in pending}
            direct = self.agent_memory.direct_applications(
                [e.original for e in pending], roles)
            for e in pending:
                if e.original not in direct or e.original in hits:
                    continue
                candidate = direct[e.original]
                good = self._apply_quality(e, candidate)
                if good:
                    e.status = STATUS_TRANSLATED
                    stats.done += 1
                    stats.from_memory += 1
                else:
                    self._failures_dirty = True
                    rejected = list(e.quality_reasons)
                    e.translation = ""
                    e.quality_reasons = ()
                    e.meta["agent_memory_rejected_reasons"] = rejected
                    e.meta.pop("quality_passed", None)
                    e.meta.pop("quality_reasons", None)
                    # C4：预算耗尽的拒绝条目直接置 failed（见工作记忆路径）
                    e.status = (
                        STATUS_FAILED if _attempt_exhausted(e)
                        else "pending")
                    if e.status == STATUS_FAILED:
                        stats.failed += 1
                self.agent_memory.apply_feedback(
                    e.original,
                    context_key_of(str(e.meta.get("role", ""))),
                    accepted=good)
                changed.append(e)
            flush()
            if progress_cb:
                progress_cb(replace(stats))

        # 1a) 向量相似去重（阶段 4 T4-3）：同游戏向量命中 ≥0.95 →
        # 复用历史译文（质量门复查，拒绝走模型链）。精确命中（hits）
        # 优先——向量只补精确路径覆盖不到的相似变体。
        if self.vector_recall is not None:
            prev_direct = direct if self.agent_memory is not None else {}
            pending = [e for e in entries if _auto_translatable(e)]
            originals = [e.original for e in pending
                         if e.original not in hits
                         and e.original not in prev_direct]
            vector_hits = self.vector_recall.dedupe(
                originals, exclude=tuple(hits))
            for e in pending:
                if e.original not in vector_hits:
                    continue
                good = self._apply_quality(e, vector_hits[e.original])
                if good:
                    e.status = STATUS_TRANSLATED
                    stats.done += 1
                    stats.from_memory += 1
                else:
                    self._failures_dirty = True
                    e.translation = ""
                    e.quality_reasons = ()
                    e.meta.pop("quality_passed", None)
                    e.meta.pop("quality_reasons", None)
                    e.meta["vector_fill_rejected"] = list(e.quality_reasons)
                    e.status = (
                        STATUS_FAILED if _attempt_exhausted(e)
                        else "pending")
                    if e.status == STATUS_FAILED:
                        stats.failed += 1
                changed.append(e)
            flush()
            if progress_cb:
                progress_cb(replace(stats))

        # 1c) 语境库直填（翻译 C6，阶段 2）：同游戏同指纹精确命中 →
        # 直填（译文仍过质量门复查，拒绝则清除命中记录防再次污染）。
        # 多义词（Resume 主菜单=继续 vs 简历）靠语境指纹区分，未命中的
        # 走模型链（prompt 注入跨游戏参考）。
        if self.context_store is not None:
            from hanhua.core.context_library import collect_window
            # direct 仅在 agent_memory 存在时定义（1b 段内），此处兜底
            agent_direct = direct if self.agent_memory is not None else {}
            pending = [e for e in entries if _auto_translatable(e)]
            for e in pending:
                if e.original in hits or e.original in agent_direct \
                        or e.status == STATUS_TRANSLATED:
                    continue
                before, after = collect_window(e.meta)
                match = self.context_store.match_exact(
                    self.context_game, e.original,
                    scene=str(e.meta.get("scene", "")),
                    ui_position=str(e.meta.get("role", "")),
                    text_type=str(e.meta.get("kind", "")),
                    ctx_before=before, ctx_after=after)
                if match is None:
                    continue
                candidate = match.recommended_translation.strip()
                if not candidate:
                    continue
                good = self._apply_quality(e, candidate)
                if good:
                    e.status = STATUS_TRANSLATED
                    stats.done += 1
                    stats.from_memory += 1
                else:
                    self._failures_dirty = True
                    # 语境记录被质量门拒绝 → 存疑标记（阶段 3 防污染）
                    if match.id is not None:
                        self.context_store.mark_suspicious(match.id)
                    e.translation = ""
                    e.quality_reasons = ()
                    e.meta.pop("quality_passed", None)
                    e.meta.pop("quality_reasons", None)
                    e.meta["context_fill_rejected"] = list(
                        e.quality_reasons)
                    e.status = (
                        STATUS_FAILED if _attempt_exhausted(e)
                        else "pending")
                    if e.status == STATUS_FAILED:
                        stats.failed += 1
                changed.append(e)
            flush()
            if progress_cb:
                progress_cb(replace(stats))

        # 2) 分批并发翻译。Q3 预算必须在这里生效（run_scope 只算统计
        # 数；若无此过滤，耗尽条目每轮仍全量重跑同一条链——「预算闸」
        # 就成了摆设）。记忆命中（1/1b）不调模型，不受预算约束；且命中
        # 后条目 status 已变，这里必须重新过滤（而非复用 run_scope）。
        pending = [entry for entry in entries
                   if _auto_translatable(entry)
                   and (force_retry_exhausted
                        or not _attempt_exhausted(entry))]
        grouped: dict[tuple[str, str], list[TextEntry]] = {}
        for entry in pending:
            key = (entry.original, str(entry.meta.get("role", "display")))
            grouped.setdefault(key, []).append(entry)
        representatives = [group[0] for group in grouped.values()]
        group_by_representative = {
            id(group[0]): group for group in grouped.values()
        }
        if cancelled is not None and cancelled.is_set():
            finalize_elapsed()
            return stats
        native_client = callable(getattr(self.client, "translate_text", None))
        if native_client:
            completed_representatives = 0
            # 进度触发计数（2026-08-15 用户实证：开头批「一批几十条」
            # + 卡顿感——同文本分组（Button ×几十）共享一次模型调用，
            # 按「代表条目数攒批」emit 时一个代表一次 done+几十，显示
            # 成虚批几十条且长时间无进度。改为按成员完成数（真实条数）
            # + 时间节流触发：批粒度与设置 batch_size 对齐，卡顿感消失）
            done_since_emit = 0
            # last_emit_ts 初始化为当前单调时钟（而非 0.0）：首条完成时
            # now - 0.0 恒 > 1.5s（wall-clock 绝对时间），若初始 0.0 会让
            # 首条触发一次「假 emit」（done=1），随后批内逐条（<1.5s）
            # 都不触发，直到末条 completed==len 才再 emit → 活动流只见
            # 「首条 + 全批」两条虚批（#27 实证：batch_size=16 显示每批
            # 2 条——真实条数 2 就是 2，但首条假 emit 让 delta 分裂）。
            last_emit_ts = time.monotonic()

            def consume_native_result(
                    result: tuple[TextEntry, str, bool]) -> None:
                nonlocal completed_representatives, done_since_emit
                nonlocal last_emit_ts
                en, tr, good = result
                candidate = en.translation or tr
                group = group_by_representative[id(en)]
                for index, member in enumerate(group):
                    member_good = (
                        good if index == 0
                        else (self._apply_quality(member, candidate)
                              if candidate else False)
                    )
                    if not candidate and index > 0:
                        self._copy_failure_state(en, member)
                    if member_good and member.translation:
                        member.status = STATUS_TRANSLATED
                        stats.done += 1
                        self._record_obj_result(member, member.translation)
                    else:
                        member.status = STATUS_FAILED
                        stats.failed += 1
                        self._failures_dirty = True
                    changed.append(member)
                # C3：语言保持（原文已是目标语言，译文=原文）与回显一样
                # 排除出记忆——「原文→原文」沉淀会毒化（后续直接复用
                # 不翻译），工作记忆与经验记忆双门一致
                if good and en.translation and self.memory:
                    if not en.meta.get("echo_exempt") and not en.meta.get("language_source_kept"):
                        new_memory.append(
                            (en.original, en.translation, self.model, self.lang))
                # 经验记忆沉淀（native 路径）：质量门通过 + 非回显/非语言
                # 保持 → 提案
                if good and en.translation and self.agent_memory:
                    if not en.meta.get("echo_exempt") and not en.meta.get("language_source_kept"):
                        self.agent_memory.propose_deferred(
                            en.original, en.translation, self.agent_game,
                            role=str(en.meta.get("role", "")))
                completed_representatives += 1
                done_since_emit += len(group)
                now = time.monotonic()
                # 进度上报 = 真实唯一文本累计 + 1.5s 时间节流兜底。此前按
                # 批次号节流（completed_representatives % batch_size==0）：
                # 同文分组使完成代表数 < 实际条数，batch_size=16 时批内
                # 逐条完成也常到不了 16 → 活动流显示「每批 2 条」虚批。
                # 1.5s 兜底也确保 batch_size=1 时逐条仍实时刷新（#27
                # 实证：设置 16 条/批实际每批 2 条——真实条数就是 2，
                # 显示如实；用户预期的「16 条一批」在本地逐条模式下
                # 不存在，逐条请求就是每条一次模型调用）。
                if (now - last_emit_ts >= 1.5
                        or done_since_emit >= self.batch_size
                        or completed_representatives
                        == len(representatives)):
                    done_since_emit = 0
                    last_emit_ts = now
                    flush()
                    emit_stats()
                elif completed_representatives == len(representatives):
                    # 兜底：全部完成但上面任一条件未触发（本轮无成功
                    # 翻译——done 全失败时逐条 status 已置 failed，
                    # stats.done 不增，1.5s 内无 emit）→ 主动 emit 一次
                    # 让 UI 看到最终 failed 状态。
                    flush()
                    emit_stats()

            self._chat_each(
                representatives, context_window,
                result_cb=consume_native_result,
            )
            flush()
            emit_stats()
            finalize_elapsed()
            return stats

        # 内置 UI 引用/词对确定性直填（2026-08-14 用户实证：play 反复
        # 译「播放」——此前直填只在 _chat_each 降级路径，非 native 批量
        # 路径先白调一次模型失败后才兜底命中）。run 分批前先填：原文
        # 精确命中 _glossary_exact（内置引用 + 用户词对）→ 直接落译文
        # 零模型请求，质量门复查兜底词对污染（不合规 → 恢复走模型链）。
        filled_reps: list[TextEntry] = []
        for rep in representatives:
            exact_zh = self._glossary_exact.get(
                rep.original.strip().casefold())
            if not exact_zh:
                filled_reps.append(rep)
                continue
            direct_state = (rep.translation, rep.status,
                            rep.quality_reasons, dict(rep.meta))
            if self._apply_quality(rep, exact_zh):
                for member in group_by_representative[id(rep)]:
                    member.translation = exact_zh
                    member.status = STATUS_TRANSLATED
                    member.quality_reasons = ()
                    member.meta = dict(member.meta)
                    _clear_review_state(member.meta)
                    member.meta["quality_passed"] = True
                    member.meta["quality_reasons"] = []
                    member.meta["deterministic_fill"] = "glossary_pair"
                    stats.done += 1
                    self._record_obj_result(member, member.translation)
                    changed.append(member)
                flush()
                emit_stats()
                continue
            (rep.translation, rep.status, rep.quality_reasons,
             rep.meta) = direct_state
            filled_reps.append(rep)
        representatives = filled_reps
        batches = [
            representatives[i:i + self.batch_size]
            for i in range(0, len(representatives), self.batch_size)
        ]
        pool = ThreadPoolExecutor(max_workers=self.concurrency)
        batch_iter = iter(batches)
        futures = {}
        try:
            for batch in batch_iter:
                futures[pool.submit(self._translate_batch, batch, context_window, emit_stats)] = batch
                if len(futures) >= self.concurrency:
                    break
            service_down_batches = 0  # F42：连续服务不可达批数
            while futures:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                if self._stop.is_set() or (cancelled is not None and cancelled.is_set()):
                    for pending_future in futures:
                        pending_future.cancel()
                    break
                for fut in done:
                    b = futures.pop(fut)
                    if self._stop.is_set() or (cancelled is not None and cancelled.is_set()):
                        break
                    try:
                        per_batch = fut.result()
                        service_down_batches = 0  # 成功批重置计数
                    except Exception as exc:  # noqa: BLE001 单批失败隔离
                        if self.service_restart is not None:
                            # F42 增强（2026-08-16，Rendezvous 11310 条
                            # 大任务防中途卡死）：服务死亡后批量失败——不仅
                            # ServiceUnavailableError（连接/超时快速失败），
                            # **任何连续失败 ≥3 批**都触发服务重启探测
                            # （重启是幂等的：ensure_running 探测到已活
                            # 服务则复用；覆盖未知异常形态——drova 两次
                            # 服务死亡未触发自愈的实测教训）
                            service_down_batches += 1
                            if service_down_batches >= 3:
                                try:
                                    self.service_restart()
                                except Exception:  # noqa: BLE001
                                    pass
                                service_down_batches = 0
                        for en in b:
                            for member in group_by_representative[id(en)]:
                                self._mark_request_failed(member, exc)
                                stats.failed += 1
                                changed.append(member)
                        flush()
                        emit_stats()
                        if not self._stop.is_set() and not (cancelled is not None and cancelled.is_set()):
                            try:
                                next_batch = next(batch_iter)
                            except StopIteration:
                                pass
                            else:
                                futures[pool.submit(self._translate_batch, next_batch, context_window, emit_stats)] = next_batch
                        continue
                    for en, tr, good in per_batch:
                        candidate = en.translation or tr
                        for index, member in enumerate(group_by_representative[id(en)]):
                            member_good = good if index == 0 else (self._apply_quality(member, candidate) if candidate else False)
                            if not candidate and index > 0:
                                self._copy_failure_state(en, member)
                            if member_good and member.translation:
                                member.status = STATUS_TRANSLATED
                                stats.done += 1
                                self._record_obj_result(
                                    member, member.translation)
                            else:
                                member.status = STATUS_FAILED
                                stats.failed += 1
                                self._failures_dirty = True
                            changed.append(member)
                        # C3：语言保持（译文=原文）与回显一样排除出记忆
                        if good and en.translation and self.memory:
                            if not en.meta.get("echo_exempt") and not en.meta.get("language_source_kept"):
                                new_memory.append(
                                    (en.original, en.translation,
                                     self.model, self.lang))
                        # 经验记忆沉淀（线程池路径）：同上
                        if good and en.translation and self.agent_memory:
                            if not en.meta.get("echo_exempt") and not en.meta.get("language_source_kept"):
                                self.agent_memory.propose_deferred(
                                    en.original, en.translation,
                                    self.agent_game,
                                    role=str(en.meta.get("role", "")))
                    flush()
                    emit_stats()
                    if not self._stop.is_set() and not (cancelled is not None and cancelled.is_set()):
                        try:
                            next_batch = next(batch_iter)
                        except StopIteration:
                            pass
                        else:
                            futures[pool.submit(self._translate_batch, next_batch, context_window, emit_stats)] = next_batch
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        emit_stats()
        finalize_elapsed()
        return stats

    def _translate_batch(self, batch: list[TextEntry], context_window: int,
                         per_item_cb: Callable[[], None] | None = None
                         ) -> list[tuple[TextEntry, str, bool]]:
        """批量翻译一条批次，含两级容错：
        1) 整批 JSON 解析失败 → 逐条降级重试（单条输出错乱概率低）
        2) 批内部分失败（缺条/占位符校验失败）→ 仅对失败子集逐条重试一次
        """
        if self._is_cancelled():
            return []
        if callable(getattr(self.client, "translate_text", None)):
            return self._chat_each(batch, context_window, per_item_cb)
        results = self._chat_batch(batch, context_window)
        if self._is_cancelled():
            return []
        segmented_attempts: set[int] = set()
        repaired_results: list[tuple[TextEntry, str, bool]] = []
        for entry, translation, good in results:
            if (not good and self._allows_fallback_retry(entry)
                    and self._needs_protected_repair(entry)):
                segmented_attempts.add(id(entry))
                repaired_results.append(
                    self._repair_protected_chat_translation(entry))
            elif (not good and self._allows_fallback_retry(entry)
                    and ({"newline_mismatch", "line_content_mismatch",
                          "untranslated_text", "action_word_residue"}
                         & set(entry.quality_reasons))
                    # 只对真多行条目逐段修复；单行失败（回显/动作词残留）
                    # 不在此修复——落到 retryable 走 native 降级
                    # （Hy-MT2 translate_text 带 references 译例，知识库
                    #  TOSS TRASH → 丢垃圾 靠该路径生效）
                    and len(_split_translation_segments(entry.original)[1]) >= 2):
                segmented_attempts.add(id(entry))
                repaired_results.append(
                    self._repair_multiline_chat_translation(entry))
            else:
                repaired_results.append((entry, translation, good))
        results = repaired_results
        failed = [e for e, tr, good in results if not good]
        retryable = [
            e for e in failed
            if id(e) not in segmented_attempts and self._allows_fallback_retry(e)
        ]
        if retryable and len(retryable) == len(batch):
            # 整批解析失败 → 全部逐条降级
            return self._chat_each(retryable, context_window, per_item_cb)
        if retryable:
            # 部分失败 → 仅重试失败子集（逐条）
            sub = self._chat_each(retryable, context_window, per_item_cb)
            sub_map = {_entry_id(e): (tr, good) for e, tr, good in sub}
            merged: list[tuple[TextEntry, str, bool]] = []
            for e, tr, good in results:
                if not good:
                    tr, good = sub_map.get(_entry_id(e), (tr, good))
                merged.append((e, tr, good))
            return merged
        return results

    @staticmethod
    def _allows_fallback_retry(entry: TextEntry) -> bool:
        role = str(entry.meta.get("role", "display"))
        disposition = str(entry.meta.get("disposition", ""))
        return (role not in {"proper_name", "structural", "code", "key"}
                and disposition not in {
                    "preserve", "proper_name", "structural", "code", "key",
                })

    @staticmethod
    def _needs_protected_repair(entry: TextEntry) -> bool:
        reasons = set(entry.quality_reasons)
        has_protected_slot = any(
            protected for protected, _part in protected_slot_parts(entry.original))
        return bool(
            {"rich_text_mismatch", "input_token_mismatch"} & reasons
            or ("placeholder_mismatch" in reasons
                and (not ({"newline_mismatch", "line_content_mismatch"}
                          & reasons)
                     or has_protected_slot))
            or (has_protected_slot
                and {"target_script_mismatch", "untranslated_text"} & reasons)
        )

    def _maybe_keep_source_language(self, e: TextEntry) -> bool:
        """中文原文直接放行判定（批/逐条两路径统一入口）。

        游戏自带中文语言包（Language/CH/*.subs、*.jsonc 等）条目原文即中文，
        模型按目标语 zh-CN 处理反而回译成英文/乱码判 target_script_mismatch
        （containment 实证：'警卫' → guard、'折纸' → origami）→ 原样保留 +
        meta 标记 language_source_kept 供人工校对筛选。判定收紧：CJK ≥ 2 且
        占字母 ≥ 50%（deadbeat 实证：歌词含单日文汉字『戦争』被误判中文源
        → 1719 字符英文原样放行）——日文原文（含假名）由
        _is_multilingual_source 兜底。返回 True = 已直填（不送模型）。
        """
        if not (is_chinese_source(e.original)
                and not _JAPANESE_KANA_RE.search(e.original)):
            return False
        e.translation = e.original
        e.status = "translated"
        e.quality_reasons = ()
        e.meta = dict(e.meta)
        _clear_review_state(e.meta)
        e.meta["quality_passed"] = True
        e.meta["quality_reasons"] = []
        e.meta["language_source_kept"] = True
        return True

    def _chat_batch(self, batch: list[TextEntry], context_window: int
                    ) -> list[tuple[TextEntry, str, bool]]:
        if self._is_cancelled():
            return []
        # C3：中文源直填在批路径同样生效（判定抽为 _maybe_keep_source_language
        # 统一入口）——此前只在逐条 _chat_each 路径，批路径中文源被模型回译
        # 成英文/乱码且无 language_source_kept 标记（记忆毒化来源之一）
        kept: list[tuple[TextEntry, str, bool]] = []
        active: list[TextEntry] = []
        for e in batch:
            if self._maybe_keep_source_language(e):
                kept.append((e, e.original, True))
            else:
                active.append(e)
        if not active:
            return kept
        batch = active
        items = [self._build_item(batch, i, context_window) for i in range(len(batch))]
        user = self._build_chat_user_prompt(items)
        # 实际 ctx 预算（2026-08-14 用户实证：--ctx-size 6144 实际
        # 2048——KV 显存不足启动自动降级；按配置组装必超限被拒）。
        # 估算（中文 1 字 ≈ 1.5 token、英文 ≈ 3 字符/token 保守）：
        # system + user 超 70% ctx → 整批逐条降级（单条 user 显著更短）。
        if self._prompt_over_budget(self.system_prompt, user):
            return kept + self._chat_each(batch, context_window)
        try:
            content, usage = self.client.chat(
                self.system_prompt, [{"role": "user", "content": user}])
        except Exception:
            self._record_usage(None)
            raise
        self._record_usage(usage)
        if self._is_cancelled():
            return kept
        arr = self._response_array(content, batch[0] if len(batch) == 1 else None)
        if arr is None:
            for entry in batch:
                self._mark_failed(entry, "invalid_response", raw_output=content)
            return kept + [(e, "", False) for e in batch]
        return kept + self._validate(batch, arr)

    def _prompt_over_budget(self, system: str, user: str) -> bool:
        """组装前预算检查：system+user 估算 tokens 是否超服务端实际 ctx。

        2026-08-14 用户实证：request (2889 tokens) exceeds context (2048)
        ——配置 --ctx-size 6144 但 llama-server 因 KV 显存不足自动降级
        实际 2048（--parallel 3）。估算保守（中文 1.5 token/字、英文
        3.5 字符/token），上限取实际 ctx 的 70%（留输出与结构余量）——
        宁可多降级也不发出必被拒的请求。
        """
        if not self.actual_ctx or self.actual_ctx <= 0:
            return False
        est = 0
        for text in (system, user):
            if not text:
                continue
            cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
            est += int(cjk * 1.5 + (len(text) - cjk) / 3.5) + 24
        return est > self.actual_ctx * 0.7

    @staticmethod
    def _obj_key(entry: TextEntry) -> str:
        """同对象标识：asset_file + obj（MonoBehaviour rawstr 数组 / TextAsset）。

        同对象内多个条目常是同一文本的不同语言版本（四语言打包）或同一
        对话流——兄弟条目的成功译文可作为重试译例。
        """
        af = entry.meta.get("asset_file")
        obj = entry.meta.get("obj")
        if af and obj is not None:
            return f"{af}#{obj}"
        return ""

    def _record_obj_result(self, entry: TextEntry, translation: str) -> None:
        """记录一条成功译文到同对象桶（重试时作译例）。"""
        key = self._obj_key(entry)
        if not key or not entry.original or not translation:
            return
        pair = (entry.original, translation)
        with self._obj_lock:
            bucket = self._obj_results.setdefault(key, [])
            if pair not in bucket:
                bucket.append(pair)

    def _obj_reference_pairs(self, entry: TextEntry) -> list[tuple[str, str]]:
        """同对象已成功条目的 (原文, 译文) 对照（最多 3 对）。"""
        key = self._obj_key(entry)
        if not key:
            return []
        with self._obj_lock:
            return list(self._obj_results.get(key, ()))[:3]

    def _chat_each(self, batch: list[TextEntry], context_window: int,
                   per_item_cb: Callable[[], None] | None = None,
                   result_cb: Callable[
                       [tuple[TextEntry, str, bool]], None] | None = None,
                   ) -> list[tuple[TextEntry, str, bool]]:
        """逐条降级翻译：并发执行 + 短超时 + 每条完成回调（UI 实时进度）。"""
        if not batch:
            return []
        config = getattr(self.client, "config", None)   # 测试 client 可能无 config
        old_timeout = getattr(config, "timeout", None) if config else None
        if old_timeout:
            config.timeout = min(old_timeout, self.FALLBACK_TIMEOUT)

        def work(i: int) -> tuple[TextEntry, str, bool]:
            e, tr, good = _work_body(i)
            # 成功译文在 worker 内立即入同对象桶：兄弟条目（四语言打包/
            # 同一对话流）的降级链在后续 work 里读译例。不能等 run() 主线程
            # 的 consume_native_result 回调——worker 完成当前条目后立即取
            # 下一个 work，record 与兄弟条目的读取形成竞态（alisa-demo
            # 实证：同批 Clé en Fer 偶发读不到 Iron Key 译例 → 回显失败；
            # -s 输出捕获的 IO 延迟掩盖了该竞态）。单 worker 下此处保证
            # 先 record 后读取；多 worker 由 _obj_lock 保证一致读。
            if good and tr:
                self._record_obj_result(e, tr)
            return e, tr, good

        def _work_body(i: int) -> tuple[TextEntry, str, bool]:
            e = batch[i]
            original_state = (
                e.translation, e.status, e.quality_reasons, dict(e.meta))

            def restore_original_state() -> None:
                (e.translation, e.status, e.quality_reasons, e.meta) = original_state
            if self._is_cancelled():
                return e, "", False
            # 中文原文直接放行（判定抽为统一入口，批路径同用）
            if self._maybe_keep_source_language(e):
                return e, e.original, True
            # 语言选项标签确定性直填（Language: ENGLISH → 语言：英语）：
            # 标签 + 语言名是封闭集合，1.8B 对选项文本乱译（doog 实证
            # 「Language: ENGLISH」4 次重试稳定乱译 → newline/line_content
            # mismatch 恒败）——确定性直填优先于模型，不走 LLM。
            deterministic = language_option_translation(e.original)
            if deterministic:
                e.translation = deterministic
                e.status = "translated"
                e.quality_reasons = ()
                e.meta = dict(e.meta)
                _clear_review_state(e.meta)
                e.meta["quality_passed"] = True
                e.meta["quality_reasons"] = []
                e.meta["deterministic_fill"] = "language_option"
                return e, deterministic, True
            # 术语/知识/记忆词对精确命中 → 确定性直填（先于模型调用）。
            # 1.8B 对单全大写键名（JUMP/Vsync）带译例仍稳定回显
            # （force-reboot 16 条恒败实证）——词对精确命中直接落译文，
            # 打破回显死循环。专名词对（FOXYPAW→FOXYPAW）直填=回显，
            # 质量门专名豁免链放行，行为与模型一致。仍过质量门复查：
            # 词对译文不合规（沉淀污染）→ 拒绝并恢复，走正常模型链。
            exact_zh = self._glossary_exact.get(
                e.original.strip().casefold())
            if exact_zh:
                direct_state = (
                    e.translation, e.status, e.quality_reasons,
                    dict(e.meta))
                if self._apply_quality(e, exact_zh):
                    e.translation = exact_zh
                    e.status = "translated"
                    e.quality_reasons = ()
                    e.meta = dict(e.meta)
                    _clear_review_state(e.meta)
                    e.meta["quality_passed"] = True
                    e.meta["quality_reasons"] = []
                    e.meta["deterministic_fill"] = "glossary_pair"
                    return e, exact_zh, True
                (e.translation, e.status, e.quality_reasons, e.meta) = (
                    direct_state)
            try:
                native_translate = getattr(self.client, "translate_text", None)
                if callable(native_translate):
                    target_lang = self.lang.rsplit("→", 1)[-1] or "zh-CN"
                    content, usage = native_translate(
                        e.original, target_lang, self.references)
                else:
                    user = self._build_chat_user_prompt([
                        self._build_item(batch, i, context_window, single=True)])
                    # 单条也超预算（system_prompt 本身巨大——全量术语/
                    # 知识注入场景）→ 明确失败原因 context_overflow，
                    # 不再笼统 request_error（2026-08-14 实证）
                    if self._prompt_over_budget(self.system_prompt, user):
                        self._mark_failed(e, "context_overflow")
                        return e, "", False
                    content, usage = self.client.chat(
                        self.system_prompt, [{"role": "user", "content": user}])
            except Exception as exc:  # noqa: BLE001
                self._record_usage(None)
                self._mark_request_failed(e, exc)
                return e, "", False
            self._record_usage(usage)
            if self._is_cancelled():
                return e, "", False
            if callable(getattr(self.client, "translate_text", None)):
                arr = [{"id": _entry_id(e), "translation": content}]
            else:
                arr = self._response_array(content, e)
            if arr is None:
                self._mark_failed(e, "invalid_response", raw_output=content)
                return e, "", False
            sub = self._validate([e], arr)
            # 首译失败状态快照：降级修复的内部复查会覆盖 entry 的
            # translation/quality_reasons/meta（_apply_quality 落盘）→
            # 修复失败后必须恢复首译状态，后续降级链（multiline/兜底/
            # 词级补译/专名重译）基于首译判定（baldis 实证：multiline
            # repair 首行回显英文 → reasons 被覆盖成 target_script_mismatch
            # → 换行合并兜底的「仅换行原因」判定失准，语义完整首译被卡死）。
            first_fail_state = (
                e.translation, e.status, e.quality_reasons, dict(e.meta))
            if (callable(getattr(self.client, "translate_text", None))
                    and not sub[0][2]
                    and self._allows_fallback_retry(e)
                    and self._needs_protected_repair(e)):
                repaired = self._repair_protected_translation(
                    e, native_translate, target_lang, previous=sub[0][1])
                if self._is_cancelled():
                    restore_original_state()
                    return e, "", False
                if repaired is not None and repaired[2]:
                    return repaired
                (e.translation, e.status, e.quality_reasons, e.meta) = (
                    first_fail_state)
            if (callable(getattr(self.client, "translate_text", None))
                    and not sub[0][2]
                    and self._allows_fallback_retry(e)
                    and ({"newline_mismatch", "line_content_mismatch",
                          "untranslated_text", "action_word_residue",
                          "target_script_mismatch"}
                         & set(e.quality_reasons))):
                repaired = self._repair_multiline_translation(
                    e, native_translate, target_lang)
                if self._is_cancelled():
                    restore_original_state()
                    return e, "", False
                # 修复成功才返回；失败（逐段仍残留英文等）恢复首译失败状态
                # 继续走后续降级链（兜底/词级补译/专名重译/双跳/同对象译例/
                # 普通重试），不截断（alisa-demo 实证：意语长句逐段修复时短段
                # 仍被译成英语，成功修复需段内双跳）。
                if repaired is not None and repaired[2]:
                    return repaired
                (e.translation, e.status, e.quality_reasons, e.meta) = (
                    first_fail_state)
            # 换行合并兜底：模型稳定把多行文本合并为单行（1.8B 长句输出
            # 倾向单行）——native 首译语义完整中文、仅因换行结构判失败，
            # multiline repair 重建也失败（逐行重译时首行被模型回显英文，
            # baldis 'Error please contact game owner\nand check log.' 实证）
            # → 放行首译（中文语义优先，Unity UI 自动换行兜底排版）。
            # 仅当换行相关是唯一失败原因、译文含中文、且无空段（\n\n 是
            # 段整体漏译证据，不得放行）时放行；放行证据 line_merged 记入
            # meta 供人工校对筛选。
            if (callable(getattr(self.client, "translate_text", None))
                    and not sub[0][2]
                    and self._allows_fallback_retry(e)
                    and sub[0][1]
                    and _CJK.search(sub[0][1])
                    and not _EMPTY_SEGMENT.search(sub[0][1])
                    and set(e.quality_reasons)
                    <= {"newline_mismatch", "line_content_mismatch"}):
                e.translation = sub[0][1]
                e.quality_reasons = ()
                e.meta = dict(e.meta)
                _clear_review_state(e.meta)
                e.meta["quality_passed"] = True
                e.meta["quality_reasons"] = []
                e.meta["line_merged"] = True
                return e, sub[0][1], True
            # 词级补译：译文含中文但残留孤立小写英文短语（'itch page' 模型
            # 漏翻）→ 短语单独翻译替换回译文；模型补译输出仍保留的词
            # （'itch' 是 itch.io 专名）→ 记入本条 meta 豁免（要求原文也
            # 含该词，防幻觉），与模型保留 Gamejolt/Markiplier 同理。
            # backrooms 实证：'available at itch page' → 补译 → 'itch 页面'。
            # 纯回显场景（译文无中文 + untranslated_text，'outstanding
            # citizen' 全小写普通词回显，baldis 实证）：短语整词引用两跳
            # 直接译出（实测 裸→回显 / 引用→杰出公民）。
            if (callable(getattr(self.client, "translate_text", None))
                    and not sub[0][2]
                    and self._allows_fallback_retry(e)
                    and sub[0][1]
                    and ((_CJK.search(sub[0][1])
                          and "target_script_mismatch"
                          in set(e.quality_reasons))
                         or (not _CJK.search(sub[0][1])
                             and "untranslated_text"
                             in set(e.quality_reasons)))):
                repaired = self._repair_word_residue(
                    e, native_translate, target_lang, sub[0][1])
                if self._is_cancelled():
                    restore_original_state()
                    return e, "", False
                if repaired is not None and repaired[2]:
                    return repaired
            # 中文显式指令逐词补译（纯回显兜底）：native 英文 prompt 下
            # 1.8B 对简单/品牌原文稳定回显（'Out of the Loop studio' →
            # 回显，containment 实证；'Markiplier was here' 专名句回显，
            # backrooms 实证）——英文 references 引用重试（proper_name
            # reference / 多语言双跳 / 同对象译例）全部失败后才到这里。
            # 把整条原文当整体译名强制翻译（中文显式指令是 1.8B 翻译
            # 意图最强信号，与审校页 AI 翻译同级降级链同源，实测稳定产出
            # 'Out of the Loop 工作室'）：strip_prompt_echo 剥指令前缀/
            # 原文回显；剥空（模型仍回显）则不强求，交后续降级链。
            # 多语言源（西语/俄语/法语等，_is_multilingual_source）跳过：
            # 该类由双跳/同对象译例/多语言源保留放行链处理，不走中文指令
            # 硬译（测试桩 0.7B 无法响应中文指令）。
            if (callable(getattr(self.client, "translate_text", None))
                    and not sub[0][2]
                    and self._allows_fallback_retry(e)
                    and sub[0][1]
                    and not _CJK.search(sub[0][1])
                    and not _is_multilingual_source(e.original)
                    and ({"untranslated_text", "target_script_mismatch"}
                         & set(e.quality_reasons))):
                if self._is_cancelled():
                    restore_original_state()
                    return e, "", False
                try:
                    direct_out, direct_usage = translate_source_directive(
                        self.client, e.original, target_lang, self.references)
                except Exception as exc:  # noqa: BLE001
                    self._record_usage(None)
                    self._mark_request_failed(e, exc)
                    return e, "", False
                self._record_usage(direct_usage)
                if self._is_cancelled():
                    restore_original_state()
                    return e, "", False
                direct_clean = strip_prompt_echo(direct_out, "", e.original)
                if direct_clean.strip() and _CJK.search(direct_clean):
                    good = self._apply_quality(e, direct_clean)
                    return e, direct_clean, good
                # 整条原文作整体译名强制翻译（'Out of the Loop studio'
                # 类短专名/品牌名：中文指令后模型仍可能整段回显——逐词
                # 补译指令把整条原文当作整体译名，禁止回显）
                try:
                    word_out, _wusage = self.client.chat(
                        "", [{"role": "user", "content":
                              "请将以下名称翻译为简体中文，直接输出译名，"
                              "不得回显原文，不要添加任何解释：\n\n"
                              + e.original}])
                except Exception as exc:  # noqa: BLE001
                    self._record_usage(None)
                    self._mark_request_failed(e, exc)
                    return e, "", False
                self._record_usage(_wusage)
                if self._is_cancelled():
                    restore_original_state()
                    return e, "", False
                word_clean = strip_prompt_echo(word_out, "", e.original)
                if word_clean.strip() and _CJK.search(word_clean):
                    good = self._apply_quality(e, word_clean)
                    return e, word_clean, good
            # glossary 术语确定性修复：glossary_mismatch（模型译漏/双关
            # 误译术语，'Slash key'→'删除键' deadbeat 实证）→ 术语段
            # 替换为译例 + 非术语语义段翻译拼接（_repair_glossary_terms）。
            if (callable(getattr(self.client, "translate_text", None))
                    and not sub[0][2]
                    and self._allows_fallback_retry(e)
                    and "glossary_mismatch" in set(e.quality_reasons)):
                repaired = self._repair_glossary_terms(
                    e, native_translate, target_lang)
                if self._is_cancelled():
                    restore_original_state()
                    return e, "", False
                if repaired is not None and repaired[2]:
                    return repaired
            # 专名 references 重译：译文无中文（回显/半翻）+ 原文含 TitleCase
            # 专名 → 注入 (专名, 专名) 引用重译——模型把专名当术语保留、
            # 只译其余部分（backrooms 实证：'Markiplier was here' 回显 →
            # 注入 → 'Markiplier 曾来过这里'）。无中文可译部分时（纯专名
            # 'Shirt Decal' 被模型补成 'T-shirt Decal'）重译让模型按引用
            # 保留专名 → 回显经 proper_name_echo 放行（物品名保留合理）。
            if (callable(getattr(self.client, "translate_text", None))
                    and not sub[0][2]
                    and self._allows_fallback_retry(e)
                    and sub[0][1]
                    and not _CJK.search(sub[0][1])
                    and ({"untranslated_text", "target_script_mismatch"}
                         & set(e.quality_reasons))):
                retried = self._retry_with_proper_name_reference(
                    e, native_translate, target_lang, sub[0][1])
                if self._is_cancelled():
                    restore_original_state()
                    return e, "", False
                if retried is not None and retried[2]:
                    return retried
            # 间隔动作词聚合重译：'* Y A W N *' 逐字空格是打字机视觉写法
            # （动作旁白），1.8B 对原形态稳定回显（a-catfiends 实证 4 次
            # 仍回显，untranslated_text 恒败）；聚合为正常词（'* YAWN *'）
            # 后模型能正确译出中文且标签原位保留（实验实证）。聚合无变化
            # 或失败 → 恢复首译状态继续后续降级链。
            if (callable(getattr(self.client, "translate_text", None))
                    and not sub[0][2]
                    and self._allows_fallback_retry(e)
                    and _is_spaced_action(e.original)
                    and not _CJK.search(sub[0][1] or "")
                    and "untranslated_text" in set(e.quality_reasons)):
                repaired = self._repair_spaced_action_translation(
                    e, native_translate, target_lang)
                if self._is_cancelled():
                    restore_original_state()
                    return e, "", False
                if repaired is not None and repaired[2]:
                    return repaired
                if repaired is not None and not repaired[2]:
                    (e.translation, e.status, e.quality_reasons, e.meta) = (
                        first_fail_state)
            # 多语言源双跳：模型对含假名/重音字母的原文（日语/意语/法语等）
            # 倾向输出**英语译文**（准确但目标语错误，质量门拒绝）→ 以英语
            # 译文为中间源再译一次中文（模型英译中强项，alisa-demo 实证
            # 日语 → Right-hand key → 右手钥匙）。失败继续落到同对象译例。
            if (callable(getattr(self.client, "translate_text", None))
                    and not sub[0][2]
                    and self._allows_fallback_retry(e)
                    and _is_multilingual_source(e.original)
                    and not _CJK.search(sub[0][1] or "")
                    and _ENGLISH_WORD.search(sub[0][1] or "")):
                if self._is_cancelled():
                    restore_original_state()
                    return e, "", False
                try:
                    via, via_usage = native_translate(
                        (sub[0][1] or "").strip(), target_lang,
                        self.references)
                except Exception as exc:  # noqa: BLE001
                    self._record_usage(None)
                    self._mark_request_failed(e, exc)
                    return e, "", False
                self._record_usage(via_usage)
                if self._is_cancelled():
                    restore_original_state()
                    return e, "", False
                if isinstance(via, str) and via.strip():
                    via_good = self._apply_quality(e, via)
                    if via_good:
                        return e, via, via_good
            # 同对象译例：失败条目的同 obj 兄弟条目已成功（多语言打包游戏
            # 同一对象存英/法/意/日四版文本；对话流对象相邻句子）→ 注入
            # 「同一物品/对话流的参考译文」重试（alisa-demo 实证：Clé en
            # Fer 回显 → 注入 Iron Key translates to 铁钥匙 → 输出铁钥匙）。
            if (not sub[0][2]
                    and self._allows_fallback_retry(e)
                    and self._obj_reference_pairs(e)):
                if self._is_cancelled():
                    restore_original_state()
                    return e, "", False
                try:
                    obj_refs = self._obj_reference_pairs(e)
                    lang_names = getattr(
                        self.client, "_TARGET_LANGUAGE_NAMES", {}) or {}
                    lang_name = lang_names.get(
                        target_lang.strip().casefold(), target_lang.strip())
                    lines = ["Reference translations from the same item:"]
                    lines.extend(
                        f"{src} translates to {tgt}"
                        for src, tgt in obj_refs)
                    lines.extend([
                        "",
                        f"Translate the following text into {lang_name} "
                        "(same item as above):",
                        "",
                        e.original,
                    ])
                    retry_content, retry_usage = self.client.chat(
                        "", [{"role": "user",
                              "content": "\n".join(lines)}])
                except Exception as exc:  # noqa: BLE001
                    self._record_usage(None)
                    self._mark_request_failed(e, exc)
                    return e, "", False
                self._record_usage(retry_usage)
                if self._is_cancelled():
                    restore_original_state()
                    return e, "", False
                if isinstance(retry_content, str) and retry_content.strip():
                    retry_good = self._apply_quality(e, retry_content)
                    return e, retry_content, retry_good
            # 多语言源兜底放行：西语/俄语等原文是 1.8B 模型能力边界
            # （双跳/词级补译/同对象译例全部失败：'Obtuviste la "Aleación
            # Anti-Telequinetica".' → 日文假名乱入、'Mierda' → 解释性
            # 垃圾、'клипборд' → Klipboard 音译）→ 保留原文放行。这类
            # 多语言文件通常是游戏自带非玩家主语言（玩家用 CH 语言包），
            # 含中文时语义已在中，放行无害；language_source_kept 供
            # 人工校对筛选。日文源（假名）虽同属 multilingual，但
            # 假名-汉字同源可译（alisa-demo 实证双跳成功），此处兜底
            # 仅限无 CJK 的拉丁/西里尔语源。
            if (not sub[0][2]
                    and self._allows_fallback_retry(e)
                    and _is_multilingual_source(e.original)
                    and not _CJK.search(e.original)):
                e.translation = e.original
                e.quality_reasons = ()
                e.meta = dict(e.meta)
                _clear_review_state(e.meta)
                e.meta["quality_passed"] = True
                e.meta["quality_reasons"] = []
                e.meta["language_source_kept"] = True
                return e, e.original, True
            if (callable(getattr(self.client, "translate_text", None))
                    and not sub[0][2]
                    and self._is_actionable_ui_retry(e)):
                if self._is_cancelled():
                    return e, "", False
                try:
                    retry_content, retry_usage = native_translate(
                        e.original, target_lang, self.references)
                except Exception as exc:  # noqa: BLE001
                    self._record_usage(None)
                    self._mark_request_failed(e, exc)
                    return e, "", False
                self._record_usage(retry_usage)
                if self._is_cancelled():
                    restore_original_state()
                    return e, "", False
                if isinstance(retry_content, str):
                    retry_good = self._apply_quality(e, retry_content)
                    return e, retry_content, retry_good
                self._mark_failed(e, "invalid_response",
                                  raw_output=str(retry_content))
                return e, "", False
            return sub[0]

        try:
            results: list[tuple[TextEntry, str, bool]] = [None] * len(batch)  # type: ignore[list-item]
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                futures = {pool.submit(work, i): i for i in range(len(batch))}
                for fut in as_completed(futures):
                    if self._stop.is_set() or (self.cancellation_event is not None and self.cancellation_event.is_set()):
                        break
                    idx = futures[fut]
                    try:
                        results[idx] = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        self._mark_request_failed(batch[idx], exc)
                        results[idx] = (batch[idx], "", False)
                    if result_cb:
                        result_cb(results[idx])
                    elif per_item_cb:
                        per_item_cb()
            return [r for r in results if r is not None]
        finally:
            if old_timeout and config:
                config.timeout = old_timeout

    @staticmethod
    def _is_actionable_ui_retry(entry: TextEntry) -> bool:
        if not ({"untranslated_text", "target_script_mismatch",
                 "input_token_mismatch", "action_word_residue"}
                & set(entry.quality_reasons)):
            return False
        role = str(entry.meta.get("role", "display"))
        disposition = str(entry.meta.get("disposition", ""))
        if role not in {"ui", "display"}:
            return False
        if disposition in {
                "preserve", "structural", "proper_name", "code", "key"}:
            return False
        return (role == "ui" or disposition == "translate"
                or entry.original.strip().casefold() in BUILTIN_UI_SOURCE_TERMS)

    def retranslate_with_feedback(self, entry: TextEntry, feedback: str,
                                  round_no: int = 1) -> tuple[bool, str]:
        """T1-4 反馈式重译：深审闭环专用（带审核理由的单条重译）。

        prompt 在常规单条翻译基础上追加 [审核反馈] 段（上次译文 +
        问题 + 建议译文），单条调用翻译模型 → 过质量门复查
        （_apply_quality，通过即落盘 entry.translation）。
        质量门检查豁免批内一致性（skip_consistency=True，2026-08-14
        minato 实证）：翻译阶段缓存的坏译文会以 consistency_mismatch
        拒绝正确的重译，重译链路整体失效。

        预算：重译经 _record_failure_attempt 计入 attempt_count，与
        现有失败链共享类别预算（默认 2 次），防死循环（实施计划
        T1-4）。round_no 供再审收敛标记（T1-5 上限 2 轮）。

        返回 (是否通过质量门, 最终译文)；请求失败返回 (False, "")。
        """
        user = self._build_chat_user_prompt([
            self._build_item([entry], 0, 0, single=True)])
        user += (
            "\n\n[审核反馈] 上次译文：{0}；问题：{1}。"
            "请针对上述问题修正译文，只输出修正后的译文，不要解释。"
            "译文必须完整保留原文的全部占位符（{{0}}、%s、<b>…</b> 等）、"
            "全部数字和换行结构，不得增删改位。"
            .format(entry.translation or "（无）", feedback))
        # 2026-08-14 用户实证「审核完成后苦等无提示」：重译走默认
        # timeout（120s）——审核服务异常/1.8B 换入失败时单条挂满 120
        # 秒 × 全批条目 = 用户等几分钟到几十分钟且零反馈。重译单条
        # 用降级短超时（与 _chat_each 同口径），失败快速进兜底/标记
        # request_error，不让用户干等。
        config = getattr(self.client, "config", None)
        old_timeout = getattr(config, "timeout", None) if config else None
        if old_timeout:
            config.timeout = min(old_timeout, self.FALLBACK_TIMEOUT)
        try:
            content, usage = self.client.chat(
                self.system_prompt, [{"role": "user", "content": user}])
        except Exception as exc:  # noqa: BLE001 - 重译失败由调用方记 blocked
            self._record_usage(None)
            self._mark_request_failed(entry, exc)
            return False, ""
        finally:
            if old_timeout:
                config.timeout = old_timeout
        self._record_usage(usage)
        if self._is_cancelled():
            return False, ""
        candidate = (content or "").strip()
        if not candidate:
            _record_failure_attempt(entry, "model_behavior")
            return False, ""
        entry.meta = dict(entry.meta)
        entry.meta["review_round"] = int(round_no)
        good = self._apply_quality(entry, candidate, skip_consistency=True)
        if not good:
            # 纯回显兜底：英文反馈 prompt 下 1.8B 对简单/品牌原文仍回显
            # （'Out of the Loop studio' 反馈重译→回显，containment 实证）
            # → 中文显式指令（翻译意图最强信号）+ 逐词补译兜底，同
            # _chat_each 降级链口径。两次都失败才按 model_behavior 计账。
            target_lang = self.lang.rsplit("→", 1)[-1] or "zh-CN"
            try:
                direct_out, _du = translate_source_directive(
                    self.client, entry.original, target_lang,
                    self.references)
            except Exception:  # noqa: BLE001 - 指令路径失败交调用方 blocked
                direct_out = ""
            direct_clean = strip_prompt_echo(direct_out, "", entry.original)
            if direct_clean.strip() and _CJK.search(direct_clean):
                good = self._apply_quality(
                    entry, direct_clean, skip_consistency=True)
                if good:
                    return good, entry.translation
                _record_failure_attempt(entry, "model_behavior")
                return good, candidate
            # 整条原文作整体译名强制翻译（品牌名/短专名类）
            try:
                word_out, _wu = self.client.chat(
                    "", [{"role": "user", "content":
                          "请将以下名称翻译为简体中文，直接输出译名，"
                          "不得回显原文，不要添加任何解释：\n\n"
                          + entry.original}])
            except Exception:  # noqa: BLE001 - 指令路径失败交调用方 blocked
                word_out = ""
            word_clean = strip_prompt_echo(word_out, "", entry.original)
            if word_clean.strip() and _CJK.search(word_clean):
                good = self._apply_quality(
                    entry, word_clean, skip_consistency=True)
                if good:
                    return good, entry.translation
                _record_failure_attempt(entry, "model_behavior")
                return good, candidate
            _record_failure_attempt(entry, "model_behavior")
        return good, candidate

    def _build_chat_user_prompt(self, items: list[dict]) -> str:
        # P1：术语按命中注入——references 段只保留内置 UI 术语（恒定小
        # 集合，跨批一致性锚点）+ 批内命中的用户术语；未命中术语不注入
        # （术语表数百条时全量注入会稀释注意力、膨胀上下文）。
        # 与 [术语命中] 行（强制语气）互补：references 是参考，命中行是硬约束。
        batch_sources = " ".join(str(it.get("text", "")) for it in items)
        builtin_sources_cf = {s.casefold() for s, _ in BUILTIN_UI_REFERENCES}
        reference_lines = ["Reference the following translations:"]
        reference_lines.extend(
            f"{source} translates to {target}"
            for source, target in self.references
            if (str(source).casefold() in builtin_sources_cf
                or source_term_applies(str(source), batch_sources))
        )
        # 翻译 C6（阶段 2）：跨游戏相似语境参考注入——本批文本命中语境库
        # 相似指纹（多义词不同词义），注入参考行（参考不强制，防跨游戏
        # 污染；单批 Top-3 封顶，注入量不随库增长）。
        context_lines = self._context_reference_lines(items)
        if context_lines:
            reference_lines.append("")
            reference_lines.extend(context_lines)
        return "\n".join(reference_lines) + "\n\n" + build_batch_user_prompt(items)

    def _context_reference_lines(self, items: list[dict]) -> list[str]:
        """参考行注入：语境库跨游戏相似（阶段 2）+ 向量相似召回
        （阶段 4 T4-4，≥0.8），合并取 Top-3（注入量封顶）。

        与直填通道互补：直填只在同游戏同指纹命中时发生；这里覆盖跨游戏
        或同原文异语境——模型参考后自行消歧（Resume 主菜单=继续、
        Save the game. → Load the game. 相似召回）。
        """
        if self.context_store is None and self.vector_recall is None:
            return []
        lines: list[str] = []
        seen: set[tuple[str, str]] = set()

        def add(text: str, translation: str) -> bool:
            key = (text, translation)
            if key in seen or not translation:
                return False
            seen.add(key)
            return True

        texts = [str(item.get("text", "")).strip()
                 for item in items if str(item.get("text", "")).strip()]
        if self.vector_recall is not None and texts:
            for ref in self.vector_recall.recall(texts, limit=3):
                if add(ref["text"], ref["translation"]):
                    lines.append(f"相似参考：{ref['text']} → "
                                 f"{ref['translation']}")
                    if len(lines) >= 3:
                        return lines
        if self.context_store is not None:
            for item in items:
                text = str(item.get("text", ""))
                if not text.strip():
                    continue
                before = [str(x) for x in item.get("ctx_before", [])]
                after = [str(x) for x in item.get("ctx_after", [])]
                for entry in self.context_store.match_similar(
                        self.context_game, text,
                        scene="", ui_position=str(item.get("role", "")),
                        text_type="", ctx_before=before, ctx_after=after,
                        limit=4):
                    if add(entry.source_text,
                           entry.recommended_translation):
                        lines.append(f"语境参考："
                                     f"{self.context_store.format_reference(entry)}")
                        if len(lines) >= 3:
                            return lines
        return lines

    def _repair_multiline_chat_translation(
            self, entry: TextEntry) -> tuple[TextEntry, str, bool]:
        """Repair one chat translation by requesting each source segment once."""
        prefix, segments, separators = _split_translation_segments(
            entry.original)
        if len(segments) < 2:
            # 单行失败不在此修复：返回失败走 _chat_each 的 native 降级
            # （Hy-MT2 translate_text 单段 prompt + references 译例——
            # 知识库 TOSS TRASH → 丢垃圾 靠该路径生效；chat 逐段修复
            # 无译例，曾把单行 action_word_residue 截胡导致重试失效）
            return entry, "", False
        rebuilt: list[str] = [prefix]
        for index, part in enumerate(segments):
            segment_entry = replace(
                entry, original=part, translation="", quality_reasons=())
            user = self._build_chat_user_prompt([
                self._build_item([segment_entry], 0, 0, single=True)])
            if self._is_cancelled():
                return entry, "", False
            try:
                content, usage = self.client.chat(
                    self.system_prompt, [{"role": "user", "content": user}])
            except Exception as exc:  # noqa: BLE001
                self._record_usage(None)
                self._mark_request_failed(entry, exc)
                return entry, "", False
            self._record_usage(usage)
            if self._is_cancelled():
                return entry, "", False
            arr = self._response_array(content, segment_entry)
            translations = [
                item.get("translation")
                for item in (arr or [])
                if isinstance(item, dict)
                and item.get("id") == _entry_id(segment_entry)
                and isinstance(item.get("translation"), str)
            ]
            if len(translations) != 1:
                self._mark_failed(entry, "invalid_response", raw_output=content)
                return entry, "", False
            translated = translations[0].strip()
            if not translated:
                self._mark_failed(entry, "line_content_mismatch")
                return entry, "", False
            rebuilt.append(translated)
            if index < len(separators):
                rebuilt.append(separators[index])
        candidate = "".join(rebuilt)
        return entry, candidate, self._apply_quality(entry, candidate)

    def _repair_protected_chat_translation(
            self, entry: TextEntry) -> tuple[TextEntry, str, bool]:
        """Repair semantic fragments through the single-item chat contract."""
        parts = protected_slot_parts(entry.original)
        rebuilt: list[str] = []
        translated_any = False
        for protected, part in parts:
            if protected or not any(char.isalpha() for char in part):
                rebuilt.append(part)
                continue
            match = re.fullmatch(r"(\s*)(.*?)(\s*)", part, re.DOTALL)
            if match is None or not match.group(2):
                rebuilt.append(part)
                continue
            if self._is_cancelled():
                return entry, "", False
            segment_entry = replace(
                entry, original=match.group(2), translation="",
                quality_reasons=())
            user = self._build_chat_user_prompt([
                self._build_item([segment_entry], 0, 0, single=True)])
            try:
                content, usage = self.client.chat(
                    self.system_prompt, [{"role": "user", "content": user}])
            except Exception as exc:  # noqa: BLE001
                self._record_usage(None)
                self._mark_request_failed(entry, exc)
                return entry, "", False
            self._record_usage(usage)
            if self._is_cancelled():
                return entry, "", False
            arr = self._response_array(content, segment_entry)
            translations = [
                item.get("translation")
                for item in (arr or [])
                if isinstance(item, dict)
                and item.get("id") == _entry_id(segment_entry)
                and isinstance(item.get("translation"), str)
            ]
            if len(translations) != 1 or not translations[0].strip():
                self._mark_failed(entry, "invalid_response", raw_output=content)
                return entry, "", False
            rebuilt.extend((match.group(1), translations[0].strip(),
                            match.group(3)))
            translated_any = True
        if not translated_any:
            return entry, "", False
        candidate = "".join(rebuilt)
        return entry, candidate, self._apply_quality(entry, candidate)

    def _repair_protected_translation(
            self, entry: TextEntry, native_translate, target_lang: str,
            previous: str = "",
            ) -> tuple[TextEntry, str, bool] | None:
        """Retry semantic source fragments while preserving structural slots."""
        parts = protected_slot_parts(entry.original)
        if not any(protected for protected, _part in parts):
            return None
        rebuilt: list[str] = []
        translated_any = False
        semantic_cjk = False
        for protected, part in parts:
            if protected or not any(char.isalpha() for char in part):
                rebuilt.append(part)
                continue
            match = re.fullmatch(r"(\s*)(.*?)(\s*)", part, re.DOTALL)
            if match is None or not match.group(2):
                rebuilt.append(part)
                continue
            if self._is_cancelled():
                return entry, "", False
            body = match.group(2)
            try:
                translated, usage = native_translate(
                    body, target_lang, self.references)
            except Exception as exc:  # noqa: BLE001
                self._record_usage(None)
                self._mark_request_failed(entry, exc)
                return entry, "", False
            self._record_usage(usage)
            if self._is_cancelled():
                return entry, "", False
            if not isinstance(translated, str) or not translated.strip():
                self._mark_failed(entry, "line_content_mismatch")
                return entry, "", False
            if _CJK.search(translated):
                semantic_cjk = True
            rebuilt.extend((match.group(1), translated.strip(), match.group(3)))
            translated_any = True
        if not translated_any:
            return None
        candidate = "".join(rebuilt)
        good = self._apply_quality(entry, candidate)
        if not good and not semantic_cjk and previous:
            # 剥离段整体未翻出中文（模型对短片段回显/截断，deadbeat 真实样本：
            # ': config' → 'config'）→ 整段译文通常语义已正确，只是丢了
            # protected 段（按键/标签）→ 把整段译文中缺失的 protected 段回填
            # 到开头（整段译文已含按键时跳过，避免 'Enter 回车：配置' 重复）
            missing = "".join(
                part for protected, part in parts
                if protected and part.casefold() not in previous.casefold())
            if missing:
                candidate = missing + " " + previous.strip()
                good = self._apply_quality(entry, candidate)
        return entry, candidate, good

    def _repair_word_residue(
            self, entry: TextEntry, native_translate, target_lang: str,
            translation: str,
            ) -> tuple[TextEntry, str, bool] | None:
        """词级补译：译文已含中文但残留孤立小写英文短语（'itch page' 模型
        漏翻）→ 短语单独翻译后替换回译文。模型补译输出仍保留的词（'itch'
        是 itch.io 专名）→ 记入本条 meta 的 word_residue_exempt（要求原文
        也含该词，防模型幻觉），质量门据此豁免——与模型保留 Gamejolt /
        Markiplier 同理，专名保留是翻译规范（backrooms 实证）。复查失败时
        清除豁免标记，避免残留 meta 影响后续重试轮。"""
        residue_phrases: list[str] = []
        for match in _ENGLISH_PHRASE.finditer(translation):
            phrase = match.group(0)
            words = _ENGLISH_WORD.findall(phrase)
            if not words or len(words) > 2 or len(phrase) > 25:
                continue
            # 纯小写普通词短语才补译：TitleCase/全大写专名（Gamejolt）已走
            # 专名豁免、数字混合（4chan）已走数字邻接豁免——补译只针对
            # 模型漏翻的小写词（'itch page'）
            if not all(word[0].islower() and not word.isupper()
                       for word in words):
                continue
            residue_phrases.append(phrase)
        source_terms_cf = {
            word.casefold()
            for word in _ENGLISH_WORD.findall(
                SAFE_KEEPERS.sub(" ", entry.original)
                .translate(_ACCENT_TO_ASCII))}
        # 单个残留词（非短语）：'…warp 房间…'——模型对游戏内专名/术语
        # 半保留（warp room 传送房间）。补译该词：模型输出翻译 → 整词
        # 替换；回显 → 模型确认保留 → word_residue_exempt 豁免放行
        # （crash-back-in-time Uka-Uka 审判邀请 实证：首译高质量仅 warp
        # 残留，旧判定重试耗尽恒败）。条件：词在原文（防幻觉）、非功能词/
        # UI 词典/物理键、译文已含中文；短语覆盖的词不重复处理。
        # 全大写 UI 词典词例外（MAX 残留 = '最大'）：全大写通常被当专名
        # 跳过补译，但词典词（MAX/ON/OFF 类）是普通语义词，模型对全大写
        # 稳定回显（deepest-sword 'MAX SEARCH OPTIMIZED' 实证：MAX 半翻
        # 且 special_action 阻断专名豁免 → 恒败）→ 词典内全大写词可补译
        residue_words: list[str] = []
        if _CJK.search(translation):
            phrase_ranges = [
                (m.start(), m.end())
                for m in _ENGLISH_PHRASE.finditer(translation)]
            for m in _ENGLISH_WORD.finditer(translation):
                if any(ps <= m.start() and m.end() <= pe
                       for ps, pe in phrase_ranges):
                    continue
                w = m.group(0)
                # 纯小写普通词 或 全大写词典词（MAX=最大）；TitleCase 与
                # 全大写非词典词（Gamejolt 类专名）维持跳过
                lower_word = w[0].islower() and not w.isupper()
                upper_ui_word = w.isupper() and (
                    w.casefold() in _DISPLAY_WORDS_CASEFOLD)
                if (3 <= len(w) <= 16
                        and (lower_word or upper_ui_word)
                        and w.casefold() in source_terms_cf
                        and w.casefold() not in _ENGLISH_FUNCTION_WORDS
                        and (w.casefold() not in _DISPLAY_WORDS_CASEFOLD
                             or w.isupper())
                        and w.casefold() not in _BUILTIN_UI_TERMS_CASEFOLD
                        and w.casefold() not in PHYSICAL_KEY_NAMES_CASEFOLD
                        and w not in residue_words):
                    residue_words.append(w)
        # 标签-值格式串（'slash: 999' → 模型 'Slash: 999' 大小写规范化
        # 回显，TitleCase 检查跳过）：按原文形态提取标签词补译（译文无
        # 中文 → 无残留词可提取），替换时大小写不敏感（译文标签已被
        # 模型改首字母大写）
        label_values: list[str] = []
        if not residue_phrases and not residue_words:
            m = _LABEL_VALUE_FORMAT.match(entry.original)
            if m:
                label_values.append(m.group(1))
        if not residue_phrases and not residue_words and not label_values:
            return None
        repaired = translation
        confirmed: list[str] = []
        # 短语与单词统一补译（单词限 2 个防请求爆炸）
        for residue in (*residue_phrases, *residue_words[:2], *label_values):
            if self._is_cancelled():
                return entry, "", False
            # 词级补译优先查知识库译例（references）：模型对借词/术语裸
            # 翻译输出解释垃圾（'slash 翻译为 "斜线"'，deadbeat 实证），
            # 而译例（slash→斩击）是验证过的确定性映射 → 直接替换，
            # 零请求且不触发解释垃圾路径。keep 型（target==source，hiss
            # pop collection 类保留映射）跳过——保留是合理行为。
            term_target = next(
                (t for s, t in self.references
                 if s.strip().casefold() == residue.casefold()
                 and t.strip().casefold() != residue.casefold()
                 and source_term_applies(s, entry.original)),
                None)
            if term_target is not None:
                out, usage = term_target, None
                self._record_usage(usage)
            else:
                try:
                    out, usage = native_translate(
                        residue, target_lang, self.references)
                except Exception as exc:  # noqa: BLE001
                    self._record_usage(None)
                    self._mark_request_failed(entry, exc)
                    return None
                self._record_usage(usage)
                if not isinstance(out, str) or not out.strip():
                    continue
                residue_words_ = _ENGLISH_WORD.findall(residue)
                # 解释式输出（'slash 翻译为 "斜线": 999'——deadbeat slash
                # 实证：裸 prompt 下模型对借词输出解释而非译文）即使含中文
                # 也是垃圾，强制走引用重试（含英文/回显的非纯中文输出同样
                # 走重试）
                explanatory = bool(_EXPLANATORY_PATTERN.search(out)
                                   or _EXPLANATORY_PREFIX.search(out))
                if residue_words_ and (
                        explanatory
                        or not _ENGLISH_WORD.search(out)
                        or not _CJK.search(out)):
                    # 裸翻译输出非纯中文（可能直译误译：'itch page'→
                    # '痒页面'，backrooms 实证；或纯英文回显：'outstanding
                    # citizen'→回显，baldis 实证；或解释式垃圾）→ 逐词保留
                    # 引用重试：模型确认的专名会保留原文（'itch 页面'），
                    # 普通词引用后直译（'杰出公民'）。两种输出不一致 →
                    # 引用版可信（模型在裸 prompt 下把专名当普通词直译，
                    # 引用后识别为专名保留）；一致（仍纯中文/仍回显）→
                    # 词确可全译/保留，用第二意见
                    try:
                        ref_out, ref_usage = native_translate(
                            residue, target_lang,
                            tuple((w, w) for w in residue_words_))
                    except Exception as exc:  # noqa: BLE001
                        self._record_usage(None)
                        self._mark_request_failed(entry, exc)
                        return None
                    self._record_usage(ref_usage)
                    if isinstance(ref_out, str) and ref_out.strip():
                        ref_stripped = ref_out.strip()
                        if (_EXPLANATORY_PATTERN.search(ref_stripped)
                                or _EXPLANATORY_PREFIX.search(ref_stripped)):
                            # 引用后仍解释式：模型不稳定/无法翻译该词 →
                            # 放弃本条（解释垃圾替换进译文会被质量门误判
                            # passed，slash 实证：垃圾被判 passed）
                            return None
                        out = ref_stripped
            # 模型补译输出保留的英文词 = 模型确认的专名（itch）→ 豁免，
            # 但要求原文也含该词（防模型幻觉新词）；输出无英文 → 完全替换
            confirmed.extend(
                word.casefold() for word in _ENGLISH_WORD.findall(out)
                if word.casefold() in source_terms_cf)
            if residue in residue_words:
                repaired = _replace_word_first(repaired, residue, out)
            elif residue in label_values:
                # 标签替换大小写不敏感（模型把 'slash: 999' 规范化为
                # 'Slash: 999'，小写标签无法精确匹配译文）
                repaired = re.sub(
                    rf"\b{re.escape(residue)}\b", out, repaired,
                    count=1, flags=re.I)
            else:
                repaired = repaired.replace(residue, out)
        if repaired == translation:
            # 补译输出无变化：短语回显 = 模型未确认（维持失败，防漏翻）；
            # 单词回显 + 已确认保留（confirmed 非空）= 模型确认该词是
            # 术语 → 走豁免复查放行（Uka-Uka warp 实证）
            if not (confirmed and residue_words):
                return None
        entry.meta = dict(entry.meta)
        entry.meta["word_residue_exempt"] = confirmed
        good = self._apply_quality(entry, repaired)
        if not good and confirmed:
            # 复查失败：清除豁免标记，避免残留 meta 影响后续重试轮
            entry.meta = dict(entry.meta)
            entry.meta.pop("word_residue_exempt", None)
        return entry, repaired, good

    def _retry_with_proper_name_reference(
            self, entry: TextEntry, native_translate, target_lang: str,
            translation: str,
            ) -> tuple[TextEntry, str, bool] | None:
        """专名 references 重译：译文纯回显（untranslated_text）+ 原文含
        TitleCase 专名 + 其余部分可译（含小写普通词）→ 注入 (专名, 专名)
        引用重译整句——模型把专名当术语保留、只译其余部分（backrooms
        实证：'Markiplier was here' 回显 → 注入 Markiplier → 'Markiplier
        曾来过这里'）。纯专名回显（'Crash Bandicoot'）无小写可译部分 →
        不触发；UI 词典词（Save/Continue）不进专名引用（真漏翻仍失败）。
        """
        original = entry.original
        if not _ENGLISH_WORD.search(translation):
            return None
        proper_words = [
            word for word in _ENGLISH_WORD.findall(original)
            if word[0].isupper() and word[1:].islower()
            and word.casefold() not in _DISPLAY_WORDS_CASEFOLD
            and word.casefold() not in _BUILTIN_UI_TERMS_CASEFOLD
            # TitleCase 动作词（Interact/Press/Use…）不是专名：注入
            # (词, 词) 保留引用会让模型把整条短语当术语回显（containment
            # 实证：'Interact hold' → (Interact, Interact) → 完整回显判
            # glossary_mismatch）；裸翻译模型反而能直译（'互动保持'）。
            # _ACTION_VERB_ZH 即动作词身份表（知识库词表，跨游戏通用）。
            and word.casefold() not in _ACTION_VERB_ZH]
        if not proper_words:
            return None
        references = self.references + tuple(
            (word, word) for word in proper_words)
        try:
            out, usage = native_translate(original, target_lang, references)
        except Exception as exc:  # noqa: BLE001
            self._record_usage(None)
            self._mark_request_failed(entry, exc)
            return entry, "", False
        self._record_usage(usage)
        if self._is_cancelled():
            return entry, "", False
        if not isinstance(out, str) or not out.strip():
            return None
        good = self._apply_quality(entry, out)
        return entry, out, good

    def _repair_spaced_action_translation(
            self, entry: TextEntry, native_translate, target_lang: str,
            ) -> tuple[TextEntry, str, bool] | None:
        """间隔动作词聚合重译：'* Y A W N *' 逐字空格是打字机视觉写法
        （哈欠/惊呼等动作旁白），1.8B 对原形态稳定回显（a-catfiends
        实证 4 次重试仍回显）；聚合为正常词（'* YAWN *'）后模型能译出
        中文且标签原位保留（实验实证：'{punch=3,2}* YAWN *{w=3}{x}'
        → '{punch=3,2}* 哎呀 *{w=3}{x}'）。聚合无变化（无间隔词）→
        None 交后续降级链。

        2026-08-11 实测补强：1.8B 对 * SCOFF */* SIGH */* YAWN *
        /* GASP * 聚合形态仍稳定回显（只去空格不翻译）——动作旁白词
        是封闭词表，先查 _SPACED_ACTION_LEXICON 确定性直填（不走模型），
        未收录才交模型；两者都失败返回 None 不截断降级链。"""
        aggregated = aggregate_spaced_letters(entry.original)
        if aggregated == entry.original:
            return None
        # 封闭词典兜底：聚合形态中 * WORD * 的 WORD 在词典 → 直填
        # （模型能力边界：单动作词不在 1.8B 翻译范围，实测稳定回显）
        from hanhua.core.knowledge import spaced_action_lexicon
        m = re.search(r"\* ([A-Z]+) \*", aggregated)
        if m and spaced_action_lexicon(m.group(1)):
            out = aggregated.replace(
                m.group(0), f"* {spaced_action_lexicon(m.group(1))} *")
            good = self._apply_quality(entry, out)
            return entry, out, good
        try:
            out, usage = native_translate(
                aggregated, target_lang, self.references)
        except Exception as exc:  # noqa: BLE001
            self._record_usage(None)
            self._mark_request_failed(entry, exc)
            return entry, "", False
        self._record_usage(usage)
        if self._is_cancelled():
            return entry, "", False
        if not isinstance(out, str) or not out.strip():
            return None
        good = self._apply_quality(entry, out)
        return entry, out, good

    def _repair_glossary_terms(
            self, entry: TextEntry, native_translate, target_lang: str,
            ) -> tuple[TextEntry, str, bool] | None:
        """glossary_mismatch 确定性修复：原文含非 keep 术语（'Slash key'
        的 slash→斩击）但模型未按译例译（slash 双关译'删除键'，deadbeat
        实证）→ 术语段直接替换为译例（防模型对借词裸翻译输出解释垃圾）+
        非术语语义段 native 翻译 → 拼接。仅处理无换行短串（多行走
        multiline repair；超长走词级补译）。失败返回 None 不截断降级链。"""
        if "\n" in entry.original or len(entry.original) > 300:
            return None
        # 标签-值格式串（'slash: 999'/'eNCORE 1'）：标签译例 + 值原样
        # 保留即完整译文——值是 HUD 数字不翻译，语义段翻译必回显（无
        # 中文）导致拼接恒败（deadbeat run4 实证：12 条标签串全败于此）
        label_m = _LABEL_VALUE_FORMAT.match(entry.original)
        if label_m:
            label = label_m.group(1)
            value = label_m.group(2)
            delim = entry.original[len(label):entry.original.index(value)]
            for source, target in self.references:
                s, t = str(source).strip(), str(target).strip()
                if (s.casefold() == label.casefold()
                        and t.casefold() != s.casefold()):
                    candidate = f"{t}{delim}{value}"
                    return entry, candidate, self._apply_quality(
                        entry, candidate)
            return None
        matches: list[tuple[int, int, str, str]] = []
        for source, target in self.references:
            s, t = str(source).strip(), str(target).strip()
            if not s or not t or t.casefold() == s.casefold():
                continue
            if not source_term_applies(s, entry.original):
                continue
            matches.extend(
                (m.start(), m.end(), s, t)
                for m in re.finditer(
                    rf"(?<![A-Za-z0-9_]){re.escape(s)}(?![A-Za-z0-9_])",
                    entry.original, re.I))
        if not matches:
            return None
        matches.sort()
        merged: list[tuple[int, int, str, str]] = []
        for m in matches:
            if merged and m[0] < merged[-1][1]:
                continue  # 与已取术语重叠 → 跳过（排序稳定，先取先得）
            merged.append(m)
        pieces: list[tuple[str, bool]] = []
        pos = 0
        for start, end, s, t in merged:
            if start > pos:
                pieces.append((entry.original[pos:start], False))
            pieces.append((t, True))
            pos = end
        if pos < len(entry.original):
            pieces.append((entry.original[pos:], False))
        # 原文全由术语组成 → 译例拼接即完整译文，无需语义段翻译
        if not any(not is_term for _, is_term in pieces):
            candidate = "".join(t for t, _ in pieces)
            return entry, candidate, self._apply_quality(entry, candidate)
        out_parts: list[str] = []
        for text, is_term in pieces:
            text = text.strip()
            if not text:
                continue
            if is_term:
                out_parts.append(text)
                continue
            try:
                out, usage = native_translate(
                    text, target_lang, self.references)
            except Exception as exc:  # noqa: BLE001
                self._record_usage(None)
                self._mark_request_failed(entry, exc)
                return None
            self._record_usage(usage)
            if (not isinstance(out, str) or not out.strip()
                    or not _CJK.search(out)):
                # 语义段翻译失败/回显 → 放弃确定性拼接（保持失败状态走
                # 后续降级链，不截断）
                return None
            out_parts.append(out.strip())
        candidate = "".join(out_parts)
        if not _CJK.search(candidate):
            return None
        return entry, candidate, self._apply_quality(entry, candidate)

    def _repair_multiline_translation(
            self, entry: TextEntry, native_translate, target_lang: str,
            ) -> tuple[TextEntry, str, bool] | None:
        """Retry one malformed result by translating source segments — lines
        first, then sentences for long single-paragraph text (which echoes
        the source as untranslated_text when the prompt exceeds the context)."""
        prefix, segments, separators = _split_translation_segments(
            entry.original)
        if len(segments) < 2:
            return None
        rebuilt: list[str] = [prefix]
        echo_exempt: list[str] = []
        # 歌词源整条走歌词专用翻译路径：1.8B 模型对纯英文歌词句稳定续写
        # 英文而非翻译（deadbeat 'Tonight...' 2677 字符实证），中文引导 +
        # 限长 + 高 repeat_penalty 才能译出中文；逐句调用（已拆句）
        lyric_source = (
            _is_lyric_source(entry.original)
            and callable(getattr(self.client, "translate_lyrics", None)))
        for index, part in enumerate(segments):
            if self._is_cancelled():
                return entry, "", False
            if lyric_source:
                try:
                    translated, usage = self.client.translate_lyrics(
                        part, target_lang, self.references)
                except Exception as exc:  # noqa: BLE001
                    self._record_usage(None)
                    self._mark_request_failed(entry, exc)
                    return entry, "", False
            else:
                try:
                    translated, usage = native_translate(
                        part, target_lang, self.references)
                except Exception as exc:  # noqa: BLE001
                    self._record_usage(None)
                    self._mark_request_failed(entry, exc)
                    return entry, "", False
            self._record_usage(usage)
            if self._is_cancelled():
                return entry, "", False
            if not isinstance(translated, str) or not translated.strip():
                self._mark_failed(entry, "line_content_mismatch")
                return entry, "", False
            # 行级回显豁免：该行译文与原文整行相同（大小写变体）→ 模型
            # 确认该行不可翻译（音效/外语/俚语行），拼接用原文行并记入
            # echo_line_exempt（歌词分块尾部回显实证：'现代杀手' 3183
            # 字符最后一块含日文/俚语行模型回显英文 → 拼接后
            # target_script_mismatch 恒败）
            if translated.strip().casefold() == part.strip().casefold():
                echo_exempt.append(part.strip())
                rebuilt.append(part.strip())
                if index < len(separators):
                    rebuilt.append(separators[index])
                continue
            # 多语言源段双跳：模型对含假名/重音/罗曼功能词的段（意语
            # "Ve ne preghiamo" 等）倾向输出英语译文 → 以英语译文为中间源
            # 再译中文（alisa-demo 实证：长句逐段修复时短段被模型译成英语）
            if (_is_multilingual_source(part)
                    and not _CJK.search(translated)
                    and _ENGLISH_WORD.search(translated)):
                via, via_usage = native_translate(
                    translated.strip(), target_lang, self.references)
                self._record_usage(via_usage)
                if isinstance(via, str) and via.strip():
                    translated = via
            rebuilt.append(translated.strip())
            if index < len(separators):
                rebuilt.append(separators[index])
        if echo_exempt:
            entry.meta = dict(entry.meta)
            entry.meta["echo_line_exempt"] = echo_exempt
        candidate = "".join(rebuilt)
        return entry, candidate, self._apply_quality(entry, candidate)

    def _response_array(self, content: str, entry: TextEntry | None) -> list[dict] | None:
        arr = extract_json_array(content) or extract_json_array_fallback(content)
        if arr is not None:
            if entry is None:
                return arr
            requested_id = _entry_id(entry)
            if any(isinstance(item, dict) and item.get("id") == requested_id
                   for item in arr):
                return arr
        if (entry is not None
                and getattr(self.client, "accepts_plain_single", False)
                and isinstance(content, str) and content.strip()):
            item_id = _entry_id(entry)
            prefix = json.dumps(item_id, ensure_ascii=False) + ":"
            echoed_values = [
                line.strip()[len(prefix):].strip()
                for line in content.splitlines()
                if line.strip().startswith(prefix)
                and line.strip()[len(prefix):].strip()
            ]
            translation = echoed_values[0] if len(echoed_values) == 1 else content.strip()
            return [{"id": item_id, "translation": translation}]
        return None

    def _build_item(self, batch: list[TextEntry], i: int, context_window: int,
                    single: bool = False) -> dict:
        e = batch[i]
        ctx_parts = []
        if e.meta.get("context_before"):
            ctx_parts.append("prev: " + str(e.meta["context_before"])[:80])
        if e.meta.get("context_after"):
            ctx_parts.append("next: " + str(e.meta["context_after"])[:80])
        for off in range(1, context_window + 1):
            if i - off >= 0:
                ctx_parts.append("prev: " + batch[i - off].original[:80])
            if i + off < len(batch):
                ctx_parts.append("next: " + batch[i + off].original[:80])
        # 字数预算：中文 1 字 ≈ 3 字节（UTF-8），译文 ≤ 预算字 → 字节 ≈ 预算×3 ≤ 容量
        explicit_budget = e.meta.get("max_chars")
        budget = (explicit_budget if type(explicit_budget) is int and explicit_budget > 0
                  else max(2, len(e.original.encode("utf-8")) // 3))
        # P1：术语按条目命中注入——只把本条原文真正命中的术语带进 prompt
        # （术语表可数百条，全部注入会稀释注意力并膨胀上下文；命中注入
        #  让模型在翻译本条时聚焦正确译名）
        glossary_hits = [
            (source, target)
            for source, target in self.glossary
            if source_term_applies(str(source), e.original)
            and str(target).strip()
        ]
        # 知识命中（2026-08-14 用户要求：自动检索相关文本再注入，不
        # 全量拼 prompt）。match_text 按原文精确匹配（内置形态函数 +
        # 持久库精确原文对照，re.search pattern），只注入 map_to 有值
        # 的对照（形态规则已在 system_prompt 规则 6/11 覆盖，重复注入
        # 无益）；上限 3 条防单条膨胀。
        knowledge_hits: list[tuple[str, str]] = []
        if self.knowledge is not None:
            try:
                for rule in self.knowledge.match_text(e.original):
                    pattern = str(rule.get("pattern") or "")
                    map_to = str(rule.get("map_to") or "")
                    if not pattern or not map_to:
                        continue
                    if rule.get("kind") in {"spaced_action",
                                            "uppercase_action"}:
                        continue  # 形态识别，非精确对照
                    knowledge_hits.append((pattern, map_to))
                    if len(knowledge_hits) >= 3:
                        break
            except Exception:  # noqa: BLE001 知识检索故障不阻断翻译
                pass
        return {"id": _entry_id(e), "text": e.original,
                "file": e.file_id, "key_path": e.key_path,
                "role": str(e.meta.get("role", "display")),
                "reason": str(e.meta.get("reason", "")),
                "confidence": e.confidence,
                "context": " | ".join(ctx_parts) if ctx_parts else "",
                "short": len(e.original) <= 12, "budget": budget,
                "input_tokens": list(interaction_input_tokens(e.original)),
                "glossary_hits": glossary_hits,
                "knowledge_hits": knowledge_hits,
                "ctx_before": list(e.meta.get("ctx_before", [])),
                "ctx_after": list(e.meta.get("ctx_after", []))}

    def _validate(self, batch: list[TextEntry], arr: list[dict]
                  ) -> list[tuple[TextEntry, str, bool]]:
        by_id: dict[str, str] = {}
        invalid_ids: set[str] = set()
        for item in arr:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            translation = item.get("translation")
            if not isinstance(item_id, str):
                continue
            if item_id in by_id or item_id in invalid_ids or not isinstance(translation, str):
                by_id.pop(item_id, None)
                invalid_ids.add(item_id)
                continue
            by_id[item_id] = translation
        results: list[tuple[TextEntry, str, bool]] = []
        for e in batch:
            item_id = _entry_id(e)
            if item_id in invalid_ids or item_id not in by_id:
                self._mark_failed(e, "invalid_response")
                results.append((e, "", False))
                continue
            tr = by_id[item_id]
            good = self._apply_quality(e, tr)
            results.append((e, tr, good))
        return results

    def _apply_quality(self, entry: TextEntry, translation: str,
                       skip_consistency: bool = False) -> bool:
        # P0-3 证据留存：保存模型原始输出（自愈/修复前的原文样），
        # 供质量门审校与复盘；修复后的归一化输出存 normalized_output
        # （两者相同则省略，避免冗余）。写回只允许 quality_passed=True
        # 的条目，raw 证据不参与写回判断。
        raw_output = translation
        # 标签自愈：译文语义正确但占位符缺失/闭合标签乱序（模型漏写标记是
        # 稳定行为）→ 确定性补全/重排后再判定（a-catfiends/the-keeper/
        # interdream 真实样本）。模型新增占位符/顺序破坏 → 原样，仍判失败
        translation = self_heal_format_tags(entry.original, translation)
        # 外语混入自愈：Hy-MT2 多语言模型在中英翻译偶发输出韩文
        # （'该基金会의官方口号' 的 의、'最致命的 상황；同时' 的 상황，
        # containment EN 语言包/字幕实证）→ target_script_mismatch 且
        # 重试稳定复发。清洗条件严苛，防误伤：
        # ① 原文纯 ASCII（回显外语专名/原文含外语的场景不动）；
        # ② 混入块（连续的非中文非 ASCII 字母段）字符不在原文且 ≤4 个；
        # ③ 块前 8 字符与块后 8 字符内都有汉字（句中夹带——删除无损；
        #    句尾独立词 '爱丽丝 설정' 是译文主体内容，不清洗仍判失败；
        #    '設定です' 的です 场景由条件 ① 原文含韩文拦截）
        original_foreign = any(
            c.isalpha() and not c.isascii() for c in entry.original)
        if not original_foreign:
            spans: list[tuple[int, int]] = []
            i = 0
            n = len(translation)
            while i < n:
                ch = translation[i]
                if (ch.isalpha() and not ch.isascii()
                        and not self._is_chinese_ideograph(ch)
                        and ch not in entry.original):
                    j = i + 1
                    while j < n:
                        cj = translation[j]
                        if (cj.isalpha() and not cj.isascii()
                                and not self._is_chinese_ideograph(cj)
                                and cj not in entry.original):
                            j += 1
                        else:
                            break
                    spans.append((i, j))
                    i = j
                else:
                    i += 1
            remove_spans: list[tuple[int, int]] = []
            for start, end in spans:
                if end - start > 4:
                    continue
                left = translation[max(0, start - 8):start]
                right = translation[end:end + 8]
                if (any(self._is_chinese_ideograph(c) for c in left)
                        and any(self._is_chinese_ideograph(c)
                                for c in right)):
                    # 吞掉块前紧邻空白（'最致命的 상황' → 删 ' 상황'，
                    # 不留悬空空格 '最致命的 ；'）
                    while start > 0 and translation[start - 1] in " \t":
                        start -= 1
                    remove_spans.append((start, end))
            if remove_spans:
                parts = []
                prev = 0
                for start, end in remove_spans:
                    parts.append(translation[prev:start])
                    prev = end
                parts.append(translation[prev:])
                translation = "".join(parts)
        result = validate_translation_quality(
            entry, translation, self._glossary_force,
            check_placeholders=self.placeholder_check,
        )
        # 格式模板串自愈：日期/数字格式模板（yyyy-MM-dd HH:mm:ss）是
        # 不可译文本——模型对格式串稳定回显或「修正」（force-reboot
        # 实证 .ss→:ss 改动），任何改动都是格式破坏 → 恢复原文。
        # quality 侧 untranslated_text 已豁免（_is_format_template，
        # fix-25），此处补 target_script_mismatch 豁免缺口（恢复原文后
        # 纯 ASCII 仍触发 contains_wrong_script，由下方目标脚本判定
        # 加格式模板豁免放行）——第三轮 3 条恒败实证。
        if _is_format_template(entry.original):
            original_stripped = entry.original.strip()
            if result.normalized_translation != original_stripped:
                result = QualityResult(
                    result.passed, result.confidence, result.reasons,
                    original_stripped)
        target = self.lang.rsplit("→", 1)[-1].strip().casefold()
        translation = result.normalized_translation
        is_simplified_chinese = target in {"zh", "zh-cn", "zh-hans"}
        contains_wrong_script = self._has_disallowed_chinese_target_letters(
            entry, translation)
        # 扣除品牌/署名/credit 保护术语后仍有字母才算「有可翻译语义」：
        # Playstation/Xbox 等纯品牌串模型保留原文是合理行为，不判失败
        # （第二个参数传原文自身：从原文中移除其保护术语）
        source_has_semantic_text = any(
            char.isalpha()
            for char in semantic_target_text(entry.original, entry.original))
        normalized = result.normalized_translation
        # P0-3：把质量门前后的输出作为证据写入 meta（raw 始终存；
        # 归一化后与 raw 相同时不重复存）。setdefault 保留**首次**调用
        # 捕获的输出：repair/自愈路径会对同一 entry 多次调用本方法，
        # 后续调用的参数是修复拼装结果而非模型原始输出，不得覆盖证据。
        entry.meta = dict(entry.meta)
        entry.meta.setdefault("raw_output", raw_output)
        if normalized != entry.meta["raw_output"]:
            entry.meta.setdefault("normalized_output", normalized)
        role = str(entry.meta.get("role", "display"))
        disposition = str(entry.meta.get("disposition", ""))
        proper_name = role == "proper_name" or disposition == "proper_name"
        contains_chinese = any(
            self._is_chinese_ideograph(char) for char in translation)
        # 纯专名/标签回显豁免：原文与译文扣除符号后的字母序列相同，
        # 且原文无小写普通词、无 UI 词典词（Crash Bandicoot / [ S K I P ] /
        # 3DI70R 2024 / AI / IMGUI 保留原文合理；'Hello world' 回显有小写词、
        # 'SFX'/'Continue' 回显在 UI 词典 → 仍判失败）
        letters_source = re.sub(r"[^A-Za-z]", "", entry.original).casefold()
        letters_target = re.sub(r"[^A-Za-z]", "", translation).casefold()
        # 英文词检查剥离专名载体（@_domeDev\ndomedev.itch.io 的 domedev 是域名，
        # 不算小写普通词）
        proper_name_words = _ENGLISH_WORD.findall(
            SAFE_KEEPERS.sub(" ", entry.original))
        # 小写词用独立词检查：'Stefánsson' 的 ASCII 碎片 nsson 不算小写普通词
        # （zero-deaths 'Sir Stefán Karl Stefánsson' 专名回显真实样本）
        # 知识库特殊文本：全大写动作指令/间隔动作词是可翻译语义文本，
        # 不得当专名豁免（taxes 'TOSS TRASH' 实证：全大写无小写词、
        # 不在 UI 词典 → 曾回显被豁免放行）
        special_action = _is_uppercase_action(
            entry.original) or _is_spaced_action(entry.original)
        proper_name_echo = (
            letters_source
            and letters_source == letters_target
            # 语言名回显豁免（Español/Deutsch/Русский…）：语言选择器的
            # 显示文本保留原名是业界惯例（游戏语言列表从不翻译语言名）。
            # Español 含独立小写词会进 has_independent_lower_word 失败
            # 分支 → 语言名身份豁免（containment level*.assets 实证 6 条）
            and (not has_independent_lower_word(entry.original)
                 or _is_language_name(entry.original))
            and not special_action
            # 多语言源（含假名/重音/罗曼功能词）回显默认仍可豁免（法语人名
            # Stefánsson、日文频道名 Korone Ch. 是专名）——仅当同 obj 已有
            # 成功译文（多语言打包数组/对话流对象，如 alisa-demo 的四语言
            # 物品名）时禁止豁免：Clé Pomme 与 Iron Key 同 obj，须翻译。
            # 语言名（Español）恒豁免：语言选择器/多语言数组中的语言标签
            # 保留原名是业界惯例，不受同 obj 译例影响（containment
            # level1-6 assets 实证：English 先译后 Español 同 obj 被拒）
            and ((_is_language_name(entry.original)
                  or not _is_multilingual_source(entry.original))
                 or proper_name
                 or not self._obj_reference_pairs(entry))
            # UI 词检查跳过末位版本词（"UCLA Gold" 的 Gold 是版本后缀，
            # 回显保留合理——见 quality._ui_check_words，baldis 实证）
            # 驼峰技术缩写（VSync）即使进 UI 词典也允许回显：界面标准术语
            # （butterflies 实证：'VSync' 回显被判 target_script_mismatch）
            # 全大写 ≤3 字母缩写（SFX/BGM/UI）同样豁免：缩写是界面标准
            # 术语，1.8B 模型对单 token 缩写稳定回显（count-my-coins
            # 'SFX' 实证：重试耗尽仍回显 → target_script_mismatch 恒败）；
            # 词典内其余词（Quit/Volume/Settings）不豁免照常要求翻译
            and not any(
                (word.casefold() in _DISPLAY_WORDS_CASEFOLD
                 or word.casefold() in _BUILTIN_UI_TERMS_CASEFOLD)
                and not is_camel_tech_abbreviation(word)
                and not (len(word) <= 3 and word.isupper())
                for word in _ui_check_words(proper_name_words)))
        # lorem ipsum 占位文本回显（无中文）是合理行为 → 豁免
        lorem_placeholder = is_lorem_ipsum_placeholder(entry.original)
        # glossary keep 术语组成的原文（hiss pop collection）TitleCase 化
        # 回显是模型保留术语 → 豁免 untranslated_text（222am 实证）
        glossary_keep_echo = _glossary_keep_echo(
            entry.original, translation, self.glossary)
        if (result.passed and is_simplified_chinese
                and ((contains_wrong_script and not proper_name_echo
                      and not lorem_placeholder
                      and not _is_format_template(entry.original))
                     or (source_has_semantic_text and not proper_name
                         and not contains_chinese and not proper_name_echo
                         and not lorem_placeholder
                         and not glossary_keep_echo
                         and not is_log_template(entry.original)
                         and not _is_format_template(entry.original)))):
            entry.translation = result.normalized_translation
            entry.quality_reasons = ("target_script_mismatch",)
            entry.meta = dict(entry.meta)
            entry.meta["quality_passed"] = False
            entry.meta["quality_reasons"] = ["target_script_mismatch"]
            _record_failure_attempt(entry, "target_script_mismatch")
            return False
        # Q1 语义门：BUILTIN_UI_REFERENCES 精确对照（权威参考译文子串要求）。
        # 原文精确命中内置 UI 术语 source 时，译文必须包含参考译名——
        # 参考译文已在 prompt 中，模型给出不相关译文（'Resume'→'简历'）是
        # 稳定错误，重试大概率复败；拦截（builtin_ui_mismatch）而非放行，
        # 防止错误译文进记忆 + 一致性锚定复制污染（Q2 记忆毒化防护闭环）。
        # 放在 echo 豁免之前：UI 术语回显由本门先拦（reason 更准确），
        # 且失败即 return，不会写入一致性锚定表。
        builtin_target = self.builtin_ui_exact.get(
            entry.original.strip().casefold())
        if (result.passed and builtin_target is not None
                and not proper_name_echo and not lorem_placeholder):
            if builtin_target not in translation:
                entry.translation = result.normalized_translation
                entry.quality_reasons = ("builtin_ui_mismatch",)
                entry.meta = dict(entry.meta)
                entry.meta["quality_passed"] = False
                entry.meta["quality_reasons"] = ["builtin_ui_mismatch"]
                _record_failure_attempt(entry, "builtin_ui_mismatch")
                return False
        if (proper_name_echo or lorem_placeholder or glossary_keep_echo
                or _is_format_template(entry.original)):
            # Q4 回显豁免打标：译文=原文（字母序列相同/占位/术语保留/
            # 格式模板）——这是「模型未翻译」而非「翻译结果」。打标后：
            # 1) 写回/审计/统计可见该条目是回显保留而非真译文；
            # 2) 记忆写入跳过（防「原文→原文」无效记忆污染跨游戏锚定）。
            entry.meta = dict(entry.meta)
            entry.meta["echo_exempt"] = (
                "proper_name" if proper_name_echo
                else "lorem_placeholder" if lorem_placeholder
                else "glossary_keep" if glossary_keep_echo
                else "format_template")
        if result.passed:
            role = str(entry.meta.get("role", "display"))
            consistency_key = (entry.original, role)
            with self._consistency_lock:
                previous = self._consistent_translations.get(consistency_key)
                if skip_consistency:
                    # 反馈重译豁免（2026-08-14 minato 实证）：翻译阶段
                    # 已把坏译文缓存进批内一致性表——重译的目的正是替换
                    # 坏译文，若按 consistency_mismatch 拒绝「与坏译文
                    # 不同的正确重译」（Pan 先生→左滑、Hearts 心脏→爱心
                    # 全部被杀），重译永不可能收敛，只能 BLOCKED 留人工。
                    # 重译通过后把新译文写入缓存（同原文其他条目后续
                    # 保持一致锚定）。
                    self._consistent_translations[consistency_key] = (
                        result.normalized_translation)
                elif previous is None:
                    self._consistent_translations[consistency_key] = (
                        result.normalized_translation)
                elif previous != result.normalized_translation:
                    entry.translation = result.normalized_translation
                    entry.quality_reasons = ("consistency_mismatch",)
                    entry.meta = dict(entry.meta)
                    entry.meta["quality_passed"] = False
                    entry.meta["quality_reasons"] = ["consistency_mismatch"]
                    _record_failure_attempt(entry, "consistency_mismatch")
                    return False
        entry.translation = result.normalized_translation
        entry.quality_reasons = result.reasons
        entry.meta = dict(entry.meta)
        # #9：重译成功清旧审核终态（BLOCKED/NEEDS_REVISION 残留会
        # fail-closed 拒绝新译文）；quality 门字段随 result 重写
        _clear_review_state(entry.meta)
        entry.meta["quality_passed"] = result.passed
        entry.meta["quality_reasons"] = list(result.reasons)
        if not result.passed and result.reasons:
            _record_failure_attempt(entry, result.reasons[0])
        return result.passed

    def _has_disallowed_chinese_target_letters(
            self, entry: TextEntry, translation: str) -> bool:
        role = str(entry.meta.get("role", "display"))
        disposition = str(entry.meta.get("disposition", ""))
        # 调试日志模板串（'MEMORY: cur = {0}MB, max = {1}MB'）：变量名
        # cur/max/real/total 是脚本标识符无语义，译文保留是正确行为 →
        # 豁免英文残留检查（final-shot 实证；回显侧豁免在 quality
        # untranslated_text 判定，见 is_log_template）
        if is_log_template(entry.original):
            return False
        allowed_terms = []
        if role == "proper_name" or disposition == "proper_name":
            allowed_terms.append(entry.original)
        allowed_terms.extend(
            str(target) for source, target in self.glossary
            if source_term_applies(str(source), entry.original)
            and str(target).strip()
        )
        # lorem ipsum 占位文本（开发者填充的假拉丁文本，无真实语义）→
        # 模型回显是合理行为（zero-deaths 'Loem iipsum solar' 真实样本）
        if is_lorem_ipsum_placeholder(entry.original):
            return False
        # 原文英文词集（casefold）：驼峰技术缩写豁免需原文也含该词
        # （防模型幻觉新词）。用原文全量计算（不剥 SAFE_KEEPERS）：
        # 版本号/域名/路径里的词（0.4.0beta 的 beta、contact@邮箱的
        # contact）在译文残留是「模型保留原文词」的证据，词集须含它们
        # 才可豁免（containment 实证：beta 在 SAFE_KEEPERS 剥后词集
        # 缺失 → digit_adjacent 豁免失效）
        source_terms_cf = {
            word.casefold()
            for word in _ENGLISH_WORD.findall(
                entry.original.translate(_ACCENT_TO_ASCII))}
        semantic = semantic_target_text(
            entry.original, translation, allowed_terms)
        # 原文交互按键词（Escape/P/X）在译文保留是正确行为 → 移除后判定
        for event in interaction_input_events(entry.original):
            if event.kind == "literal_glyph":
                semantic = semantic.replace(event.value, "", 1)
        # @用户名紧邻的显示名（"game by fie (@zkfie)" 的 fie 是作者名）→ 豁免
        display_names = set()
        for match in _AT_USER.finditer(semantic):
            head = semantic[max(0, match.start() - 12):match.start()]
            for word in _DISPLAY_NAME_BEFORE_AT.findall(head):
                display_names.add(word.casefold())
        # 模型正确保留的专名载体 → 移除后判定（不算英文残留）：
        # 3+ 段路径（User/Blah/Hey/HotelParadiseScreenshot）、域名（itch.io /
        # OpenGameArt.com）、@用户名（@zkfie / @SoftdevWu）、版本号（0.4.0beta）
        semantic = SAFE_KEEPERS.sub(" ", semantic)
        # multiline 行级回显豁免：repair 确认不可翻译的行（音效/外语/
        # 俚语行，模型整行回显）拼接用原文行 → 从语义词中移除这些行
        # （歌词分块尾部回显实证：回显行被当英文残留恒判失败）
        for line in entry.meta.get("echo_line_exempt", []):
            semantic = semantic.replace(str(line), " ")
        # 聊天/控制台命令（"/kick"、/give）→ 游戏命令保留原文是正确行为
        semantic = _SLASH_COMMAND.sub(" ", semantic)
        # 非 ASCII 字母（俄/日/韩/阿拉伯文…）→ 目标脚本错误，判失败
        # （中文目标不允许混入其他脚本字母；日文汉字与中文同码区不受影响）
        # 原文本身含该脚本字母（"Russian Localization - Алеся Апухтина" 的
        # 译者名）且译文已含中文翻译 → 模型保留人名合理，不算目标脚本错误
        # （纯日文回显 "ゲーム設定" 无中文翻译 → 仍判失败并重试）
        source_foreign = {
            char for char in entry.original
            if char.isalpha() and not char.isascii()
            and not self._is_chinese_ideograph(char)}
        # 原文自身的汉字（日文汉字同码区）→ 译文中出现它们可能是回显：
        # "ゲーム設定" → "ゲーム設定" 的 設定 不能证明译文含中文翻译
        source_ideographs = {
            char for char in entry.original
            if self._is_chinese_ideograph(char)}
        has_chinese = any(
            self._is_chinese_ideograph(char) and char not in source_ideographs
            for char in translation)
        # 原文引号内片段的英文词：译文保留原文引文是正确行为（见模块级
        # _QUOTE_CONTENT 注释）——仅当译文已含中文翻译（纯回显不豁免）
        quote_words: set[str] = set()
        for match in _QUOTE_CONTENT.finditer(entry.original):
            quote_words.update(
                word.casefold()
                for word in _ENGLISH_WORD.findall(
                    match.group(1).translate(_ACCENT_TO_ASCII)))
        if any(char.isalpha() and not char.isascii()
               and not self._is_chinese_ideograph(char)
               and not (char in source_foreign and has_chinese)
               for char in semantic):
            return True
        # 重音归一化串：英文词提取专用（_ENGLISH_WORD 纯 ASCII 会拆碎
        # 带重音专名 → 小写碎片误判英文残留）。长度不变（一对一词符），
        # finditer 索引与 semantic 对齐；非 ASCII 字母检查已在上面用原串完成
        semantic_ascii = semantic.translate(_ACCENT_TO_ASCII)
        # 签名位豁免：原文破折号后的尾部小写名（"Turkish Localization -
        # yamur <3" 的 yamur 是译者署名）→ 译文保留是正确行为。
        # 要求译文已含中文翻译（纯回显 "Turkish Localization - yamur" 不豁免）
        signature_words: set[str] = set()
        parts = re.split(r"[-–—]\s+", entry.original)
        if len(parts) > 1:
            signature_words = {
                word.casefold()
                for word in _ENGLISH_WORD.findall(
                    parts[-1].translate(_ACCENT_TO_ASCII))}
        # 问候行豁免：译文首行以问候语开头（Hello, there. / Hi!）且首行英文词
        # ≤2 个、译文已含中文 → 问候保留是本地化惯例（mimic-search 的
        # "Hello,\n\n\n几小时前…"、soul-delivery 的 "Hello, there.\n\n在过去的
        # 6个月里…"）。纯回显（无中文）不豁免。
        greeting_words: set[str] = set()
        first_line = translation.splitlines()[0] if translation.splitlines() else ""
        first_words = _ENGLISH_WORD.findall(
            first_line.translate(_ACCENT_TO_ASCII))
        if (first_words and has_chinese
                and first_words[0].casefold() in _GREETING_WORDS
                and len(first_words) <= 2):
            greeting_words = {word.casefold() for word in first_words}
        # rich-text 包裹的小写词（<color=#FFD700><b>lucd</b></color> 的作者名
        # 高亮、lucd#9569 Discord id）→ 译文保留是正确行为（slendergus 真实
        # 样本）；要求已含中文（纯回显 "<b>hello</b>" 不豁免）。
        # 注意：semantic 已剥离标签，须从带标签的原文译文提取
        rich_words: set[str] = set()
        if has_chinese:
            rich_words = {
                match.group(1).casefold()
                for match in re.finditer(
                    r">([A-Za-z]{3,})<",
                    translation.translate(_ACCENT_TO_ASCII))}
        # 连续英文短语（词间无中文间隔）→ 明确半翻，判失败；
        # 但短语中全为专名形态（TitleCase/全大写且非词典词，如 "Amitte Sukku"
        # 人名并列、Escape 按键名）不算英文残留
        # 数字邻接词（"4chan" 的 chan、"23andMe" 的 and）：数字+字母混合形态
        # 多为网站/用户名/版本号（backrooms 实证：译文保留 "4chan" 被拆出
        # 小写碎片 "chan" → 误判英文残留）；要求原文也含该词（防模型幻觉）。
        # 用原文计算：SAFE_KEEPERS 会把版本号（0.4.0beta）整段剥掉，semantic
        # 中已无邻接数字（containment 实证：'0.4.0beta' 的 beta 在译文残留
        # 时因此漏判 → 版本后缀词恒败）
        original_ascii = entry.original.translate(_ACCENT_TO_ASCII)
        digit_adjacent_words = {
            match.group(0).casefold()
            for match in _ENGLISH_WORD.finditer(original_ascii)
            if (match.start() > 0
                and original_ascii[match.start() - 1].isdigit())
            or (match.end() < len(original_ascii)
                and original_ascii[match.end()].isdigit())}
        # 下划线连接标识符的组成部分（Pixabay 音乐作者用户名
        # Tim_Kulig_Free_Music / Brotheration_Records / Eremit_der_Schatten
        # ——eyeless-jack 实证）：下划线连接是标识符/用户名/文件名的形态
        # 特征（真实英文句子用空格），译文保留用户名正确 → 其各段豁免。
        # 用原文计算（防模型幻觉新词）。
        underscore_identifier_words: set[str] = set()
        for um in re.finditer(r"[A-Za-z]+(?:_[A-Za-z]+)+", original_ascii):
            underscore_identifier_words.update(
                w.casefold() for w in _ENGLISH_WORD.findall(um.group(0)))
        # 键位绑定后缀豁免（faerie-afterlight 实证 ×10）：'Press {0} to
        # open Map of ...</color>.:map' 的 '.:map' 是按键绑定显示标记
        # （键名后缀），译文保留 ':map'/':interact'/':jump' 是正确行为
        # ——绑定名是引擎指令不是英文残留。要求原文同形（':' + 小写
        # 键名）防模型幻觉新增后缀。
        keybind_suffix_words = {
            m.group(1).casefold()
            for m in re.finditer(r":([a-z]{2,})", original_ascii)}
        # 原文 TitleCase 首词短语段豁免（faerie 实证 ×6）：'Before Pish
        # Shop'→'Pish Shop之前'（商店专名）、"Wispy's Chat (Auto
        # Dialogue)"→"Wispy's Chat (自动对话)"（频道专名）、'Solium
        # dual\tPolar-Solium'→'Solium dual：双极型电池'（物品专名）、
        # 'Vallon noir III' 法语物品名、多语言打包对话（'...Wispy:
        # Mungkin suara itu sungguh datang dari Lucentia...'——模型只译
        # 英语段、保留印尼语/西语段是正确行为）。
        # 形态：原文中以 TitleCase 词（≥3 字符、非功能词/交互动作词/
        # UI 词典词/术语表词）开头、后续词任意大小写延续（词间间隔
        # ≤3 字符，容 's 属格与 tab）的连续词段，段长 ≥2 且段内含
        # 非功能词 → 段内词全豁免。
        # 防过宽：'I like 吃披萨' 的 I 单字符不成立段首（1.8B 模型输出
        # 功能词残留是真半翻译）；'Press 按钮' 的 Press 是交互动作词；
        # 'Slash key' 命中术语表 (slash, 斩击) → 术语要求优先，不豁免
        # （deadbeat 实证）；'The Fidelity' 的 The 是功能词段首不成立。
        title_phrase_words: set[str] = set()
        _term_cf = {
            str(s).casefold() for s, _ in _glossary_pairs(self.glossary)}
        _term_cf |= {
            str(t).casefold() for _, t in _glossary_pairs(self.glossary)}
        # 外语文本特征（faerie 实证 '¿Acaso se me cayó por'：西语段内
        # 2 字母功能词 se/me 被 _ENGLISH_WORD 的 ≥3 过滤 → 段间隙 7 > 3
        # 断开 → cayo/por 漏豁免）。译文含非 ASCII 字母（且非中文表意字）
        # 说明存在保留的外语段 → 段间隙放宽到 7（容纳两个 2 字母外语
        # 功能词）。防过宽：纯 ASCII 译文（'Use it in the room 在房间
        # 使用' 类真半翻译）不放宽，间隙 4-7 仍断开。
        _foreign_gap = any(
            char.isalpha() and not char.isascii()
            and not self._is_chinese_ideograph(char)
            for char in translation)
        _title_matches = list(_ENGLISH_WORD.finditer(original_ascii))
        _phrase_seg: list[tuple[int, int]] = []  # (词起点, 词终点)
        for _tm in _title_matches:
            _w = _tm.group(0)
            _wcf = _w.casefold()
            _is_title_start = (
                len(_w) >= 3 and _w[0].isupper() and _w[1:].islower()
                and _wcf not in _ENGLISH_FUNCTION_WORDS
                and _wcf not in _ACTION_VERB_ZH
                and _wcf not in _DISPLAY_WORDS_CASEFOLD
                and _wcf not in _BUILTIN_UI_TERMS_CASEFOLD
                and _wcf not in _term_cf)
            _gap = (_tm.start() - _phrase_seg[-1][1]
                    if _phrase_seg else 4)
            _gap_ok = _gap <= 3 or (_foreign_gap and _gap <= 7)
            if _is_title_start and _gap_ok:
                _phrase_seg.append((_tm.start(), _tm.end()))
            elif _phrase_seg and _gap_ok:
                # 段延续：小写词/功能词紧跟 TitleCase 首词（Solium dual、
                # Vallon noir III、Mungkin suara itu…）
                _phrase_seg.append((_tm.start(), _tm.end()))
            else:
                _phrase_seg = ([( _tm.start(), _tm.end())]
                               if _is_title_start else [])
            if len(_phrase_seg) >= 2 and any(
                    original_ascii[s:e].casefold()
                    not in _ENGLISH_FUNCTION_WORDS
                    for s, e in _phrase_seg):
                title_phrase_words.update(
                    original_ascii[s:e].casefold()
                    for s, e in _phrase_seg)
        # 词级补译确认的保留词（'itch page' 补译 → 模型输出保留 itch 专名）
        # → 仅本条生效的豁免（补译时已校验词在原文出现，防幻觉）
        word_residue_exempt = {
            str(word).casefold()
            for word in entry.meta.get("word_residue_exempt", [])}
        # 模型小写化专名：原文 TitleCase 词在译文以小写出现（Bossfight →
        # bossfight）→ 专名保留、大小写形态差异不算英文残留（baldis 实证：
        # 'Triangle Button: Pause (Quit In The Bossfight Gamemode)' 译文
        # '…bossfight 游戏模式…'）。UI 词典词除外（Save → save 是真漏翻）。
        title_in_source = {
            word.casefold()
            for word in _ENGLISH_WORD.findall(
                SAFE_KEEPERS.sub(" ", entry.original))
            if word[0].isupper()}
        lowercased_proper = {
            word.casefold() for word in title_in_source
            if word.casefold() not in _DISPLAY_WORDS_CASEFOLD
            and word.casefold() not in _BUILTIN_UI_TERMS_CASEFOLD
            and word.casefold() not in _ENGLISH_FUNCTION_WORDS
            and word.casefold() not in _ACTION_VERB_ZH}
        # 译文引号内的 TitleCase 短语：模型用引号包裹专名（游戏内按钮名/
        # 关卡名/成就名，如 按钮 "Jump During Playtime"）是稳定行为——
        # 引号是模型对专名的强调标记，保留原文合理（baldis 实证：Button
        # 类条目模型输出 按钮"Jump During Playtime" 被当英文短语误判）。
        # 每个词都须在原文出现（防误译放行：X Button 的 "Jump Along"——
        # Along 不在原文 → 是模型直译误译的专名，不得豁免）。
        # 公共实现见 quality.quoted_proper_terms（交互动作词检查共用）
        translated_quote_proper = quoted_proper_terms(
            translation.translate(_ACCENT_TO_ASCII),
            SAFE_KEEPERS.sub(" ", entry.original)
            .translate(_ACCENT_TO_ASCII)) if has_chinese else set()
        # 连字符拼写变体（hi-hat/hi-hat）：原文连写词（hihat）在译文按
        # 标准写法拆分（Hi-hat 是踩镲标准名）→ 译文连字符词去连字符后
        # 等于原文词 → 合法拼写变体，其分词残留豁免（crash-back-in-time
        # 'hihat cymbal'→'Hi-hat 钹' 实证：hat 被当普通词残留误判恒败）
        dehyphenated_variants: set[str] = set()
        if has_chinese:
            for vm in re.finditer(
                    r"[A-Za-z]{2,}(?:-[A-Za-z]{2,})+", semantic_ascii):
                if vm.group(0).replace("-", "").casefold() in source_terms_cf:
                    dehyphenated_variants.update(
                        w.casefold()
                        for w in _ENGLISH_WORD.findall(vm.group(0)))
        phrase = _ENGLISH_PHRASE.search(semantic_ascii)
        if phrase:
            semantic_words = _ENGLISH_WORD.findall(semantic_ascii)
            # 短语覆盖的语义词索引（按在全文中的位置对齐 —— 邻居判断必须
            # 用全文：'Fun New' 中 New 的右邻 School 在短语外）
            p_indices = [
                i for i, m in enumerate(_ENGLISH_WORD.finditer(semantic_ascii))
                if phrase.start() <= m.start() and m.end() <= phrase.end()]
            for i in p_indices:
                word = semantic_words[i]
                if word.casefold() in dehyphenated_variants:
                    continue
                if word.casefold() in PHYSICAL_KEY_NAMES_CASEFOLD:
                    continue
                if word.casefold() in display_names:
                    continue
                if (word.casefold() in quote_words and has_chinese):
                    continue
                if (word.casefold() in signature_words and has_chinese):
                    continue
                if word.casefold() in greeting_words:
                    continue
                if (word.casefold() in rich_words and has_chinese):
                    continue
                if (word.casefold() in digit_adjacent_words
                        and word.casefold() in source_terms_cf):
                    continue
                # 下划线连接标识符组成部分（用户名 Tim_Kulig_Free_Music）：
                # 保留是正确行为 → 豁免（eyeless-jack 实证）
                if word.casefold() in underscore_identifier_words:
                    continue
                # 键位绑定后缀（':map'/':interact'——faerie 实证）：
                # 绑定名是引擎指令，译文保留正确 → 豁免
                if word.casefold() in keybind_suffix_words:
                    continue
                # 原文 TitleCase 短语段/多语言段（'Pish Shop' 商店专名、
                # 'Mungkin suara itu…' 印尼语段——faerie 实证）：模型
                # 保留专名/外语段只译其余 → 豁免
                if word.casefold() in title_phrase_words:
                    continue
                if word.casefold() in word_residue_exempt:
                    continue
                if word.casefold() in translated_quote_proper:
                    continue
                # 模型小写化专名：原文 TitleCase 词在译文小写残留
                # （Bossfight → bossfight）→ 专名保留不是漏翻
                if word.islower() and word.casefold() in lowercased_proper:
                    continue
                # 驼峰技术缩写（VSync/MonoBehaviour）→ 界面标准术语，保留
                # 原文合理（vincent 'VSync: OFF' → 'VSync：关闭'）；形态要求
                # 首大写 + 内部混合大小写（全大写 SETTINGS/TitleCase Save
                # 仍按词典规则判定）且原文也含该词（防模型幻觉新词）
                if (is_camel_tech_abbreviation(word)
                        and word.casefold() in source_terms_cf):
                    continue
                # 小写词/UI 词典词夹在 TitleCase 专名词之间（《Baldi's Fun New
                # School Remastered》的 New、'Craftydelight the Asset Store' 的
                # the）→ 专名短语的一部分，豁免；孤立词（'按下 the button'、
                # 句首的 Open）仍判失败
                if word.islower() or word.casefold() in _DISPLAY_WORDS_CASEFOLD:
                    left_title = (i > 0
                                  and semantic_words[i - 1][0].isupper()
                                  and semantic_words[i - 1][1:].islower())
                    right_title = (i + 1 < len(semantic_words)
                                   and semantic_words[i + 1][0].isupper()
                                   and semantic_words[i + 1][1:].islower())
                    if not (left_title and right_title):
                        # UI 词典词（TitleCase 形态）右侧连续 ≥2 个非词典
                        # TitleCase 专名词 → 专名短语（'Play Games Plugin'
                        # 的 Play——Google Play Games 插件专名，Play 是品牌
                        # 词非按钮动词；driftapocalypse 日志串实证，短语
                        # 被标点断开仍按全局词序列判定）→ 豁免；
                        # 短组合（'Play Button'/'Play Store' 漏翻回显）与
                        # 全词典词序列（'Play Settings' 漏翻回显）仍判失败
                        if (not word.islower()
                                and word.casefold() in _DISPLAY_WORDS_CASEFOLD):
                            j = i + 1
                            right_proper = []
                            while (j < len(semantic_words)
                                   and semantic_words[j][0].isupper()
                                   and semantic_words[j][1:].islower()):
                                right_proper.append(semantic_words[j])
                                j += 1
                            if (len(right_proper) >= 2 and any(
                                    w.casefold()
                                    not in _DISPLAY_WORDS_CASEFOLD
                                    for w in right_proper)):
                                continue
                        # 原文非词典小写词保留豁免（sdfsdfsdfsdfsdfsdf 开发者
                        # 乱串、playsub 命令、readme 文件名）：原文即含该
                        # 小写词、译文已含中文、词非功能词/UI 词典（the/
                        # button 残留仍是漏译证据）且有非普通词形态特征
                        # （噪音/命令参数/文件引用）→ 模型保留原文词是
                        # 合理行为（containment 实证；普通词 ram 等仍失败）
                        if (has_chinese
                                and word.islower()
                                and word.casefold() in source_terms_cf
                                and word.casefold() not in _ENGLISH_FUNCTION_WORDS
                                and word.casefold() not in _DISPLAY_WORDS_CASEFOLD
                                and word.casefold() not in _BUILTIN_UI_TERMS_CASEFOLD
                                and _kept_word_plausible(
                                    entry.original, word)):
                            continue
                        return True
        # 单个残留词：小写普通词或 UI 词典词 → 半翻失败；
        # 物理按键名（Escape/Enter/F1…）、大写/TitleCase 专名（Windows/CBS/Orbit）、
        # @用户名显示名（fie）、破折号后署名（yamur）、首行问候（Hello）、
        # rich-text 包裹词（lucd）→ 豁免
        semantic_words = _ENGLISH_WORD.findall(semantic_ascii)
        for i, word in enumerate(semantic_words):
            if word.casefold() in dehyphenated_variants:
                continue
            if word.casefold() in PHYSICAL_KEY_NAMES_CASEFOLD:
                continue
            if word.casefold() in display_names:
                continue
            if (word.casefold() in quote_words and has_chinese):
                continue
            if (word.casefold() in signature_words and has_chinese):
                continue
            if word.casefold() in greeting_words:
                continue
            if (word.casefold() in rich_words and has_chinese):
                continue
            if (word.casefold() in digit_adjacent_words
                    and word.casefold() in source_terms_cf):
                continue
            # 下划线连接标识符组成部分（用户名 Tim_Kulig_Free_Music）：
            # 保留是正确行为 → 豁免（eyeless-jack 实证）
            if word.casefold() in underscore_identifier_words:
                continue
            # 键位绑定后缀（':map'/':interact'——faerie 实证）：
            # 绑定名是引擎指令，译文保留正确 → 豁免
            if word.casefold() in keybind_suffix_words:
                continue
            # 原文 TitleCase 短语段/多语言段（'Pish Shop' 商店专名、
            # 'Mungkin suara itu…' 印尼语段——faerie 实证）：模型
            # 保留专名/外语段只译其余 → 豁免
            if word.casefold() in title_phrase_words:
                continue
            if word.casefold() in word_residue_exempt:
                continue
            if word.casefold() in translated_quote_proper:
                continue
            # 模型小写化专名：原文 TitleCase 词在译文小写残留
            # （Bossfight → bossfight）→ 专名保留不是漏翻
            if word.islower() and word.casefold() in lowercased_proper:
                continue
            # 驼峰技术缩写（VSync/MonoBehaviour）→ 界面标准术语，保留
            # 原文合理（vincent 'VSync: OFF' → 'VSync：关闭'）；形态要求
            # 首大写 + 内部混合大小写（全大写 SETTINGS/TitleCase Save
            # 仍按词典规则判定）且原文也含该词（防模型幻觉新词）
            if (is_camel_tech_abbreviation(word)
                    and word.casefold() in source_terms_cf):
                continue
            # UI 词典词/小写冠词夹在 TitleCase 专名词之间
            # （《Baldi's Fun New School Remastered》的 New、'Craftydelight the
            # Asset Store' 的 the）→ 专名短语的一部分，豁免；
            # 孤立词（'Save 游戏'、'按下 the button'、句首的 Open）仍判失败
            if word.islower() or word.casefold() in _DISPLAY_WORDS_CASEFOLD:
                left_title = (i > 0 and semantic_words[i - 1][0].isupper()
                              and semantic_words[i - 1][1:].islower())
                right_title = (i + 1 < len(semantic_words)
                               and semantic_words[i + 1][0].isupper()
                               and semantic_words[i + 1][1:].islower())
                if not (left_title and right_title):
                    # UI 词典词右侧连续 ≥2 个非词典 TitleCase 专名词 →
                    # 豁免（同短语分支：'Play Games Plugin' 的 Play）
                    if (not word.islower()
                            and word.casefold() in _DISPLAY_WORDS_CASEFOLD):
                        j = i + 1
                        right_proper = []
                        while (j < len(semantic_words)
                               and semantic_words[j][0].isupper()
                               and semantic_words[j][1:].islower()):
                            right_proper.append(semantic_words[j])
                            j += 1
                        if (len(right_proper) >= 2 and any(
                                w.casefold()
                                not in _DISPLAY_WORDS_CASEFOLD
                                for w in right_proper)):
                            continue
                    # 原文非词典小写词保留豁免（同短语分支：sdfsdf 乱串/
                    # readme/playsub 类——原文含该词+译文含中文+非功能词/
                    # UI 词典+非普通词形态 → 保留合理）
                    if (has_chinese
                            and word.islower()
                            and word.casefold() in source_terms_cf
                            and word.casefold() not in _ENGLISH_FUNCTION_WORDS
                            and word.casefold() not in _DISPLAY_WORDS_CASEFOLD
                            and word.casefold() not in _BUILTIN_UI_TERMS_CASEFOLD
                            and _kept_word_plausible(
                                entry.original, word)):
                        continue
                    return True
        return False

    @staticmethod
    def _is_chinese_ideograph(char: str) -> bool:
        value = ord(char)
        return (0x3400 <= value <= 0x9FFF
                or 0xF900 <= value <= 0xFAFF
                or 0x20000 <= value <= 0x2FA1F)

    @staticmethod
    def _mark_failed(entry: TextEntry, reason: str,
                     raw_output: str = "") -> None:
        entry.status = STATUS_FAILED
        entry.quality_reasons = (reason,)
        entry.meta = dict(entry.meta)
        entry.meta["quality_passed"] = False
        entry.meta["quality_reasons"] = [reason]
        # Q3：失败分类 + attempt 预算记账
        _record_failure_attempt(entry, reason)
        # P0-3：invalid_response 时模型返回的原始内容作为证据留存
        if raw_output:
            entry.meta["raw_output"] = raw_output

    def _mark_request_failed(self, entry: TextEntry, exc: Exception) -> None:
        self._mark_failed(entry, "request_error")
        self._failures_dirty = True
        secret = getattr(getattr(self.client, "config", None), "api_key", "")
        entry.meta["request_error_detail"] = json.dumps(
            sanitize_exception(exc, (secret,)), ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _copy_failure_state(source: TextEntry, target: TextEntry) -> None:
        target.translation = source.translation
        target.quality_reasons = source.quality_reasons
        target.meta = dict(target.meta)
        target.meta["quality_passed"] = False
        target.meta["quality_reasons"] = list(source.quality_reasons)
        if "request_error_detail" in source.meta:
            target.meta["request_error_detail"] = source.meta["request_error_detail"]
        if "raw_output" in source.meta:
            target.meta["raw_output"] = source.meta["raw_output"]

    def _record_usage(self, usage) -> None:
        with self._metrics_lock:
            self._requests += 1
            if usage is not None:
                self._input_tokens += max(0, int(getattr(usage, "prompt", 0)))
                self._output_tokens += max(0, int(getattr(usage, "completion", 0)))
