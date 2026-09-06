"""语义审核器（reviewer.py）测试（2026-08-13 本地四级化后重写）。

覆盖：本地 4B 逐条审核调用、JSON 解析容错、无服务失败降级、
四级构造兼容（旧二值 verdict 映射）、术语词对提取（沉淀输入）。

注：原测试测云端 API（requests.post 到 api.deepseek.com）——云端
审核已按执行指令从代码删除，测试同步改为本地 llama.cpp 服务语义
（服务实例注入 fake，不真实启动）。
"""

from pathlib import Path

from hanhua.core.models import TextEntry
from hanhua.core.reviewer import (ReviewItem, ReviewResult, SemanticReviewer,
                                  _REVIEW_SYSTEM_PROMPT, _build_item_prompt,
                                  _parse_result, extract_term_pairs,
                                  _reason_claims_negation_dropped,
                                  _translation_dropped_negation)


class _FakeService:
    """假的本地审核服务：按输入返回预设 content。"""

    def __init__(self, outputs=None, error=None):
        self.outputs = list(outputs or [])
        self.error = error
        self.prompts = []
        self.max_tokens_calls = []

    def chat(self, prompt, *, max_tokens=1024, temperature=0.1,
             timeout=120.0):
        self.prompts.append(prompt)
        self.max_tokens_calls.append(max_tokens)
        if self.error is not None:
            raise self.error
        return self.outputs.pop(0) if len(self.outputs) > 1 \
            else self.outputs[0]

    @property
    def usable(self) -> bool:
        return True


def _make_reviewer(service=None):
    return SemanticReviewer(service=service or _FakeService(
        outputs=['{"level": "PASS", "reason": "正确"}']))


def test_reviewer_usable_with_service():
    reviewer = _make_reviewer()
    assert reviewer.usable is True


def test_build_item_prompt_contains_all_fields():
    """单条 prompt 包含类型/原文/译文与四级定义与 JSON 要求。"""
    item = ReviewItem(entry_id="a1", original="Resume", translation="继续",
                      text_type="按钮")
    prompt = _build_item_prompt(item)
    assert "类型：按钮" in prompt
    assert "原文：Resume" in prompt
    assert "译文：继续" in prompt
    assert "PASS|MINOR|MAJOR|CRITICAL" in prompt
    assert "resume" in prompt.casefold()


def test_review_result_needs_optimization():
    """flag（旧二值）→ needs_optimization=True（映射 MAJOR）；pass → False。"""
    assert ReviewResult("1", verdict="flag").needs_optimization is True
    assert ReviewResult("1", verdict="flag").level == "MAJOR"
    assert ReviewResult("2").needs_optimization is False
    assert ReviewResult("3", level="CRITICAL").needs_optimization is True
    assert ReviewResult("4", level="MINOR").needs_optimization is False


def test_review_batch_parses_level_json():
    """本地服务返回四级 JSON → 解析为 ReviewResult；未覆盖条目保守缺失。"""
    service = _FakeService(outputs=[
        '{"level": "CRITICAL", "reason": "Resume 在 UI 语境是继续", '
        '"issues": [{"type": "术语错误", "detail": "简历误译", '
        '"suggestion": "继续"}]}',
        '{"level": "PASS", "reason": "正确"}',
    ])
    reviewer = _make_reviewer(service)
    items = [
        ReviewItem(entry_id="1", original="Resume", translation="简历",
                   text_type="按钮"),
        ReviewItem(entry_id="2", original="Start Game", translation="开始游戏",
                   text_type="按钮"),
    ]
    results, cancelled = reviewer.review_batch(items)
    assert cancelled == 0
    assert len(results) == 2
    assert results["1"].level == "CRITICAL"
    assert results["1"].issue == "术语错误"
    assert results["1"].suggestion == "继续"
    assert results["1"].needs_optimization is True
    assert results["2"].level == "PASS"
    assert results["2"].needs_optimization is False
    assert service.prompts[0].startswith("你是游戏本地化质量审核员")


def test_review_batch_handles_service_failure():
    """服务异常 → 显式 TRANSPORT_ERROR 错误结果（fail-closed，不伪装 pass）。"""
    reviewer = _make_reviewer(_FakeService(error=RuntimeError("网络故障")))
    items = [ReviewItem(entry_id="1", original="Hi", translation="你好")]
    results, cancelled = reviewer.review_batch(items)
    assert cancelled == 0
    assert len(results) == 1
    assert results["1"].is_error
    assert results["1"].error == "TRANSPORT_ERROR"
    assert results["1"].reviewed is False


def test_review_batch_handles_non_json_output():
    """服务返回非 JSON → 显式 PARSE_ERROR（不得伪装成「没有发现问题」）。"""
    reviewer = _make_reviewer(_FakeService(outputs=["一段普通文本"]))
    items = [ReviewItem(entry_id="1", original="Hi", translation="你好")]
    results, cancelled = reviewer.review_batch(items)
    assert cancelled == 0
    assert len(results) == 1
    assert results["1"].is_error
    assert results["1"].error == "PARSE_ERROR"
    assert results["1"].reviewed is False


def test_review_batch_cancellation_returns_cancelled_count():
    """取消事件触发 → 剩余条目计入 cancelled_count（取消是显式终态，
    不得归入 error 或 pass）。"""
    import threading
    reviewer = _make_reviewer(_FakeService(
        outputs=['{"level": "PASS", "reason": "正确"}']))
    items = [ReviewItem(entry_id=str(i), original=f"text{i}",
                        translation=f"译文{i}") for i in range(5)]
    evt = threading.Event()
    evt.set()   # 预先置位 → 整批都应计入 cancelled
    results, cancelled = reviewer.review_batch(items, cancellation_event=evt)
    assert cancelled == len(items)
    assert results == {}


def test_extract_term_pairs():
    """术语类 flag + 建议含英文原词与中文 → 提取词对（知识库沉淀）。"""
    results = [
        ReviewResult("1", level="MAJOR", issue="术语错误",
                     suggestion="Resume→继续"),
        ReviewResult("2", level="CRITICAL", issue="术语错误",
                     suggestion="Resume→继续"),
        ReviewResult("3", level="MAJOR", issue="语境不当",
                     suggestion="你好呀"),
        ReviewResult("4", level="PASS"),
    ]
    pairs = extract_term_pairs(results)
    assert ("Resume", "继续") in pairs
    # 语境不当不是术语类 → 不入词对
    assert len(pairs) == 2


