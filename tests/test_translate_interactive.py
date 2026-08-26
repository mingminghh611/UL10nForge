# -*- coding: utf-8 -*-
"""交互式单条直译链（translate_interactive）与专名提取（proper_words_of）测试。

2026-08-26 统一：翻译工具页与审校页「AI 翻译」共用 translator.
translate_interactive 多级降级链——中文显式指令 → 整体译名 → 专名保留
引用。这些测试覆盖降级链的各级触发条件，防止「模型未产出译文」误报复发。
"""
import httpx

from hanhua.core.models import ApiConfig
from hanhua.core.translator import (create_client, proper_words_of,
                                    translate_interactive)


def _client(content: str) -> tuple:
    """构造返回固定 content 的本地客户端（MockTransport）。"""
    def factory():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            })
        return httpx.Client(transport=httpx.MockTransport(handler))
    return create_client(ApiConfig(
        mode="local", provider="anthropic",
        base_url="http://127.0.0.1:8080/v1", api_key="k",
        model="Hy-MT2-1.8B-Q6_K"), transport_factory=factory)


def _script_client(responses: list[str]) -> tuple:
    """构造按调用顺序依次返回 responses 的客户端。

    用于验证降级链按 ①中文指令 → ②整体译名 → ③专名引用 顺序触发。
    """
    queue = list(responses)

    def factory():
        def handler(request: httpx.Request) -> httpx.Response:
            content = queue.pop(0) if queue else ""
            return httpx.Response(200, json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            })
        return httpx.Client(transport=httpx.MockTransport(handler))
    return create_client(ApiConfig(
        mode="local", provider="anthropic",
        base_url="http://127.0.0.1:8080/v1", api_key="k",
        model="Hy-MT2-1.8B-Q6_K"), transport_factory=factory)


# ── proper_words_of：专名提取 ────────────────────────────────────────

def test_proper_words_merges_adjacent_title_case():
    """相邻 TitleCase 词合并为一个专名（Doctor + Strange → Doctor Strange）。"""
    assert proper_words_of("Doctor Strange is the main character") == \
        ["Doctor Strange"]


def test_proper_words_brand_phrase_kept_whole():
    """品牌名中只有真专名词被提取（Out/the/of 为介词/功能词不并入，
    Loop 保留）；'studio' 小写不取 → ['Loop']。注入 (Loop, Loop) 让模型
    保留品牌名 Loop 只译 studio → 'Out of the Loop 工作室'。"""
    assert proper_words_of("Out of the Loop studio") == ["Loop"]


def test_proper_words_simple_sentence_empty():
    """普通句子无专名 → 空（不触发保留引用）。"""
    assert proper_words_of("This is a simple sentence") == []


def test_proper_words_ui_words_excluded():
    """UI 词典词/动作词不进专名（Save/Continue/Interact 是界面词）。"""
    assert proper_words_of("Save and Continue") == []
    assert proper_words_of("Interact hold") == []


# ── translate_interactive：各级降级 ──────────────────────────────────

def test_interactive_first_pass_valid_chinese():
    """① 中文显式指令直接产出含中文译文 → 直接返回。"""
    client = _client("Out of the Loop 工作室")
    assert translate_interactive(client, "Out of the Loop studio") == \
        "Out of the Loop 工作室"


def test_interactive_first_pass_english_echo_goes_to_directive():
    """① 中文指令产出纯英文（Iron Key 未翻译，回显）→ 无 CJK 视为未译，
    走 ② 整体译名指令（与批量翻译 untranslated_text 口径一致）。"""
    client = _script_client(["Iron Key", "铁钥匙"])
    assert translate_interactive(client, "Iron Key") == "铁钥匙"


def test_interactive_echo_stripped_goes_to_directive():
    """② ①回显整段被剥空 → 走中文「整体译名」指令。"""
    # 第 1 次调用：中文指令下模型仍回显原文整段（"Out of the Loop studio"）
    # strip_prompt_echo 剥空 → 触发 ② 整体译名指令
    client = _script_client(["Out of the Loop studio", "Out of the Loop 工作室"])
    assert translate_interactive(client, "Out of the Loop studio") == \
        "Out of the Loop 工作室"


def test_interactive_directive_punct_residue_returns_source():
    """② 整体译名仍只剩纯标点 → 继续 ③ 专名引用；③ 也空 → ④ 返回原文兜底。"""
    client = _script_client(["Out of the Loop studio", "."])
    assert translate_interactive(client, "Out of the Loop studio") == \
        "Out of the Loop studio"


def test_interactive_proper_name_reference_fallback():
    """③ ①、② 均空 → 专名保留引用重译（Markiplier → 专名保留 + 译其余）。"""
    # ① 回显原文 → 空；② 整体译名仍回显 → 空；③ 注入专名引用 → 产出中文
    client = _script_client([
        "Markiplier was here",          # ① 回显原文，剥空
        "Markiplier was here",          # ② 整体译名仍回显，剥空
        "Markiplier 曾来过这里",        # ③ 专名引用 → 译文
    ])
    assert translate_interactive(client, "Markiplier was here") == \
        "Markiplier 曾来过这里"


def test_interactive_all_empty_returns_source():
    """④ 全部降级为空 → 返回原文兜底（2026-08-26 用户要求绝不空输出）。"""
    client = _script_client(["", "", ""])
    assert translate_interactive(client, "Something untranslatable") == \
        "Something untranslatable"
