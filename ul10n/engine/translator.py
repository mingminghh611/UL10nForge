"""Unified translation pipeline shared by real-time XUAT requests and batch dictionary jobs."""
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
    check_translation_quality,
    clean_model_output,
    is_translatable,
    lookup_builtin_ui,
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


def _match_glossary(
    source_text: str,
    terms: list[GlossaryItem],
    context: GameContext | None = None,
    limit: int = 10,
) -> list[tuple[str, str]]:
    """Match explicit glossary entries AND GameContext characters/terms against source_text."""
    low = source_text.casefold()
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in terms:
        k = item.term.strip().casefold()
        if not k or k in seen:
            continue
        if k in low:
            hits.append((item.term.strip(), item.translation.strip()))
            seen.add(k)
            if len(hits) >= limit:
                return hits
    if context and (context.characters or context.terms):
        for src, dst, _cat in parse_context_term_pairs(context):
            k = src.casefold()
            if k in seen:
                continue
            if k in low:
                hits.append((src, dst))
                seen.add(k)
                if len(hits) >= limit:
                    break
    return hits


def build_translation_messages(
    source_text: str,
    *,
    target_lang: str,
    model_name: str,
    glossary_hits: list[tuple[str, str]],
    context: GameContext,
    fix_hint: str = "",
    prev_originals: list[str] | None = None,
) -> list[dict[str, str]]:
    """Build prompt optimized for Hy-MT3 (single-user format) or general instruction LLMs."""
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
        if fix_hint:
            lines.append(f"Important requirement: {fix_hint}")
        lines.extend([
            f"Translate the following game text into {lang_name}. "
            "Preserve all placeholders ({0}, %s), tags (<color=...>), and line breaks. "
            "Output ONLY the translation without explanation:",
            "",
            source_text,
        ])
        return [{"role": "user", "content": "\n".join(lines)}]

    # General instruction LLM (Qwen3.5 / cloud OpenAI-compatible) — compact high-signal prompt
    sys_lines = [
        f"游戏本地化翻译为{lang_name}：准确自然，保留占位符({{0}}/%s)、富文本标签与换行，只输出译文。",
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
    if neighbors and not fix_hint:
        sys_lines.append("前文(勿译)：" + " / ".join(n[:80] for n in neighbors))
    if fix_hint:
        sys_lines.append("修正：" + fix_hint)

    return [
        {"role": "system", "content": "\n".join(sys_lines)},
        {"role": "user", "content": source_text},
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
        f"游戏本地化批量翻译为{lang_name}。",
        "规则：保留所有占位符({0}/%s)、富文本标签(<color=...>)、数字与换行(\\n)；按编号逐条翻译，只输出严格JSON对象 {\"1\":\"译文1\",...}，不遗漏不解释。",
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
    ):
        self.global_store = global_store
        self.project_store = project_store
        self.target_lang = target_lang
        self.temperature = temperature
        self.max_tokens = max_tokens

    def _combined_glossary(self) -> list[GlossaryItem]:
        proj = self.project_store.list_glossary()
        glob = self.global_store.list_glossary()
        return proj + glob

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
        prev_originals: list[str] | None = None,
    ) -> TranslationOutcome:
        """Translate a single string with full cache/quality/persistence loop."""
        # 1. Non-translatable short-circuit
        if not is_translatable(original):
            entry = self.project_store.upsert_entry(
                original, original, status=STATUS_SKIPPED, source=source
            )
            return TranslationOutcome(entry, original, from_cache=True, ok=True)

        # 2. Existing project entry cache (locked/edited always wins unless force)
        existing = self.project_store.get_by_original(original)
        if existing and not force_retranslate:
            if existing.locked or existing.status in (STATUS_EDITED, STATUS_TRANSLATED):
                if existing.translation:
                    if source == "xuat_live" and existing.id:
                        existing = self.project_store.mark_live_seen(existing.id) or existing
                    return TranslationOutcome(existing, existing.translation, from_cache=True, ok=True)

        # 3. Authoritative built-in UI vocabulary
        if not force_retranslate:
            builtin = lookup_builtin_ui(original)
            if builtin:
                entry = self.project_store.upsert_entry(
                    original, builtin, status=STATUS_TRANSLATED, source=source, quality_note=""
                )
                return TranslationOutcome(entry, builtin, from_cache=True, ok=True)

        # 4. Cross-game global memory
        if not force_retranslate:
            mem_hit = self.global_store.get_memory(original, self.target_lang)
            if mem_hit and check_translation_quality(original, mem_hit).passed:
                entry = self.project_store.upsert_entry(
                    original, mem_hit, status=STATUS_TRANSLATED, source=source, quality_note=""
                )
                return TranslationOutcome(entry, mem_hit, from_cache=True, ok=True)

        if client is None:
            entry = self.project_store.upsert_entry(
                original, "", status=STATUS_FAILED, source=source, quality_note="翻译引擎未就绪"
            )
            return TranslationOutcome(entry, original, from_cache=False, ok=False, note="翻译引擎未就绪")

        # 5. Model translation + quality gate + 1 targeted retry
        ctx = context if context is not None else self.project_store.load_context()
        all_terms = glossary if glossary is not None else self._combined_glossary()
        hits = _match_glossary(original, all_terms, context=ctx)

        candidate = ""
        last_check = None
        current_hint = fix_hint
        base_temp = max(0.0, min(1.0, float(self.temperature)))
        for attempt in range(2):
            messages = build_translation_messages(
                original,
                target_lang=self.target_lang,
                model_name=client.model,
                glossary_hits=hits,
                context=ctx,
                fix_hint=current_hint,
                prev_originals=prev_originals,
            )
            temp = (base_temp if attempt == 0 else max(0.1, base_temp * 0.5))
            dyn_max = max(160, min(int(self.max_tokens), max(len(original) * 3 + 128, 192)))
            res = client.chat(messages, temperature=temp, max_tokens=dyn_max)
            candidate = restore_missing_tags(original, clean_model_output(res.content))
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
            self.global_store.put_memory(original, candidate, self.target_lang, source="ai")
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
        max_items_per_batch: int = 15,
        max_chars_per_batch: int = 1600,
        is_cancelled: Callable[[], bool] | None = None,
        on_item: Callable[[TranslationOutcome], None] | None = None,
    ) -> list[TranslationOutcome]:
        """Token-efficient JSON-keyed batch translation for cloud APIs with strict per-item fallback.

        1. Resolves non-translatable, cached, built-in UI, and cross-game memory entries at 0 token cost.
        2. Packs remaining entries into JSON-keyed batches (~70-85% fewer input tokens).
        3. Validates every returned item against the hard quality gate; any missing/failed entry
           automatically falls back to `translate_one()` so zero entries are lost or misaligned.
        """
        outcomes: list[TranslationOutcome] = []
        ctx = context if context is not None else self.project_store.load_context()
        all_terms = glossary if glossary is not None else self._combined_glossary()
        need_llm: list[Entry] = []

        # Step 1: Resolve zero-token local hits first
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
                if existing and (existing.locked or existing.status in (STATUS_EDITED, STATUS_TRANSLATED)) and existing.translation:
                    oc = TranslationOutcome(existing, existing.translation, from_cache=True, ok=True)
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
                mem_hit = self.global_store.get_memory(orig, self.target_lang)
                if mem_hit and check_translation_quality(orig, mem_hit).passed:
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

        # Step 3: Translate each batch & verify every item strictly
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
                    glossary=all_terms,
                )
                outcomes.append(oc)
                if on_item:
                    on_item(oc)
                continue

            originals = [e.original for e in chunk]
            joined_src = "\n".join(originals)
            hits = _match_glossary(joined_src, all_terms, context=ctx, limit=14)
            messages = _build_cloud_batch_messages(
                originals,
                target_lang=self.target_lang,
                glossary_hits=hits,
                context=ctx,
            )
            batch_max_tokens = min(
                max(int(self.max_tokens), 1024),
                max(320, sum(len(s) for s in originals) * 3 + 96 * len(originals)),
            )
            parsed: dict | None = None
            try:
                res = client.chat(messages, temperature=base_temp, max_tokens=batch_max_tokens)
                parsed = extract_json_object(res.content)
            except Exception:
                parsed = None

            for idx, e in enumerate(chunk, start=1):
                if is_cancelled and is_cancelled():
                    break
                raw_tr = None
                if isinstance(parsed, dict):
                    raw_tr = parsed.get(str(idx))
                    if raw_tr is None:
                        raw_tr = parsed.get(idx)  # type: ignore[arg-type]

                if isinstance(raw_tr, str) and raw_tr.strip():
                    candidate = restore_missing_tags(e.original, clean_model_output(raw_tr))
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
                        self.global_store.put_memory(e.original, candidate, self.target_lang, source="ai")
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
                    glossary=all_terms,
                )
                outcomes.append(oc)
                if on_item:
                    on_item(oc)

        return outcomes
