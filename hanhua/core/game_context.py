# -*- coding: utf-8 -*-
"""游戏语境识别（设计文档 §3-24 核心模块，2026-08-21 新增）。

定位：轻量、实用、低负担的游戏语境辅助系统（设计文档 §1/§26）——
在正式翻译前，让模型快速建立足够的游戏背景信息，为翻译/审校/重排
提供简单、稳定、可复用的上下文。

设计原则（§2）：不追求小模型深度理解游戏——只回答几个简单问题
（什么类型/什么题材/文本内容/明显角色/明显专名/语言风格/翻译注意点）。
小模型无法确定时输出「未知」（§19 保守模式），禁止强行猜测。

模块职责：
1. 代表性文本抽样（§4）：UI 10 / 对白 20 / 任务 10 / 物品 10 /
   技能 10 / 剧情 20 / 其他 10——程序按条目 reason/role/kind 自动
   分类抽样，不让模型自己判断看哪些文本。
2. 识别 prompt 构建（§5）：给出分类样本，要求输出简洁结构化 JSON
   {game_name, genre, setting, summary, characters[], terms[],
   style, translation_notes[]}（terms 只识别类型，不强制定中文译名 §8）。
3. 结构化解析（§5-10）：保守容错——字段缺失/非法默认「未知」/空数组。
4. 本地/云端统一（§18）：create_client 按 ApiConfig 分发，本地 4B 与
   云端大模型走同一识别流程、同一注入格式（功能链路统一，模型能力不同）。
5. 持久化（§23）：ProjectStore KV（key='game_context'）存原始 JSON；
   三态（未建立/已建立/需要更新）由 UI 层判定。

与知识库/检索的关系（§13）：三者职责分离——Game Context 回答
「这个游戏是什么」，知识库回答「这个具体东西是什么意思」，检索回答
「当前这条文本需要哪些知识」。本模块只产出 Game Context，不触碰
知识库/检索。

不做的事（§20）：剧情总结、人物关系图、自动 Wiki、世界观推理、
高阶知识图谱、复杂置信度、多轮 Agent、长上下文分析。
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from .models import ApiConfig
from .translator import create_client

# 设计文档 §4：抽样类别 → 每类条数
SAMPLE_BUDGET: dict[str, int] = {
    "ui": 10,
    "dialogue": 20,
    "quest": 10,
    "item": 10,
    "skill": 10,
    "story": 20,
    "other": 10,
}
TOTAL_SAMPLE = sum(SAMPLE_BUDGET.values())   # 90

# 持久化键
GAME_CONTEXT_KEY = "game_context"

# 「需要更新」判定阈值（§23）：新增可翻译文本数 ≥ 已有总量 25% 时提示更新
CONTEXT_UPDATE_RATIO = 0.25

# Game Context 字段白名单（防旧库/脏 JSON 混入未知字段膨胀上下文 §12）
_CONTEXT_FIELDS = (
    "game_name", "genre", "setting", "summary",
    "characters", "terms", "style", "translation_notes",
)

# Game Context 键 → GameProfile.context_* 字段（save_game_context 同步
# 进档案，翻译/审校 prompt 注入同一份数据——§15/§16）
_CONTEXT_FIELD_MAP: dict[str, str] = {
    "context_game_name": "game_name",
    "context_genre": "genre",
    "context_setting": "setting",
    "context_summary": "summary",
    "context_characters": "characters",
    "context_terms": "terms",
    "context_style": "style",
    "context_translation_notes": "translation_notes",
}

_KNOWN_GENRES = frozenset("""
RPG 动作 冒险 策略 模拟 视觉小说 恐怖 解谜 射击 竞速 格斗 体育 音乐
休闲 角色扮演 文字 卡牌 塔防 生存 建造 养成 恋爱 放置 弹幕 横版
""".split())


# ── 类别判定（程序自动分类，§4） ─────────────────────────────

# reason/role 子串 → 类别（reason 细粒度优先，role 兜底，kind 再兜底）
_CATEGORY_RULES: list[tuple[tuple[str, ...], str]] = [
    # UI 控件/菜单/交互提示
    (("interaction_prompt", "single_visible_string", "core_menu_collection",
      "core_menu_control", "object_has_display_evidence", "display_phrase",
      "localization_key_value", "menu_button"), "ui"),
    # 角色对白
    (("dialogue_line", "dialog", "chat", "conv", "subtitle",
      "dialogue"), "dialogue"),
    # 任务
    (("quest", "objective"), "quest"),
    # 物品
    (("item", "inventory", "equipment"), "item"),
    # 技能
    (("skill", "spell", "ability", "power", "talent"), "skill"),
    # 剧情/叙述
    (("story", "narration", "natural_language", "display_phrase_long",
      "plot", "scene"), "story"),
]


def _category_of(meta: dict, text: str = "") -> str:
    """单条目 → 抽样类别（默认 other）。

    判定优先级：reason（提取器细粒度分类）→ role（兜底）→ kind。
    natural_language 长文本归剧情（>60 字符），短文本归对白——
    叙述与对白在 reason 层都标 natural_language，长度是可靠区分信号；
    必须先于规则循环判定（story 规则也含 natural_language 子串，
    顺序依赖会把所有长文短白都归 story）。
    """
    reason = str(meta.get("reason") or "")
    if reason == "natural_language":
        return "story" if len(text) > 60 else "dialogue"
    role = str(meta.get("role") or "")
    kind = str(meta.get("kind") or "")
    for keys, cat in _CATEGORY_RULES:
        for key in keys:
            if key in reason or key in role or key in kind:
                return cat
    return "other"


def sample_entries(rows: list[dict], budget: dict[str, int] | None = None,
                   max_text_len: int = 200) -> list[dict]:
    """代表性文本抽样（§4）：按类别均匀抽样，返回 [{category, text}]。

    rows 为 store.get_entries() 原始行（含 meta 字符串/字典兼容）；
    每类取前 budget 条（顺序即库内顺序，天然稳定、不随机）。
    text 截断 max_text_len（控制输入 token 规模，§12 不膨胀）。

    类别数不足 budget 时取该类全部（绝不跨类补数——宁可类别缺，不用
    异类文本冒充，避免污染模型判断）。
    """
    budget = budget or SAMPLE_BUDGET
    from .models import entry_from_row
    buckets: dict[str, list[dict]] = {cat: [] for cat in budget}
    for row in rows:
        try:
            entry = entry_from_row(row)
        except Exception:  # noqa: BLE001 单行坏数据跳过，不阻断抽样
            continue
        text = str(entry.original or "").strip()
        if not text:
            continue
        # 跳过跳过态（skipped/blocked）：提取器判定为引擎控件/结构/键名
        # 的条目（按钮状态 Normal/Highlighted、输入轴 Horizontal/Submit、
        # 序列化字段名）是程序已判明的非翻译文本——抽样喂给识别模型只会
        # 污染判断（把按钮状态当剧情词）。pending/failed/translated 才是
        # 用户可见文本候选。2026-08-31 用户实证「介绍全未知」：Cell
        # Machine 的 20 条样本里 10 条是这类被跳过项，genre/setting 被
        # 带偏成 未知。translated 条目保留（记忆直填的结果仍是游戏文本）。
        status = str(entry.status or "").lower()
        if status in ("skipped", "blocked"):
            continue
        cat = _category_of(entry.meta, text)
        if cat not in buckets:
            cat = "other"
        if len(buckets[cat]) >= budget[cat]:
            continue
        buckets[cat].append({
            "category": cat,
            "text": text[:max_text_len],
        })
    out: list[dict] = []
    for cat in budget:
        out.extend(buckets[cat])
    return out


# ── 识别 prompt（§5 schema + §19 保守模式） ──────────────────

_RECOGNITION_SYSTEM = (
    "你是游戏语境识别助手。根据给出的游戏文本样本，回答几个简单问题，"
    "输出严格 JSON 对象，不要输出任何其他文字。\n"
    "JSON schema：\n"
    "{\n"
    '  "game_name": "游戏名（样本中可推断时填；否则 未知）",\n'
    '  "genre": "游戏类型（如 RPG/动作/冒险/策略/模拟/视觉小说/恐怖/解谜，'
    '允许简单组合如 动作RPG；不确定填 未知）",\n'
    '  "setting": "题材/背景（如 中世纪奇幻/现代都市/科幻/校园/末日/历史；'
    '无法确定填 未知，禁止强行猜测）",\n'
    '  "summary": "一句简短游戏简介（≤50字，不加推测细节）",\n'
    '  "characters": ["角色名：身份或特征（只收明显能确认的角色，如 '
    'Alice：女性角色，似乎是教师；身份无法确定填 未知，禁止深层关系推理）"],\n'
    '  "terms": ["专有名词：类型（只识别名词类型，如 Mana：游戏机制 / '
    'The Order：组织 / Academy：地点机构——不要在此确定中文译名）"],\n'
    '  "style": "粗粒度语言风格（如 整体现代自然游戏化；对白偏口语；UI简洁；'
    '不确定填 未知）",\n'
    '  "translation_notes": ["翻译注意点，最多5条，如 角色对白保持口语化"]\n'
    "}\n"
    "约束：\n"
    "- 只依据样本中明确出现的内容作答，不脑补剧情、不做人物关系推理。\n"
    "- 任何无法确定的字段填 未知 或空数组，不要猜答案。\n"
    "- characters 最多 12 个，terms 最多 15 个，translation_notes 最多 5 条。\n"
    "- 所有字段值必须是字符串或字符串数组，不要嵌套对象。"
)


def build_recognition_user_prompt(samples: list[dict],
                                  source_lang: str = "auto") -> str:
    """识别 user prompt：分类样本 + 保守模式要求（§4/§19）。"""
    lang_note = ("（原文可能是英语/日语/韩语等，按实际判断）"
                 if source_lang == "auto" else f"（原文语言：{source_lang}）")
    lines = [
        "以下是程序从该游戏自动抽取的代表性文本样本"
        f"{lang_note}，按类别标记。请据此完成游戏语境识别：",
        "",
    ]
    cat_names = {
        "ui": "UI 界面文本", "dialogue": "角色对白", "quest": "任务文本",
        "item": "物品文本", "skill": "技能文本", "story": "剧情/叙述",
        "other": "其他",
    }
    current = None
    for s in samples:
        cat = str(s.get("category") or "other")
        if cat != current:
            current = cat
            lines.append(f"【{cat_names.get(cat, cat)}】")
        lines.append(s.get("text", ""))
    lines.append("")
    lines.append(
        "请严格按上面要求的 JSON schema 输出游戏语境识别结果。"
        "无法确定的字段填 未知 或空数组，不要猜测。")
    return "\n".join(lines)


# ── 结构化解析（§5-10 保守容错） ─────────────────────────────

def _clean_str(value: Any, default: str = "未知") -> str:
    """值 → 干净字符串（空/非字符串 → default；去空白）。"""
    if isinstance(value, str):
        s = value.strip()
        return s if s else default
    if value is None:
        return default
    return str(value).strip()[:200]


def _clean_str_list(value: Any, limit: int) -> list[str]:
    """值 → 干净字符串数组（去空项/去重，上限 limit）。"""
    out: list[str] = []
    if not isinstance(value, list):
        return out
    seen: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            # 容忍 {name: ..., type: ...} 形态 → "name：type"
            name = _clean_str(item.get("name"), "")
            typ = _clean_str(item.get("type"), "")
            if name and name != "未知":
                entry = f"{name}：{typ}" if typ and typ != "未知" else name
            elif typ and typ != "未知":
                entry = f"未知：{typ}"
            else:
                continue
        else:
            entry = _clean_str(item, "")
            if not entry or entry == "未知":
                continue
        if entry not in seen:
            seen.add(entry)
            out.append(entry[:120])
        if len(out) >= limit:
            break
    return out


def _meaningless(value: Any) -> bool:
    """语境字段是否无实际内容（模型无法确定的表现）。

    「未知」字符串、空串、空数组都算无内容——不注入翻译 prompt、
    不同步进档案（防「全未知介绍」黑屏与档案语义污染）。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip() == "未知"
    if isinstance(value, list):
        return not value
    return False


