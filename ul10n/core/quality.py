"""Deterministic quality guard, placeholder/tag protection, word-boundary glossary matching, and UI terms."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

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
_JP_RE = re.compile(r"[\u3040-\u30ff]")
_KR_RE = re.compile(r"[\uac00-\ud7af]")
_RU_RE = re.compile(r"[\u0400-\u04ff]")
_JP_KR_RE = re.compile(r"[\u3040-\u30ff\uac00-\ud7af]")
_HAS_LATIN_RE = re.compile(r"[A-Za-z]")
_HAS_CJK_JP_KR_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_ZH_NUM_RE = re.compile(r"[零一二两三四五六七八九十百千万亿]")

# Placeholders & markup tags that MUST survive translation intact (XUAT {{A}} templates first!)
_PLACEHOLDER_RE = re.compile(
    r"(\{\{[0-9A-Za-z_]+\}\}|\{[0-9A-Za-z_.:,-]+\}|%[0-9$.+-]*[sdifuxX%]|\$\{[A-Za-z0-9_.]+\})"
)
_RICHTEXT_TAG_RE = re.compile(
    r"</?[A-Za-z][A-Za-z0-9_-]*(?:=[^<>]*|\s+[A-Za-z_][A-Za-z0-9_-]*=[^<>]*)?/?>"
)
_OUTER_WRAP_TAG_RE = re.compile(
    r"^\s*(<([A-Za-z][A-Za-z0-9_-]*)(?:=[^<>]*)?>)(.+?)(</\2>)\s*$",
    re.S | re.I,
)
_INLINE_SELF_TAG_RE = re.compile(
    r"<(?:sprite(?:\s+[^<>]*|=[^<>]*)?|br\s*/?|space=[^<>]*|voffset=[^<>]*|page)>",
    re.I,
)
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

    # 3. Untranslated echo check (for non-trivial Latin/Japanese/Korean/Cyrillic sentences)
    src_plain = _RICHTEXT_TAG_RE.sub("", _PLACEHOLDER_RE.sub("", src)).strip()
    tr_plain = _RICHTEXT_TAG_RE.sub("", _PLACEHOLDER_RE.sub("", tr)).strip()
    if len(src_plain) >= 6 and (
        _LATIN_WORD_RE.search(src_plain) or _JP_KR_RE.search(src_plain) or _RU_RE.search(src_plain)
    ):
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


# ── Word-Boundary Glossary Matcher (Zero False Positives + Sub-ms Cached Lookup) ──


def _compile_term_pattern(term: str) -> re.Pattern[str] | None:
    """Compile word-boundary regex for Latin/ASCII terms; return None for CJK/JP/KR substring terms."""
    t = (term or "").strip()
    if not t:
        return None
    if _HAS_LATIN_RE.search(t) and not _HAS_CJK_JP_KR_RE.search(t):
        # Match whole word + optional English plural/possessive suffix ('s / s / es)
        return re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(t)}(?:s|es|'s|’s)?(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
    return None


def term_in_text(term: str, text: str) -> bool:
    """Return True if `term` appears in `text` respecting word boundaries for Latin words."""
    t = (term or "").strip()
    if not t or not text:
        return False
    pat = _compile_term_pattern(t)
    if pat is not None:
        return bool(pat.search(text))
    return t.casefold() in text.casefold()


class GlossaryMatcher:
    """Pre-compiled, longest-match-first glossary index for ultra-fast live & batch translation."""

    __slots__ = ("_entries", "_exact_map", "_project_exact_map", "_project_entries")

    def __init__(
        self,
        project_pairs: Iterable[tuple[str, str]] = (),
        global_pairs: Iterable[tuple[str, str]] = (),
        context_pairs: Iterable[tuple[str, str]] = (),
    ) -> None:
        # Priority: project glossary > global glossary > context-extracted terms
        dedup: dict[str, tuple[str, str, bool]] = {}
        for is_proj, pairs in (
            (True, project_pairs),
            (False, global_pairs),
            (True, context_pairs),
        ):
            for raw_src, raw_dst in pairs:
                src = (raw_src or "").strip()
                dst = (raw_dst or "").strip()
                if not src or not dst:
                    continue
                key = src.casefold()
                if key not in dedup:
                    dedup[key] = (src, dst, is_proj)

        # Sort longest terms first so "The Keeper" matches before "Keeper"
        ordered = sorted(dedup.values(), key=lambda x: (-len(x[0]), x[0].casefold()))
        self._entries: list[tuple[str, str, str, re.Pattern[str] | None, bool]] = []
        self._project_entries: list[tuple[str, str, str, re.Pattern[str] | None]] = []
        self._exact_map: dict[str, str] = {}
        self._project_exact_map: dict[str, str] = {}

        for src, dst, is_proj in ordered:
            low = src.casefold()
            pat = _compile_term_pattern(src)
            self._entries.append((src, dst, low, pat, is_proj))
            self._exact_map[low] = dst
            if is_proj:
                self._project_entries.append((src, dst, low, pat))
                self._project_exact_map[low] = dst

    def exact_project_lookup(self, text: str) -> str | None:
        """Exact whole-string lookup in project glossary / context terms (highest priority)."""
        key = (text or "").strip().casefold()
        return self._project_exact_map.get(key)

    def has_project_hit(self, text: str) -> bool:
        """Return True if any project-specific term occurs in `text` (prevents global-memory hijack)."""
        if not self._project_entries or not text:
            return False
        low = text.casefold()
        for _src, _dst, t_low, pat in self._project_entries:
            if pat is not None:
                if t_low in low and pat.search(text):
                    return True
            elif t_low in low:
                return True
        return False

    def match(self, text: str, limit: int = 10) -> list[tuple[str, str]]:
        """Return up to `limit` matching `(source_term, target_translation)` pairs."""
        if not self._entries or not text:
            return []
        low = text.casefold()
        hits: list[tuple[str, str]] = []
        for src, dst, t_low, pat, _is_proj in self._entries:
            if t_low not in low:
                continue
            if pat is None or pat.search(text):
                hits.append((src, dst))
                if len(hits) >= limit:
                    break
        return hits


# ── Safe Tag Peel & Inline Sprite Freeze Masking ─────────────────────────


def protect_inline_markup(text: str) -> tuple[str, list[tuple[str, str]], list[tuple[str, str]]]:
    """Peel balanced outer wrapping tags and freeze self-closing TMP tags like `<sprite=0>`.

    Keeps the text sent to small/fast models clean so tags never get dropped or corrupted.
    """
    outer_tags: list[tuple[str, str]] = []
    core = text or ""

    # Peel up to 3 balanced outer wrapping tags (e.g. <b><color=#ff0>Alert</color></b>)
    for _ in range(3):
        m = _OUTER_WRAP_TAG_RE.match(core)
        if not m:
            break
        open_tag, tag_name, inner, close_tag = m.groups()
        # Only peel if the same tag name does NOT appear unclosed/re-opened inside `inner`
        if re.search(rf"</?{re.escape(tag_name)}(?:\b|=|>)", inner, re.I):
            break
        if not inner.strip():
            break
        outer_tags.append((open_tag, close_tag))
        core = inner

    frozen_tokens: list[tuple[str, str]] = []

    def _freeze(match: re.Match[str]) -> str:
        tok = f"⟦S{len(frozen_tokens)}⟧"
        frozen_tokens.append((tok, match.group(0)))
        return tok

    masked = _INLINE_SELF_TAG_RE.sub(_freeze, core)
    return masked, outer_tags, frozen_tokens


def restore_inline_markup(
    translated: str,
    outer_tags: list[tuple[str, str]],
    frozen_tokens: list[tuple[str, str]],
) -> str:
    """Restore frozen inline tags and re-wrap peeled outer tags deterministically."""
    out = (translated or "").strip()
    for tok, raw_tag in frozen_tokens:
        if tok in out:
            out = out.replace(tok, raw_tag)
    for open_tag, close_tag in reversed(outer_tags):
        if open_tag not in out and close_tag not in out:
            out = f"{open_tag}{out}{close_tag}"
    return out


# ── Quote Style Normalization, UI Length Cue, Language & Memory Safety ───


def apply_quote_style(text: str, style: str = "auto") -> str:
    """Unify Chinese dialogue quotation marks (`corner` -> 「…」, `curly` -> “…”) outside XML tags."""
    st = (style or "auto").strip().lower()
    if st not in ("corner", "curly") or not text:
        return text

    # Protect rich-text tags so attribute quotes like <color="red"> are never touched
    parts = _RICHTEXT_TAG_RE.split(text)
    tags = _RICHTEXT_TAG_RE.findall(text)
    out_parts: list[str] = []
    for seg in parts:
        if st == "corner":
            seg = seg.replace("“", "「").replace("”", "」")
            # Convert balanced straight quotes inside prose segments
            if seg.count('"') >= 2 and seg.count('"') % 2 == 0:
                buf: list[str] = []
                opened = False
                for ch in seg:
                    if ch == '"':
                        buf.append("」" if opened else "「")
                        opened = not opened
                    else:
                        buf.append(ch)
                seg = "".join(buf)
        elif st == "curly":
            seg = seg.replace("「", "“").replace("」", "”")
        out_parts.append(seg)

    merged: list[str] = []
    for idx, seg in enumerate(out_parts):
        merged.append(seg)
        if idx < len(tags):
            merged.append(tags[idx])
    return "".join(merged)


def is_short_ui_button(text: str) -> bool:
    """Return True when `text` looks like a compact 1–2 word UI button/tab label."""
    if not text or "\n" in text:
        return False
    plain = _RICHTEXT_TAG_RE.sub("", _PLACEHOLDER_RE.sub("", text)).strip()
    if not plain or len(plain) > 15:
        return False
    if any(p in plain for p in (".", "!", "?", "。", "！", "？", ",", "，")):
        return False
    words = plain.split()
    return 1 <= len(words) <= 2


def is_safe_for_global_memory(original: str, source: str = "ai") -> bool:
    """Guard cross-game translation memory against short ambiguous single-word pollution."""
    s = (original or "").strip()
    if not s:
        return False
    if source == "manual":
        return True
    # Standard UI terms already live in BUILTIN_UI_TERMS; avoid polluting global memory with
    # short 1-2 word ambiguous domain words like "Fire", "Light", "Warden", "Charge", "Draw".
    plain = _RICHTEXT_TAG_RE.sub("", _PLACEHOLDER_RE.sub("", s)).strip()
    if not plain:
        return False
    if _JP_KR_RE.search(plain):
        return len(plain) >= 5
    words = plain.split()
    if len(words) <= 2 and len(plain) < 16:
        return False
    return True


def detect_source_language(texts: Iterable[str]) -> str:
    """Detect dominant source language (`en` | `ja` | `ko` | `ru` | `auto`) from game texts."""
    jp = kr = ru = latin = 0
    sampled = 0
    for raw in texts:
        s = _RICHTEXT_TAG_RE.sub("", (raw or "")).strip()
        if not s:
            continue
        jp += len(_JP_RE.findall(s))
        kr += len(_KR_RE.findall(s))
        ru += len(_RU_RE.findall(s))
        latin += len(_LATIN_WORD_RE.findall(s))
        sampled += 1
        if sampled >= 120:
            break
    if jp >= 4:
        return "ja"
    if kr >= 4:
        return "ko"
    if ru >= 6 and ru > latin:
        return "ru"
    if latin >= 4:
        return "en"
    return "auto"
