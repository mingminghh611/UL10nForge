"""4-level semantic reviewer with mandatory issue-driven retranslation and direct auto-fix.

Flow for every reviewed entry:
1. Reasoning model evaluates `(original, translation)` -> `(level, reason, suggestion)`.
2. If `PASS`: accept immediately.
3. If `MINOR` / `MAJOR` / `CRITICAL`:
   a. First ask the translation model to retranslate with explicit review issue + requirements.
   b. Validate the retranslation with the hard quality gate (and re-review for MAJOR/CRITICAL).
   c. If the translation model still doesn't fix it, directly auto-apply the reasoning model's
      cleaned `suggestion` as long as it passes the hard quality gate (preserving placeholders/tags).
   d. Only flag (`STATUS_FLAGGED`) for human attention if both auto-fix paths fail.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ul10n.core.models import Entry, GameContext, GlossaryItem, STATUS_FLAGGED, STATUS_TRANSLATED
from ul10n.core.quality import (
    check_translation_quality,
    clean_model_output,
    extract_placeholders,
    extract_tags,
    is_translatable,
    lookup_builtin_ui,
    restore_missing_tags,
    term_in_text,
)
from ul10n.engine.context_builder import parse_context_term_pairs
from ul10n.engine.llm_client import LLMClient, extract_json_array, extract_json_object
from ul10n.engine.translator import TranslationPipeline

_NEGATION_COND_RE = re.compile(
    r"\b(?:not|no|never|don't|doesn't|didn't|won't|can't|cannot|shouldn't|wouldn't|couldn't|"
    r"without|unless|neither|nor|none|nothing|nobody|nowhere|forbid|avoid|stop)\b",
    re.IGNORECASE,
)
_TRIVIAL_SHORT_RE = re.compile(
    r"^(?:\d{1,2}:\d{2}\s*(?:AM|PM)?|[+-]?\d+\s*(?:pts|xp|hp|mp|fps|ms|m|s|%|km|kg)|"
    r"(?:wave|floor|level|stage|day|act|chapter|slot|part)\s*#?\d+|"
    r"\d+m\s*\d+s)$",
    re.IGNORECASE,
)


def is_high_risk_for_review(entry: Entry, glossary: list[GlossaryItem]) -> bool:
    """Smart triage: return True only when an entry genuinely benefits from 4B semantic review.

    Skips trivial UI terms, time/numeric tags, and clean 1–4 word labels with no risk signals,
    focusing LLM review strictly on dialogues, negations, rich tags, glossary conflicts, and anomalies.
    """
    orig = (entry.original or "").strip()
    tr = (entry.translation or "").strip()
    if not orig or not tr:
        return False
    if entry.status == STATUS_FLAGGED:
        return True
    if _find_glossary_violations(entry, glossary):
        return True
    if not is_translatable(orig) or _TRIVIAL_SHORT_RE.match(orig):
        return False
    builtin = lookup_builtin_ui(orig)
    if builtin and tr == builtin:
        return False

    # High-risk triggers
    if "\n" in orig or len(orig) >= 28 or len(orig.split()) >= 6:
        return True
    if _NEGATION_COND_RE.search(orig):
        return True
    if extract_tags(orig) or extract_placeholders(orig):
        return True
    # Length ratio anomaly (suspected omission or verbose hallucination)
    if len(orig) >= 12 and (len(tr) <= 2 or len(tr) > len(orig) * 2.2):
        return True
    return False

_REVIEW_SYSTEM = (
    "你是游戏本地化资深审校员。请核对原文与中文译文的语义准确性、否定逻辑、人称关系、"
    "游戏术语、自然度与占位符/富文本标签完整性。\n"
    "分级标准：\n"
    "- PASS：准确自然，无需修改\n"
    "- MINOR：语义大致正确但措辞生硬、不够地道或有小瑕疵，应改进\n"
    "- MAJOR：存在明显术语误用、漏意、错译或语义偏差，必须修改\n"
    "- CRITICAL：严重误译、否定颠倒、幻觉、未翻译或结构/占位符损坏，必须修改\n"
    "只输出严格 JSON 对象，不要输出任何额外文字：\n"
    '{"level": "PASS|MINOR|MAJOR|CRITICAL", '
    '"reason": "一句话简述问题与修改要求（PASS时留空，不超过45字）", '
    '"suggestion": "修正后的完整中文译文（PASS时留空；非PASS时必须直接给出改进后的完整中文译文，严格保留原文占位符与标签）"}'
)


@dataclass(frozen=True)
class ReviewVerdict:
    level: str
    reason: str
    suggestion: str = ""


def review_single_entry(
    reason_client: LLMClient,
    entry: Entry,
    context: GameContext,
) -> ReviewVerdict:
    """Evaluate one translated entry and return a normalized ReviewVerdict with suggestion."""
    ctx_line = ""
    if context.is_ready:
        parts = [p for p in (context.genre, context.setting, context.style) if p]
        if parts:
            ctx_line = f"游戏语境：{' / '.join(parts)}\n"
    user_prompt = (
        f"{ctx_line}"
        f"原文：{entry.original}\n"
        f"译文：{entry.translation}\n"
        "请给出审校分级与修正译文 JSON："
    )
    res = reason_client.chat(
        [
            {"role": "system", "content": _REVIEW_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.1,
        max_tokens=320,
    )
    obj = extract_json_object(res.content)
    if not obj:
        return ReviewVerdict("MINOR", "审校输出格式异常，已保留译文", "")
    level = str(obj.get("level") or "PASS").strip().upper()
    if level not in ("PASS", "MINOR", "MAJOR", "CRITICAL"):
        level = "MINOR"
    reason = str(obj.get("reason") or "").strip()[:120]
    suggestion = str(obj.get("suggestion") or "").strip()
    return ReviewVerdict(level, reason, suggestion)


def _clean_suggestion(original: str, raw_suggestion: str) -> str:
    if not raw_suggestion or not raw_suggestion.strip():
        return ""
    cleaned = restore_missing_tags(original, clean_model_output(raw_suggestion))
    return cleaned.strip()


def _apply_auto_fix(
    pipeline: TranslationPipeline,
    entry: Entry,
    new_translation: str,
    note: str,
) -> Entry:
    """Persist an auto-fixed translation to project DB and cross-game memory."""
    updated = pipeline.project_store.update_entry_by_id(
        entry.id,  # type: ignore[arg-type]
        translation=new_translation,
        status=STATUS_TRANSLATED,
        quality_note="",
        review_level="FIXED",
        review_reason=note[:160],
    )
    pipeline.global_store.put_memory(
        entry.original, new_translation, pipeline.target_lang, source="ai"
    )
    return updated or entry


def review_and_refine_entry(
    entry: Entry,
    reason_client: LLMClient,
    translate_client: LLMClient,
    pipeline: TranslationPipeline,
    context: GameContext,
    *,
    max_rounds: int = 2,
) -> tuple[Entry, ReviewVerdict]:
    """Review entry and automatically fix any MINOR/MAJOR/CRITICAL issues.

    1. Instructs the translation model to retranslate following the review issue & guidance.
    2. If the retranslation still fails or doesn't improve, directly auto-applies the
       review model's validated `suggestion`.
    3. Only marks `STATUS_FLAGGED` if both auto-fix paths fail quality validation.
    """
    current = entry
    original_translation = (entry.translation or "").strip()
    verdict = review_single_entry(reason_client, current, context)

    # Already accurate & natural
    if verdict.level == "PASS":
        updated = pipeline.project_store.update_entry_by_id(
            current.id,  # type: ignore[arg-type]
            status=STATUS_TRANSLATED,
            review_level="PASS",
            review_reason="",
        )
        return updated or current, verdict

    initial_reason = verdict.reason or "表达不够准确自然"
    best_suggestion = _clean_suggestion(current.original, verdict.suggestion)

    # Phase 1: Ask the translation model to retranslate with the specific review issue & requirement
    rounds = 1 if verdict.level == "MINOR" else max(1, max_rounds)
    for _ in range(rounds):
        hint_parts = [f"当前译文「{current.translation}」存在问题：{verdict.reason or '措辞不够准确自然'}。"]
        if best_suggestion:
            hint_parts.append(f"请按此修正要点重新翻译（可参考改法「{best_suggestion}」），输出更准确的完整中文译文。")
        else:
            hint_parts.append("请严格修正上述问题，输出准确自然的完整中文译文。")

        outcome = pipeline.translate_one(
            current.original,
            translate_client,
            source=current.source,
            force_retranslate=True,
            fix_hint="".join(hint_parts),
            context=context,
        )
        candidate = (outcome.translation or "").strip()
        changed = bool(candidate and candidate != original_translation)
        quality_ok = outcome.ok and check_translation_quality(current.original, candidate).passed

        if changed and quality_ok:
            if verdict.level == "MINOR":
                updated = _apply_auto_fix(
                    pipeline,
                    outcome.entry,
                    candidate,
                    f"已按审校要求重译：{initial_reason}",
                )
                return updated, ReviewVerdict("FIXED", f"已按审校要求重译：{initial_reason}", candidate)

            # For MAJOR / CRITICAL, verify the retranslation with the reasoning model
            re_verdict = review_single_entry(reason_client, outcome.entry, context)
            if re_verdict.suggestion:
                cand_sug = _clean_suggestion(current.original, re_verdict.suggestion)
                if cand_sug:
                    best_suggestion = cand_sug
            if re_verdict.level in ("PASS", "MINOR"):
                updated = _apply_auto_fix(
                    pipeline,
                    outcome.entry,
                    candidate,
                    f"已按审校要求重译：{initial_reason}",
                )
                return updated, ReviewVerdict(re_verdict.level, f"已按审校要求重译：{initial_reason}", candidate)
            current = outcome.entry
            verdict = re_verdict
        else:
            # Retranslation didn't improve or failed quality check -> fall through to direct suggestion fix
            break

    # Phase 2: Directly auto-apply the reasoning model's validated suggestion
    if (
        best_suggestion
        and best_suggestion != original_translation
        and check_translation_quality(current.original, best_suggestion).passed
    ):
        fix_note = f"AI 审校已自动修正：{initial_reason}"
        updated = _apply_auto_fix(pipeline, current, best_suggestion, fix_note)
        return updated, ReviewVerdict("FIXED", fix_note, best_suggestion)

    # Phase 3: Fallback when neither retranslation nor direct suggestion could improve it
    if verdict.level == "MINOR":
        # Restore original translation if retranslation failed
        updated = pipeline.project_store.update_entry_by_id(
            current.id,  # type: ignore[arg-type]
            translation=original_translation or current.translation,
            status=STATUS_TRANSLATED,
            review_level="MINOR",
            review_reason=verdict.reason,
        )
        return updated or current, verdict

    updated = pipeline.project_store.update_entry_by_id(
        current.id,  # type: ignore[arg-type]
        translation=original_translation or current.translation,
        status=STATUS_FLAGGED,
        review_level=verdict.level,
        review_reason=verdict.reason,
    )
    return updated or current, verdict


_BATCH_REVIEW_SYSTEM = (
    "游戏本地化审校总监：代入玩家游玩视角，结合游戏语境批量核对编号词条。\n"
    "仅查：错译、漏译、否定颠倒、人称错乱、术语冲突、明显机翻腔或破坏沉浸感。\n"
    "克制原则：准确自然的译文直接放行，绝不为改而改；全合格直接输出 []。\n"
    '只输出严格JSON数组：[{"idx":编号,"level":"MINOR|MAJOR|CRITICAL","reason":"简述(<=25字)","suggestion":"修正后完整中文译文(保留占位符与标签)"}]'
)


def _find_glossary_violations(
    entry: Entry,
    glossary: list[GlossaryItem],
) -> list[tuple[str, str]]:
    """Return `(term, expected_translation)` pairs present in original (word-boundary safe) but missing from translation."""
    if not glossary or not entry.original or not entry.translation:
        return []
    tr = entry.translation
    violations: list[tuple[str, str]] = []
    seen: set[str] = set()
    for g in glossary:
        t = g.term.strip()
        dst = g.translation.strip()
        if not t or not dst or len(t) < 2 or t.casefold() in seen:
            continue
        if term_in_text(t, entry.original) and dst not in tr:
            violations.append((t, dst))
            seen.add(t.casefold())
            if len(violations) >= 3:
                break
    return violations


def _fix_flagged_entry_fast(
    entry: Entry,
    verdict: ReviewVerdict,
    translate_client: LLMClient,
    pipeline: TranslationPipeline,
    context: GameContext,
    glossary: list[GlossaryItem] | None = None,
    *,
    direct_suggestion_first: bool = False,
) -> tuple[Entry, ReviewVerdict]:
    """Single-pass fast auto-fix.

    When `direct_suggestion_first=True` (cloud API), directly applies a quality-validated
    suggestion from the reasoning model to avoid wasting a redundant second API call.
    """
    original_tr = (entry.translation or "").strip()
    reason = verdict.reason or "表达不够准确自然"
    suggestion = _clean_suggestion(entry.original, verdict.suggestion)

    if (
        direct_suggestion_first
        and suggestion
        and suggestion != original_tr
        and check_translation_quality(entry.original, suggestion).passed
    ):
        cand_entry = Entry(
            id=entry.id,
            original=entry.original,
            translation=suggestion,
            status=entry.status,
            source=entry.source,
        )
        if not _find_glossary_violations(cand_entry, glossary or []):
            note = f"AI 审校已自动修正：{reason}"
            updated = _apply_auto_fix(pipeline, entry, suggestion, note)
            return updated, ReviewVerdict("FIXED", note, suggestion)

    hint_parts = [f"当前译文「{original_tr}」存在问题：{reason}。"]
    if suggestion:
        hint_parts.append(f"请按问题修正并可参考改法「{suggestion}」，只输出准确自然的完整中文译文。")
    else:
        hint_parts.append("请严格修正上述问题，只输出准确自然的完整中文译文。")

    outcome = pipeline.translate_one(
        entry.original,
        translate_client,
        source=entry.source,
        force_retranslate=True,
        fix_hint="".join(hint_parts),
        context=context,
        glossary=glossary,
    )
    candidate = (outcome.translation or "").strip()
    if (
        outcome.ok
        and candidate
        and candidate != original_tr
        and check_translation_quality(entry.original, candidate).passed
    ):
        note = f"已按审校要求重译：{reason}"
        updated = _apply_auto_fix(pipeline, outcome.entry, candidate, note)
        return updated, ReviewVerdict("FIXED", note, candidate)

    # Direct fallback: apply reasoning model's validated suggestion immediately
    if (
        suggestion
        and suggestion != original_tr
        and check_translation_quality(entry.original, suggestion).passed
    ):
        note = f"AI 审校已自动修正：{reason}"
        updated = _apply_auto_fix(pipeline, entry, suggestion, note)
        return updated, ReviewVerdict("FIXED", note, suggestion)

    if verdict.level == "MINOR":
        updated = pipeline.project_store.update_entry_by_id(
            entry.id,  # type: ignore[arg-type]
            translation=original_tr or entry.translation,
            status=STATUS_TRANSLATED,
            review_level="MINOR",
            review_reason=reason,
        )
        return updated or entry, verdict

    updated = pipeline.project_store.update_entry_by_id(
        entry.id,  # type: ignore[arg-type]
        translation=original_tr or entry.translation,
        status=STATUS_FLAGGED,
        review_level=verdict.level,
        review_reason=reason,
    )
    return updated or entry, verdict


def review_batch_and_refine(
    entries: list[Entry],
    reason_client: LLMClient,
    translate_client: LLMClient,
    pipeline: TranslationPipeline,
    context: GameContext,
    *,
    glossary: list[GlossaryItem] | None = None,
    batch_size: int = 10,
    mode: str = "smart",
    is_cancelled: Callable[[], bool] | None = None,
    on_item: Callable[[Entry, ReviewVerdict], None] | None = None,
) -> list[tuple[Entry, ReviewVerdict]]:
    """High-throughput batched semantic + glossary-consistency review & automatic fix.

    - `mode="smart"` (default): 0ms fast-pass for clean low-risk UI labels; focuses 4B LLM
      exclusively on high-risk dialogues, negations, rich tags, glossary mismatches, and anomalies.
    - `mode="full"`: reviews all non-builtin entries with the 4B LLM.
    """
    results: list[tuple[Entry, ReviewVerdict]] = []
    pending_llm: list[Entry] = []
    all_glossary = glossary if glossary is not None else pipeline._combined_glossary()
    smart_mode = (mode or "smart").lower() != "full"

    # 1. Deterministic pre-pass: glossary violations fixed immediately, safe low-risk items 0ms PASS
    for entry in entries:
        if is_cancelled and is_cancelled():
            return results
        tr = (entry.translation or "").strip()
        builtin = lookup_builtin_ui(entry.original)
        violations = _find_glossary_violations(entry, all_glossary)
        if violations:
            term_spec = "；".join(f"{t} 必须译为「{d}」" for t, d in violations)
            vd = ReviewVerdict("MAJOR", f"术语未统一：{term_spec}")
            upd, res_vd = _fix_flagged_entry_fast(
                entry, vd, translate_client, pipeline, context, glossary=all_glossary
            )
            results.append((upd, res_vd))
            if on_item:
                on_item(upd, res_vd)
        elif (
            not is_translatable(entry.original)
            or (builtin and tr == builtin)
            or (smart_mode and not is_high_risk_for_review(entry, all_glossary))
        ):
            updated = pipeline.project_store.update_entry_by_id(
                entry.id,  # type: ignore[arg-type]
                status=STATUS_TRANSLATED,
                review_level="PASS",
                review_reason="",
            ) or entry
            v = ReviewVerdict("PASS", "")
            results.append((updated, v))
            if on_item:
                on_item(updated, v)
        else:
            pending_llm.append(entry)

    # Build compact in-game perspective header (base context; chunk-matched terms appended per batch)
    base_ctx_lines: list[str] = []
    if context.game_name:
        base_ctx_lines.append(f"游戏：《{context.game_name}》")
    if context.is_ready:
        parts = [p for p in (context.genre, context.setting, context.style) if p]
        if parts:
            base_ctx_lines.append("语境：" + " / ".join(parts))
        if context.summary:
            base_ctx_lines.append(f"简介：{context.summary[:60]}")
        if context.characters:
            base_ctx_lines.append("角色：" + "，".join(context.characters[:6]))
        if context.notes:
            base_ctx_lines.append("要点：" + "；".join(n[:40] for n in context.notes[:2]))
    all_term_pairs = [(g.term, g.translation) for g in all_glossary]
    if not all_term_pairs and context:
        all_term_pairs = [(s, d) for s, d, _ in parse_context_term_pairs(context)]

    is_cloud_reason = bool(getattr(reason_client, "is_cloud", False))
    eff_batch_size = max(1, 18 if (is_cloud_reason and batch_size == 10) else batch_size)

    # 2. Sparse batch review with reasoning model
    for start in range(0, len(pending_llm), eff_batch_size):
        if is_cancelled and is_cancelled():
            break
        chunk = pending_llm[start : start + eff_batch_size]
        lines = []
        chunk_raw_parts: list[str] = []
        for idx, e in enumerate(chunk, start=1):
            o_flat = e.original.replace("\n", "\\n")[:240]
            t_flat = (e.translation or "").replace("\n", "\\n")[:240]
            lines.append(f"[{idx}] {o_flat} || {t_flat}")
            chunk_raw_parts.append(o_flat)
        chunk_text = "\n".join(chunk_raw_parts)
        matched_terms = [
            (s, d) for s, d in all_term_pairs if s and term_in_text(s, chunk_text)
        ][:12]
        hdr_lines = list(base_ctx_lines)
        if matched_terms:
            hdr_lines.append("术语：" + "，".join(f"{s}={d}" for s, d in matched_terms))
        ctx_header = ("\n".join(hdr_lines) + "\n") if hdr_lines else ""
        user_prompt = f"{ctx_header}词条({len(chunk)}条)：\n" + "\n".join(lines)

        res = reason_client.chat(
            [
                {"role": "system", "content": _BATCH_REVIEW_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=min(1200, 96 * len(chunk) + 120),
        )
        raw_items = extract_json_array(res.content)
        if raw_items is None:
            for e in chunk:
                if is_cancelled and is_cancelled():
                    break
                upd, vd = review_and_refine_entry(
                    e, reason_client, translate_client, pipeline, context, max_rounds=1
                )
                results.append((upd, vd))
                if on_item:
                    on_item(upd, vd)
            continue

        issues_by_idx: dict[int, ReviewVerdict] = {}
        for item in raw_items:
            try:
                idx_val = int(item.get("idx"))
            except (TypeError, ValueError):
                continue
            if 1 <= idx_val <= len(chunk):
                lvl = str(item.get("level") or "MINOR").strip().upper()
                if lvl == "PASS":
                    continue
                if lvl not in ("MINOR", "MAJOR", "CRITICAL"):
                    lvl = "MINOR"
                issues_by_idx[idx_val] = ReviewVerdict(
                    level=lvl,
                    reason=str(item.get("reason") or "").strip()[:120],
                    suggestion=str(item.get("suggestion") or "").strip(),
                )

        # 3. Apply PASS immediately, auto-fix flagged entries
        for idx, e in enumerate(chunk, start=1):
            if is_cancelled and is_cancelled():
                break
            verdict = issues_by_idx.get(idx)
            if verdict is None:
                upd = pipeline.project_store.update_entry_by_id(
                    e.id,  # type: ignore[arg-type]
                    status=STATUS_TRANSLATED,
                    review_level="PASS",
                    review_reason="",
                ) or e
                vd = ReviewVerdict("PASS", "")
            else:
                upd, vd = _fix_flagged_entry_fast(
                    e,
                    verdict,
                    translate_client,
                    pipeline,
                    context,
                    glossary=all_glossary,
                    direct_suggestion_first=is_cloud_reason,
                )
            results.append((upd, vd))
            if on_item:
                on_item(upd, vd)

    return results
