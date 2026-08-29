from __future__ import annotations

from pathlib import Path
import struct
import threading
import time

import pytest

from hanhua.core.models import ApiConfig
from hanhua.core.local_model import (build_server_command, discover_model,
                                     discover_server, LocalModelError,
                                     LocalModelManager, _http_probe,
                                     resolve_local_parallel,
                                     validate_runtime_manifest)


@pytest.fixture(autouse=True)
def _pin_hardware_planning(monkeypatch):
    """把硬件档位规划固定为「探测失败」，与真实显存环境解耦。

    背景（2026-08-13）：_planned_gpu_layers 对真实硬件探测
    （probe_hardware），地毯式排查并行跑 llama-server 占显存时，档位会
    落入 4~6GB CPU 档 → backend 断言（'gpu'）、parallel cap（4→2）、
    GPU→CPU fallback 链全部随环境抖动（6 测试失败实证）。返回 None =
    模拟探测失败，回退「用户默认 -1 全层」路径，与 planner 接入前的
    测试语义一致。
    """
    monkeypatch.setattr(
        "hanhua.core.local_model._planned_gpu_layers", lambda *a, **k: None)


def test_local_parallel_defaults_and_hardware_caps():
    automatic = ApiConfig(local_concurrency=0)
    excessive = ApiConfig(local_concurrency=99)

    # GPU 默认双槽（2026-08-29）：单槽时模型 p50 ~100ms 推理大部分时间在
    # 等客户端的下一条请求，双槽吞吐接近翻倍；CPU 推理本身跑满核心保持
    # 单槽。上限不放宽（GPU 4 / CPU 2）——多槽 KV 显存倍增 + 长文本排队
    # 超时雪崩的风险边界仍在；显存紧张可手动调回 local_concurrency=1。
    assert resolve_local_parallel(automatic, "gpu") == 2
    assert resolve_local_parallel(automatic, "cpu") == 1
    assert resolve_local_parallel(excessive, "gpu") == 4
    assert resolve_local_parallel(excessive, "cpu") == 2


def _write_fake_gguf(path: Path) -> Path:
    header = b"GGUF" + struct.pack("<IQQ", 3, 1, 1)
    path.write_bytes(header + b"\x00" * ((1024 * 1024) - len(header)))
    return path


def _write_fake_runtime(root: Path, *, cuda: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    names = [
        "llama-server.exe", "llama-server-impl.dll", "llama-common.dll",
        "llama.dll", "ggml.dll", "ggml-base.dll", "ggml-cpu-test.dll",
    ]
    if cuda:
        names += [
            "ggml-cuda.dll", "cublas64_13.dll", "cublasLt64_13.dll",
            "cudart64_13.dll",
        ]
    for name in names:
        (root / name).write_bytes(b"runtime")
    return root / "llama-server.exe"


def test_discover_model_rejects_truncated_gguf(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"GGUF")

    with pytest.raises(LocalModelError) as caught:
        discover_model(model, tmp_path)

    assert caught.value.code == "invalid_model"


def test_local_manager_rejects_incomplete_runtime_before_process_start(tmp_path):
    server = tmp_path / "llama-server.exe"
    server.write_bytes(b"exe")
    model = _write_fake_gguf(tmp_path / "model.gguf")
    created = []
    manager = LocalModelManager(
        tmp_path, process_factory=lambda *args, **kwargs: created.append(args),
    )

    with pytest.raises(LocalModelError) as caught:
        manager.ensure_running(ApiConfig(
            mode="local", local_server_path=str(server),
            local_model_path=str(model),
        ))

    assert caught.value.code == "runtime_incomplete"
    assert created == []


def test_http_probe_requires_health_and_expected_model(monkeypatch):
    calls = []

    class Response:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url, **kwargs):
        calls.append((url, kwargs.get("headers")))
        if url.endswith("/health"):
            return Response({"status": "ok"})
        return Response({"data": [{"id": "C:/models/Hy-MT2.gguf"}]})

    monkeypatch.setattr("hanhua.core.local_model.httpx.get", fake_get)

    assert _http_probe("http://127.0.0.1:8080", "token", "Hy-MT2") is True
    assert _http_probe("http://127.0.0.1:8080", "token", "other") is False
    assert calls[0][0].endswith("/health")
    assert calls[1][0].endswith("/v1/models")
    assert calls[1][1] == {"Authorization": "Bearer token"}


