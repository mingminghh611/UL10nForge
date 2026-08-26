"""「翻译」页（#11）测试：轻量翻译应用——模型信息、可编辑提示词、
后台翻译、历史落盘回填、长文本分块、导航接入。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from hanhua.core.agent_memory import AgentMemory
from hanhua.core.glossary import GlossaryStore
from hanhua.core.knowledge import KnowledgeBase
from hanhua.core.settings import SettingsStore
from hanhua.ui.app_state import AppState
from hanhua.ui.main_window import MainWindow, PAGES
from hanhua.ui.pages.translate_tool_page import (
    TranslateToolPage,
    _BLOCK_CHARS,
    _is_symbol_only,
)
from conftest import await_reload


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _Window:
    def navigate(self, _page):
        pass


def _state(tmp_path: Path) -> AppState:
    settings = SettingsStore(tmp_path / "settings.json")
    settings.load()
    return AppState(tmp_path, settings)


class _FakeClient:
    """假翻译客户端：译文 = 原文前缀「译：」。

    2026-08-26 起工具页本地模式走共享降级链 translate_interactive，其
    经 translate_source_directive 优先调用客户端 translate_text（Hy-MT2
    契约，真实 LocalOpenAIClient 有该方法）——假客户端补齐同名方法以
    匹配真实契约。
    """

    def __init__(self, config=None):
        self.config = config
        self.calls = []

    def chat(self, system, messages):
        self.calls.append((system, messages))
        return "译：" + (messages[0]["content"] or ""), None

    def translate_text(self, source_text, target_lang, glossary=()):
        self.calls.append(("translate_text", source_text))
        return "译：" + source_text, None


def _fake_client_factory(client):
    def factory(config, transport_factory=None):
        client.config = config
        return client
    return factory


def _run_until_idle(page, timeout_ms=8000):
    deadline = time.monotonic() + timeout_ms / 1000.0
    while page._running and time.monotonic() < deadline:
        QTest.qWait(10)
    assert not page._running, "翻译超时"


def test_tool_page_model_label_and_history_disabled(qapp, tmp_path):
    """未配置模型：提示去设置；无历史：下拉禁用。"""
    page = TranslateToolPage(_state(tmp_path), _Window())
    # 默认本地模式（F56）：模型未找到提示
    assert "未找到" in page.model_label.text()
    assert not page.history_combo.isEnabled()
    assert page.dst_edit.isReadOnly()
    # 默认提示词是游戏本地化角色（#10 精简版头部）
    assert "游戏本地化" in page.prompt_edit.toPlainText()


def test_tool_page_translate_and_history_persist(qapp, tmp_path, monkeypatch):
    """翻译 → 译文显示 + 历史落盘 json + 下拉可回填。"""
    client = _FakeClient()
    monkeypatch.setattr("hanhua.ui.pages.translate_tool_page.create_client",
                        _fake_client_factory(client))
    state = _state(tmp_path)
    state.settings.api.mode = "api"
    state.settings.api.base_url = "http://x"
    state.settings.api.api_key = "k"
    state.settings.api.model = "test-model"
    page = TranslateToolPage(state, _Window())
    page.src_edit.setPlainText("Hello world\nSecond line")
    page.translate_btn.click()
    _run_until_idle(page)
    await_reload(page)
    assert page.dst_edit.toPlainText() == "译：Hello world\nSecond line"
    assert client.calls, "必须调用翻译客户端"
    # 落盘
    history_path = Path(state.app_dir) / "quick_translate_history.json"
    data = json.loads(history_path.read_text(encoding="utf-8"))
    assert data[0]["src"] == "Hello world\nSecond line"
    assert data[0]["model"] == "test-model"
    assert page.history_combo.isEnabled()
    # 回填
    page.src_edit.clear()
    page.dst_edit.clear()
    page.history_combo.setCurrentIndex(0)
    page.history_combo.activated.emit(0)
    assert page.src_edit.toPlainText() == "Hello world\nSecond line"
    assert page.dst_edit.toPlainText() == "译：Hello world\nSecond line"


def test_tool_page_unconfigured_api_blocked(qapp, tmp_path, monkeypatch):
    """API 模式未配置 → 点翻译只提示，不调客户端。"""
    calls = []
    monkeypatch.setattr("hanhua.ui.pages.translate_tool_page.create_client",
                        lambda config, transport_factory=None: calls.append(1))
    page = TranslateToolPage(_state(tmp_path), _Window())
    page.src_edit.setPlainText("Hello")
    page.translate_btn.click()
    _run_until_idle(page)
    assert calls == []
    assert page.dst_edit.toPlainText() == ""
    assert "失败" not in page.status_label.text()


def test_tool_page_local_model_starts_service(qapp, tmp_path, monkeypatch):
    """本地模式：worker 先 ensure_running 再翻译（配置透传 endpoint）。"""
    class _FakeLocalModel:
        def ensure_running(self, config, cancellation_event=None):
            return type("Runtime", (), {
                "endpoint": "http://127.0.0.1:9999",
                "api_key": "local-key",
                "model": "local-model",
            })()

    client = _FakeClient()
    monkeypatch.setattr("hanhua.ui.pages.translate_tool_page.create_client",
                        _fake_client_factory(client))
    state = _state(tmp_path)
    state.settings.api.mode = "local"
    state.settings.api.local_model_path = "D:/models/hy-mt2.gguf"
    page = TranslateToolPage(state, _Window())
    page.src_edit.setPlainText("Hello")
    # 直接测后台函数（等价 worker 内路径）
    blocks = TranslateToolPage._split_blocks("Hello")
    out = TranslateToolPage._run_blocks(
        state.settings.api, page.prompt_edit.toPlainText(), blocks,
        _FakeLocalModel(), Path(tmp_path))
    assert out == ["译：Hello"]
    assert client.config.base_url == "http://127.0.0.1:9999"
    assert client.config.model == "local-model"


def test_tool_page_local_auto_discover_model(qapp, tmp_path, monkeypatch):
    """本地模式自动发现：settings 不存模型路径也能翻译（模型已启动场景）。"""
    import struct

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    header = b"GGUF" + struct.pack("<IQQ", 3, 1, 1)
    (models_dir / "Hy-MT2-1.8B-Q6_K.gguf").write_bytes(
        header + b"\x00" * (1024 * 1024 - len(header)))

    class _FakeLocalModel:
        def ensure_running(self, config, cancellation_event=None):
            return type("Runtime", (), {
                "endpoint": "http://127.0.0.1:9999",
                "api_key": "local-key",
                "model": "Hy-MT2-1.8B-Q6_K",
            })()

    client = _FakeClient()
    monkeypatch.setattr("hanhua.ui.pages.translate_tool_page.create_client",
                        _fake_client_factory(client))
    state = _state(tmp_path)
    state.settings.api.mode = "local"        # local_model_path 保持空
    state.local_model = _FakeLocalModel()
    page = TranslateToolPage(state, _Window())
    # 标签按自动发现显示模型名，不再误报「未配置」
    assert "Hy-MT2-1.8B" in page.model_label.text()
    page.src_edit.setPlainText("Hello")
    page.translate_btn.click()
    _run_until_idle(page)
    await_reload(page)
    assert page.dst_edit.toPlainText() == "译：Hello"
    assert client.calls, "必须调用翻译客户端"
    history_path = Path(state.app_dir) / "quick_translate_history.json"
    data = json.loads(history_path.read_text(encoding="utf-8"))
    assert data[0]["model"] == "Hy-MT2-1.8B-Q6_K"


def test_tool_page_warning_dedup_on_repeated_click(qapp, tmp_path, monkeypatch):
    """连点翻译：失败提示只弹一条，不叠加多条消息。"""
    from hanhua.ui.widgets import Toast
    calls = []
    monkeypatch.setattr("hanhua.ui.pages.translate_tool_page.create_client",
                        lambda config, transport_factory=None: calls.append(1))
    page = TranslateToolPage(_state(tmp_path), _Window())
    page.src_edit.setPlainText("Hello")
    base = len(Toast._stack)
    page.translate_btn.click()
    QTest.qWait(50)
    page.translate_btn.click()
    QTest.qWait(50)
    assert len(Toast._stack) - base == 1, "连点应只弹一条提示"
    assert calls == []


def test_tool_page_split_blocks_keeps_lines():
    """长文本按行分块（行不拆分，块 ≤ _BLOCK_CHARS）。"""
    lines = ["x" * 500 + str(i) for i in range(20)]
    text = "\n".join(lines)
    assert len(text) > _BLOCK_CHARS * 3
    blocks = TranslateToolPage._split_blocks(text)
    assert len(blocks) > 1
    assert "\n".join(blocks) == text  # 行不拆分，块间换行拼接 = 原文本
    for block in blocks:
        assert len(block) <= _BLOCK_CHARS
    # 短文本不分块
    assert TranslateToolPage._split_blocks("short") == ["short"]


def test_tool_page_symbol_input_blocked_without_model(qapp, tmp_path, monkeypatch):
    """2026-08-20 用户实证：输入 {}【】等纯符号/非文字时，翻译前直接
    拦截——不调模型（避免小模型回显整段提示词塞满译文栏），译文区
    清空并提示「无法翻译」。"""
    calls = []
    monkeypatch.setattr("hanhua.ui.pages.translate_tool_page.create_client",
                        lambda config, transport_factory=None: calls.append(1))
    page = TranslateToolPage(_state(tmp_path), _Window())
    page.src_edit.setPlainText("{}【】")
    page.translate_btn.click()
    QTest.qWait(50)
    assert calls == [], "纯符号输入不应调用模型"
    assert page.dst_edit.toPlainText() == ""
    assert "无法翻译" in page.status_label.text()


def test_is_symbol_only_classification():
    """纯符号判定：无字母/数字/CJK 为纯符号；含任一即非纯符号。"""
    assert _is_symbol_only("{}【】") is True
    assert _is_symbol_only("{} 【】 \n  ") is True
    assert _is_symbol_only("hello") is False
    assert _is_symbol_only("你好") is False
    assert _is_symbol_only("123") is False
    assert _is_symbol_only("{}a") is False
    assert _is_symbol_only("") is False


def test_tool_page_reset_prompt_uses_project_profile(qapp, tmp_path):
    """「使用当前游戏档案提示词」按档案（游戏名/个性化要求）生成。"""
    state = _state(tmp_path)
    page = TranslateToolPage(state, _Window())
    page.prompt_edit.setPlainText("自定义")
    # 无项目 → 档案为空，重置为默认角色提示词
    page._reset_prompt()
    assert "游戏本地化" in page.prompt_edit.toPlainText()
    # 模拟项目档案带游戏名与个性化要求
    profile = type("Profile", (), {
        "game_name": "DemoGame", "genre": "RPG",
        "world_setting": "赛博朋克", "tone_notes": "",
        "prompt_style": "专名音译",
        "source_lang": "en", "target_lang": "zh-CN",
    })()
    state.project = type("Project", (), {"profile": profile})()
    page._reset_prompt()
    text = page.prompt_edit.toPlainText()
    assert "DemoGame" in text
    assert "专名音译" in text


def test_main_window_has_translate_tool_page(qapp, tmp_path):
    """导航 5 项：翻译页可程序化进入。"""
    window = MainWindow(_state(tmp_path))
    assert "translate_tool" in PAGES
    assert window.pages["translate_tool"] is not None
    window.navigate("translate_tool")
    assert window.current_page() == "translate_tool"


# ── #38/#39（2026-08-14 用户实证）：工具页默认提示词注入 ──────────

def test_default_prompt_is_lean_pure_translation(qapp, tmp_path):
    """2026-08-15 用户要求：工具页就是纯翻译小工具——默认提示词
    精简为批量翻译同款 build_system_prompt（角色+规则），不注入
    术语库/知识库/经验记忆（此前注入上万 token 撑爆 ctx）。"""
    page = TranslateToolPage(_state(tmp_path), _Window())
    prompt = page._default_prompt()
    # 精简角色提示词恒在（批量翻译同源）
    assert "本地化" in prompt
    # 不再注入三库大块（术语表/知识库/记忆参考均已移除）
    assert "【术语表" not in prompt
    assert "【特殊情况规则" not in prompt
    assert "【补充规则" not in prompt
    assert len(prompt) < 1500


def test_default_prompt_empty_library_constant(qapp, tmp_path):
    """空库（无任何沉淀）时提示词与满库完全一致——纯翻译提示词
    不随库规模变化（2026-08-15 精简后无注入，长度恒定）。"""
    page = TranslateToolPage(_state(tmp_path), _Window())
    p1 = page._default_prompt()
    assert p1.strip()
    # 同一档案下重复生成结果一致（无随机库注入）
    assert page._default_prompt() == p1


def test_default_prompt_never_grows_with_library(qapp, tmp_path):
    """2026-08-15 用户要求：纯翻译小工具——大库规模下提示词长度
    恒定（不注入词对，无预算膨胀问题）。"""
    glossary = GlossaryStore(tmp_path / "glossary.db")
    glossary.init_schema()
    for i in range(300):
        glossary.add(f"term{i}", f"译名{i}")
    glossary.close()
    agent = AgentMemory(tmp_path / "agent_memory.db")
    agent.init_schema()
    for i in range(200):
        agent.propose(f"phrase {i}", f"译句{i}", game="t")
        agent.propose(f"phrase {i}", f"译句{i}", game="t")
    agent.close()
    page = TranslateToolPage(_state(tmp_path), _Window())
    prompt = page._default_prompt()
    assert len(prompt) < 1500
    assert "term299" not in prompt
    assert "phrase " not in prompt