# ── #43 阶段 E：十维审校（重构指令 §10 / §16 知识优先级链） ─────────

def test_system_prompt_has_ten_dimensions():
    """审核系统 prompt 含十维（含幻觉/自然度/歧义/机翻痕迹）。"""
    for dim in ("语义准确", "游戏语境", "术语一致", "自然度", "风格",
                "完整性", "幻觉", "结构完整", "歧义", "机翻痕迹",
                "原文低质量"):
        assert dim in _REVIEW_SYSTEM_PROMPT
    assert "overall_score" in _REVIEW_SYSTEM_PROMPT   # JSON 契约扩展
    assert "dimensions" in _REVIEW_SYSTEM_PROMPT


def test_system_prompt_terms_include_disabled_enabled():
    """术语一致维度含 Disabled=已禁用 / Enabled=已启用 权威译名（2026-08-31
    用户实证「Disabled 残疾人士 vs 已禁用」：缺权威译名时 4B 对垃圾译文
    「残疾人士」幻觉式 PASS）。"""
    assert "Disabled=已禁用" in _REVIEW_SYSTEM_PROMPT
    assert "Enabled=已启用" in _REVIEW_SYSTEM_PROMPT
    assert "残疾人士" in _REVIEW_SYSTEM_PROMPT  # 明确的反例
    assert "已启动" in _REVIEW_SYSTEM_PROMPT     # Enabled 反例


def test_system_prompt_has_low_quality_worked_examples():
    """维度 11 带低质量原文具体示例（come-back 实证：4B 只读抽象规则仍
    把 not only 强调句当否定漏译）。"""
    assert "i cant start playing my games if im not consumed the consume" in _REVIEW_SYSTEM_PROMPT
    assert "not only does flex taps powerful adhesive hold the mountain up" in _REVIEW_SYSTEM_PROMPT
    assert "宽普钦" in _REVIEW_SYSTEM_PROMPT
    assert "敌人会掉落金币" in _REVIEW_SYSTEM_PROMPT   # 真否定漏译反例


def test_negation_dropped_verifiable_claim():
    """「否定漏译/语义相反」是可验证主张：not only 强调句的 not 不是
    否定（译文「不仅」已传达）；原文真否定 + 译文无中文否定才成立。"""
    # 4B 罐头理由：not only 强调句被当「否定句漏译否定词」
    assert _reason_claims_negation_dropped(
        "原文含否定词 not，译文完全缺失导致语义相反；应补回'不'字")
    assert _reason_claims_negation_dropped(
        "原文为否定句（not only...but also...），译文漏译否定词，语义相反")
    # 非否定类理由不触发
    assert not _reason_claims_negation_dropped(
        "原文 consume 为动词原形，译文误作名词")
    # not only 强调句：not 被剥离后无否定 → 非真漏译
    assert not _translation_dropped_negation(
        "not only does flex taps powerful adhesive hold the mountain up",
        "不仅柔韧胶带凭借强力粘合固定了山脉")
    # 译文已带「不」→ 非真漏译
    assert not _translation_dropped_negation(
        "you must not enter", "你绝不能进入")
    # 原文真否定 + 译文无中文否定 → 真漏译（不可重审豁免）
    assert _translation_dropped_negation(
        "the enemy does not drop any gold", "敌人会掉落金币")


def test_parse_result_ten_dimension_fields():
    """十维 JSON（overall_score + dimensions）→ ReviewResult 新字段。"""
    r = _parse_result(
        '{"level": "MAJOR", "overall_score": 62, '
        '"dimensions": {"语义准确": 90, "自然度": 55, "术语一致": 80}, '
        '"reason": "翻译腔重", '
        '"issues": [{"type": "机翻痕迹", "detail": "语序直译", '
        '"suggestion": "地道表达"}]}', "e0")
    assert r.overall_score == 62
    assert r.dimensions == {"语义准确": 90, "自然度": 55, "术语一致": 80}
    assert r.level == "MAJOR"
    assert r.issue == "机翻痕迹"


def test_parse_result_legacy_json_compat():
    """旧模型输出（无新字段）→ overall_score=0 / dimensions={}（零破坏）。"""
    r = _parse_result('{"level": "PASS", "reason": "正确"}', "e0")
    assert r.overall_score == 0
    assert r.dimensions == {}
    assert r.level == "PASS"


def test_parse_result_score_clamped_and_bad_types_ignored():
    """越界分截断 0-100；非数值/非 dict 类型安全忽略。"""
    r = _parse_result(
        '{"level": "PASS", "overall_score": 250, "dimensions": {"a": "x"}}',
        "e0")
    assert r.overall_score == 100
    assert r.dimensions == {}
    r2 = _parse_result('{"level": "PASS", "overall_score": "高"}', "e0")
    assert r2.overall_score == 0


def test_review_result_dimension_defaults():
    """ReviewResult 默认 overall_score=0 / dimensions={}（构造兼容）。"""
    r = ReviewResult("1", level="PASS")
    assert r.overall_score == 0
    assert r.dimensions == {}


def test_build_item_prompt_injects_hints():
    """术语参考 + 语境参考注入 prompt；旧调用（无 hint）不注入。"""
    item = ReviewItem(entry_id="a1", original="Resume", translation="继续",
                      text_type="按钮", term_hint="Resume=继续；Save=保存",
                      context_hint="「继续」(context_exact, 置信 0.90)")
    prompt = _build_item_prompt(item)
    assert "术语参考：Resume=继续；Save=保存" in prompt
    assert "语境参考：「继续」(context_exact, 置信 0.90)" in prompt
    legacy = _build_item_prompt(ReviewItem(
        entry_id="a2", original="Hi", translation="你好"))
    # 系统 prompt 维度 3 提到「术语参考/语境参考」字样，注入形态以冒号区分
    assert "术语参考：" not in legacy
    assert "语境参考：" not in legacy


def test_review_batch_ten_dimension_end_to_end():
    """端到端：十维 JSON 输出 → 评分/维度随结果透出。"""
    service = _FakeService(outputs=[
        '{"level": "MINOR", "overall_score": 88, '
        '"dimensions": {"语义准确": 95, "自然度": 80}, '
        '"reason": "略有翻译腔"}'])
    reviewer = _make_reviewer(service)
    items = [ReviewItem(entry_id="1", original="Resume", translation="继续",
                        text_type="按钮", term_hint="Resume=继续")]
    results, cancelled = reviewer.review_batch(items)
    assert cancelled == 0
    assert results["1"].overall_score == 88
    assert results["1"].dimensions["自然度"] == 80
    assert results["1"].level == "MINOR"
    # hint 已进送审 prompt（知识优先级链注入生效）
    assert "术语参考：Resume=继续" in service.prompts[0]


