"""AI 写回分诊层（M2，2026-09-06 0.39.0）——本地 AI 智能写回引擎判定层。

《本地 AI 智能写回引擎设计与实施文档 V1.0》落地：确定性检查 → AI 判断 →
确定性执行 → 确定性验证。本模块只承担「AI 判断」一环，且只处理程序规则
**无法确定**的条目（§40 不做 6：所有文本都必须调用 AI——不做）：确定性层
（提取 disposition / logic_audit 键环境回退 / writer 不可变字段闸门 /
占位符守恒）照旧全部生效，AI 判定只能让规则不确定的条目「照写（allow）」
或「保守跳过（review/reject）」，永远不能启用一条被确定性规则拦下的写回
（§15 AI 不能越过程序的安全边界；AI 建议 < 程序硬约束）。

分诊对象（宁缺勿滥）：write_back_v2 待写回候选中 logic_audit 形态审计
「warn/note 级标识符形态 + 未落入键环境」的条目——camelCase/snake_case/
kebab_case/uppercase_const/lowercode_word/short_code_word（非常见按钮词）。
这正是「是 UI 文本还是代码键」无法靠形态判定的灰色地带（CombatMusic 是
技能名还是动画触发器？）。明确排除：
- type_descriptor / 键环境对象（audit severity=revert）——确定性回退层
  所有，AI 无权参与；
- logic_compare_word / LOGIC_KEYS_COMMON 短词（back/play/start）——核心
  按钮显示文本，保持现有照写行为（跳过它们 = 覆盖面积塌方）；
- numeric_mix / digit_leading / single_char——B19/F50 实证真实显示文本
  高发形态（2F 楼层 / x2 倍数 / 单字符档位），不进分诊（旧问题不复现）。

判定语义（与 WriteResult 记账对齐）：
- allow → 照写（下游确定性闸门仍会复查，AI 无权放行确定性拦截）；
- review / reject → 不写该条，note_logic_reverted 记账（reason
  ai_triage_*）：resolved 标记（不触发 rejected 发布阻断）+ 原文进
  logic_reverted_sources（运行时插件排除表，防按名比较断链）+ C10
  状态同步。等价于「保守回退保留原文」——宁漏勿坏。

故障语义（§25/§26/§52/§71-7）分两档：
- 模型不可用（缺失/未配置）→ 分诊层整体不参与（degraded，零跳过）：
  候选按确定性规则照写。理由：§70 回归条款「现有已支持游戏写回结果不得
  因引入 AI 非预期变化」+ 发布打包 Lite 通道无模型必须保持 0.38.0 行为
  （旧问题不复现：F50/B19 类缺译不得经写回端复现）。缓存里的历史真实
  模型结论（版本匹配）仍然生效——那是当时的真实判定，不因今天模型缺席
  而翻案；
- 模型在场但批内请求失败/输出非法 → 本批一律 review 跳过（fail-closed，
  绝不默认 allow）。连续 2 批失败熔断（防 ensure_running 反复重试把写回
  挂死 100+ 分钟），余下批直接 review 不再请求。

缓存（§29/§30）：判定按条目落 store meta（ai_writeback_verdict =
{"v": verdict, "pv": prompt 版本}），prompt 版本变更即整体失效；运行内
同（原文,译文,形态,语境）去重只问一次。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# prompt 版本（§31）：判定规则/prompt 文案变更必须换版本——缓存按版本
# 失效，防止新规则读旧结论。
_JUDGE_PROMPT_VERSION = "writeback_judge_v1"

# 单批判定条数（§27：10~30 条/批；§28 稳定>速度，与审校 review_batch_
# size=20 同源）。
_BATCH_SIZE = 20

# 单条原文/译文进 prompt 的截断（超长串几乎必然是文档正文，不在标识符
# 分诊范围内；判定只需形态与语境证据）。
_PROMPT_TEXT_LIMIT = 120

# 缓存 meta 键（§29/§30）：值 {"v": verdict, "pv": _JUDGE_PROMPT_VERSION}
_MUTED = "ai_writeback_verdict"

_VERDICTS = ("allow", "review", "reject")

# 分诊形态白名单：warn 级标识符形态 + note 级灰色形态（排除依据见模块
# docstring——numeric_mix/digit_leading/single_char 是 B19/F50 真实显示
# 文本高发形态，logic_compare_word/common 短词是核心按钮文本）。
_TRIAGE_PATTERNS = frozenset({
    "camel_case", "snake_case", "kebab_case", "uppercase_const",
    "lowercode_word", "short_code_word",
})

# 连续批失败熔断阈值（防模型启动失败时逐批 120s 超时把写回挂死）。
_MAX_CONSECUTIVE_FAILURES = 2


@dataclass
class WritebackTriageReport:
    """单次写回分诊汇总（写日志/报告用，fail-closed 不抛异常）。"""
    scanned: int = 0      # 规则不确定（形态命中未落键环境）候选条数
    cached: int = 0       # 命中版本化缓存未问模型的条数
    asked: int = 0        # 实际送模型判定的组数（去重后）
    allowed: int = 0      # 判 allow（照写）条数（含缓存/去重展开）
    review: int = 0       # 判 review（保守跳过）条数
    rejected: int = 0     # 判 reject（拒绝写回）条数
    degraded: bool = False  # 模型不可用（整体不参与）或批内故障
    error: str = ""

    def summary(self) -> str:
        if self.degraded and not self.asked and not self.review \
                and not self.rejected:
            return (f"AI 写回分诊不可用（{self.error or '模型缺失'}）："
                    f"{self.scanned} 条规则不确定候选按确定性规则照写"
                    f"（分诊层未参与，缓存生效 {self.cached} 条）")
        return (f"AI 写回分诊：规则不确定 {self.scanned} 条，缓存 "
                f"{self.cached} 条，询问 {self.asked} 组 → 放行 "
                f"{self.allowed} / 复核跳过 {self.review} / 拒绝 "
                f"{self.rejected}"
                + (f"（降级：{self.error}）" if self.degraded else ""))


# ── 候选语境（确定性事实，零推断）─────────────────────────────────────

def _meta_of(entry: dict) -> dict:
    raw = entry.get("meta")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def _field_leaf(meta: dict) -> str:
    field_path = meta.get("field_path")
    if isinstance(field_path, list):
        for part in reversed(field_path):
            text = str(part)
            if not text.isdigit():
                return text[:40]
    return ""


def _context_of(meta: dict) -> str:
    """确定性语境：字段叶子名/对象内显示证据/提取分类/类名/文件名。

    与 ai_recognition._item_context 同口径（只陈述提取现场事实），另加
    提取器条目级处置 reason（input_binding 等键环境信号——分诊池已排除
    键环境，但 reason 仍是模型可用的证据提示）。
    """
    parts: list[str] = []
    leaf = _field_leaf(meta)
    if leaf:
        parts.append(f"字段 {leaf}")
    if meta.get("obj_has_values"):
        parts.append("对象含其他显示文本")
    else:
        parts.append("对象无其他显示文本")
    reason = str(meta.get("reason") or "").strip()
    if reason:
        parts.append(f"提取分类 {reason[:40]}")
    for key, label in (("script_class", "类"), ("asset_file", "文件")):
        value = str(meta.get(key) or "").strip()
        if value:
            parts.append(f"{label} {value[:40]}")
    return "；".join(parts)


def _triage_pattern_of(entry: dict) -> str | None:
    """规则不确定（形态命中但未落入确定性键环境）→ 返回形态名。

    单一来源：logic_audit.audit_entries_before_writeback——键环境
    （obj_is_key_list/代码类 reason）条目的 severity 已被升为 revert，
    这里只收 warn/note 中「标识符 vs 显示文本」灰色形态（白名单见
    _TRIAGE_PATTERNS）。type_descriptor/键环境 = 确定性回退层所有。
    """
    from hanhua.core.unity.logic_audit import (
        LOGIC_KEYS_COMMON, audit_entries_before_writeback)
    records = audit_entries_before_writeback([entry])
    if not records:
        return None
    rec = records[0]
    pattern, severity = rec["pattern"], rec["severity"]
    if severity not in ("warn", "note"):
        return None  # revert = 确定性回退层所有，AI 无权参与
    if pattern not in _TRIAGE_PATTERNS:
        return None
    if pattern == "short_code_word" \
            and str(entry.get("original") or "").strip().lower() \
            in LOGIC_KEYS_COMMON:
        return None
    return pattern


# ── 模型判定 ─────────────────────────────────────────────────────────────

def _clip(text: str) -> str:
    text = str(text or "").strip()
    if len(text) > _PROMPT_TEXT_LIMIT:
        return text[:_PROMPT_TEXT_LIMIT] + "…"
    return text


def _build_batch_prompt(batch: list[tuple[dict, dict, str]]) -> str:
    """批量判定 prompt（§65：短/固定/结构化/低自由度）。

    程序已完成占位符/富文本/编码检查（writer 确定性层），prompt 明示
    不要重复评估（§65）；只判「原文是显示文本还是代码键」这一个问题。
    错误代价不对称引导：写坏键 = 断链，漏翻一条 = 无害 → 拿不准一律
    review。
    """
    lines = [
        "你是 Unity 游戏写回安全审查器。判断下列字符串是「玩家可见的显示文本」"
        "还是「代码按名查找的键」。",
        "显示文本：按钮/菜单/物品/技能名等玩家在界面里看到的词句，写回中文"
        "译文安全且必要。",
        "代码键：对象名、动画/输入动作/状态名、资源名等引擎按名查找的字符串，"
        "写回译文会破坏游戏逻辑。",
        "程序已完成占位符、富文本和编码检查，不要评估这些，只判断原文性质。",
        "每条附提取现场语境（字段名/对象内显示证据/提取分类/类名/文件名），"
        "只作参考：文本类字段（text/label/description）倾向显示文本，"
        "技术类字段（path/id/key/编号名）倾向代码键。",
        "判定：allow=较确定是显示文本；reject=较确定是代码键；"
        "review=拿不准。拿不准一律 review（宁漏翻不可写坏游戏）。",
        "只输出 JSON 数组，每项 {\"i\": 索引, \"v\": \"allow\"|\"review\"|"
        "\"reject\"}，不要输出其他内容。",
        "",
        "字符串列表：",
    ]
    for index, (entry, meta, pattern) in enumerate(batch):
        line = (f"{index}. 原文 {_clip(entry.get('original'))!r} "
                f"译文 {_clip(entry.get('translation'))!r} 形态 {pattern}")
        context = _context_of(meta)
        if context:
            line += f"（{context}）"
        lines.append(line)
    return "\n".join(lines)


_VERDICT_ITEM = re.compile(r"\{\s*\"i\"\s*:\s*(\d+)\s*,\s*\"v\"\s*:\s*"
                           r"\"(allow|review|reject)\"\s*\}")


def _parse_verdicts(raw: str) -> dict[int, str] | None:
    """解析模型 JSON 数组 → {索引: verdict}。

    容错同 ai_recognition._parse_verdicts：剥 ``` 围栏；JSON 整体失败
    退化为逐项正则。返回 None 仅当一条有效判定都没有（调用方按整批
    review 兜底，§26 AI_INVALID_OUTPUT → REVIEW，绝不猜）。"""
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        body = text[3:]
        if "```" in body:
            body = body.split("```", 1)[0]
        body = body.strip()
        if body.startswith("json"):
            body = body[4:].strip()
        text = body
    verdicts: dict[int, str] = {}
    try:
        data = json.loads(text)
        if isinstance(data, list):
            for item in data:
                if (isinstance(item, dict) and isinstance(item.get("i"), int)
                        and item.get("v") in _VERDICTS):
                    verdicts[item["i"]] = item["v"]
            if verdicts:
                return verdicts
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    for match in _VERDICT_ITEM.finditer(text):
        verdicts[int(match.group(1))] = match.group(2)
    return verdicts or None


