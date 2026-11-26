"""Unified translation pipeline shared by real-time XUAT requests and batch dictionary jobs.

Key optimizations:
- Pre-compiled word-boundary `GlossaryMatcher` (zero false positives on short Latin words, <0.1ms live).
- Project glossary priority over built-ins & cross-game memory (prevents cross-game semantic hijack).
- KV-Cache / Prompt-Cache friendly prompt split (game-invariant `system`, per-line dynamic `user`).
- Safe outer rich-text tag peel & inline `<sprite=...>` freeze masking (slashes retry latency).
- XUAT `{{A}}` dynamic-number guidance, compact UI button length cues, and quote-style normalization.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from ul10n.core.db import GlobalStore, ProjectStore
from ul10n.core.models import (
    Entry,
    GameContext,
    GlossaryItem,
    STATUS_EDITED,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_TRANSLATED,
)
from ul10n.core.quality import (
    GlossaryMatcher,
    apply_quote_style,
    check_translation_quality,
    clean_model_output,
    is_safe_for_global_memory,
    is_short_ui_button,
    is_translatable,
    lookup_builtin_ui,
    protect_inline_markup,
    restore_inline_markup,
    restore_missing_tags,
)
from ul10n.engine.context_builder import parse_context_term_pairs
from ul10n.engine.llm_client import LLMClient, extract_json_object, is_hy_mt_model

_TARGET_LANG_NAMES = {
    "zh-cn": "Simplified Chinese",
    "zh": "Simplified Chinese",
    "zh-tw": "Traditional Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
}


@dataclass(frozen=True)
class TranslationOutcome:
    entry: Entry
    translation: str
    from_cache: bool
    ok: bool
    note: str = ""


def build_glossary_matcher(
    project_terms: list[GlossaryItem],
    global_terms: list[GlossaryItem] | None = None,
    context: GameContext | None = None,
) -> GlossaryMatcher:
    """Compile a fast word-boundary GlossaryMatcher with strict project > global > context priority."""
    proj_pairs = [(g.term, g.translation) for g in (project_terms or [])]
    glob_pairs = [(g.term, g.translation) for g in (global_terms or [])]
    ctx_pairs = (
        [(src, dst) for src, dst, _cat in parse_context_term_pairs(context)]
        if context and (context.characters or context.terms)
        else []
    )
    return GlossaryMatcher(
        project_pairs=proj_pairs,
        global_pairs=glob_pairs,
        context_pairs=ctx_pairs,
    )


def _match_glossary(
    source_text: str,
    terms: list[GlossaryItem],
    context: GameContext | None = None,
    limit: int = 10,
    matcher: GlossaryMatcher | None = None,
) -> list[tuple[str, str]]:
    """Match explicit glossary entries AND GameContext characters/terms with word-boundary safety."""
    if matcher is not None:
        return matcher.match(source_text, limit=limit)
    compiled = build_glossary_matcher(terms, [], context)
    return compiled.match(source_text, limit=limit)


def build_translation_messages(
    source_text: str,
    *,
    target_lang: str,
    model_name: str,
    glossary_hits: list[tuple[str, str]],
    context: GameContext,
    fix_hint: str = "",
    prev_originals: list[str] | None = None,
    short_ui: bool = False,
) -> list[dict[str, str]]:
    """Build KV-cache-friendly prompts for Hy-MT3 or general instruction LLMs."""
    lang_name = _TARGET_LANG_NAMES.get(target_lang.casefold(), "Simplified Chinese")
    neighbors = [s.strip().replace("\n", " ")[:120] for s in (prev_originals or []) if s and s.strip()][-2:]

    if is_hy_mt_model(model_name):
        lines: list[str] = []
        if context.is_ready:
            ctx_parts = [p for p in (context.genre, context.setting, context.style) if p]
            if ctx_parts:
                lines.append(f"Game context: {' / '.join(ctx_parts)}")
            if context.notes:
                lines.append("Notes: " + "; ".join(context.notes[:2]))
        if glossary_hits:
            lines.append("Reference the following translations:")
            for src, dst in glossary_hits:
                lines.append(f"{src} translates to {dst}")
            lines.append("")
        if neighbors and not fix_hint:
            lines.append("Adjacent lines for context only (do NOT translate them): " + " | ".join(neighbors))
        if short_ui and not fix_hint:
            lines.append("Style: concise UI button/label (2-4 Chinese characters preferred).")
        if fix_hint:
            lines.append(f"Important requirement: {fix_hint}")
        lines.extend([
            f"Translate the following game text into {lang_name}. "
            "Preserve all placeholders ({0}, {{A}}, %s), tokens (⟦S0⟧), tags (<color=...>), and line breaks. "
            "If {{A}}/{{B}} appear, they represent runtime numbers; keep them intact with natural Chinese measure words. "
            "Output ONLY the translation without explanation:",
            "",
            source_text,
        ])
        return [{"role": "user", "content": "\n".join(lines)}]

    # General instruction LLM (Qwen3.5 / DeepSeek / OpenAI / Gemini):
    # Keep `system` 100% invariant per game so llama-server KV cache & cloud Prompt Caching stay hot!
    sys_lines = [
        f"你是中文游戏本地化译者，将游戏文本自然准确地译为{lang_name}：贴合语境与口吻，杜绝机翻腔；按钮菜单简洁干练；"
        "严格保留占位符({0}/{{A}}/%s)、标记(⟦S0⟧)、富文本标签(<color=...>)、按键名与换行；"
        "{{A}}/{{B}} 代表运行时动态数字，需原样保留并搭配自然中文量词；只输出译文不解释。",
    ]
    if context.is_ready:
        ctx_bits = [
            context.genre,
            context.setting,
            context.style,
            (context.summary[:60] if context.summary else ""),
        ]
        ctx_str = " / ".join(b for b in ctx_bits if b)
        if ctx_str:
            sys_lines.append("语境：" + ctx_str)
        if context.notes:
            sys_lines.append("要点：" + "；".join(n[:40] for n in context.notes[:2]))

    # Dynamic per-sentence hints go in `user` BEFORE `source_text` (source_text always stays on last line)
    user_parts: list[str] = []
    if glossary_hits:
        user_parts.append("术语：" + "，".join(f"{s}={t}" for s, t in glossary_hits))
    if neighbors and not fix_hint:
        user_parts.append("前文(勿译)：" + " / ".join(n[:80] for n in neighbors))
    if short_ui and not fix_hint:
        user_parts.append("要求：界面短词，尽量精炼为2~4字防溢出。")
    if fix_hint:
        user_parts.append("修正：" + fix_hint)
    if user_parts:
        user_content = "\n".join(user_parts) + "\n\n" + source_text
    else:
        user_content = source_text

    return [
        {"role": "system", "content": "\n".join(sys_lines)},
        {"role": "user", "content": user_content},
    ]


def _build_cloud_batch_messages(
    originals: list[str],
    *,
    target_lang: str,
    glossary_hits: list[tuple[str, str]],
    context: GameContext,
) -> list[dict[str, str]]:
    """Compact JSON-keyed batch prompt saving 70-85% cloud input tokens with strict ID alignment."""
    lang_name = _TARGET_LANG_NAMES.get(target_lang.casefold(), "Simplified Chinese")
    sys_lines = [
        f"你是中文游戏本地化译者，将下列游戏文本自然准确地批量译为{lang_name}（避免机翻腔，短按钮尽量2~4字精炼）。",
        "规则：严格保留占位符({0}/{{A}}/%s)、标记(⟦S0⟧)、富文本标签(<color=...>)、按键名、数字与换行(\\n)；"
        "{{A}}/{{B}}代表运行时数字请配自然量词；按编号逐条翻译，只输出严格JSON对象 {\"1\":\"译文1\",...}，不遗漏不解释。",
    ]
    if context.is_ready:
        ctx_bits = [
            context.genre,
            context.setting,
            context.style,
            (context.summary[:60] if context.summary else ""),
        ]
        ctx_str = " / ".join(b for b in ctx_bits if b)
        if ctx_str:
            sys_lines.append("语境：" + ctx_str)
        if context.notes:
            sys_lines.append("要点：" + "；".join(n[:40] for n in context.notes[:2]))
    if glossary_hits:
        sys_lines.append("术语：" + "，".join(f"{s}={t}" for s, t in glossary_hits))

    payload = {str(i): text for i, text in enumerate(originals, start=1)}
    return [
        {"role": "system", "content": "\n".join(sys_lines)},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


class TranslationPipeline:
    """Translates single entries (live XUAT) or batches with memory, glossary, and quality gate."""

    def __init__(
        self,
        global_store: GlobalStore,
        project_store: ProjectStore,
        *,
        target_lang: str = "zh-CN",
        temperature: float = 0.2,
        max_tokens: int = 1024,
        quote_style: str = "auto",
    ):
        self.global_store = global_store
        self.project_store = project_store
        self.target_lang = target_lang
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.quote_style = quote_style or "auto"

    def _combined_glossary(self) -> list[GlossaryItem]:
        proj = self.project_store.list_glossary()
        glob = self.global_store.list_glossary()
        return proj + glob

    def _resolve_matcher(
        self,
        context: GameContext,
        glossary: list[GlossaryItem] | None,
        glossary_matcher: GlossaryMatcher | None,
    ) -> GlossaryMatcher:
        if glossary_matcher is not None:
            return glossary_matcher
        if glossary is not None:
            return build_glossary_matcher(glossary, [], context)
        return build_glossary_matcher(
            self.project_store.list_glossary(),
            self.global_store.list_glossary(),
            context,
        )

    def _try_safe_memory_hit(self, original: str, matcher: GlossaryMatcher) -> str | None:
        """Return cross-game memory hit ONLY when safe (no project glossary conflict or ambiguous 1-word risk)."""
        if matcher.has_project_hit(original):
            return None
        mem_entry = self.global_store.get_memory_entry(original, self.target_lang)
        if not mem_entry:
            return None
        mem_tr, mem_src = mem_entry
        if mem_src != "manual" and not is_safe_for_global_memory(original, "ai"):
            return None
        if not check_translation_quality(original, mem_tr).passed:
            return None
        return apply_quote_style(mem_tr, self.quote_style)

    def _maybe_store_memory(self, original: str, translation: str, matcher: GlossaryMatcher) -> None:
        """Persist to cross-game memory ONLY when context-independent and safe against semantic drift."""
        if not is_safe_for_global_memory(original, "ai"):
            return
        if matcher.has_project_hit(original):
            return
        self.global_store.put_memory(original, translation, self.target_lang, source="ai")

    def translate_one(
        self,
        original: str,
        client: LLMClient | None,
        *,
        source: str = "xuat_live",
        force_retranslate: bool = False,
        fix_hint: str = "",
        context: GameContext | None = None,
        glossary: list[GlossaryItem] | None = None,
        glossary_matcher: GlossaryMatcher | None = None,
        prev_originals: list[str] | None = None,
    ) -> TranslationOutcome:
        """Translate a single string with full cache/glossary/quality/persistence loop."""
        # 1. Non-translatable short-circuit
        if not is_translatable(original):
            entry = self.project_store.upsert_entry(
                original, original, status=STATUS_SKIPPED, source=source
            )
            return TranslationOutcome(entry, original, from_cache=True, ok=True)

        # 2. Existing project entry cache (skipped / locked / edited / translated always wins unless force)
        existing = self.project_store.get_by_original(original)
        if existing and not force_retranslate:
            if existing.status == STATUS_SKIPPED:
                return TranslationOutcome(existing, original, from_cache=True, ok=True)
            if existing.locked or existing.status in (STATUS_EDITED, STATUS_TRANSLATED):
                if existing.translation:
                    if source == "xuat_live" and existing.id:
                        existing = self.project_store.mark_live_seen(existing.id) or existing
                    return TranslationOutcome(existing, existing.translation, from_cache=True, ok=True)

        ctx = context if context is not None else self.project_store.load_context()
        matcher = self._resolve_matcher(ctx, glossary, glossary_matcher)

        # 3. Exact project glossary match (beats built-ins and cross-game memory!)
        if not force_retranslate:
            exact_term = matcher.exact_project_lookup(original)
            if exact_term:
                entry = self.project_store.upsert_entry(
                    original, exact_term, status=STATUS_TRANSLATED, source=source, quality_note=""
                )
                return TranslationOutcome(entry, exact_term, from_cache=True, ok=True)

        # 4. Authoritative built-in UI vocabulary
        if not force_retranslate:
            builtin = lookup_builtin_ui(original)
            if builtin:
                entry = self.project_store.upsert_entry(
                    original, builtin, status=STATUS_TRANSLATED, source=source, quality_note=""
                )
                return TranslationOutcome(entry, builtin, from_cache=True, ok=True)

        # 5. Context-safe cross-game global memory
        if not force_retranslate:
            mem_hit = self._try_safe_memory_hit(original, matcher)
            if mem_hit:
                entry = self.project_store.upsert_entry(
                    original, mem_hit, status=STATUS_TRANSLATED, source=source, quality_note=""
                )
                return TranslationOutcome(entry, mem_hit, from_cache=True, ok=True)

        if client is None:
            entry = self.project_store.upsert_entry(
                original, "", status=STATUS_FAILED, source=source, quality_note="翻译引擎未就绪"
            )
            return TranslationOutcome(entry, original, from_cache=False, ok=False, note="翻译引擎未就绪")

        # 6. Model translation with safe tag peel/freeze + quality gate + 1 targeted retry
        hits = matcher.match(original, limit=10)
        masked_src, outer_tags, frozen_tokens = protect_inline_markup(original)
        short_ui = is_short_ui_button(original)

        candidate = ""
        last_check = None
        current_hint = fix_hint
        base_temp = max(0.0, min(1.0, float(self.temperature)))
        for attempt in range(2):
            messages = build_translation_messages(
                masked_src,
                target_lang=self.target_lang,
                model_name=client.model,
                glossary_hits=hits,
                context=ctx,
                fix_hint=current_hint,
                prev_originals=prev_originals,
                short_ui=short_ui,
            )
            temp = base_temp if attempt == 0 else max(0.1, base_temp * 0.5)
            dyn_max = max(160, min(int(self.max_tokens), max(len(masked_src) * 3 + 128, 192)))
            res = client.chat(messages, temperature=temp, max_tokens=dyn_max)
            cleaned = clean_model_output(res.content)
            unmasked = restore_inline_markup(cleaned, outer_tags, frozen_tokens)
            candidate = apply_quote_style(restore_missing_tags(original, unmasked), self.quote_style)
            last_check = check_translation_quality(original, candidate)
            if last_check.passed:
                break
            current_hint = f"上次译文存在问题（{last_check.note}），请严格修正并只输出准确中文译文。"

        if last_check and last_check.passed:
            entry = self.project_store.upsert_entry(
                original,
                candidate,
                status=STATUS_TRANSLATED,
                source=source,
                quality_note="",
                review_level="" if force_retranslate else None,
                review_reason="" if force_retranslate else None,
                preserve_locked=not force_retranslate,
            )
            self._maybe_store_memory(original, candidate, matcher)
            return TranslationOutcome(entry, candidate, from_cache=False, ok=True)

        note = last_check.note if last_check else "翻译未通过质量检查"
        entry = self.project_store.upsert_entry(
            original,
            candidate,
            status=STATUS_FAILED,
            source=source,
            quality_note=note,
            preserve_locked=not force_retranslate,
        )
        return TranslationOutcome(entry, candidate or original, from_cache=False, ok=False, note=note)

    def translate_batch_cloud(
        self,
        entries: list[Entry],
        client: LLMClient,
        *,
        force_retranslate: bool = False,
        context: GameContext | None = None,
        glossary: list[GlossaryItem] | None = None,
        glossary_matcher: GlossaryMatcher | None = None,
        max_items_per_batch: int = 15,
        max_chars_per_batch: int = 1600,
        is_cancelled: Callable[[], bool] | None = None,
        on_item: Callable[[TranslationOutcome], None] | None = None,
    ) -> list[TranslationOutcome]:
        """Token-efficient JSON-keyed batch translation for cloud APIs with strict per-item fallback."""
        outcomes: list[TranslationOutcome] = []
        ctx = context if context is not None else self.project_store.load_context()
        matcher = self._resolve_matcher(ctx, glossary, glossary_matcher)
        need_llm: list[Entry] = []

        # Step 1: Resolve zero-token local hits first (respecting project glossary priority)
        for entry in entries:
            if is_cancelled and is_cancelled():
                return outcomes
            orig = entry.original
            if not is_translatable(orig):
                upd = self.project_store.upsert_entry(orig, orig, status=STATUS_SKIPPED, source=entry.source)
                oc = TranslationOutcome(upd, orig, from_cache=True, ok=True)
                outcomes.append(oc)
                if on_item:
                    on_item(oc)
                continue
            if not force_retranslate:
                existing = self.project_store.get_by_original(orig)
                if existing and existing.status == STATUS_SKIPPED:
                    oc = TranslationOutcome(existing, orig, from_cache=True, ok=True)
                    outcomes.append(oc)
                    if on_item:
                        on_item(oc)
                    continue
                if existing and (existing.locked or existing.status in (STATUS_EDITED, STATUS_TRANSLATED)) and existing.translation:
                    oc = TranslationOutcome(existing, existing.translation, from_cache=True, ok=True)
                    outcomes.append(oc)
                    if on_item:
                        on_item(oc)
                    continue
                exact_term = matcher.exact_project_lookup(orig)
                if exact_term:
                    upd = self.project_store.upsert_entry(orig, exact_term, status=STATUS_TRANSLATED, source=entry.source, quality_note="")
                    oc = TranslationOutcome(upd, exact_term, from_cache=True, ok=True)
                    outcomes.append(oc)
                    if on_item:
                        on_item(oc)
                    continue
                builtin = lookup_builtin_ui(orig)
                if builtin:
                    upd = self.project_store.upsert_entry(orig, builtin, status=STATUS_TRANSLATED, source=entry.source, quality_note="")
                    oc = TranslationOutcome(upd, builtin, from_cache=True, ok=True)
                    outcomes.append(oc)
                    if on_item:
                        on_item(oc)
                    continue
                mem_hit = self._try_safe_memory_hit(orig, matcher)
                if mem_hit:
                    upd = self.project_store.upsert_entry(orig, mem_hit, status=STATUS_TRANSLATED, source=entry.source, quality_note="")
                    oc = TranslationOutcome(upd, mem_hit, from_cache=True, ok=True)
                    outcomes.append(oc)
                    if on_item:
                        on_item(oc)
                    continue
            need_llm.append(entry)

        # Step 2: Pack remaining entries into character- & count-bounded batches
        chunks: list[list[Entry]] = []
        cur: list[Entry] = []
        cur_chars = 0
        for e in need_llm:
            elen = len(e.original)
            if cur and (len(cur) >= max_items_per_batch or cur_chars + elen > max_chars_per_batch):
                chunks.append(cur)
                cur = []
                cur_chars = 0
            cur.append(e)
            cur_chars += elen
        if cur:
            chunks.append(cur)

        base_temp = max(0.0, min(1.0, float(self.temperature)))

        # Step 3: Translate each batch with tag masking & verify every item strictly
        for chunk in chunks:
            if is_cancelled and is_cancelled():
                break
            if len(chunk) == 1:
                oc = self.translate_one(
                    chunk[0].original,
                    client,
                    source=chunk[0].source,
                    force_retranslate=force_retranslate,
                    context=ctx,
                    glossary=glossary,
                    glossary_matcher=matcher,
                )
                outcomes.append(oc)
                if on_item:
                    on_item(oc)
                continue

            masked_pack = [protect_inline_markup(e.original) for e in chunk]
            masked_originals = [m[0] for m in masked_pack]
            joined_src = "\n".join(e.original for e in chunk)
            hits = matcher.match(joined_src, limit=14)
            messages = _build_cloud_batch_messages(
                masked_originals,
                target_lang=self.target_lang,
                glossary_hits=hits,
                context=ctx,
            )
            batch_max_tokens = min(
                max(int(self.max_tokens), 1024),
                max(320, sum(len(s) for s in masked_originals) * 3 + 96 * len(masked_originals)),
            )
            parsed: dict | None = None
            try:
                res = client.chat(messages, temperature=base_temp, max_tokens=batch_max_tokens)
                parsed = extract_json_object(res.content)
            except Exception:
                parsed = None

            for idx, (e, (_msrc, outer_tags, frozen_tokens)) in enumerate(zip(chunk, masked_pack), start=1):
                if is_cancelled and is_cancelled():
                    break
                raw_tr = None
                if isinstance(parsed, dict):
                    raw_tr = parsed.get(str(idx))
                    if raw_tr is None:
                        raw_tr = parsed.get(idx)  # type: ignore[arg-type]

                if isinstance(raw_tr, str) and raw_tr.strip():
                    unmasked = restore_inline_markup(clean_model_output(raw_tr), outer_tags, frozen_tokens)
                    candidate = apply_quote_style(restore_missing_tags(e.original, unmasked), self.quote_style)
                    if check_translation_quality(e.original, candidate).passed:
                        upd = self.project_store.upsert_entry(
                            e.original,
                            candidate,
                            status=STATUS_TRANSLATED,
                            source=e.source,
                            quality_note="",
                            review_level="" if force_retranslate else None,
                            review_reason="" if force_retranslate else None,
                            preserve_locked=not force_retranslate,
                        )
                        self._maybe_store_memory(e.original, candidate, matcher)
                        oc = TranslationOutcome(upd, candidate, from_cache=False, ok=True)
                        outcomes.append(oc)
                        if on_item:
                            on_item(oc)
                        continue

                # Strict per-item fallback guarantees zero dropped or corrupted entries
                oc = self.translate_one(
                    e.original,
                    client,
                    source=e.source,
                    force_retranslate=force_retranslate,
                    context=ctx,
                    glossary=glossary,
                    glossary_matcher=matcher,
                )
                outcomes.append(oc)
                if on_item:
                    on_item(oc)

        return outcomes
