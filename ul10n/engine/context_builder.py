"""Game context inference: sample representative entries, infer structured GameContext,
and sync confirmed proper nouns / character names into the project glossary.
"""
from __future__ import annotations

import re

from ul10n.core.db import ProjectStore
from ul10n.core.models import Entry, GameContext
from ul10n.engine.llm_client import LLMClient, extract_json_object

_CONTEXT_SYSTEM = (
    "游戏本地化总监：根据游戏名与抽样文本生成简洁语境档案。\n"
    "1. 填写 genre、setting、summary、style，勿全留空。\n"
    "2. characters 与 terms 仅收录样本中真实出现的角色/专名，格式「原文=译名」，绝不编造。\n"
    "3. 只输出严格 JSON 对象，不要任何额外解释。"
)

_PAIR_SPLIT_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9\s.'\-/:]{1,42}?)\s*(?:=|：|:|->|→)\s*(.+)$")
_GENERIC_UI_SKIP = frozenset({
    "ok", "yes", "no", "back", "next", "play", "exit", "quit", "save", "load",
    "on", "off", "menu", "pause", "resume", "settings", "options", "audio",
    "video", "graphics", "controls", "language", "credits", "cancel", "apply",
    "confirm", "continue", "start", "retry", "close", "volume", "fullscreen",
})


def sample_representative_entries(entries: list[Entry], max_samples: int = 55) -> list[str]:
    """Select high-signal, token-efficient short/medium/long samples (skipping generic UI noise)."""
    short: list[str] = []
    medium: list[str] = []
    long_txt: list[str] = []
    seen: set[str] = set()

    ordered = sorted(entries, key=lambda e: (0 if e.source == "xuat_live" else 1, e.id or 0))
    for e in ordered:
        t = (e.original or "").strip()
        if not t or len(t) < 2 or t in seen or t.casefold() in _GENERIC_UI_SKIP:
            continue
        seen.add(t)
        snippet = t.replace("\n", " / ")[:130]
        if len(t) <= 24:
            if len(short) < 15:
                short.append(snippet)
        elif len(t) <= 100:
            if len(medium) < 25:
                medium.append(snippet)
        else:
            if len(long_txt) < 15:
                long_txt.append(snippet)
        if len(short) + len(medium) + len(long_txt) >= max_samples:
            break

    # Fallback if all entries were generic UI labels
    if not (short or medium or long_txt):
        for e in ordered[:20]:
            t = (e.original or "").strip()
            if t and t not in seen:
                seen.add(t)
                short.append(t.replace("\n", " / ")[:100])

    return (long_txt + medium + short)[:max_samples]


def infer_game_context(
    client: LLMClient,
    entries: list[Entry],
    game_name: str = "",
) -> GameContext:
    """Run one compact reasoning call to infer GameContext from sampled game entries."""
    samples = sample_representative_entries(entries)
    if not samples:
        return GameContext(game_name=game_name)

    sample_block = "\n".join(f"- {s}" for s in samples)
    user_prompt = (
        f"游戏：{game_name or '未知'}\n"
        f"抽样（{len(samples)}条）：\n{sample_block}\n\n"
        '输出JSON：{"game_name":"","genre":"类型","setting":"题材(<=20字)","summary":"简介(<=40字)",'
        '"style":"翻译风格","characters":["原文=译名(<=8个)"],"terms":["原文=译名(<=12个)"],"notes":["注意点(<=4条)"]}'
    )

    res = client.chat(
        [
            {"role": "system", "content": _CONTEXT_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.15,
        max_tokens=768,
    )
    parsed = extract_json_object(res.content)
    if not parsed:
        return GameContext(
            game_name=game_name,
            style="UI 简洁干练，对话与提示自然流畅",
        )
    ctx = GameContext.from_dict(parsed)
    if not ctx.game_name and game_name:
        ctx.game_name = game_name
    if not (ctx.genre or ctx.setting or ctx.summary or ctx.style):
        ctx.style = "UI 简洁干练，按键与时间提示保持规范格式"
    return ctx


def parse_context_term_pairs(ctx: GameContext) -> list[tuple[str, str, str]]:
    """Extract clean `(term, translation, category)` tuples from `ctx.characters` and `ctx.terms`."""
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for cat, items in (("角色", ctx.characters), ("术语", ctx.terms)):
        for raw in items or []:
            m = _PAIR_SPLIT_RE.match((raw or "").strip())
            if not m:
                continue
            src = m.group(1).strip()
            dst = m.group(2).strip()
            # Trim trailing explanations like "灯塔（游戏主要场景）" -> keep concise translation
            dst = re.split(r"[（(，,。;；]", dst, maxsplit=1)[0].strip()
            if not src or not dst or len(dst) > 24 or src.casefold() in seen:
                continue
            seen.add(src.casefold())
            out.append((src, dst, cat))
    return out


def sync_context_terms_to_glossary(ctx: GameContext, store: ProjectStore) -> int:
    """Import confirmed context term pairs into project glossary without overwriting user terms."""
    existing = {g.term.strip().casefold() for g in store.list_glossary()}
    added = 0
    for term, translation, category in parse_context_term_pairs(ctx):
        if term.casefold() in existing:
            continue
        store.upsert_glossary(term, translation, category=category, note="语境自动提取")
        existing.add(term.casefold())
        added += 1
    return added