def _cache_read(meta: dict) -> str | None:
    """读版本化缓存（§29/§30）：prompt 版本不匹配即失效。"""
    verdict = meta.get(_MUTED)
    if (isinstance(verdict, dict)
            and verdict.get("pv") == _JUDGE_PROMPT_VERSION
            and verdict.get("v") in _VERDICTS):
        return verdict["v"]
    return None


def _dedup_key(entry: dict, meta: dict, pattern: str) -> tuple:
    """运行内去重键：模型可见的全部证据（原文/译文/形态/语境）。"""
    return (str(entry.get("original") or ""),
            str(entry.get("translation") or ""),
            pattern, _field_leaf(meta), str(meta.get("reason") or ""),
            str(meta.get("script_class") or ""),
            str(meta.get("asset_file") or ""),
            bool(meta.get("obj_has_values")))


def _acquire_service(app_dir) -> tuple[object | None, str]:
    """懒建 ReviewModelService + 在场预检（不触发启动/3 分钟超时）。

    返回 (service, "") 或 (None, 错误摘要)。"""
    if not app_dir:
        return None, "未提供模型目录（app_dir 缺失）"
    try:
        from hanhua.core.review_server import ReviewModelService
        service = ReviewModelService(Path(app_dir).resolve())
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)[:200]
    try:
        spec = service._spec()
        if not spec.is_available:
            return None, f"审核模型缺失：{spec.path}"
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)[:200]
    return service, ""