# ── 批量审核（2026-08-14 全量送审提速：一次给多条，缺失/坏条目逐条兜底） ──

def test_review_batch_grouped_parses_array():
    """batch_size>1 → 组批一次 chat，解析 JSON 数组（与条目一一对应）。"""
    from hanhua.core.reviewer import ReviewConfig, _build_batch_prompt
    service = _FakeService(outputs=['''
        [{"entry_id": "1", "level": "CRITICAL", "reason": "否定被吞",
          "overall_score": 30,
          "issues": [{"type": "语义错误", "detail": "not 被吞",
                      "suggestion": "不是"}]},
         {"entry_id": "2", "level": "PASS", "reason": "正确",
          "overall_score": 95},
         {"entry_id": "3", "level": "MAJOR", "reason": "术语误用",
          "overall_score": 70}]
    '''])
    reviewer = SemanticReviewer(
        service=service, config=ReviewConfig(batch_size=3))
    items = [ReviewItem(entry_id=str(i), original=f"text{i}",
                        translation=f"译文{i}") for i in (1, 2, 3)]
    results, cancelled = reviewer.review_batch(items)
    assert cancelled == 0
    assert len(results) == 3
    assert results["1"].level == "CRITICAL"
    assert results["1"].overall_score == 30
    assert results["1"].needs_optimization is True
    assert results["2"].level == "PASS"
    assert results["3"].level == "MAJOR"
    assert len(service.prompts) == 1                 # 只发一次请求（组批）
    assert "### 条目 1" in service.prompts[0]
    assert "### 条目 3" in service.prompts[0]
    batch = _build_batch_prompt(items)
    assert "JSON 数组" in batch                       # 数组输出要求
    assert "输出严格 JSON 对象" not in batch.split("本次一次给出")[0]


def test_review_batch_grouped_missing_entry_falls_back_per_item():
    """组批数组缺条目 → 缺失条目逐条兜底（降级不降质，不伪装 PASS）。"""
    from hanhua.core.reviewer import ReviewConfig
    # 第一次调用（组批）：返回数组只含 1 号；后续逐条兜底返回单对象
    service = _FakeService(outputs=[
        '[{"entry_id": "1", "level": "PASS", "reason": "正确"}]',
        '{"entry_id": "2", "level": "MAJOR", "reason": "翻译腔"}',
    ])
    reviewer = SemanticReviewer(
        service=service, config=ReviewConfig(batch_size=3))
    items = [ReviewItem(entry_id=str(i), original=f"text{i}",
                        translation=f"译文{i}") for i in (1, 2)]
    results, cancelled = reviewer.review_batch(items)
    assert cancelled == 0
    assert len(results) == 2
    assert results["1"].level == "PASS"
    assert results["2"].level == "MAJOR"             # 兜底判定成功
    assert len(service.prompts) == 2                 # 1 组批 + 1 兜底


def test_review_batch_grouped_array_parse_failure_all_fallback():
    """组批输出非数组（模型输出单对象/乱码）→ 全部逐条兜底。"""
    from hanhua.core.reviewer import ReviewConfig
    service = _FakeService(outputs=[
        '{"level": "PASS", "reason": "旧格式单对象"}',          # 组批调用（非数组 → 全组兜底）
        '{"level": "PASS", "reason": "逐条兜底判定"}',
        '{"level": "CRITICAL", "reason": "逐条兜底判定2"}',
        '{"level": "MINOR", "reason": "逐条兜底判定3"}',
    ])
    reviewer = SemanticReviewer(
        service=service, config=ReviewConfig(batch_size=3))
    items = [ReviewItem(entry_id=str(i), original=f"text{i}",
                        translation=f"译文{i}") for i in (1, 2, 3)]
    results, cancelled = reviewer.review_batch(items)
    assert cancelled == 0
    assert len(results) == 3
    assert results["1"].level == "PASS"              # 兜底单对象解析成功
    assert results["2"].level == "CRITICAL"
    assert results["3"].level == "MINOR"
    assert len(service.prompts) == 4                 # 1 组批 + 3 兜底


def test_review_batch_grouped_progress_and_cancel():
    """组批进度按组回调；取消时剩余组计入 cancelled_count。"""
    import threading
    from hanhua.core.reviewer import ReviewConfig
    service = _FakeService(outputs=[
        '[{"entry_id": "1", "level": "PASS", "reason": "正确"}, '
        '{"entry_id": "2", "level": "PASS", "reason": "正确"}]',
    ])
    reviewer = SemanticReviewer(
        service=service, config=ReviewConfig(batch_size=2))
    items = [ReviewItem(entry_id=str(i), original=f"text{i}",
                        translation=f"译文{i}") for i in (1, 2, 3, 4)]
    seen = []
    evt = threading.Event()
    evt.set()   # 预先置位 → 第一组也应全部计入 cancelled（组前检查）
    results, cancelled = reviewer.review_batch(
        items, on_progress=lambda d, t: seen.append((d, t)),
        cancellation_event=evt)
    assert cancelled == 4
    assert results == {}
    assert seen == []


def test_review_batch_grouped_batch_size_one_unchanged():
    """batch_size=1 → 逐条路径（旧版行为不变，调用次数 = 条目数）。"""
    from hanhua.core.reviewer import ReviewConfig
    service = _FakeService(outputs=[
        '{"level": "PASS", "reason": "正确"}',
        '{"level": "MINOR", "reason": "语序"}',
    ])
    reviewer = SemanticReviewer(
        service=service, config=ReviewConfig(batch_size=1))
    items = [ReviewItem(entry_id=str(i), original=f"text{i}",
                        translation=f"译文{i}") for i in (1, 2)]
    results, cancelled = reviewer.review_batch(items)
    assert cancelled == 0
    assert len(results) == 2
    assert len(service.prompts) == 2                 # 逐条 2 次请求
    assert all(p.startswith("你是游戏本地化质量审核员") for p in service.prompts)


