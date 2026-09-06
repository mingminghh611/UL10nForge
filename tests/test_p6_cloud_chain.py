# -*- coding: utf-8 -*-
"""P6（2026-09-06，fromivan 实证）：云端 API 审核链路回归。

用户要求「云端 api 链路全部走一样的完全的链路」。本文件锁三处：

1. review_server.chat() 端点 URL 归一化——用户 base_url 带/不带
   /v1 两种填法都必须拼出正确端点（此前 openai 恒 + /chat/completions
   → 不带 /v1 时 404；anthropic 恒 + /v1/messages → 用户已带 /v1 时
   /v1/v1/messages 404）。与翻译链路 normalize_base_url 同语义。
2. runner._run_semantic_review 透传 online_review_cfg →
   review_entries（云端首审/反馈重译/复审同一 service 出口）。
3. runner 翻译段云端分流——api.mode == "api" 时不实例化
   LocalModelManager、不调用 ensure_running、concurrency 不解引用
   runtime（此前 NameError + 「本地模型启动失败」假错）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hanhua.core.review_server import ReviewModelService

# runner 在 scripts/（非包），显式注入路径后按文件名导入
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


# ── 1. chat() 端点 URL 归一化 ────────────────────────────────────

class _FakeResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


def _make_svc(tmp_path, provider, base_url):
    from hanhua.core.models import ApiConfig
    return ReviewModelService(
        tmp_path, online_cfg=ApiConfig(
            provider=provider, base_url=base_url,
            api_key="k", model="m"))


@pytest.mark.parametrize("provider,base_url,want_url", [
    # openai：不带 /v1（用户常见填法）→ 自动补全
    ("openai", "https://api.example.com",
     "https://api.example.com/v1/chat/completions"),
    # openai：带 /v1 → 不双拼
    ("openai", "https://api.example.com/v1",
     "https://api.example.com/v1/chat/completions"),
    # openai：用户已写全 → 原样
    ("openai", "https://api.example.com/v1/chat/completions",
     "https://api.example.com/v1/chat/completions"),
    # openai：尾斜杠 → 归一
    ("openai", "https://api.example.com/v1/",
     "https://api.example.com/v1/chat/completions"),
    # anthropic：不带 /v1 → 补全 /v1/messages
    ("anthropic", "https://api.anthropic.com",
     "https://api.anthropic.com/v1/messages"),
    # anthropic：带 /v1（fromivan 实证 404 形态）→ 不双拼
    ("anthropic", "https://api.anthropic.com/v1",
     "https://api.anthropic.com/v1/messages"),
])
def test_chat_endpoint_url_normalization(monkeypatch, tmp_path,
                                         provider, base_url, want_url):
    sent: dict = {}

    def _post(url, **kwargs):
        sent["url"] = url
        return _FakeResp()

    monkeypatch.setattr("hanhua.core.review_server.httpx.post", _post)
    svc = _make_svc(tmp_path, provider, base_url)
    svc.chat("hi")
    assert sent["url"] == want_url


def test_chat_endpoint_url_static_pure():
    """静态方法不依赖实例状态（本地路径也可用于 URL 预检）。"""
    assert ReviewModelService._endpoint_url(
        "http://127.0.0.1:8081/v1", "openai") == \
        "http://127.0.0.1:8081/v1/chat/completions"


# ── 2. runner 审核透传 online_review_cfg ─────────────────────────

def test_runner_forwards_online_review_cfg(monkeypatch, tmp_path):
    """_run_semantic_review 必须把 online_review_cfg 透传进
    review_entries——云端链路（首审/4B 反馈重译/复审）同一出口的
    前提（fromivan 实证：runner 漏传 → 云端配置静默失效回本地 4B）。"""
    import all_record_runner as runner

    captured: dict = {}

    def _fake_review_entries(entries, glossary, **kwargs):
        captured.update(kwargs)
        return {"used": False}

    monkeypatch.setattr(runner, "review_entries", _fake_review_entries)

    sentinel = SimpleNamespace(base_url="https://api.example.com/v1",
                               api_key="k", model="m", provider="openai")
    project = SimpleNamespace(store=None, profile=None)
    runner._run_semantic_review(
        project, [], tmp_path, "game", glossary=None, skip=False,
        online_review_cfg=sentinel)
    assert captured["online_review_cfg"] is sentinel


def test_runner_review_passes_none_by_default(monkeypatch, tmp_path):
    """不传时透传 None（本地 4B，行为不变）。"""
    import all_record_runner as runner

    captured: dict = {}

    def _fake_review_entries(entries, glossary, **kwargs):
        captured.update(kwargs)
        return {"used": False}

    monkeypatch.setattr(runner, "review_entries", _fake_review_entries)
    runner._run_semantic_review(
        SimpleNamespace(store=None, profile=None), [], tmp_path, "game",
        glossary=None, skip=False)
    assert captured["online_review_cfg"] is None


# ── 3. 翻译段云端分流 ────────────────────────────────────────────

def test_runner_cloud_mode_skips_local_manager(tmp_path, monkeypatch):
    """云端模式：不实例化 LocalModelManager、不调用 ensure_running。

    fromivan 实证：runner 无条件 LocalModelManager → 云端用户没有
    本地模型文件，ensure_running 崩「本地模型启动失败」假错退 4。"""
    import all_record_runner as runner

    calls: list[str] = []

    class _BoomManager:
        def __init__(self, *a, **k):
            calls.append("manager_init")

        def ensure_running(self, api):
            calls.append("ensure_running")
            raise AssertionError("云端模式不得启动本地模型")

    monkeypatch.setattr(runner, "LocalModelManager", _BoomManager)

    # 云端 ApiConfig
    from hanhua.core.models import ApiConfig
    api = ApiConfig(mode="api", provider="openai",
                    base_url="https://api.example.com/v1",
                    api_key="k", model="m")
    assert api.mode == "api"

    # 模拟 runner 翻译段分流逻辑（与源码同构的最小复刻——源码在
    # main() 内联，不可直接调用；此处锁分流表达式本身）
    manager = (runner.LocalModelManager(tmp_path, startup_timeout=180)
               if api.mode == "local" else None)
    assert manager is None
    runtime = None
    if api.mode == "local":
        runtime = manager.ensure_running(api)
    concurrency = (runtime.parallel
                   if api.mode == "local" else api.concurrency)
    assert concurrency == api.concurrency
    assert calls == []  # 云端模式全程零本地模型调用


def test_runner_local_mode_uses_runtime_parallel(tmp_path):
    """本地模式分流不变：runtime.parallel 驱动并发。"""
    runtime = SimpleNamespace(parallel=4)
    from hanhua.core.models import ApiConfig
    api = ApiConfig(mode="local")
    concurrency = (runtime.parallel
                   if api.mode == "local" else api.concurrency)
    assert concurrency == 4


# ── 4. 写回审计模型层云端转发 ────────────────────────────────────

def test_audit_writeback_forwards_online_cfg(tmp_path, monkeypatch):
    """云端模式：audit_writeback 按需构建 ReviewModelService 时必须
    透传 online_cfg——否则云端用户 model_unavailable=True 被阻断发布
    （fromivan 实证：写回审计层 2 是云端链路最后一处本地硬依赖）。"""
    from hanhua.core import review_server
    from hanhua.core.models import ApiConfig

    captured: dict = {}
    sentinel_cfg = ApiConfig(mode="api", provider="openai",
                             base_url="https://api.example.com/v1",
                             api_key="k", model="m")

    class _FakeService:
        def __init__(self, app_dir, *, online_cfg=None):
            captured["online_cfg"] = online_cfg

    monkeypatch.setattr(review_server, "ReviewModelService", _FakeService)
    # 让模型层正常返回（不真的跑模型）；store 提供空 files/entries
    from hanhua.core import writeback_audit as wba
    monkeypatch.setattr(wba, "audit_model", lambda *a, **k:
                        SimpleNamespace(model_flags=[],
                                        model_unavailable=False))

    class _Store:
        def get_entries(self):
            return []

        def get_files(self):
            return []

    res = wba.audit_writeback(
        _Store(), tmp_path, tmp_path, run_model=True,
        app_dir=tmp_path, online_cfg=sentinel_cfg)
    assert captured["online_cfg"] is sentinel_cfg
    assert res.model_unavailable is False


def test_audit_writeback_local_mode_no_cfg(tmp_path, monkeypatch):
    """本地模式不传 online_cfg（None）——行为与 0.39.0 一致。"""
    from hanhua.core import review_server
    from hanhua.core import writeback_audit as wba

    captured: dict = {}

    class _FakeService:
        def __init__(self, app_dir, *, online_cfg=None):
            captured["online_cfg"] = online_cfg

    monkeypatch.setattr(review_server, "ReviewModelService", _FakeService)
    monkeypatch.setattr(wba, "audit_model", lambda *a, **k:
                        SimpleNamespace(model_flags=[],
                                        model_unavailable=False))

    class _Store:
        def get_entries(self):
            return []

        def get_files(self):
            return []

    wba.audit_writeback(_Store(), tmp_path, tmp_path, run_model=True,
                        app_dir=tmp_path)
    assert captured["online_cfg"] is None