# ── 主入口 ───────────────────────────────────────────────────────────────

def run_writeback_triage(candidates: list[dict], store=None, *,
                         service=None, app_dir=None, on_log=None,
                         ) -> tuple[dict[tuple[str, str], str],
                                    WritebackTriageReport]:
    """写回分诊主入口：候选池 → 缓存/去重 → 模型批量判定 → 跳过表。

    candidates：write_back_v2 全部待写回候选（write-ready 且
    译文≠原文的行字典）；本函数内部按形态过滤出规则不确定子集。
    store：ProjectStore（缓存落库 update_entry_metas；None = 只判定
    不落库，测试用）。

    返回 (skip_map, report)：skip_map 键 (file_id, key_path) →
    note_logic_reverted 记账 reason（ai_triage_review/reject 前缀）。
    调用方（writer.write_back_v2）把 skip 的条目移出 patch 流并按回退
    记账；allow 条目照写（下游确定性闸门独立复查）。
    """
    report = WritebackTriageReport()
    log = on_log or (lambda _msg: None)

    pool: list[tuple[dict, dict, str]] = []
    for entry in candidates:
        pattern = _triage_pattern_of(entry)
        if pattern:
            pool.append((entry, _meta_of(entry), pattern))
    report.scanned = len(pool)
    if not pool:
        return {}, report

    skip_map: dict[tuple[str, str], str] = {}
    meta_rows: list[tuple[str, str, dict]] = []

    def _apply(entry: dict, verdict: str, suffix: str,
               cacheable: bool) -> None:
        if verdict == "allow":
            report.allowed += 1
        else:
            key = (str(entry.get("file_id") or ""),
                   str(entry.get("key_path") or ""))
            skip_map[key] = f"ai_triage_{verdict}{suffix}"
            if verdict == "review":
                report.review += 1
            else:
                report.rejected += 1
        if cacheable:
            meta_rows.append((str(entry.get("file_id") or ""),
                              str(entry.get("key_path") or ""),
                              {_MUTED: {"v": verdict,
                                        "pv": _JUDGE_PROMPT_VERSION}}))

    # 第一遍：版本化缓存命中（§29）——上轮真实模型结论直接复用（含模型
    # 今天缺席的场景：缓存是当时的真实判定，不翻案）。缓存判定不重写
    # meta（避免无谓写放大）。
    groups: dict[tuple, list[tuple[dict, dict, str]]] = {}
    order: list[tuple] = []
    for entry, meta, pattern in pool:
        cached = _cache_read(meta)
        if cached is not None:
            report.cached += 1
            _apply(entry, cached, ":cached", False)
            continue
        key = _dedup_key(entry, meta, pattern)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((entry, meta, pattern))
    queue = [groups[key][0] for key in order]

    # 第二遍：模型判定。模型不可用 → 分诊层整体不参与（零跳过，候选按
    # 确定性规则照写——§70 回归条款 + Lite 通道行为保持）。模型在场但
    # 批内失败 → 本批 review（fail-closed，§25/§26），连续失败熔断。
    if queue:
        if service is None:
            service, error = _acquire_service(app_dir)
            if error:
                report.degraded = True
                report.error = error
        if service is not None:
            log(f"AI 写回分诊：{report.scanned} 条规则不确定候选"
                f"（去重 {len(queue)} 组，批量 {_BATCH_SIZE}）")
            consecutive_failures = 0
            for offset in range(0, len(queue), _BATCH_SIZE):
                batch = queue[offset:offset + _BATCH_SIZE]
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    # 熔断：模型持续不可用，余下批直接 review 不再请求
                    # （防逐批 120s 超时把写回挂死）
                    for entry, _meta, _pattern in batch:
                        _apply(entry, "review", ":model_error", False)
                    report.degraded = True
                    continue
                report.asked += len(batch)
                try:
                    raw = service.chat(_build_batch_prompt(batch),
                                       max_tokens=512, temperature=0.0)
                except Exception as exc:  # noqa: BLE001
                    consecutive_failures += 1
                    report.degraded = True
                    report.error = str(exc)[:200]
                    for entry, _meta, _pattern in batch:
                        _apply(entry, "review", ":model_error", False)
                    continue
                consecutive_failures = 0
                verdicts = _parse_verdicts(raw)
                if verdicts is None:
                    for entry, _meta, _pattern in batch:
                        _apply(entry, "review", ":invalid_output", False)
                    continue
                for index, (entry, meta, pattern) in enumerate(batch):
                    verdict = verdicts.get(index)
                    if verdict in _VERDICTS:
                        # 同组去重成员共用同一判定（证据完全一致）。
                        # P5b（0.42.1 审计）：只有确定性结论（allow/reject）
                        # 可入缓存——review 是「模型不确定」的保守跳过，
                        # 缓存它会把一次保守判定永久化（后续轮次即使模型
                        # 在场也不再重判，条目永远跳过）；reject 是明确的
                        # 「不该写」结论，复用安全。
                        cacheable = verdict in ("allow", "reject")
                        members = groups.get(_dedup_key(entry, meta, pattern),
                                             [(entry, meta, pattern)])
                        for member, _m, _p in members:
                            _apply(member, verdict, "", cacheable)
                    else:
                        # 单条缺失/非法 → review（不缓存，下次再问）
                        _apply(entry, "review", ":invalid_output", False)

    if meta_rows and store is not None:
        try:
            store.update_entry_metas(meta_rows)
        except Exception:  # noqa: BLE001 缓存落库失败不影响写回主流程
            pass
    return skip_map, report