# ── 2026-08-14 二次提速：输出精简 + 预算拆组 + max_tokens 收紧 ──

def test_review_prompt_trimmed_fields_and_shorter_cutoffs():
    """输出要求精简为 level+reason；短文本截断 220、术语 120。

    提速依据：20 条 × (600+600+400 字符) ≈ 万级 token 超 ctx 8192 →
    llama-server 静默截断 prompt 尾部 → 后半批输出缺失 → 逐条兜底
    （每条 10-30s）——「半分钟一批」的真凶；输出每项多出 score/issues/
    suggestion ≈ 50-150 token × 20 条，4B 生成它们要几十秒。
    """
    from hanhua.core.reviewer import (
        _REVIEW_BATCH_OUTPUT, _REVIEW_SYSTEM_PROMPT, _build_batch_prompt,
        _build_item_prompt)
    # 系统/批量输出要求不再要求旧臃肿字段（解析器仍兼容旧模型输出；
    # 兼容说明会提到字段名，故断言「要求格式」而非字段名不存在）
    assert '"overall_score": 0-100' not in _REVIEW_SYSTEM_PROMPT
    assert '"dimensions"' not in _REVIEW_BATCH_OUTPUT
    assert '"issues"' not in _REVIEW_BATCH_OUTPUT
    assert "修正要点" in _REVIEW_SYSTEM_PROMPT
    # 短文本截断：原文/译文 >600 才截到 600（0.39.1 放宽，见下个测试）
    item = ReviewItem(entry_id="a1", original="x" * 900,
                      translation="译" * 900, term_hint="术" * 900)
    prompt = _build_item_prompt(item)
    assert "x" * 600 in prompt
    assert "x" * 601 not in prompt
    batch = _build_batch_prompt([item])
    assert "术" * 120 in batch
    assert "术" * 121 not in batch


def test_review_long_text_cap_raised_to_600():
    """0.39.1 fromivan 幻觉增义误判修复：长文本截断 220 → 600。

    英文信件原文 ~470 字符、中文译文 ~300 字符——旧 220 截断后审核
    模型看不到的区间恰好是译文演绎来源（「across the Soviet Union」
    段），合理译文被误判「幻觉增义」。短文本维持 220 口径不变。
    """
    from hanhua.core.reviewer import _build_item_prompt
    letter_en = ("Dear Ivan, " * 40)[:470]          # 470 字符英文信
    letter_zh = "亲爱的伊万：" * 40                  # 240 字符中文译文
    item = ReviewItem(entry_id="l1", original=letter_en,
                      translation=letter_zh)
    prompt = _build_item_prompt(item)
    # 原文 470 字符全部在场（旧 220 截断下后半缺失）
    assert letter_en in prompt
    assert letter_zh in prompt
    # 短文本不受影响：220 内原样
    short = ReviewItem(entry_id="l2", original="Resume", translation="继续")
    assert "原文：Resume" in _build_item_prompt(short)


def test_review_batch_splits_by_token_budget():
    """组批按估算 token 预算拆组（batch_size 是上限）——超 ctx 的
    prompt 会被 llama-server 静默截断尾部，预算拆组保证放得下。

    估算口径与 prompt 截断一致（0.39.1 长文本 600 cap）：每条
    中文长文本（译文 2000 字 → 截 600 + term_hint 截 120 + 40 格式）
    ≈ 761 token，batch_size=20 时 14 条 ≈ 10.6k 超 4500 预算 →
    5×761=3805 放得下、6×761=4566 超预算 → 拆成 [5, 5, 4] 三组
    （20 条短文本约 1.3k 仍在预算内不拆）。
    """
    from hanhua.core.reviewer import ReviewConfig
    import json

    def group_json(ids):
        return json.dumps([{"entry_id": str(i), "level": "PASS",
                            "reason": "正确"} for i in ids],
                          ensure_ascii=False)
    service = _FakeService(outputs=[group_json(range(1, 6)),
                                    group_json(range(6, 11)),
                                    group_json(range(11, 15))])
    reviewer = SemanticReviewer(
        service=service, config=ReviewConfig(batch_size=20))
    items = [ReviewItem(entry_id=str(i), original="text",
                        translation="译" * 2000,
                        term_hint="术语参考" * 30) for i in range(1, 15)]
    results, cancelled = reviewer.review_batch(items)
    assert cancelled == 0
    assert len(results) == 14
    assert len(service.prompts) == 3                 # [5, 5, 4] 三组
    # 各组不串组不截断（「条目 1」是「条目 12」的子串，用完整 id 断言）
    assert "### 条目 1\n" in service.prompts[0]
    assert "### 条目 5\n" in service.prompts[0]
    assert "### 条目 5\n" not in service.prompts[1]
    assert "### 条目 6\n" in service.prompts[1]
    assert "### 条目 10\n" in service.prompts[1]
    assert "### 条目 10\n" not in service.prompts[2]
    assert "### 条目 11\n" in service.prompts[2]
    assert "### 条目 14\n" in service.prompts[2]


def test_review_batch_max_tokens_capped():
    """组批 max_tokens 收紧：128/条 + 256 余量、封顶 4096（此前
    1024×20=20480 是话痨放大器——长输出到分钟级且截断即全组兜底）。"""
    from hanhua.core.reviewer import ReviewConfig
    service = _FakeService(outputs=[
        '[{"entry_id": "1", "level": "PASS", "reason": "正确"}, '
        '{"entry_id": "2", "level": "PASS", "reason": "正确"}]',
    ])
    reviewer = SemanticReviewer(
        service=service, config=ReviewConfig(batch_size=2))
    items = [ReviewItem(entry_id=str(i), original=f"text{i}",
                        translation=f"译文{i}") for i in (1, 2)]
    results, _cancelled = reviewer.review_batch(items)
    assert len(results) == 2
    assert service.max_tokens_calls == [max(1024, min(4096, 128 * 2 + 256))]
    assert service.max_tokens_calls[0] == 1024      # 1024 保底生效


# ── 2026-08-14 用户实证：request_error 全批——4B 反馈重译通道 ──────

