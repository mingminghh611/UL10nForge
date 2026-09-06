"""审核模型（Qwen3.5-4B）本地服务管理（任务一阶段 1 T1-2 基础）。

背景：LocalModelManager 是「单模型」管理器（model_runtime.json 只
记录一个签名，翻译/审核是不同模型文件，交替 ensure_running 会互杀）。
阶段 0 的多实例方案下，审核服务需要自己的独立 runtime 状态文件
review_runtime.json——复用同一套「跨实例探测复用 + 按需启动」机制，
但互不干扰：翻译实例的启停不碰审核实例。

服务特征（ModelSpec review）：
- 端口 8081（DEFAULT_PORTS）
- ctx 8192（DEFAULT_CTX）
- `--reasoning off`（thinking 模型：默认把输出预算耗在 reasoning_content，
  冒烟实证 content 空串 + finish=length；关闭后稳定输出合法 JSON）
- keep-alive -1 常驻（审核穿插在排查各环节，常驻避免反复读 3GB）
"""
from __future__ import annotations

import json
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from .local_model import build_server_command, discover_server, sha256_of
from .model_registry import ModelRegistry, ModelSpec

_RUNTIME_STATE_FILENAME = "review_runtime.json"


def _spawn(cmd: list[str], log_path: Path) -> subprocess.Popen:
    """启动 llama-server 进程（Windows 无窗口标志）。"""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    handle = log_path.open("a", encoding="utf-8", errors="replace")
    return subprocess.Popen(
        cmd, cwd=str(Path(cmd[0]).parent), stdout=handle,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace", creationflags=creationflags)


class ReviewModelService:
    """审核模型服务：本地 llama.cpp 或在线 API 端点（二选一）。

    用法（SemanticReviewer 内部调用）：
        svc = ReviewModelService(app_dir)
        info = svc.ensure_running()   # {"base_url": ".../v1", "api_key": ...}
        result = svc.chat(prompt)     # 直接对话（内部走 /v1/chat/completions）
        svc.release()                 # 不杀进程：保留给后续实例复用

    online_cfg（2026-08-14 在线 API 模式）：传入 ApiConfig 且
    base_url/api_key/model 齐全时走「在线端点」——ensure_running 直接
    返回配置端点，不启动/探测本地进程（在线模式不多跑本地模型）；
    缺任一项则回退本地启动路径（现有行为不变）。
    """

    def __init__(self, app_dir: str | Path, *,
                 process_factory=None, probe=None, sleep=None,
                 token_factory=None, startup_timeout: float = 180.0,
                 online_cfg=None):
        self.app_dir = Path(app_dir).resolve()
        self._process_factory = process_factory or subprocess.Popen
        self._probe = probe or self._http_probe
        self._sleep = sleep or time.sleep
        self._token_factory = token_factory or (
            lambda: secrets.token_urlsafe(24))
        self.startup_timeout = max(10.0, float(startup_timeout))
        self._process: subprocess.Popen | None = None
        self._log_handle = None
        self._runtime: dict | None = None      # {"base_url", "api_key"}
        self._lock = threading.RLock()
        self._online: dict | None = None
        self._online_provider: str = "openai"
        if online_cfg is not None and online_cfg.base_url \
                and online_cfg.api_key and online_cfg.model:
            self._online = {
                "base_url": str(online_cfg.base_url).rstrip("/"),
                "api_key": str(online_cfg.api_key),
                "model": str(online_cfg.model),
            }
            # Anthropic 原生端点（api.anthropic.com/messages）：鉴权头
            # x-api-key + anthropic-version，请求体格式与 OpenAI 不同。
            # 其余（含本地 llama-server /v1）走 OpenAI 兼容形式。在线
            # 端点没有 provider 强制 1:1——OpenAI 兼容代理（DeepSeek/
            # 硅基流动等）都自称 openai。
            self._online_provider = str(
                getattr(online_cfg, "provider", "") or "openai")

    # ── 跨实例运行时状态（review_runtime.json，独立于翻译实例） ────
    @property
    def _state_file(self) -> Path:
        return self.app_dir / _RUNTIME_STATE_FILENAME

    def _save_state(self, port: int, api_key: str, model: str,
                    signature: tuple) -> None:
        try:
            self._state_file.write_text(
                json.dumps({
                    "port": int(port), "api_key": api_key,
                    "model": str(model),
                    "signature": [str(item) for item in signature],
                }, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # 状态文件只是加速复用，失败不影响启动

    def _load_state(self) -> dict | None:
        try:
            if not self._state_file.is_file():
                return None
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("port"), int):
                return None
            return data
        except (OSError, ValueError):
            return None

    @staticmethod
    def _http_probe(base: str, api_key: str, expected_model: str) -> bool:
        """探测审核实例：/health 200 且 /v1/models 含目标模型。

        必须携带 Authorization 头（2026-08-13 hickory 实证：不带 key
        的探测对带鉴权的 llama-server 返回 401 → 误判「实例不可用」→
        并行 runner 各自重复启动 4B；Windows llama-server SO_REUSEADDR
        多实例绑同一端口，连接被最新实例接收，复用者拿旧 key 请求 →
        Invalid API Key → 审核静默 0 判定）。
        """
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            health = httpx.get(base + "/health", timeout=2, trust_env=False, verify=False)
            if health.status_code != 200:
                return False
            models = httpx.get(base + "/v1/models", timeout=2,
                               headers=headers, trust_env=False, verify=False)
            if models.status_code != 200:
                return False
            ids = [str(m.get("id", ""))
                   for m in models.json().get("data", [])]
            return any(expected_model.casefold() in i.casefold()
                       for i in ids)
        except httpx.HTTPError:
            return False

    def _spec(self) -> ModelSpec:
        return ModelRegistry(self.app_dir).by_kind("review")

    # ── 主入口 ────────────────────────────────────────────────────
    def ensure_running(self, cancellation_event=None,
                       context_size: int | None = None,
                       gpu_choice: str | None = None) -> dict:
        """确保审核服务就绪，返回 {"base_url": ".../v1", "api_key": ...}。

        - 本实例已启动同签名服务 → 直接复用
        - review_runtime.json 记录同签名服务 → 探测通过则复用
        - 否则启动新实例（build_server_command + --reasoning off）

        gpu_choice（环境设置页四模型卡片）：auto → hardware_planner
        决策；cpu → gpu_layers=0 强制 CPU；gpu → 999 强制全层
        （llama.cpp 对超层数 clamp 到全部层，绕过 planner 的 -1 接管）。
        在线模式（构造时 online_cfg 完整）：直接返回外部端点——
        不探测本地端口、不启动 llama-server（不多跑本地模型）。
        """
        if self._online is not None:
            return dict(self._online)
        spec = self._spec()
        if not spec.is_available:
            raise RuntimeError(
                f"审核模型缺失：{spec.path}（models/ 目录无 Qwen3.5-4B GGUF）")
        # 硬件智能分配（任务三接通）：静态档位规划覆盖模板 ctx 与 GPU
        # 层数（4~6GB 档 4B 上 GPU / 显存紧张降 4096 / 无 GPU 全 CPU）；
        # 探测失败回退模板默认值（ctx 模板 + gpu_layers=-1 全层）
        plan = None
        try:
            from hanhua.core.hardware_planner import (
                plan_allocation, probe_hardware)
            plan = plan_allocation(
                probe_hardware(), ModelRegistry(self.app_dir)).get("review")
        except Exception:  # noqa: BLE001 - 探测失败不阻断启动
            plan = None
        server = discover_server("", self.app_dir)
        ctx = context_size or (plan.ctx if plan else spec.default_ctx)
        if gpu_choice == "cpu":
            gpu_layers = 0
        elif gpu_choice == "gpu":
            gpu_layers = 999
        else:
            gpu_layers = plan.gpu_layers if plan else -1
        # 审计 Phase D（P1-12）：模型 sha256 进签名——审核模型文件更新后
        # 旧 state 签名不匹配 → 不复用旧实例（自动重启新模型）
        signature = (server, spec.path, sha256_of(spec.path), spec.port,
                     ctx, gpu_layers, 1)
        with self._lock:
            if (self._process is not None
                    and self._process.poll() is None
                    and self._runtime is not None):
                return dict(self._runtime)
        # 跨实例复用
        state = self._load_state()
        if state is not None and tuple(state.get("signature", ())) == tuple(
                str(item) for item in signature):
            base = f"http://127.0.0.1:{int(state['port'])}"
            if self._probe(base, str(state.get("api_key", "")),
                           spec.path.stem):
                with self._lock:
                    self._runtime = {
                        "base_url": base + "/v1",
                        "api_key": str(state.get("api_key", "")),
                        "port": int(state["port"]),
                    }
                    return dict(self._runtime)
        # 启动新实例前清场（hickory 实证 2026-08-13）：Windows 下
        # llama-server 多实例可用 SO_REUSEADDR 绑同一端口，新连接由内核
        # 随机分发给任一实例——8081 上残留实例（崩溃 runner 的 4B、手动
        # 测试实例）与本实例并存时，复用者拿 runtime key 请求会被其他
        # 实例 401 拒（「审核 0 判定」静默失败）。启动前若 8081 已被
        # 其他 llama-server 占用（key 与 runtime 不匹配才走到这里），
        # 先杀占用者再启动，保证端口上永远只有本实例。
        if cancellation_event is not None and cancellation_event.is_set():
            raise RuntimeError("审核服务启动已取消")
        self._clear_stale_review_port(spec.port)
        api_key = self._token_factory()
        cmd = build_server_command(
            server, spec.path, port=spec.port, api_key=api_key,
            context_size=ctx, gpu_layers=gpu_layers, parallel=1,
            cache_reuse=512)
        cmd.extend(spec.server_args)   # ("--reasoning", "off")
        self._stop_locked()
        log_path = self.app_dir / "logs" / "review-server.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = self._process_factory(
                cmd, cwd=str(Path(cmd[0]).parent), stdout=log_path.open(
                    "a", encoding="utf-8", errors="replace"),
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
                if sys.platform == "win32" else 0)
        except OSError as exc:
            raise RuntimeError(f"审核服务启动失败：{exc}") from exc
        with self._lock:
            self._process = proc
        deadline = time.monotonic() + self.startup_timeout
        base = f"http://127.0.0.1:{spec.port}"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                tail = self._log_tail(log_path)
                raise RuntimeError(f"审核服务异常退出（{proc.returncode}）："
                                   f"{tail[-400:]}")
            if cancellation_event is not None and cancellation_event.is_set():
                raise RuntimeError("审核服务启动已取消")
            if self._probe(base, api_key, spec.path.stem):
                self._save_state(spec.port, api_key, spec.path, signature)
                with self._lock:
                    self._runtime = {
                        "base_url": base + "/v1", "api_key": api_key,
                        "port": spec.port,
                    }
                # 2026-08-14 孤儿实证：GUI close 未清理 review 4B →
                # 会话间残留累积。注册到共享协调器（同 app_dir 单例），
                # 退出 stop_all_coordinators 统一回收。
                try:
                    from hanhua.core.runtime_coordinator import (
                        get_coordinator)
                    get_coordinator(self.app_dir).register(
                        "review", proc, spec.port, api_key, spec.path)
                except Exception:  # noqa: BLE001 - 注册失败不影响主流程
                    pass
                return dict(self._runtime)
            self._sleep(2.0)
        tail = self._log_tail(log_path)
        raise RuntimeError(f"审核服务启动超时（{self.startup_timeout}s）："
                           f"{tail[-400:]}")

    @staticmethod
    def _clear_stale_review_port(port: int) -> None:
        """杀掉占用审核端口的残留 llama-server（确保端口上唯一实例）。

        Windows: netstat -ano 列出 LISTENING 该端口的 PID → taskkill /F。
        只杀端口占用者（并行 runner 的翻译实例在不同端口，不受影响）；
        杀不掉的（权限/已消失）静默继续——启动失败由现有超时/退出
        检测兜底。
        """
        # 弹窗根因（2026-08-13 用户实证：送审时命令行窗口反复跳出又消失）
        # ——netstat/taskkill 缺 CREATE_NO_WINDOW，每次探测失败清场都闪
        # 控制台窗口。与 _spawn/ensure_running 的 Popen 对齐：win32 全部
        # 加 CREATE_NO_WINDOW。
        nowindow = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if sys.platform == "win32" else 0)
        try:
            out = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True,
                errors="replace", timeout=10,
                creationflags=nowindow).stdout
        except (OSError, subprocess.TimeoutExpired):
            return
        pids: set[str] = set()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "TCP" and \
                    parts[1].endswith(f":{port}") and \
                    parts[3] == "LISTENING":
                pids.add(parts[4])
        for pid in pids:
            # 2026-08-26 安全加固：只杀 llama-server.exe——此前对端口上
            # 任意 PID 直接 taskkill /F，若用户有无关服务占用该端口会被
            # 误杀（与 local_model._kill_llama_on_port 同判据：先 tasklist
            # 校验进程名再杀）。杀不掉的（权限/已消失）静默继续。
            try:
                rows = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, errors="replace",
                    timeout=10, creationflags=nowindow).stdout
                name = rows.split(",")[0].strip('"') if rows else ""
                if name.lower() != "llama-server.exe":
                    continue
            except (OSError, subprocess.TimeoutExpired):
                continue
            try:
                subprocess.run(["taskkill", "/F", "/PID", pid],
                               capture_output=True, timeout=10,
                               creationflags=nowindow)
            except (OSError, subprocess.TimeoutExpired):
                continue

    @staticmethod
    def _log_tail(path: Path, limit: int = 2000) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")[-limit:]
        except OSError:
            return "（无日志）"

    def _stop_locked(self) -> None:
        """终止本实例启动的进程（跨实例复用的外部实例不动）。"""
        with self._lock:
            proc = self._process
            self._process = None
            self._runtime = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ── 对话 ─────────────────────────────────────────────────────
    @staticmethod
    def _endpoint_url(base_url: str, provider: str) -> str:
        """端点 URL 归一化（与 translator.normalize_base_url 同语义）。

        fromivan 实证（2026-09-06）：用户在设置里填的 base_url 有两种
        习惯——带 /v1（https://host/v1）与不带（https://host）。此前
        chat() 硬拼固定后缀：openai 恒 + /chat/completions（不带 /v1
        的配置 → 404）；anthropic 恒 + /v1/messages（用户已带 /v1 →
        /v1/v1/messages 404）。翻译链路（BaseClient）早已按后缀智能
        补全，审核链路补齐同一规则——两链路行为一致。
        """
        url = base_url.strip().rstrip("/")
        if provider == "anthropic":
            if url.endswith("/messages"):
                return url
            return url + ("/messages" if url.endswith("/v1")
                          else "/v1/messages")
        if url.endswith("/chat/completions"):
            return url
        return url + ("/chat/completions" if url.endswith("/v1")
                      else "/v1/chat/completions")

    def chat(self, prompt: str, *, max_tokens: int = 1024,
             temperature: float = 0.1, timeout: float = 120.0) -> str:
        """审核模型单轮对话，返回 content 文本（无 reasoning）。

        本地（llama.cpp /v1）与 OpenAI 兼容在线端点都走 /chat/completions
        Bearer 形式；Anthropic 原生端点（api.anthropic.com）走
        /v1/messages + x-api-key 头 + anthropic-version（2026-08-21
        云端链路修复：此前在线端点恒用 OpenAI 形式，Anthropic provider
        兼容性缺失；2026-09-06 修复 base_url 带/不带 /v1 的两种填法，
        见 _endpoint_url）。
        """
        info = self.ensure_running()
        if self._online_provider == "anthropic":
            resp = httpx.post(
                self._endpoint_url(info["base_url"], "anthropic"),
                headers={
                    "x-api-key": info["api_key"],
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={"model": info["model"], "max_tokens": max_tokens,
                      "temperature": temperature,
                      "system": prompt,
                      "messages": []},
                timeout=timeout, trust_env=False, verify=False)
            resp.raise_for_status()
            data = resp.json()
            # Anthropic content 是数组：[{"type": "text", "text": "..."}]
            for block in data.get("content", []) or []:
                if block.get("type") == "text" and block.get("text"):
                    return str(block["text"])
            return ""
        resp = httpx.post(
            self._endpoint_url(info["base_url"], "openai"),
            headers={"Authorization": f"Bearer {info['api_key']}"},
            json={"model": info.get("model", "local"),
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": temperature, "max_tokens": max_tokens},
            timeout=timeout, trust_env=False, verify=False)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def release(self) -> None:
        """保留服务供后续复用（幂等，不杀外部实例）。"""
        with self._lock:
            self._process = None
            self._runtime = None

    def stop(self) -> None:
        """终止本实例启动的审核服务进程（清理场景用）。

        在线模式无本地进程（online_cfg 时 _process 恒 None）：外部端点
        不在本地管理范围，no-op（_online 保留——服务「恒在」）。
        """
        if self._online is not None:
            return
        self._stop_locked()