def parse_game_context(raw: str) -> dict:
    """识别模型输出 → 规范 Game Context dict（保守容错）。

    任何解析失败/字段非法都回落为「未知」/空数组，绝不抛异常——识别
    失败不阻断翻译（调用方降级：无 Game Context 即回到旧版行为）。
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        body = text[3:]
        if "```" in body:
            body = body.split("```", 1)[0]
        body = body.strip()
        if body.startswith("json"):
            body = body[4:].strip()
        text = body
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, AttributeError):
        return {}
    if not isinstance(data, dict):
        return {}
    ctx: dict = {}
    for field in _CONTEXT_FIELDS:
        value = data.get(field)
        if field in ("characters", "terms", "translation_notes"):
            limit = {"characters": 12, "terms": 15,
                     "translation_notes": 5}.get(field, 5)
            ctx[field] = _clean_str_list(value, limit)
        elif field == "summary":
            ctx[field] = _clean_str(value, "未知")[:150]
        else:
            ctx[field] = _clean_str(value, "未知")
    return ctx


def game_context_summary(ctx: dict | None) -> str:
    """Game Context → 用户可见的简短摘要（状态卡副行用，§24）。

    形如「奇幻 RPG · 魔法学院背景」；空上下文 → 空串。
    """
    if not ctx:
        return ""
    parts: list[str] = []
    if ctx.get("genre") and ctx["genre"] != "未知":
        parts.append(str(ctx["genre"]))
    if ctx.get("setting") and ctx["setting"] != "未知":
        parts.append(str(ctx["setting"]))
    if not parts and ctx.get("game_name") and ctx["game_name"] != "未知":
        parts.append(str(ctx["game_name"]))
    return " · ".join(parts)


# ── 识别器（本地/云端统一，§18） ─────────────────────────────

class GameContextRecognizer:
    """游戏语境识别：create_client 统一本地/云端调用。

    config: ApiConfig（mode=api 时走云端大模型；mode=local 时走本地
    翻译模型）——功能链路统一，模型能力不同（§18）。

    调用方式（线程 Worker 内，UI 层模式）：
        recognizer = GameContextRecognizer(config)
        raw = recognizer.recognize(samples)   # 返回 JSON 字符串
        ctx = parse_game_context(raw)          # 结构化结果
    """

    def __init__(self, config: ApiConfig, timeout: float = 180.0,
                 max_tokens: int = 2048, temperature: float = 0.2):
        self.config = config
        self.timeout = float(timeout)
        self.max_tokens = int(max_tokens)
        self.temperature = float(temperature)

    def recognize(self, samples: list[dict],
                  source_lang: str = "auto") -> str:
        """运行识别，返回模型原始输出（JSON 字符串）。

        请求失败抛异常（调用方 catch 后降级：无 Game Context 不阻断）。
        BaseClient.chat 签名 (system, messages)——max_tokens/temperature/
        timeout 从 ApiConfig 读取（与翻译/审核链路同一约定），此处构造
        带识别参数（识别输出为 JSON schema ≤2k tokens）的配置副本。

        2026-08-31：本地路径（mode=local）改为直连 llama-server 的
        /v1/chat/completions——原 create_client 把 system 放 messages[0]
        且默认带 response_format（部分 llama-server 组合下 JSON 被包进
        ```json 代码块/报错），4B 审核模型识别输出因此丢失（黑屏空介绍
        的另一直接根因）。直连走与 ReviewModelService.chat 相同的形式
        （system 独立、无 response_format、cleanup 兜底），与 4B 审核
        链路行为完全一致，稳定返回可解析 JSON。
        """
        user = build_recognition_user_prompt(samples, source_lang)
        if self.config.mode == "local":
            payload = {
                "model": self.config.model or "local",
                "messages": [
                    {"role": "system", "content": _RECOGNITION_SYSTEM},
                    {"role": "user", "content": user},
                ],
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
            try:
                resp = httpx.post(
                    self.config.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {self.config.api_key}"},
                    json=payload,
                    timeout=self.timeout, trust_env=False, verify=False)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
            except (httpx.HTTPError, KeyError, TypeError, ValueError,
                    IndexError) as exc:
                raise RuntimeError(f"游戏语境识别请求失败：{exc}") from exc
            return str(content or "")
        client = create_client(replace(
            self.config,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            timeout=self.timeout))
        result = client.chat(
            _RECOGNITION_SYSTEM, [{"role": "user", "content": user}])
        if isinstance(result, tuple):
            return str(result[0] or "")
        return str(result or "")


# ── 持久化（ProjectStore KV，§23） ──────────────────────────

def load_game_context(store) -> dict:
    """从 ProjectStore KV 读 Game Context（缺失/损坏 → {}）。

    白名单过滤防旧库脏数据膨胀，但放行内部元数据字段（_sampled_total，
    §23 需要更新判定基线）——它不属于注入上下文的语境字段。
    """
    try:
        value = store.get_profile_value(GAME_CONTEXT_KEY, {})
    except Exception:  # noqa: BLE001 存储异常降级
        return {}
    if isinstance(value, dict):
        return {k: v for k, v in value.items()
                if k in _CONTEXT_FIELDS or k == "_sampled_total"}
    return {}


def save_game_context(store, ctx: dict) -> None:
    """Game Context → ProjectStore KV（覆盖旧值）。

    _sampled_total 是「需要更新」判定基线（§23）非语境字段，一并持久化
    但禁止作为注入上下文（build_game_context_block 只读 context_* 字段）。
    """
    clean = {k: ctx.get(k) for k in _CONTEXT_FIELDS if ctx.get(k) not in (None, "")}
    baseline = ctx.get("_sampled_total")
    if isinstance(baseline, (int, float)) and baseline > 0:
        clean["_sampled_total"] = int(baseline)
    try:
        store.set_profile_value(GAME_CONTEXT_KEY, clean)
    except Exception:  # noqa: BLE001 持久化失败降级（不阻断识别流程）
        pass
    # 2026-08-31 语境生效（此前 context_* 字段从未被赋值——识别结果只
    # 落在 KV，翻译/审校的 build_game_context_block 恒读空 profile →
    # 语境零效果）。同一份数据同步进 game_profile：get_profile() 读出的
    # profile 携带 context_* 字段，翻译 system prompt 与审校 hint 才能
    # 真正注入游戏语境。「未知」/空数组是模型无法确定的表现，不产生
    # 注入价值且会污染档案语义——只同步有实际内容的字段。profile 无
    # 字段/存储失败都静默降级，不阻断识别。
    try:
        profile = store.get_profile()
        changed = False
        for key, ck in _CONTEXT_FIELD_MAP.items():
            value = ctx.get(ck)
            if _meaningless(value):
                value = ""
            current = getattr(profile, key, None)
            if isinstance(current, list) != isinstance(value, list) \
                    or current != value:
                setattr(profile, key, value)
                changed = True
        if changed:
            store.set_profile(profile)
    except Exception:  # noqa: BLE001 档案同步失败不阻断识别
        pass


def clear_game_context(store) -> None:
    """删除 Game Context（项目重扫时清空，防陈旧语境误用）。"""
    try:
        store.del_profile_value(GAME_CONTEXT_KEY)
    except Exception:  # noqa: BLE001
        pass


def context_needs_update(store, actionable_total: int) -> bool:
    """「需要更新」三态判定（§23）：已有 Game Context 且新增可翻译文本
    ≥ 已有总量 25%（估算：识别时文本总量未存，用上下文里记录的可翻译
    数对比；无记录时按增量比例近似）。

    返回 False 时 UI 显示「已建立」；无 Game Context 显示「未建立」。
    """
    ctx = load_game_context(store)
    if not ctx:
        return False
    baseline = int(ctx.get("_sampled_total") or 0)
    if baseline <= 0:
        return False
    return actionable_total >= baseline * (1 + CONTEXT_UPDATE_RATIO)