class _FakeTranslator:
    """假翻译器：记录调用；_apply_quality 默认放行（可切换拒绝）。"""

    def __init__(self, apply_ok: bool = True):
        self.apply_ok = apply_ok
        self.retranslate_calls: list[tuple] = []
        self.apply_calls: list[str] = []

    def retranslate_with_feedback(self, entry, feedback, round_no=1):
        self.retranslate_calls.append((entry.original, feedback, round_no))
        entry.translation = "开始游戏（1.8B）"
        return True, entry.translation

    def _apply_quality(self, entry, candidate, skip_consistency: bool = False):
        self.apply_calls.append(candidate)
        if not self.apply_ok:
            return False
        entry.translation = candidate
        return True


def test_semantic_reviewer_retranslate_prompt_has_role_and_fields():
    """4B 反馈重译必须带固定游戏本地化角色（2026-08-14 用户要求：
    任何翻译路径必须给模型固定角色，至少是游戏翻译者）。"""
    service = _FakeService(outputs=["开始游戏"])
    reviewer = _make_reviewer(service)
    out = reviewer.retranslate_with_feedback(
        "Play", "播放", "Play 在按钮语境应译为开始，不是播放")
    assert out == "开始游戏"
    prompt = service.prompts[0]
    assert "游戏本地化翻译专家" in prompt
    assert "原文：Play" in prompt
    assert "上次译文：播放" in prompt
    assert "审核反馈" in prompt


def test_retranslate_prefers_4b_channel():
    """request_error 根因修复：反馈重译优先 4B 通道（审核模型已在
    显存——6~8GB 档 1.8B 换入 OOM 导致全批 request_error）；4B 输出
    过机械质量门（translator._apply_quality）后再审收敛；1.8B 通道
    不被调用。"""
    from hanhua.core.reviewer import _retranslate_with_feedback
    service = _FakeService(outputs=[
        "开始",                                # 4B 重译输出
        '{"level": "PASS", "reason": "正确"}',  # 再审收敛
    ])
    reviewer = _make_reviewer(service)
    translator = _FakeTranslator()
    entry = TextEntry(file_id="f", key_path="k", original="Play",
                      translation="播放", status="translated",
                      meta={"role": "display"})
    result = ReviewResult("e0", level="CRITICAL",
                          reason="Play 误译播放，应译为开始")
    outcome = _retranslate_with_feedback(
        translator, entry, result, None, reviewer=reviewer,
        app_dir=Path("."))
    assert outcome == "converged"
    assert translator.retranslate_calls == []   # 4B 成功，1.8B 不调
    assert translator.apply_calls == ["开始"]   # 机械门复查 4B 输出
    assert entry.status == "translated"
    assert entry.translation == "开始"
    assert entry.meta.get("review_outcome") == "APPROVED"
    assert entry.meta.get("retranslated") is True


def test_retranslate_4b_failure_falls_back_to_translator():
    """4B 通道请求失败 → 回退 1.8B 通道（网络/服务故障不阻断重译）。"""
    from hanhua.core.reviewer import _retranslate_with_feedback
    service = _FakeService(error=RuntimeError("4B 服务故障"))
    reviewer = _make_reviewer(service)
    translator = _FakeTranslator()
    entry = TextEntry(file_id="f", key_path="k", original="Play",
                      translation="播放", status="translated",
                      meta={"role": "display"})
    result = ReviewResult("e0", level="MAJOR", reason="语气不符")
    outcome = _retranslate_with_feedback(
        translator, entry, result, None, reviewer=reviewer,
        app_dir=Path("."))
    assert translator.retranslate_calls          # 回退 1.8B 生效
    # 再审走同一故障服务 → fail-closed REVIEW_ERROR（不伪装收敛）
    assert outcome == "error"
    assert entry.meta.get("review_outcome") == "REVIEW_ERROR"


def test_retranslate_4b_output_rejected_by_gate_falls_back():
    """4B 输出未过机械质量门 → 恢复原译文并回退 1.8B 通道（防污染
    「上次译文」反馈）。"""
    from hanhua.core.reviewer import _retranslate_with_feedback
    service = _FakeService(outputs=[
        "播放播放播放",                              # 4B 输出：含未翻译
        '{"level": "PASS", "reason": "正确"}',      # 再审收敛
    ])
    reviewer = _make_reviewer(service)
    translator = _FakeTranslator(apply_ok=False)   # 机械门拒绝 4B 输出
    entry = TextEntry(file_id="f", key_path="k", original="Play",
                      translation="播放", status="translated",
                      meta={"role": "display"})
    result = ReviewResult("e0", level="MAJOR",
                          reason="术语不一致，应译为开始")
    outcome = _retranslate_with_feedback(
        translator, entry, result, None, reviewer=reviewer,
        app_dir=Path("."))
    assert translator.apply_calls == ["播放播放播放"]
    assert translator.retranslate_calls             # 回退 1.8B
    assert outcome == "converged"
    assert entry.translation == "开始游戏（1.8B）"


# ── 2026-08-14 minato 实证：审校管线四根因回归测试 ──────────────

def test_retranslate_skips_consistency_gate():
    """反馈重译豁免批内一致性：翻译阶段缓存的坏译文不得以
    consistency_mismatch 拒绝正确重译（Pan 先生→左滑 全被杀的根因）。
    """
    from hanhua.core.batch_translator import BatchTranslator
    from hanhua.core.reviewer import _retranslate_with_feedback

    class _DummyClient:
        pass

    translator = BatchTranslator(client=_DummyClient())
    entry = TextEntry(file_id="f", key_path="k", original="Pan",
                      translation="先生", status="translated",
                      meta={"kind": "rawstr", "role": "display"})
    assert translator._apply_quality(entry, "先生")   # 坏译文入缓存
    service = _FakeService(outputs=[
        "左滑",                                       # 4B 重译输出
        '{"level": "PASS", "reason": "正确"}',        # 再审收敛
    ])
    reviewer = _make_reviewer(service)
    result = ReviewResult("e0", level="CRITICAL",
                          reason="Pan 译为先生严重错误，应为左滑")
    outcome = _retranslate_with_feedback(
        translator, entry, result, None, reviewer=reviewer,
        app_dir=Path("."))
    assert outcome == "converged"
    assert entry.translation == "左滑"