def test_local_startup_can_be_cancelled_without_waiting_for_timeout(tmp_path):
    server = _write_fake_runtime(tmp_path / "runtime")
    model = _write_fake_gguf(tmp_path / "model.gguf")
    probing = threading.Event()
    caught = []

    class FakeProcess:
        pid = 789

        def __init__(self, _command, **_kwargs):
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    def never_ready(_url, _token, _model):
        probing.set()
        return False

    manager = LocalModelManager(
        tmp_path, process_factory=FakeProcess, probe=never_ready,
        sleep=lambda _seconds: time.sleep(0.01), startup_timeout=1,
    )
    config = ApiConfig(
        mode="local", local_server_path=str(server),
        local_model_path=str(model),
    )

    def start():
        try:
            manager.ensure_running(config)
        except LocalModelError as exc:
            caught.append(exc)

    thread = threading.Thread(target=start)
    thread.start()
    assert probing.wait(timeout=1)
    started = time.monotonic()
    manager.stop()
    elapsed = time.monotonic() - started
    thread.join(timeout=2)

    assert elapsed < 0.5
    assert not thread.is_alive()
    assert caught and caught[0].code == "startup_cancelled"


def test_local_runtime_discovers_bundled_files_and_builds_loopback_command(tmp_path):
    runtime = tmp_path / "runtime" / "llama"
    runtime.mkdir(parents=True)
    server = runtime / "llama-server.exe"
    server.write_bytes(b"exe")
    models = tmp_path / "models"
    models.mkdir()
    model = _write_fake_gguf(models / "Hy-MT2-1.8B-Q6_K.gguf")

    assert discover_server("", tmp_path) == server.resolve()
    assert discover_model("", tmp_path) == model.resolve()

    command = build_server_command(
        server, model, port=18080, api_key="secret", context_size=4096,
        gpu_layers=-1, parallel=3,
    )
    assert command[0] == str(server.resolve())
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--port") + 1] == "18080"
    assert command[command.index("--model") + 1] == str(model.resolve())
    assert command[command.index("--ctx-size") + 1] == "4096"
    assert command[command.index("--api-key") + 1] == "secret"
    assert command[command.index("--n-gpu-layers") + 1] == "-1"
    assert command[command.index("--parallel") + 1] == "3"
    assert "--jinja" in command
    assert "--cache-reuse" not in command  # 默认关闭（显式传参才启用）

    with_cache = build_server_command(
        server, model, port=18080, api_key="secret", context_size=4096,
        gpu_layers=-1, parallel=3, cache_reuse=512,
    )
    assert with_cache[with_cache.index("--cache-reuse") + 1] == "512"


def test_local_manager_starts_once_reports_runtime_and_stops_owned_process(tmp_path):
    server = _write_fake_runtime(tmp_path / "runtime")
    model = _write_fake_gguf(tmp_path / "model.gguf")
    created = []

    class FakeProcess:
        pid = 321

        def __init__(self, command, **_kwargs):
            self.command = command
            self.returncode = None
            self.terminated = False
            created.append(self)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    config = ApiConfig(
        mode="local", local_server_path=str(server),
        local_model_path=str(model), local_port=18081,
    )
    manager = LocalModelManager(
        tmp_path, process_factory=FakeProcess,
        probe=lambda _url, _token, _model: True, sleep=lambda _seconds: None,
        token_factory=lambda: "local-token",
    )

    first = manager.ensure_running(config)
    second = manager.ensure_running(config)

    assert first == second
    assert len(created) == 1
    assert first.endpoint == "http://127.0.0.1:18081/v1"
    assert first.api_key == "local-token"
    assert first.backend == "gpu"
    assert first.pid == 321
    assert first.parallel == 2  # GPU 默认双槽（2026-08-29 提速），CPU 保持单槽

    config.local_concurrency = 4
    third = manager.ensure_running(config)

    assert third.parallel == 4
    assert len(created) == 2
    assert created[0].terminated is True  # signature 变化 → 旧进程被替换

    manager.stop()
    assert created[1].terminated is True
    assert manager.runtime is None


