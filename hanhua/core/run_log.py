# -*- coding: utf-8 -*-
"""全量运行日志（0.41.0 任务七：实时处理流/运行记录全量记录）。

背景（用户实证）：GUI 运行区只有两块内存 UI——实时处理流
（ActivityFeed，max_items=120 丢最旧）与运行记录（QPlainTextEdit，
MaximumBlockCount=2000 静默截断），start()/切换项目还会 clear()。
翻译几十分钟的长跑里，早前的批次进度、审核判定、写回审计明细
在界面上和磁盘上都不复存在——事后复盘「当时到底发生了什么」无据可查。

本模块是运行信息单一落盘出口（append-only、线程安全、永不抛错）：
- 每个项目一个日志文件 `<app_dir>/run-logs/<游戏目录名>__<md5[:6]>.log`，
  按游戏目录身份（绝对路径 md5 前缀）区分同名目录；
- 每轮扫描/翻译/写回以 begin_session 分节，跨会话持续追加；
- 所有 IO 失败（磁盘满/权限/非法路径）静默禁用——日志是附属功能，
  绝不阻断翻译/写回主流程（与 record_writer 同一原则）。

事件来源（接线方）：
- home_page：扫描阶段事件（detection/text_scan/tool_analysis/
  binary_scan，phase/status/message/current/total）+ 扫描完成/失败；
- translate_page：翻译日志行、批次进度、审核判定/处置进度、审核汇总、
  写回阶段/验证明细、地毯式审计事件、停止/重试/阻断、记录导出路径。

写入只经主线程信号回调发生（worker 经 Qt 信号回主线程），但内部仍
持锁——防御未来直接从 worker 线程调用。
"""
from __future__ import annotations

import datetime
import hashlib
import re
import threading
from pathlib import Path

# Windows 文件名非法字符 + 控制字符 → 下划线
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# 模块级缓存：同一日志文件（同项目）跨页面共享一个 RunLog 实例，
# 避免多处打开同一文件句柄互相覆盖缓冲。
_LOG_CACHE: dict[Path, "RunLog"] = {}
_CACHE_LOCK = threading.RLock()


def run_log_path(app_dir: Path, game_dir: Path) -> Path:
    """项目的运行日志路径：`run-logs/<安全目录名>__<md5[:6]>.log`。

    目录名做字符消毒并截断（防超长路径）；md5 前缀用绝对路径区分
    不同位置的同名游戏目录。同一 game_dir 恒定映射到同一文件，
    home_page（扫描期，项目未建）与 translate_page（项目已开）
    用同一 key 落到同一文件。
    """
    game_dir = Path(game_dir)
    safe = _UNSAFE_CHARS.sub("_", game_dir.name).strip("._ ")[:40] or "game"
    digest = hashlib.md5(str(game_dir).encode("utf-8")).hexdigest()[:6]
    return Path(app_dir) / "run-logs" / f"{safe}__{digest}.log"


def get_run_log(app_dir: Path, game_dir: Path) -> "RunLog":
    """取（或建）该游戏的 RunLog（同路径共享实例，线程安全）。"""
    path = run_log_path(app_dir, game_dir)
    with _CACHE_LOCK:
        log = _LOG_CACHE.get(path)
        if log is None:
            log = RunLog(path)
            _LOG_CACHE[path] = log
        return log


def close_all_run_logs() -> None:
    """进程退出时统一关闭全部日志句柄（失败静默）。"""
    with _CACHE_LOCK:
        logs = list(_LOG_CACHE.values())
        _LOG_CACHE.clear()
    for log in logs:
        log.close()


class RunLog:
    """append-only 运行日志：分节 + 事件行，永不抛错。

    行格式：
        ════ 2026-09-07 12:00:00 会话标题 ════          （begin_session）
        12:00:01.234 [translate][success] 本批完成 12 条  （event）
    多行 text 原样写入（首行带时间戳/类别前缀，续行缩进），
    事后 grep 类别即可复盘全链路。
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._fh = None
        self._disabled = False

    @property
    def path(self) -> Path:
        return self._path

    def _ensure_open(self):
        if self._fh is None and not self._disabled:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                # utf-8（非 utf-8-sig）：跨会话追加不能叠 BOM
                self._fh = open(self._path, "a", encoding="utf-8",
                                errors="replace")
            except OSError:
                self._disabled = True

    def begin_session(self, title: str) -> None:
        """新会话分节（开始扫描/翻译/写回/打开项目时调用）。"""
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._write(f"════ {stamp} {title} ════")

    def event(self, category: str, text: str, status: str = "") -> None:
        """记一条事件。category=链路环节（scan/translate/review/
        writeback/audit/control/record），status 可选（success/warning/
        error/running）。text 原样写入（多行不丢）。"""
        stamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        prefix = f"{stamp} [{category}]"
        if status:
            prefix += f"[{status}]"
        body = str(text)
        if "\n" in body:
            # 多行明细：首行带前缀，续行缩进两格保持可读
            body = body.replace("\n", "\n  ")
        self._write(f"{prefix} {body}")

    def _write(self, line: str) -> None:
        if self._disabled:
            return
        with self._lock:
            try:
                self._ensure_open()
                if self._fh is None:
                    return
                self._fh.write(line + "\n")
                self._fh.flush()   # 逐条落盘：崩溃也不丢已记录事件
            except OSError:
                self._disabled = True
                fh, self._fh = self._fh, None
                if fh is not None:
                    try:
                        fh.close()
                    except OSError:
                        pass

    def close(self) -> None:
        with self._lock:
            fh, self._fh = self._fh, None
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