def test_retranslate_repairs_multiline_candidate():
    """多行候选确定性修复：单行原文 + 双候选输出「左滑\n左移」不再
    newline 恒败——取首行修复后过门收敛。"""
    from hanhua.core.batch_translator import BatchTranslator
    from hanhua.core.reviewer import _retranslate_with_feedback

    class _DummyClient:
        pass

    translator = BatchTranslator(client=_DummyClient())
    entry = TextEntry(file_id="f", key_path="k", original="Hearts",
                      translation="心脏", status="translated",
                      meta={"kind": "rawstr", "role": "display"})
    translator._apply_quality(entry, "心脏")
    service = _FakeService(outputs=[
        "爱心\n红心",                                 # 4B 多行双候选
        '{"level": "PASS", "reason": "正确"}',        # 再审收敛
    ])
    reviewer = _make_reviewer(service)
    result = ReviewResult("e0", level="CRITICAL",
                          reason="Hearts 语境下应译为爱心")
    outcome = _retranslate_with_feedback(
        translator, entry, result, None, reviewer=reviewer,
        app_dir=Path("."))
    assert outcome == "converged"
    assert entry.translation == "爱心"


def test_repair_multiline_rejects_metadata_echo():
    """0.39.1 fromivan：多行候选首行是提示词元数据回显时拒绝修复——
    「[来源文件] …」是模型复述 prompt 结构而非译文，写回即污染。"""
    from hanhua.core.reviewer import _repair_multiline_candidate
    # 元数据回显 → 拒绝（返回空串，走重试/人工）
    assert _repair_multiline_candidate(
        "Start", "[来源文件] From Ivan_Data/resources.assets\n开始游戏") == ""
    assert _repair_multiline_candidate(
        "Start", "原文：Start\n译文：开始") == ""
    assert _repair_multiline_candidate(
        "Start", "[定位键] xxx\n开始") == ""
    # 正常双候选 → 仍修复
    assert _repair_multiline_candidate("Hearts", "爱心\n红心") == "爱心"
    # 单行候选 / 多行原文 → 原本就拒绝
    assert _repair_multiline_candidate("Hearts", "爱心") == ""
    assert _repair_multiline_candidate("a\nb", "甲\n乙") == ""


def test_reason_claims_missing_translation():
    """批审幻觉防护判据：可验证主张「译文缺失」与译文字面矛盾。"""
    from hanhua.core.reviewer import _reason_claims_missing_translation
    assert _reason_claims_missing_translation(
        "译文缺失，仅保留术语参考，未翻译原文'Time'，导致界面空白。")
    assert _reason_claims_missing_translation("仅列出术语表，未翻译原文")
    assert not _reason_claims_missing_translation("术语翻译准确，可直接入库。")
    assert not _reason_claims_missing_translation("")


def test_reason_claims_language_error():
    """C12b：可验证主张「语言错误/不该译成中文」——中文本地化流程里
    恒不成立（把原文译成中文是任务本身），命中即幻觉。"""
    from hanhua.core.reviewer import _reason_claims_language_error
    # fake-it 4B 真实输出形态
    assert _reason_claims_language_error(
        "原文为法语，译文误译成中文，语言完全错误。")
    assert _reason_claims_language_error(
        "原文为德语，不应翻译成中文，应保留原语言。")
    assert _reason_claims_language_error("译文语言错误，应为法语。")
    # 真实语义问题不受影响
    assert not _reason_claims_language_error(
        "原文 lecture=读者，译文误译为讲师，语义错误。")
    assert not _reason_claims_language_error("术语误用：Resume 应译为继续。")
    assert not _reason_claims_language_error("")


def test_language_error_claim_falls_back_to_single_review(monkeypatch):
    """C12b：批审「原文为法语，译文误译成中文」→ 判定与事实矛盾 →
    逐条重审兜底（把原文译成中文是本职工作）。"""
    import hanhua.core.reviewer as rev

    class _LangHalluReviewer:
        usable = True
        one_calls = 0

        def __init__(self, app_dir=None, config=None, online_cfg=None):
            pass

        def review_batch(self, items, on_progress=None,
                         cancellation_event=None):
            return {it.entry_id: rev.ReviewResult(
                it.entry_id, level="CRITICAL",
                reason="原文为法语，译文误译成中文，语言完全错误")
                for it in items}, 0

        def review_one(self, item):
            type(self).one_calls += 1
            return rev.ReviewResult(item.entry_id, level="PASS",
                                    reason="正确")

        def retranslate_with_feedback(self, *a, **k):
            return "x"

    monkeypatch.setattr(rev, "SemanticReviewer", _LangHalluReviewer)

    class _Glossary:
        def list_all(self):
            return []

    class _Store:
        def batch_update_translation_results(self, rows):
            pass

    entries = [TextEntry(
        id=1, file_id="f", key_path="k",
        original="Rendre tous les lecteurs crédules.",
        translation="让所有读者变得轻信。", status="translated",
        meta={"kind": "typetree", "role": "display"})]
    summary = rev.review_entries(
        entries, _Glossary(), game_name="fake-it",
        translator=None, memory=None, store=_Store(),
        app_dir=Path("."), model_name="qwen", max_send_rate=1.0)
    assert _LangHalluReviewer.one_calls == 1      # 兜底重审一次
    assert summary["results"]["e0"].level == "PASS"


def test_batch_hallucination_falls_back_to_single_review(monkeypatch):
    """审校输出声称「译文缺失」但译文非空 → 逐条重审兜底（防罐头理由）。
    防护统一在 review_entries 层（单条/批量路径都覆盖，每条目至多一次）。"""
    import hanhua.core.reviewer as rev

    class _HalluReviewer:
        usable = True
        one_calls = 0

        def __init__(self, app_dir=None, config=None, online_cfg=None):
            pass

        def review_batch(self, items, on_progress=None,
                         cancellation_event=None):
            return {it.entry_id: ReviewResult(
                it.entry_id, level="CRITICAL",
                reason="译文缺失，仅保留术语参考，未翻译原文") for it in items}, 0

        def review_one(self, item):
            type(self).one_calls += 1
            return ReviewResult(item.entry_id, level="PASS", reason="正确")

        def retranslate_with_feedback(self, *a, **k):
            return "x"

    monkeypatch.setattr(rev, "SemanticReviewer", _HalluReviewer)

    class _Glossary:
        def list_all(self):
            return []

    class _Store:
        def batch_update_translation_results(self, rows):
            pass

    entries = [TextEntry(id=1, file_id="f", key_path="k", original="Time",
                         translation="时间", status="translated",
                         meta={"kind": "textasset", "role": "display"})]
    summary = rev.review_entries(
        entries, _Glossary(), game_name="minato",
        translator=None, memory=None, store=_Store(),
        app_dir=Path("."), model_name="qwen", max_send_rate=1.0)
    assert _HalluReviewer.one_calls == 1          # 兜底重审一次
    assert summary["results"]["e0"].level == "PASS"


