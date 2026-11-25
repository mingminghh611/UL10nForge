"""Deterministic quality guard, placeholder/tag protection, and authoritative UI terms."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Authoritative UI single-word mappings (prevents cross-game pollution such as
# Play -> 播放, Resume -> 简历, Disabled -> 残疾人士)
BUILTIN_UI_TERMS: dict[str, str] = {
    "play": "开始",
    "start": "开始",
    "resume": "继续",
    "continue": "继续",
    "settings": "设置",
    "options": "选项",
    "quit": "退出",
    "exit": "退出",
    "back": "返回",
    "save": "保存",
    "load": "读取",
    "controls": "控制",
    "volume": "音量",
    "sfx": "音效",
    "resolution": "分辨率",
    "default": "默认",
    "disabled": "已禁用",
    "enabled": "已启用",
    "new game": "新游戏",
    "main menu": "主菜单",
    "press any key": "按任意键",
    "interact": "交互",
    "cancel": "取消",
    "confirm": "确认",
    "apply": "应用",
    "yes": "是",
    "no": "否",
}

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_JP_KR_RE = re.compile(r"[\u3040-\u30ff\uac00-\ud7af]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_ZH_NUM_RE = re.compile(r"[零一二两三四五六七八九十百千万亿]")

# Placeholders & markup tags that MUST survive translation intact (XUAT {{A}} templates first!)
_PLACEHOLDER_RE = re.compile(
    r"(\{\{[0-9A-Za-z_]+\}\}|\{[0-9A-Za-z_.:,-]+\}|%[0-9$.+-]*[sdifuxX%]|\$\{[A-Za-z0-9_.]+\})"
)
_RICHTEXT_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9_-]*(?:=[^<>]*)?>")
_ONLY_SYMBOL_NUM_RE = re.compile(r"^[\s0-9.,:;!?+\-*/=%$€£¥()\[\]{}<>|_~`#@&^'\"\\/]+$")
_VERSION_STRING_RE = re.compile(r"^(?:v|ver\.?|build)\s*\d+(?:\.\d+){1,3}[a-z0-9\-]*$", re.I)

# Numbers (supports European thousands like 200.000 and Chinese 万/亿 multipliers)
_NUM_RE = re.compile(r"(?<![A-Za-z0-9])(\d+(?:[.,]\d+)?)\s*(万|亿|million|billion|k|K)?")


def is_translatable(text: str) -> bool:
    """Return False for empty strings, pure numbers/punctuation, version tags, or already-Chinese text."""
    s = (text or "").strip()
    if not s:
        return False
    if _ONLY_SYMBOL_NUM_RE.match(s) or _VERSION_STRING_RE.match(s):
        return False
    # Japanese (Hiragana/Katakana) and Korean (Hangul) are always translatable even with Kanji
    if _JP_KR_RE.search(s):
        return True
    # If already predominantly Chinese without significant non-CJK letters, skip
    cjk_count = len(_CJK_RE.findall(s))
    if cjk_count >= 2 and not _LATIN_WORD_RE.search(_RICHTEXT_TAG_RE.sub("", s)):
        return False
    return True


def lookup_builtin_ui(text: str) -> str | None:
    """Return authoritative Chinese translation for exact standard UI labels."""
    key = (text or "").strip().casefold()
    return BUILTIN_UI_TERMS.get(key)


def extract_placeholders(text: str) -> list[str]:
    return _PLACEHOLDER_RE.findall(text or "")


def extract_tags(text: str) -> list[str]:
    return _RICHTEXT_TAG_RE.findall(text or "")


def _parse_numbers(text: str) -> list[float]:
    cleaned = _RICHTEXT_TAG_RE.sub(" ", text or "")
    cleaned = _PLACEHOLDER_RE.sub(" ", cleaned)
    vals: list[float] = []
    for num_str, mult in _NUM_RE.findall(cleaned):
        # European thousands separator heuristic: 200.000 -> 200000
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", num_str):
            raw = float(num_str.replace(".", ""))
        elif "," in num_str and "." not in num_str and re.fullmatch(r"\d{1,3}(?:,\d{3})+", num_str):
            raw = float(num_str.replace(",", ""))
        else:
            try:
                raw = float(num_str.replace(",", "."))
            except ValueError:
                continue
        factor = {
            "万": 1e4,
            "亿": 1e8,
            "million": 1e6,
            "billion": 1e9,
            "k": 1e3,
            "K": 1e3,
        }.get(mult or "", 1.0)
        vals.append(raw * factor)
    return vals


@dataclass(frozen=True)
class QualityCheckResult:
    passed: bool
    reasons: tuple[str, ...] = ()

    @property
    def note(self) -> str:
        return "; ".join(self.reasons)


def check_translation_quality(original: str, translation: str) -> QualityCheckResult:
    """Run fast deterministic quality checks on a translation candidate."""
    reasons: list[str] = []
    src = (original or "").strip()
    tr = (translation or "").strip()

    if not tr:
        return QualityCheckResult(False, ("译文为空",))

    # 1. Placeholder preservation
    src_ph = extract_placeholders(src)
    if src_ph:
        tr_ph = extract_placeholders(tr)
        for ph in src_ph:
            if tr_ph.count(ph) < src_ph.count(ph):
                reasons.append(f"占位符缺失: {ph}")
                break

    # 2. Rich-text tag count & open/close pairing parity
    src_tags = extract_tags(src)
    if src_tags:
        tr_tags = extract_tags(tr)
        if len(tr_tags) < len(src_tags):
            reasons.append("富文本标签丢失")
        else:
            src_close = sum(1 for t in src_tags if t.startswith("</"))
            tr_close = sum(1 for t in tr_tags if t.startswith("</"))
            if src_close > 0 and tr_close < src_close:
                reasons.append("富文本闭合标签损坏")

    # 3. Untranslated echo check (for non-trivial Latin/Japanese/Korean sentences)
    src_plain = _RICHTEXT_TAG_RE.sub("", _PLACEHOLDER_RE.sub("", src)).strip()
    tr_plain = _RICHTEXT_TAG_RE.sub("", _PLACEHOLDER_RE.sub("", tr)).strip()
    if len(src_plain) >= 6 and (_LATIN_WORD_RE.search(src_plain) or _JP_KR_RE.search(src_plain)):
        if tr_plain.casefold() == src_plain.casefold():
            reasons.append("疑似未翻译（原文回显）")
        elif not _CJK_RE.search(tr_plain) and len(_LATIN_WORD_RE.findall(src_plain)) >= 2:
            reasons.append("译文缺少中文字符")

    # 4. Suspicious prompt echo / hallucination markers
    if any(marker in tr for marker in ("Translate the following", "翻译结果：", "译文：", "[来源文件]")):
        reasons.append("疑似包含提示词回显")
    if " || " in tr and "||" not in src:
        reasons.append("译文包含异常分隔符 ||")

    # 5. Severe length runaway (model hallucinating/looping)
    if len(src) >= 10 and len(tr) > max(len(src) * 4, 240):
        reasons.append("译文异常过长（疑似循环续写）")

    # 6. Numeric sanity check (also catches when ALL numbers vanished from translation!)
    src_nums = _parse_numbers(src)
    if src_nums and len(src_nums) <= 4:
        tr_nums = _parse_numbers(tr)
        has_zh_num = bool(_ZH_NUM_RE.search(tr_plain))
        for num in src_nums:
            if num > 10:
                if tr_nums:
                    if not any(abs(num - tn) <= max(1e-6, abs(num) * 0.01) for tn in tr_nums):
                        reasons.append(f"数值不一致: {int(num) if num.is_integer() else num}")
                        break
                elif not has_zh_num:
                    reasons.append(f"数值丢失: {int(num) if num.is_integer() else num}")
                    break

    return QualityCheckResult(not reasons, tuple(reasons))


def clean_model_output(text: str) -> str:
    """Strip common model wrapper noise (think blocks, quotes, markdown fences, prefixes, || echoes)."""
    s = re.sub(r"<think>.*?</think>", "", (text or ""), flags=re.S | re.I).strip()
    if s.startswith("```"):
        s = re.sub(r"^```[A-Za-z0-9_-]*\s*|\s*```$", "", s).strip()
    for prefix in ("译文：", "译文:", "翻译：", "翻译:", "Translation:", "Output:"):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "“" and s[-1] == "”")):
        inner = s[1:-1].strip()
        if inner and '"' not in inner:
            s = inner
    # Collapse echoed "A || A" repetition emitted occasionally by small models
    if " || " in s:
        parts = [p.strip() for p in s.split(" || ") if p.strip()]
        if parts and len(set(parts)) == 1:
            s = parts[0]
    return s


def restore_missing_tags(original: str, translation: str) -> str:
    """Light mechanical self-heal for outer wrapping rich-text tags (e.g. <color=...>text</color>)."""
    m = re.fullmatch(r"(<[A-Za-z][A-Za-z0-9_-]*(?:=[^<>]*)?>)(.+?)(</[A-Za-z][A-Za-z0-9_-]*>)", original.strip(), re.S)
    if m:
        open_tag, _, close_tag = m.groups()
        if open_tag not in translation and close_tag not in translation:
            return f"{open_tag}{translation.strip()}{close_tag}"
    return translation