def test_local_manager_restart_conservative_reuses_port_and_token(tmp_path):
    """OOM 恢复：保守重启复用端口/token（客户端连接不失效），单槽 CPU 无缓存。"""
    server = _write_fake_runtime(tmp_path / "runtime")
    model = _write_fake_gguf(tmp_path / "model.gguf")
    commands = []
    tokens = iter(["first-token", "second-token"])

    class FakeProcess:
        pid = 555

        def __init__(self, command, **_kwargs):
            self.command = command
            self.returncode = None
            self.terminated = False
            commands.append(self)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    manager = LocalModelManager(
        tmp_path, process_factory=FakeProcess,
        probe=lambda _url, _token, _model: True, sleep=lambda _seconds: None,
        token_factory=lambda: next(tokens),
    )
    config = ApiConfig(
        mode="local", local_server_path=str(server),
        local_model_path=str(model), local_port=18083,
        local_concurrency=4,
    )
    runtime = manager.ensure_running(config)
    assert runtime.parallel == 4
    assert runtime.api_key == "first-token"

    restarted = manager.restart_conservative()

    assert restarted is not None
    assert restarted.port == runtime.port          # 端口复用（客户端 URL 不失效）
    assert restarted.api_key == "first-token"      # token 复用（未重新生成）
    assert restarted.parallel == 1                 # 单槽
    assert restarted.backend == "cpu"              # CPU 推理（零 GPU 显存）
    last_command = commands[-1].command
    assert last_command[last_command.index("--parallel") + 1] == "1"
    assert last_command[last_command.index("--n-gpu-layers") + 1] == "0"
    assert "--cache-reuse" not in last_command
    assert commands[0].terminated is True          # 旧进程被替换

    # signature 失效 → 下次 ensure_running 重新评估完整配置（GPU 恢复）
    again = manager.ensure_running(config)
    assert again.parallel == 4
    assert again.api_key == "second-token"
    manager.stop()


def test_restart_conservative_without_runtime_returns_none(tmp_path):
    manager = LocalModelManager(tmp_path)
    assert manager.restart_conservative() is None


def test_local_manager_falls_back_without_cache_reuse_when_server_rejects_flag(
        tmp_path):
    """旧版 llama-server 不认识 --cache-reuse 会报错退出 → 自动降级重试。"""
    server = _write_fake_runtime(tmp_path / "runtime")
    model = _write_fake_gguf(tmp_path / "model.gguf")
    created = []

    class FakeProcess:
        pid = 456

        def __init__(self, command, **_kwargs):
            self.command = command
            self.terminated = False
            created.append(self)
            # 模拟旧版：遇到 --cache-reuse 立即以错误码退出
            self.returncode = 1 if "--cache-reuse" in command else None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    config = ApiConfig(
        mode="local", local_server_path=str(server),
        local_model_path=str(model), local_port=18082,
    )
    manager = LocalModelManager(
        tmp_path, process_factory=FakeProcess,
        probe=lambda _url, _token, _model: True, sleep=lambda _seconds: None,
        token_factory=lambda: "local-token",
    )

    runtime = manager.ensure_running(config)

    assert runtime.endpoint == "http://127.0.0.1:18082/v1"
    # 默认 gpu_layers=-1 → 尝试组合：(999, 带缓存) → (0, 带缓存) → (999, 不带缓存)
    assert len(created) == 3
    assert "--cache-reuse" in created[0].command
    assert "--cache-reuse" in created[1].command
    assert "--cache-reuse" not in created[2].command
    # 前两次带缓存启动被旧版 server 拒绝（已退出，无需 terminate）
    assert created[0].terminated is False
    assert created[1].terminated is False

    manager.stop()
    assert created[2].terminated is True
    assert manager.runtime is None


class _ReusableFakeProcess:
    """跨实例测试用：进程保持存活直到被 terminate，记录终止状态。"""

    def __init__(self, _command, **_kwargs):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return None

    def kill(self):
        self.terminated = True


def test_cross_instance_reuse_shared_state_and_stop_ownership(tmp_path):
    """跨实例复用：A 启动服务写状态文件，B（同 state_dir）探测命中直接复用；
    B.stop() 不杀 A 的进程；A.stop() 杀自己的进程并清理状态文件。"""
    server = _write_fake_runtime(tmp_path / "runtime")
    model = _write_fake_gguf(tmp_path / "model.gguf")
    state_dir = tmp_path / "state"
    live: list[_ReusableFakeProcess] = []

    def factory(command, **_kwargs):
        proc = _ReusableFakeProcess(command, **_kwargs)
        live.append(proc)
        return proc

    config = ApiConfig(
        mode="local", local_server_path=str(server),
        local_model_path=str(model), local_port=18084,
    )
    manager_a = LocalModelManager(
        tmp_path, state_dir=state_dir, process_factory=factory,
        probe=lambda _url, _token, _model: True, sleep=lambda _s: None,
        token_factory=lambda: "token-a",
    )
    runtime_a = manager_a.ensure_running(config)

    assert runtime_a.backend == "gpu"
    assert len(live) == 1 and live[0].terminated is False
    assert (state_dir / "model_runtime.json").is_file()

    # 实例 B：不同 app_dir、相同 state_dir → 探测命中直接复用，不新开进程
    manager_b = LocalModelManager(
        tmp_path / "other", state_dir=state_dir, process_factory=factory,
        probe=lambda _url, _token, _model: True, sleep=lambda _s: None,
        token_factory=lambda: "token-b",
    )
    runtime_b = manager_b.ensure_running(config)

    assert runtime_b is not runtime_a
    assert runtime_b.endpoint == runtime_a.endpoint
    assert runtime_b.backend == "external"   # 复用标记：非本实例启动
    assert runtime_b.pid is None
    assert runtime_b.api_key == "token-a"    # 复用 A 的 token
    assert len(live) == 1                    # 未新开进程

    # B.stop() 只清自己的引用，不动 A 的服务（外部服务不归 B 管）
    manager_b.stop()
    assert live[0].terminated is False
    assert manager_b.runtime is None

    # A.stop() 杀自己启动的进程并清理状态文件（B 复用后将不可再命中）
    manager_a.stop()
    assert live[0].terminated is True
    assert not (state_dir / "model_runtime.json").exists()