def test_report_wrong_translation_from_snapshot():
    """报告错译栏取送审快照（suggestion 恒空后不再显示「无译文记录」）。"""
    from hanhua.core.reviewer import _translation_of
    r = ReviewResult("e7", level="CRITICAL",
                     reason="Charges 误译为等级降低")
    assert _translation_of(r) == "（无译文记录）"   # 无快照时兼容
    summary = {"wrong_translations": {"e7": "等级会降低"}}
    assert _translation_of(r, summary) == "等级会降低"


def test_strip_prompt_echo():
    """工具页输出清洗：提示词/原文回显剥除，正常译文零影响。"""
    from hanhua.core.translator import strip_prompt_echo
    system = "你是专业游戏本地化翻译专家，把游戏文本翻译为简体中文。"
    # 提示词全文回显 + 原文 + 译文
    out = strip_prompt_echo(system + "\n\nhello world\n\n你好，世界",
                            system, "hello world")
    assert out == "你好，世界"
    # 仅回显提示词 + 原文（未翻译）→ 剩空串
    assert strip_prompt_echo(system + "\n" + "abc", system, "abc") == ""
    # 正常译文不受影响
    assert strip_prompt_echo("你好，世界", system, "hello world") == "你好，世界"
    # 译文恰好以原文开头（部分保留）→ 剥原文前缀
    assert strip_prompt_echo("hello world\n你好", system, "hello world") == "你好"


def test_review_entries_term_hint_only_matching_pairs(monkeypatch):
    """审校术语参考按条目命中注入 + 按游戏过滤（minato 实证：全局
    前 20 条跨游戏词对注入教唆 4B「英文应保留原文」，Button→按钮
    被判 CRITICAL——修复后无命中词对的条目零注入）。"""
    import hanhua.core.reviewer as rev
    captured = {}

    class _FakeReviewer:
        usable = True

        def __init__(self, app_dir=None, config=None, online_cfg=None):
            pass

        def review_batch(self, items, on_progress=None,
                         cancellation_event=None):
            captured["items"] = items
            return {it.entry_id: ReviewResult(
                it.entry_id, level="PASS", reason="正确") for it in items}, 0

        def retranslate_with_feedback(self, *a, **k):
            return "x"

    monkeypatch.setattr(rev, "SemanticReviewer", _FakeReviewer)

    class _FakeGlossary:
        def list_all(self):
            return [
                {"status": "active", "term": "SCP-173",
                 "translation": "SCP-173", "games": ""},
                {"status": "active", "term": "GLISLYA",
                 "translation": "GLISLYA", "games": "other"},
                {"status": "active", "term": "FPS",
                 "translation": "FPS", "games": ""},
            ]

    class _FakeStore:
        def batch_update_translation_results(self, rows):
            pass

    entries = [
        TextEntry(id=1, file_id="f", key_path="a", original="Button",
                  translation="按钮", status="translated",
                  meta={"kind": "textasset", "role": "display"}),
        TextEntry(id=2, file_id="f", key_path="b",
                  original="Show FPS counter",
                  translation="显示 FPS 计数器", status="translated",
                  meta={"kind": "textasset", "role": "display"}),
    ]
    rev.review_entries(
        entries, _FakeGlossary(), game_name="minato",
        translator=None, memory=None, store=_FakeStore(),
        app_dir=Path("."), model_name="qwen",
        max_send_rate=1.0)
    by_orig = {it.original: it for it in captured["items"]}
    # 无命中词对 → 不注入术语参考（此前注入 20 条跨游戏词对）
    assert by_orig["Button"].term_hint == ""
    # 命中词对注入 + 保留型词对带语义标注
    assert "FPS=专名保留原文" in by_orig["Show FPS counter"].term_hint
    # 未命中/其他游戏词对不注入
    assert "SCP-173" not in by_orig["Show FPS counter"].term_hint
    assert "GLISLYA" not in by_orig["Show FPS counter"].term_hint


def test_review_entries_counterexample_hint_injected(monkeypatch, tmp_path):
    """C16 审核反例召回：fail_case/审核 域同原文历史误译注入
    context_hint（「勿重蹈」提示）——此前 1674 条反例只写不读。"""
    import hanhua.core.reviewer as rev
    from hanhua.core.knowledge import KnowledgeBase

    captured = {}

    class _FakeReviewer:
        usable = True

        def __init__(self, app_dir=None, config=None, online_cfg=None):
            pass

        def review_batch(self, items, on_progress=None,
                         cancellation_event=None):
            captured["items"] = items
            return {it.entry_id: ReviewResult(
                it.entry_id, level="PASS", reason="正确") for it in items}, 0

        def retranslate_with_feedback(self, *a, **k):
            return "x"

    monkeypatch.setattr(rev, "SemanticReviewer", _FakeReviewer)

    # 反例库建在 data_dir（与 GUI 传 ~/.hanhua 对应；tmp_path 即 data 根）
    import json
    kb = KnowledgeBase(tmp_path / "knowledge.db")
    note = json.dumps({
        "schema": "review_failure_v1", "game": "fake it",
        "original": "Make all readers gullible",
        "wrong_translation": "让读者容易上当",
        "correct_translation": "", "review_reason": "漏译祈使语气",
        "suggestion": "", "converged": True,
        "final_outcome": "APPROVED"}, ensure_ascii=False)
    kb.store.upsert("fail_case", "审核", "fake it_Data/f:12",
                    action="apply_fix", note=note, game="fake it")
    kb.close()

    class _FakeGlossary:
        def list_all(self):
            return []

    class _FakeStore:
        def batch_update_translation_results(self, rows):
            pass

    entries = [
        TextEntry(id=1, file_id="f", key_path="a",
                  original="Make all readers gullible",
                  translation="让所有读者变得轻信",
                  status="translated",
                  meta={"kind": "textasset", "role": "display"}),
    ]
    rev.review_entries(
        entries, _FakeGlossary(), game_name="fake it",
        translator=None, memory=None, store=_FakeStore(),
        app_dir=Path("."), data_dir=tmp_path, model_name="qwen",
        max_send_rate=1.0)
    (it,) = captured["items"]
    assert "历史错译反例" in it.context_hint
    assert "让读者容易上当" in it.context_hint
    assert "勿重蹈" in it.context_hint


