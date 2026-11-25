"""Unified local llama.cpp runtime manager.

Single lifecycle manager replacing legacy multi-manager fragmentation:
- Auto-selects backend: CUDA 12.4 (NVIDIA) -> Vulkan (AMD/Intel/NVIDIA) -> CPU
- Supports staggered on-demand loading:
  - `translate` (Hy-MT3-1.8B): kept warm while translating / playing
  - `reason` (Qwen3.5-4B): launched on demand for context & review, auto-unloads after idle TTL
- Single-model mode: reuses one server when user prefers minimal VRAM/disk footprint
"""
from __future__ import annotations

import ctypes
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from ul10n.core.models import AppSettings, RoleEndpointConfig
from ul10n.engine.llm_client import LLMClient


def _create_kill_on_close_job() -> int:
    """Create a Windows Job Object that terminates all assigned children when parent dies."""
    if sys.platform != "win32":
        return 0
    try:
        kernel32 = ctypes.windll.kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return 0

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [(f, ctypes.c_uint64) for f in ("r", "w", "o", "rt", "wt", "ot")]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        info.BasicLimitInformation.LimitFlags = 0x2000
        # JobObjectExtendedLimitInformation = 9
        ok = kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            kernel32.CloseHandle(job)
            return 0
        return int(job)
    except Exception:
        return 0


def _assign_process_to_job(job_handle: int, proc: subprocess.Popen) -> None:
    if sys.platform != "win32" or not job_handle:
        return
    try:
        handle = getattr(proc, "_handle", None)
        if handle:
            ctypes.windll.kernel32.AssignProcessToJobObject(
                ctypes.c_void_p(job_handle), ctypes.c_void_p(int(handle))
            )
    except Exception:
        pass

_ROLE_DEFAULT_PORTS = {"translate": 18801, "reason": 18802}
_ROLE_HINTS = {
    "translate": ("hy-mt3", "hy-mt2", "hy-mt"),
    "reason": ("qwen3.5", "qwen3", "gemma-4", "gemma4", "phi-4"),
}


@dataclass
class ManagedServer:
    role: str
    model_path: Path
    backend: str
    port: int
    api_key: str
    process: subprocess.Popen
    last_used: float
    idle_ttl: float = 0.0  # 0 = stay resident until stop()

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


def _detect_nvidia_gpu() -> str | None:
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=flags,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().splitlines()[0].strip()
    except Exception:
        pass
    return None


def _pick_free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return int(s.getsockname()[1])
            except OSError:
                continue
    raise RuntimeError("无法分配本地监听端口")