def test_cross_instance_skips_reuse_on_signature_mismatch(tmp_path):
    """签名不匹配（不同模型）→ 不复用，各自启动自己的服务。"""
    server = _write_fake_runtime(tmp_path / "runtime")
    model = _write_fake_gguf(tmp_path / "model.gguf")
    other = _write_fake_gguf(tmp_path / "other.gguf")
    state_dir = tmp_path / "state"
    live: list[_ReusableFakeProcess] = []

    def factory(command, **_kwargs):
        proc = _ReusableFakeProcess(command, **_kwargs)
        live.append(proc)
        return proc

    config_a = ApiConfig(
        mode="local", local_server_path=str(server),
        local_model_path=str(model), local_port=18085,
    )
    config_b = ApiConfig(
        mode="local", local_server_path=str(server),
        local_model_path=str(other), local_port=18085,
    )
    manager_a = LocalModelManager(
        tmp_path, state_dir=state_dir, process_factory=factory,
        probe=lambda _url, _token, _model: True, sleep=lambda _s: None,
        token_factory=lambda: "token-a",
    )
    runtime_a = manager_a.ensure_running(config_a)

    manager_b = LocalModelManager(
        tmp_path / "other", state_dir=state_dir, process_factory=factory,
        probe=lambda _url, _token, _model: True, sleep=lambda _s: None,
        token_factory=lambda: "token-b",
    )
    runtime_b = manager_b.ensure_running(config_b)

    assert runtime_b.backend == "gpu"      # 模型不同 → 全新启动
    assert runtime_b.api_key == "token-b"
    assert len(live) == 2

    manager_b.stop()
    manager_a.stop()
    assert all(proc.terminated for proc in live)
    assert not (state_dir / "model_runtime.json").exists()


def test_runtime_manifest_requires_server_cpu_and_cuda_dependencies(tmp_path):
    required = [
        "llama-server.exe", "llama-server-impl.dll", "llama-common.dll",
        "llama.dll", "ggml.dll", "ggml-base.dll", "ggml-cpu-x64.dll",
        "ggml-cuda.dll", "cublas64_13.dll", "cublasLt64_13.dll",
        "cudart64_13.dll",
    ]
    for name in required:
        (tmp_path / name).write_bytes(b"runtime")

    assert validate_runtime_manifest(tmp_path, cuda=True) == ()

    (tmp_path / "ggml-cuda.dll").unlink()
    missing = validate_runtime_manifest(tmp_path, cuda=True)
    assert "ggml-cuda.dll" in missing


def test_local_manager_falls_back_from_gpu_to_cpu_once(tmp_path):
    server = _write_fake_runtime(tmp_path / "runtime")
    model = _write_fake_gguf(tmp_path / "model.gguf")
    commands = []

    class FakeProcess:
        pid = 456

        def __init__(self, command, **_kwargs):
            commands.append(command)
            self.returncode = 1 if len(commands) == 1 else None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    manager = LocalModelManager(
        tmp_path, process_factory=FakeProcess,
        probe=lambda _url, _token, _model: True, sleep=lambda _seconds: None,
        token_factory=lambda: "token",
    )
    config = ApiConfig(
        mode="local", local_server_path=str(server),
        local_model_path=str(model), local_port=18082,
        local_gpu_layers=-1,
    )

    runtime = manager.ensure_running(config)
    try:
        assert runtime.backend == "cpu"
        assert len(commands) == 2
        assert commands[0][commands[0].index("--n-gpu-layers") + 1] == "-1"
        assert commands[1][commands[1].index("--n-gpu-layers") + 1] == "0"
    finally:
        manager.stop()