def test_review_entries_negation_claim_re_reviews(monkeypatch):
    """可验证「否定漏译」罐头理由 → 逐条重审（come-back 实证：4B 把
    not only 强调句当否定句漏译；译文已含「不仅」/「不」却报语义相反）。

    重审返回 PASS → 译文放行（APPROVED）；真否定漏译（原文真否定、译文
    无否定）不触发重审，保持 CRITICAL → 重译。
    """
    import hanhua.core.reviewer as rev
    calls = {"n": 0}
    first = {"level": "CRITICAL",
             "reason": "原文含否定词 not，译文完全缺失导致语义相反"}

    class _FakeReviewer:
        usable = True

        def __init__(self, app_dir=None, config=None, online_cfg=None):
            pass

        def review_batch(self, items, on_progress=None,
                         cancellation_event=None):
            # 先 CRITICAL（否定漏译罐头理由）→ 触发重审兜底
            return {it.entry_id: ReviewResult(
                it.entry_id, level=first["level"], reason=first["reason"])
                for it in items}, 0

        def review_one(self, item):
            calls["n"] += 1
            return ReviewResult(item.entry_id, level="PASS", reason="正确")

        def retranslate_with_feedback(self, *a, **k):
            return "x"

    monkeypatch.setattr(rev, "SemanticReviewer", _FakeReviewer)

    class _FakeStore:
        def batch_update_translation_results(self, rows):
            pass

    # not only 强调句 + 译文「不仅」→ 非真漏译 → 触发重审 → PASS 放行
    entries = [
        TextEntry(id=1, file_id="f", key_path="a",
                  original="not only does flex taps powerful adhesive hold "
                           "the mountain up",
                  translation="不仅柔韧胶带凭借强力粘合固定了山脉。",
                  status="translated",
                  meta={"kind": "textasset", "role": "display"}),
        # 真否定漏译 → 不触发重审 → 保持 CRITICAL → 需重译
        TextEntry(id=2, file_id="f", key_path="b",
                  original="the enemy does not drop any gold",
                  translation="敌人会掉落金币。", status="translated",
                  meta={"kind": "textasset", "role": "display"}),
    ]
    summary = rev.review_entries(
        entries, None, game_name="t",
        translator=None, memory=None, store=_FakeStore(),
        app_dir=Path("."), model_name="qwen", max_send_rate=1.0)
    assert calls["n"] == 1                 # 仅 not only 触发重审
    assert entries[0].meta["review_outcome"] == "APPROVED"
    assert entries[1].meta["review_outcome"] in ("NEEDS_REVISION", "BLOCKED")


def test_active_glossary_pairs_game_filter():
    """术语词对按游戏过滤：明确归属其他游戏的词对不参与本游戏审校。"""
    from hanhua.core.reviewer import _active_glossary_pairs

    class _G:
        def list_all(self):
            return [
                {"status": "active", "term": "A", "translation": "甲",
                 "games": ""},
                {"status": "active", "term": "B", "translation": "乙",
                 "games": "other"},
                {"status": "candidate", "term": "C", "translation": "丙",
                 "games": ""},
            ]

    pairs = _active_glossary_pairs(_G(), game="minato")
    assert pairs == [("A", "甲")]        # 无归属通用 + 本游戏；candidate 排除


def test_strip_prompt_echo_variants_enhanced():
    """回显增强（2026-08-16 用户反馈）：模型把提示词以变形回显——
    引号/System: 前缀/代码块/编号/标签行——必须清洗；正常译文零影响。"""
    from hanhua.core.translator import strip_prompt_echo
    system = "你是专业游戏本地化翻译专家，把游戏文本翻译为简体中文。请只输出译文，不要重复指令。"
    src = "hello world"
    # System: 前缀回显
    assert strip_prompt_echo("System: " + system + "\n\n你好，世界",
                             system, src) == "你好，世界"
    # 引号包裹提示词 + 原文
    assert strip_prompt_echo('"' + system + '"\n"hello world"\n你好，世界',
                             system, src) == "你好，世界"
    # 代码块包裹提示词 + 标签原文行
    assert strip_prompt_echo('```' + system + '```\n原文：hello world\n'
                             '译文：你好，世界', system, src) == "译文：你好，世界"
    # 编号行回显（提示词+原文+译文都编号）
    assert strip_prompt_echo('1. ' + system + '\n2. 原文：hello world\n'
                             '3. 你好，世界', system, src) == "你好，世界"
    # 纯引号原文回显 → 空
    assert strip_prompt_echo('"hello world"', system, src) == ""
    # 标签原文行（无提示词）
    assert strip_prompt_echo('原文：hello world\n你好，世界',
                             system, src) == "你好，世界"
    # 正常译文零影响
    assert strip_prompt_echo("你好，世界", system, src) == "你好，世界"


def test_strip_prompt_echo_symbol_block_prompts():
    """2026-08-20 用户实证：{}【】等不可翻译输入，小模型把整段提示词
    当输出回显——①前缀剥除后第二句提示词残留、②逐行匹配在拆成
    <15 字符短句时不触发，终末护栏按去空白归一后「剩余是提示词
    连续片段」判定纯回显 → 返回空串（比塞提示词给用户安全）。
    正常译文不是翻译指令的长片段，零误伤。"""
    from hanhua.core.translator import strip_prompt_echo
    system = ("你是专业游戏本地化翻译专家，把游戏文本翻译为简体中文。"
              "请只输出译文，不要重复指令。")
    src = "{}【】"
    # 模型把整段提示词改行/合并回显后穿插原文符号（前缀剥除后第二句残留）
    out = ("你是专业游戏本地化翻译专家，把游戏文本翻译为简体中文。\n"
           "请只输出译文，不要重复指令。\n\n{}【】")
    assert strip_prompt_echo(out, system, src) == ""
    # 提示词 + 大量原文残片穿插 → 仍判纯回显
    out2 = system + "\n" + src + "\n" + src
    assert strip_prompt_echo(out2, system, src) == ""
    # 正常译文（恰好短，非翻译指令片段）零影响
    assert strip_prompt_echo("这是一条译文", system, src) == "这是一条译文"
    # 正常译文与提示词无整段包含关系（短前缀不触发 ≥10 门槛）
    assert strip_prompt_echo(system[:8] + "的简短译文", system, src) != ""