class LlamaRuntimeManager:
    """Manages local llama-server processes with hardware auto-detection & idle unloading."""

    def __init__(
        self,
        app_root: str | Path,
        *,
        on_status_change: Callable[[dict[str, Any]], None] | None = None,
        reason_idle_ttl: float = 240.0,
    ):
        self.app_root = Path(app_root).resolve()
        self.models_dir = self.app_root / "models"
        self.runtime_dir = self.app_root / "runtime"
        self.logs_dir = self.app_root / "logs"
        self.reason_idle_ttl = reason_idle_ttl
        self._on_status_change = on_status_change
        self._lock = threading.RLock()
        self._role_locks: dict[str, threading.Lock] = {
            "translate": threading.Lock(),
            "reason": threading.Lock(),
        }
        self._servers: dict[str, ManagedServer] = {}
        self._starting: set[str] = set()
        self._job_handle = _create_kill_on_close_job()
        self._nvidia_gpu = _detect_nvidia_gpu()
        self._stop_reaper = threading.Event()
        self._reaper_thread = threading.Thread(target=self._reaper_loop, daemon=True)
        self._reaper_thread.start()

    def discover_model(self, role: str) -> Path | None:
        if not self.models_dir.is_dir():
            return None
        ggufs = sorted(self.models_dir.glob("*.gguf"), key=lambda p: p.name.casefold())
        if not ggufs:
            return None
        hints = _ROLE_HINTS.get(role, ())
        for hint in hints:
            for p in ggufs:
                if hint in p.name.casefold():
                    return p.resolve()
        return ggufs[0].resolve()

    def resolve_server_binary(self, hardware_mode: str = "auto") -> tuple[Path, str, int]:
        """Return `(llama_server_exe, backend_label, gpu_layers)`."""
        cuda_dir = self.runtime_dir / "llama-cuda"
        vulkan_dir = self.runtime_dir / "llama-vulkan"
        cuda_exe = cuda_dir / "llama-server.exe"
        vulkan_exe = vulkan_dir / "llama-server.exe"

        has_cuda_bundle = (
            cuda_exe.is_file()
            and (cuda_dir / "cublas64_12.dll").is_file()
            and (cuda_dir / "cudart64_12.dll").is_file()
        )
        has_vulkan_bundle = vulkan_exe.is_file() and (vulkan_dir / "ggml-vulkan.dll").is_file()

        if hardware_mode == "cpu":
            exe = vulkan_exe if vulkan_exe.is_file() else cuda_exe
            if exe.is_file():
                return exe, "CPU", 0
        else:
            # RTX 50-series (Blackwell sm_120) lacks native kernels in CUDA 12.4 builds
            # and hits slow PTX JIT; route Blackwell directly to Vulkan (125+ tok/s).
            is_blackwell = bool(self._nvidia_gpu and "rtx 50" in self._nvidia_gpu.casefold())
            if has_cuda_bundle and self._nvidia_gpu and not is_blackwell:
                return cuda_exe, "CUDA", -1
            if has_vulkan_bundle:
                return vulkan_exe, "Vulkan", -1
            if has_cuda_bundle and self._nvidia_gpu:
                return cuda_exe, "CUDA", -1
            if cuda_exe.is_file():
                return cuda_exe, "CPU", 0

        from_path = shutil.which("llama-server") or shutil.which("llama-server.exe")
        if from_path:
            return Path(from_path).resolve(), ("CPU" if hardware_mode == "cpu" else "AUTO"), (0 if hardware_mode == "cpu" else -1)
        raise FileNotFoundError("未找到本地推理程序 llama-server.exe，请检查 runtime 目录。")

    def status_snapshot(self, settings: AppSettings) -> dict[str, Any]:
        """Return clean, human-friendly runtime status (zero noisy technical clutter)."""
        with self._lock:
            roles_info: dict[str, Any] = {}
            for role in ("translate", "reason"):
                cfg = self._role_cfg(settings, role)
                if cfg.mode == "api":
                    ready = bool(cfg.base_url and cfg.model)
                    roles_info[role] = {
                        "mode": "api",
                        "state": "ready" if ready else "unconfigured",
                        "label": cfg.model or "未配置",
                        "backend": "云端",
                    }
                    continue
                srv = self._servers.get(role)
                model_path = self.discover_model(role)
                if srv and srv.alive:
                    state = "ready"
                    backend = srv.backend
                elif role in self._starting:
                    state = "starting"
                    backend = ""
                elif model_path is None:
                    state = "missing"
                    backend = ""
                else:
                    state = "idle"
                    backend = ""
                roles_info[role] = {
                    "mode": "local",
                    "state": state,
                    "label": model_path.stem if model_path else "未安装模型",
                    "backend": backend,
                }
            return {
                "gpu_name": self._nvidia_gpu or "",
                "hardware_mode": settings.hardware_mode,
                "roles": roles_info,
            }

    @staticmethod
    def _role_cfg(settings: AppSettings, role: str) -> RoleEndpointConfig:
        if role == "reason":
            rs = settings.reason_role
            tr = settings.translate_role
            # When using cloud API and reason endpoint is left blank (or shares URL with blank key),
            # seamlessly inherit from translate_role so review/context never fail with 401/empty URL.
            if rs.mode == "api" and tr.mode == "api":
                if not rs.base_url or not rs.model:
                    return tr
                if not rs.api_key and rs.base_url.rstrip("/") == tr.base_url.rstrip("/") and tr.api_key:
                    return RoleEndpointConfig(
                        mode=rs.mode,
                        base_url=rs.base_url,
                        api_key=tr.api_key,
                        model=rs.model,
                        temperature=rs.temperature,
                        max_tokens=rs.max_tokens,
                    )
            return rs
        return settings.translate_role

    def create_client_for_role(self, role: str, settings: AppSettings) -> LLMClient:
        """Return a ready LLMClient for `translate` or `reason`, auto-starting local server if needed."""
        cfg = self._role_cfg(settings, role)

        if cfg.mode == "api":
            if not cfg.base_url or not cfg.model:
                raise RuntimeError(f"在线 API（{role}）尚未填写接口地址或模型名。")
            return LLMClient(cfg.base_url, cfg.api_key, cfg.model)

        srv = self.ensure_local_server(role, hardware_mode=settings.hardware_mode)
        return LLMClient(srv.base_url, srv.api_key, srv.model_path.stem)

    def ensure_local_server(self, role: str, *, hardware_mode: str = "auto") -> ManagedServer:
        # Per-role startup mutex guarantees concurrent cold-start requests never spawn duplicate servers
        role_lock = self._role_locks.setdefault(role, threading.Lock())
        with role_lock:
            with self._lock:
                srv = self._servers.get(role)
                if srv and srv.alive:
                    srv.last_used = time.monotonic()
                    return srv
                model_path = self.discover_model(role)
                if model_path is None:
                    raise RuntimeError(f"models/ 目录下未找到 {role} 模型文件（.gguf）。")
                exe, backend, gpu_layers = self.resolve_server_binary(hardware_mode)
                port = _pick_free_port(_ROLE_DEFAULT_PORTS.get(role, 18805))
                api_key = secrets.token_urlsafe(18)
                self._starting.add(role)

            self._emit_status()
            try:
                srv = self._spawn_and_wait(
                    role=role,
                    exe=exe,
                    model_path=model_path,
                    backend=backend,
                    gpu_layers=gpu_layers,
                    port=port,
                    api_key=api_key,
                )
                with self._lock:
                    self._servers[role] = srv
                    self._starting.discard(role)
                self._emit_status()
                return srv
            except Exception:
                with self._lock:
                    self._starting.discard(role)
                self._emit_status()
                raise

    def _spawn_and_wait(
        self,
        *,
        role: str,
        exe: Path,
        model_path: Path,
        backend: str,
        gpu_layers: int,
        port: int,
        api_key: str,
    ) -> ManagedServer:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.logs_dir / f"llama-{role}.log"
        ctx_size = "4096" if role == "translate" else "8192"
        parallel = "4" if role == "translate" else "1"
        cmd = [
            str(exe),
            "--model", str(model_path),
            "--host", "127.0.0.1",
            "--port", str(port),
            "--api-key", api_key,
            "--ctx-size", ctx_size,
            "--n-gpu-layers", str(gpu_layers),
            "--parallel", parallel,
            "--flash-attn", "auto",
        ]
        if role == "translate":
            cmd.append("--cont-batching")
        if role == "reason":
            cmd.extend(["--reasoning", "off"])

        flags = 0
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        with log_path.open("a", encoding="utf-8", errors="replace") as log_fh:
            proc = subprocess.Popen(
                cmd,
                cwd=str(exe.parent),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
        # Child inherited the handle; parent handle is closed cleanly (zero FD leak)
        _assign_process_to_job(self._job_handle, proc)

        # Wait until /health returns 200
        deadline = time.monotonic() + 120.0
        health_url = f"http://127.0.0.1:{port}/health"
        headers = {"Authorization": f"Bearer {api_key}"}
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                tail = ""
                try:
                    tail = log_path.read_text(encoding="utf-8", errors="replace")[-400:]
                except OSError:
                    pass
                raise RuntimeError(f"本地模型启动退出（code={proc.returncode}）：{tail}")
            try:
                r = httpx.get(health_url, headers=headers, timeout=1.5, trust_env=False)
                if r.status_code == 200:
                    return ManagedServer(
                        role=role,
                        model_path=model_path,
                        backend=backend,
                        port=port,
                        api_key=api_key,
                        process=proc,
                        last_used=time.monotonic(),
                        idle_ttl=self.reason_idle_ttl if role == "reason" else 0.0,
                    )
            except httpx.HTTPError:
                pass
            time.sleep(0.4)

        proc.terminate()
        raise RuntimeError(f"本地模型启动超时（{model_path.name}）。")

    def stop_role(self, role: str) -> None:
        with self._lock:
            srv = self._servers.pop(role, None)
        if srv and srv.alive:
            srv.process.terminate()
            try:
                srv.process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                srv.process.kill()
        self._emit_status()

    def stop_all(self) -> None:
        self._stop_reaper.set()
        with self._lock:
            roles = list(self._servers.keys())
        for role in roles:
            self.stop_role(role)
        if sys.platform == "win32" and self._job_handle:
            try:
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self._job_handle))
            except Exception:
                pass
            self._job_handle = 0

    def _reaper_loop(self) -> None:
        while not self._stop_reaper.wait(15.0):
            now = time.monotonic()
            to_stop: list[str] = []
            with self._lock:
                for role, srv in self._servers.items():
                    if srv.idle_ttl > 0 and srv.alive and (now - srv.last_used) >= srv.idle_ttl:
                        to_stop.append(role)
            for role in to_stop:
                self.stop_role(role)

    def _emit_status(self) -> None:
        if self._on_status_change:
            try:
                self._on_status_change({"type": "runtime_changed"})
            except Exception:
                pass
