"""AI 辅助识别（0.38.0 任务二④）：候选层二次分类——覆盖机制的地基。

识别链路（任务二）的最后一环：提取器把「无字段名证据、无句子形态」的
typetree 字符串放进候选层（kind=typetree_candidate / typetree_prefilter，
status=skipped, role=candidate, confidence=low）。「过滤不是删除」——候选
层一直有证据留档，但没有消费端，等于识别覆盖率被确定性规则封顶（目标
95%+ 无法达成：规则无法判定 'Combat Music' 是按钮还是对象名）。

本模块用本地审核模型（Qwen3.5-4B，复用 ReviewModelService——同实例跨
翻译/审核/识别三链复用，不额外占显存）对候选层做**批量二次分类**：

- **upgrade**：模型判定为玩家可见显示文本 → 升格 pending/display/
  translate + confidence=medium + confidence_promoted=True → 进入翻译
  池（is_actionable_translation 放行，且仍会过 _final_structural_backstop
  无歧义结构终检——宁漏勿坏：AI 判定绝不越过确定性硬闸门）；
- **confirm-skip**：模型判定为键/代码/引擎串 → 维持 skipped，ai_verdict
  留档（下次扫描重复询问同一批是浪费——muted 元数据短路）；
- **fail-closed**：模型缺失/请求失败/解析失败 → 不改任何 meta（宁漏勿坏
  在识别端的体现：AI 故障绝不能把键名升格成译文写坏游戏）。

安全契约（防止 AI 误识别破坏游戏）：
1. 只处理 kind∈{typetree_candidate, typetree_prefilter} 且 role=candidate
   且 status=skipped 的条目——display/prefilter display 层永不重判；
2. 升格前本地预校验 is_key_style_identifier——键风格标识符（ui_newGame/
   MENU_PLAY）模型再怎么判 display 也不放行（模型幻觉防线）；
3. 升格后再次用 is_actionable_translation 终检——任何一项不满足
   （含 _final_structural_backstop 命中）就不写回升级；
4. muted（ai_verdict 已有结论的条目）不再询问，除非 force_rescan。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from hanhua.core.models import (
    STATUS_PENDING,
    STATUS_SKIPPED,
    TextEntry,
    entry_from_row,
    is_actionable_translation,
)
from hanhua.core.placeholders import is_key_style_identifier

# 候选层 kind 白名单：只有这两个 kind 是提取器标记的「低置信候选」。
# 其他 skipped（引擎串 il2cpp_sentence、prefilter 键样本等）是确定性
# 判定，AI 无权推翻（不误识别 > 不漏识别的底线）。
# N2 结论（hickory 全量普查实证）：rawstr skipped 中带显示证据的表面
# （script_class_config / prefilter_engine_string / prefilter_key_
# identifier 且 obj_has_values）几乎全是正确跳过的确定性证据——输入绑定
# '<Gamepad>/leftStick'、URP 混合模式、TMP 样式表名、GUID、版本号；唯一
# 真漏网是对话拟声词 'tck – er…'（B17，已在确定性闸门用 _INTERJECTION_
# WORDS 根治）。扩大 AI 收集面只会引入噪声，白名单保持收窄。
_CANDIDATE_KINDS = frozenset({"typetree_candidate", "typetree_prefilter"})

# 单次批量分类条数上限：Qwen3.5-4B ctx 8192 预算——识别 prompt 固定
# 部分 ≈300 token，每条目 ≤120 token（索引+原文截断 120 字符），40 条
# ≈5k 输出 JSON ≈40×15=600 token，安全余量 >2k。与审校 review_batch_
# size=20 的平衡点同理（>50 输出数组漏条目概率上升）。
_BATCH_SIZE = 40

# 单条原文进 prompt 的最大字符数（超长候选几乎必然是文档/脚本正文，
# 截断不影响分类判定；写回仍用完整原文）
_PROMPT_TEXT_LIMIT = 120

# 单游戏单轮处理的候选总量上限：候选层在大型游戏可达数万条，全量
# 询问既慢又稀释低信噪比批次。按「可疑度排序取头部」（见 _rank_candidates）
# 只问最可能漏的——上限内覆盖绝大多数真实显示文本。
_MAX_CANDIDATES_PER_RUN = 2000

# muted 短路：ai_verdict 已判定过的条目不再询问
_MUTED = "ai_verdict"

# verdict 常量
VERDICT_DISPLAY = "display"
VERDICT_STRUCTURAL = "structural"

# N3：文本类型维度——模型在判定 display 时顺带标注文本用途，落库到
# meta.ai_text_type，供翻译端语境注入（对话/任务文本用叙事语气提示）
# 与审计端复核口径使用。判不出/structural 时缺省。
VERDICT_TYPES = ("dialogue", "ui", "quest", "item", "menu", "system")

# N1：单条语境元素进 prompt 的最大长度（field_path 叶子名/类名/文件名
# 都是短 token，40 字符截断只是防御性上限）
_CONTEXT_LIMIT = 40


@dataclass
class AiRecognitionReport:
    """单轮 AI 辅助识别汇总（写日志/报告用，fail-closed 不抛异常）。"""
    scanned: int = 0          # 进入本轮的候选条数
    asked: int = 0            # 实际送模型判定的条数（去 muted/预校验后）
    upgraded: int = 0         # 判 display 且通过预校验+终检、已升格的条数
    confirmed_skipped: int = 0  # 模型确认 skip 的条数
    precheck_blocked: int = 0   # 模型判 display 但键风格预校验拦下的条数
    degraded: bool = False      # 模型不可用/请求失败（fail-closed 空转）
    error: str = ""

    def summary(self) -> str:
        if self.degraded:
            return f"AI 辅助识别不可用：{self.error}" if self.error \
                else "AI 辅助识别不可用（模型缺失或启动失败）"
        return (f"AI 辅助识别：候选 {self.scanned} 条，判定 {self.asked} 条，"
                f"升格 {self.upgraded} 条，确认跳过 {self.confirmed_skipped} 条"
                + (f"，键风格拦截 {self.precheck_blocked} 条"
                   if self.precheck_blocked else ""))


# ── 候选收集 ─────────────────────────────────────────────────────────────

def _rank_key(entry: TextEntry) -> tuple:
    """可疑度排序：display 证据越弱越靠前。

    - obj_has_values=False 的候选（所在对象无任何值证据）优先——真 UI
      文本所在对象通常有其他 display 叶子，已随对象升格；孤条候选漏网
      概率最高；
    - 原文含空格（词组形态）优先——单词/短 token 是键的概率远高于词组。
    """
    has_values = bool(entry.meta.get("obj_has_values"))
    spaced = " " in (entry.original or "").strip()
    return (has_values, not spaced, entry.key_path)


def collect_candidates(rows: list[dict], *, limit: int = _MAX_CANDIDATES_PER_RUN,
                       ) -> list[TextEntry]:
    """从 store 行中筛出 AI 可判定的候选条目（排序 + 截断）。

    只收：meta.kind ∈ _CANDIDATE_KINDS ∧ role=candidate ∧ status=skipped
    ∧ 未 muted（meta.ai_verdict 缺失）∧ 原文非空。
    """
    picked: list[TextEntry] = []
    for row in rows:
        if row.get("status") != STATUS_SKIPPED:
            continue
        meta = row.get("meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
        if not isinstance(meta, dict):
            continue
        if meta.get("kind") not in _CANDIDATE_KINDS:
            continue
        if meta.get("role") != "candidate":
            continue
        if meta.get(_MUTED):
            continue
        entry = entry_from_row(row)
        if not (entry.original or "").strip():
            continue
        picked.append(entry)
    picked.sort(key=_rank_key)
    return picked[:max(0, int(limit))]


# ── 模型判定 ─────────────────────────────────────────────────────────────

def _item_context(entry: TextEntry) -> str:
    """N1：从 meta 提取确定性语境描述（提取器已留档的证据，零模型成本）。

    语境来源（只取确定性事实，不做推断）：
    - field_path 叶子名（'roomName' → 字段 roomName）——对象字段名是
      最强信号：hickory '2F' 在 roomName 字段下是楼层名，在 m_text 下
      就是显示文本；
    - obj_has_values（对象内是否有其他 display 证据）——真 UI 文本所在
      对象通常有其他 display 叶子；
    - script_class / asset_file（类名/资产文件名）——InputActionAsset
      下的串大概率是输入绑定，CombatMusic.asset 下大概率是音频事件名。
    """
    parts: list[str] = []
    field_path = entry.meta.get("field_path") or []
    if isinstance(field_path, list):
        leaf = next((str(p) for p in reversed(field_path)
                     if isinstance(p, str) and not str(p).isdigit()), "")
        if leaf:
            parts.append(f"字段 {leaf[:_CONTEXT_LIMIT]}")
    if entry.meta.get("obj_has_values"):
        parts.append("对象含其他显示文本")
    else:
        parts.append("对象无其他显示文本")
    script_class = str(entry.meta.get("script_class") or "").strip()
    if script_class:
        parts.append(f"类 {script_class[:_CONTEXT_LIMIT]}")
    asset_file = str(entry.meta.get("asset_file") or "").strip()
    if asset_file:
        parts.append(f"文件 {asset_file[:_CONTEXT_LIMIT]}")
    return "；".join(parts)


def _build_batch_prompt(items: list[TextEntry]) -> str:
    """批量分类 prompt：JSON 数组输出（索引对应），两级判定 + 类型标注。

    判定标准写给 4B 模型要具体：显示文本 = 玩家在界面/对话/物品栏能
    看到的词句；结构 = 键名/路径/ID/代码/引擎内部串。宁可判 structural
    也不要把键判成 display（宁漏勿坏方向性引导——错误升级会写坏游戏，
    错误跳过只是漏翻一条）。

    N1：每条附确定性语境（字段名/对象内显示证据/类名/资产文件——见
    _item_context）。语境只是证据提示，判定权仍在模型，且本地预校验
    （is_key_style_identifier + is_actionable_translation）继续兜底。
    """
    lines = [
        "你是 Unity 游戏文本识别器。判断下列字符串是「显示文本」还是「结构串」。",
        "显示文本(display)：玩家界面/对话/物品栏/任务/设置里看到的词句"
        "（如 Combat Music、Open the File、Settings、Health Potion）。",
        "结构串(structural)：键名、对象名、资源路径、ID、代码标识符、"
        "枚举值、类名、文件名、格式串、引擎内部串"
        "（如 ui_newGame、MENU_PLAY、Player prefab、Canvas/HUD、icon_sword_01）。",
        "每条附有提取现场的语境（字段名/对象内显示证据/类名/文件名），"
        "只作参考证据：字段名像文本字段（text/label/roomName/dialogue 等）"
        "倾向 display，像技术字段（path/id/key/GUID 等）倾向 structural。",
        "若判 display，再标注文本类型 t（dialogue=对话/ui=界面文字/"
        "quest=任务/item=物品/menu=菜单项/system=系统提示），判不出省略 t。",
        "拿不准时判 structural。只输出 JSON 数组，每项 {\"i\": 索引, \"v\": "
        "\"display\" 或 \"structural\", \"t\": 类型(可选)}，不要输出其他内容。",
        "",
        "字符串列表：",
    ]
    for index, entry in enumerate(items):
        text = (entry.original or "").strip()
        if len(text) > _PROMPT_TEXT_LIMIT:
            text = text[:_PROMPT_TEXT_LIMIT] + "…"
        context = _item_context(entry)
        if context:
            lines.append(f"{index}. {text!r}（{context}）")
        else:
            lines.append(f"{index}. {text!r}")
    return "\n".join(lines)


# N3：verdict 项解析——v 必有，t 可选（模型省略/乱写时忽略）
_VERDICT_ITEM = re.compile(r"\{\s*\"i\"\s*:\s*(\d+)\s*,\s*\"v\"\s*:\s*"
                           r"\"(display|structural)\""
                           r"(?:\s*,\s*\"t\"\s*:\s*\"([a-z_]{1,20})\")?"
                           r"\s*\}")


def _parse_verdicts(raw: str) -> dict[int, dict]:
    """解析模型 JSON 数组输出 → {索引: {"v": verdict, "t": 类型(可选)}}。

    容错同 _parse_result：剥 ``` 围栏；JSON 整体失败时退化为逐项正则
    抽取（4B 模型偶发截断/夹带说明文字，逐项抽取能保住已判条目）。
    解析不出的条目静默丢弃（保持原 skipped 状态，fail-closed）。
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
    verdicts: dict[int, dict] = {}

    def _accept(index: int, verdict: str, text_type: str | None) -> None:
        item: dict = {"v": verdict}
        if (text_type and text_type in VERDICT_TYPES):
            item["t"] = text_type
        verdicts[index] = item

    try:
        data = json.loads(text)
        if isinstance(data, list):
            for item in data:
                if (isinstance(item, dict)
                        and isinstance(item.get("i"), int)
                        and item.get("v") in (VERDICT_DISPLAY,
                                              VERDICT_STRUCTURAL)):
                    t = item.get("t")
                    _accept(item["i"], item["v"],
                            t if isinstance(t, str) else None)
            return verdicts
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    for match in _VERDICT_ITEM.finditer(text):
        _accept(int(match.group(1)), match.group(2), match.group(3))
    return verdicts


def _classify_batch(service, items: list[TextEntry]) -> dict[int, str] | None:
    """单批判定。任何异常 → None（fail-closed：本批维持原状）。"""
    try:
        raw = service.chat(
            _build_batch_prompt(items), max_tokens=2048, temperature=0.0)
    except Exception:  # noqa: BLE001 传输/启动失败不改变任何 meta
        return None
    if not raw:
        return None
    return _parse_verdicts(raw) or None


# ── 升格/落库 ────────────────────────────────────────────────────────────

def _upgrade_meta(entry: TextEntry, note: dict) -> dict:
    """升格 meta：满足 is_actionable_translation 的最小字段集 + 留痕。

    role=display + disposition=translate（authoritative 分支）+
    confidence=medium + confidence_promoted=True（rescan 的 quality_keys
    会保留 promoted，但 role/disposition 会被提取器重新断言——
    提取层形态判定升级时以提取层为准，这是预期行为）。
    kind 改为 ai_upgraded 防止下轮重复收进候选池（muted 同效）。
    """
    meta = {
        "kind": "ai_upgraded",
        "role": "display",
        "disposition": "translate",
        "confidence": "medium",
        "confidence_promoted": True,
        _MUTED: VERDICT_DISPLAY,
        "ai_verdict_source": "typetree_candidate",
        "ai_note": note,
    }
    # 保留原 field_path/obj/asset_file 写回定位信息（写回按 key_path，
    # 这些 meta 是审计链）
    for key in ("field_path", "obj", "asset_file", "obj_has_values",
                "tmp_tag_refs"):
        if key in entry.meta:
            meta[key] = entry.meta[key]
    return meta


def _verify_upgradeable(entry: TextEntry) -> bool:
    """升格预校验：模型判 display 后、写库前的确定性闸门。

    1. is_key_style_identifier —— 键风格标识符模型无权推翻（写回
       immutable_field_protected 反正会拦，但提前拦省一次翻译）；
    2. is_actionable_translation 终检（含 _final_structural_backstop 的
       URL/路径/GUID/程序集名/引擎串判定）——构造升格后的假想条目
       判定，任何命中都不落库。
    """
    if is_key_style_identifier(entry.original or ""):
        return False
    upgraded = TextEntry(
        file_id=entry.file_id, key_path=entry.key_path,
        original=entry.original, status=STATUS_PENDING,
        meta={**entry.meta, "role": "display",
              "disposition": "translate", "confidence": "medium"})
    return is_actionable_translation(upgraded)


@dataclass
class _PendingWrites:
    """单轮落库缓冲（meta 合并 + 状态翻转分开写，见 update_entry_metas）。"""
    meta_rows: list[tuple[str, str, dict]] = field(default_factory=list)
    status_rows: list[tuple[str, str]] = field(default_factory=list)
    upgraded: int = 0
    confirmed: int = 0
    precheck_blocked: int = 0


def _apply_verdicts(items: list[TextEntry], verdicts: dict[int, dict],
                    pending: _PendingWrites) -> None:
    """把一批 verdict 转成落库缓冲（只缓冲，不直接写——单 commit 批量）。"""
    for index, entry in enumerate(items):
        verdict_item = verdicts.get(index)
        if verdict_item is None:
            continue  # 模型漏判：维持原状（宁漏勿坏）
        verdict = verdict_item.get("v")
        if verdict == VERDICT_DISPLAY:
            if not _verify_upgradeable(entry):
                # 键风格/结构终检拦下：记录 muted 防重复询问，状态不动
                pending.meta_rows.append((entry.file_id, entry.key_path, {
                    _MUTED: VERDICT_STRUCTURAL,
                    "ai_precheck": "key_style_or_structural",
                }))
                pending.precheck_blocked += 1
                continue
            pending.meta_rows.append((
                entry.file_id, entry.key_path,
                _upgrade_meta(entry, {"ai_model_verdict": VERDICT_DISPLAY,
                                      "ai_text_type":
                                          verdict_item.get("t") or ""})))
            pending.status_rows.append((entry.file_id, entry.key_path,
                                        STATUS_PENDING))
            pending.upgraded += 1
        else:
            pending.meta_rows.append((entry.file_id, entry.key_path, {
                _MUTED: VERDICT_STRUCTURAL}))
            pending.confirmed += 1


def run_ai_recognition(store, app_dir, *, on_log=None,
                       limit: int = _MAX_CANDIDATES_PER_RUN,
                       service=None) -> AiRecognitionReport:
    """主入口：候选层 → 模型二次分类 → 升格/确认落库。

    store: ProjectStore（get_entries/update_entry_metas/set_status）；
    app_dir: 模型资源根（ReviewModelService 定位 models/）；
    service: 注入口（测试用 _FakeService）；缺省懒建 ReviewModelService，
    模型缺失/启动失败 → degraded=True，不改任何 meta。

    批次循环内逐批落库（中断只损失未落批次，已升格条目立即生效）。
    """
    report = AiRecognitionReport()
    log = on_log or (lambda _msg: None)
    rows = store.get_entries()
    candidates = collect_candidates(rows, limit=limit)
    report.scanned = len(candidates)
    if not candidates:
        return report

    if service is None:
        try:
            from hanhua.core.review_server import ReviewModelService
            service = ReviewModelService(Path(app_dir).resolve())
        except Exception as exc:  # noqa: BLE001
            report.degraded = True
            report.error = str(exc)[:200]
            return report
        # 模型文件缺失直接放弃（不触发 3 分钟启动超时）——识别是
        # 增益环节，不值得阻塞主流程
        try:
            spec = service._spec()
            if not spec.is_available:
                report.degraded = True
                report.error = f"审核模型缺失：{spec.path}"
                return report
        except Exception as exc:  # noqa: BLE001
            report.degraded = True
            report.error = str(exc)[:200]
            return report

    log(f"AI 辅助识别：{len(candidates)} 条候选进入二次分类"
        f"（批量 {_BATCH_SIZE}）")
    for offset in range(0, len(candidates), _BATCH_SIZE):
        batch = candidates[offset:offset + _BATCH_SIZE]
        report.asked += len(batch)
        verdicts = _classify_batch(service, batch)
        if verdicts is None:
            # fail-closed：本批不改任何 meta，继续下一批（单批失败多半
            # 是偶发传输错误；整体不可用在循环里自然全部失败——可接受，
            # 因为每批只有一次轻量请求，无启动风暴：ensure_running 幂等）
            continue
        pending = _PendingWrites()
        _apply_verdicts(batch, verdicts, pending)
        if pending.meta_rows:
            store.update_entry_metas(pending.meta_rows)
        if pending.status_rows:
            for file_id, key_path, status in pending.status_rows:
                store.set_status(file_id, key_path, status)
        report.upgraded += pending.upgraded
        report.confirmed_skipped += pending.confirmed
        report.precheck_blocked += pending.precheck_blocked
        if pending.upgraded:
            log(f"AI 辅助识别：本批升格 {pending.upgraded} 条"
                f"（累计 {report.upgraded}）")
    return report
