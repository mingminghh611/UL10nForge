"""Windows global-hotkey floating overlay HUD (`Alt+L`) for live in-game translation editing.

Features:
- System-wide `Alt+L` toggle even while a Unity game has focus.
- Instant 3-way filter: 实时 (live captured) / 全部 (all) / 待修 (flagged + failed).
- Opacity toggle (97% / 82%) for unobtrusive overlay over gameplay.
- Inline edit + Save (`Ctrl+Enter`), async AI Retranslate, 1-click Undo (`prev_translation`),
  and Skip/Ignore — every change hot-syncs immediately to `00_UL10nForge.txt` so XUAT
  refreshes visible text inside the running game without restarting.
- Non-disruptive live refresh: incoming game lines never overwrite text you're actively typing.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import queue
import sys
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ul10n.server.state import AppServices

_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_NOREPEAT = 0x4000
_VK_L = 0x4C
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_HOTKEY_ID = 0x554C
_HOTKEY_CANDIDATES = (
    ("Alt+L", _MOD_ALT | _MOD_NOREPEAT, _VK_L),
    ("Alt+Shift+L", _MOD_ALT | _MOD_SHIFT | _MOD_NOREPEAT, _VK_L),
    ("Ctrl+Shift+L", _MOD_CONTROL | _MOD_SHIFT | _MOD_NOREPEAT, _VK_L),
)


class InGameOverlayHUD:
    """Thread-safe native topmost overlay window with Windows global hotkey (`Alt+L`)."""

    def __init__(self, services: AppServices, hotkey_label: str = "Alt+L") -> None:
        self.services = services
        self.hotkey_label = hotkey_label
        self._cmd_q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._ui_thread: threading.Thread | None = None
        self._hk_thread: threading.Thread | None = None
        self._hk_thread_id: int = 0
        self._stop_evt = threading.Event()
        self._visible = False
        self._hotkey_registered = False
        self._started = False

    @property
    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._started,
            "visible": self._visible,
            "hotkey": self.hotkey_label,
            "hotkey_active": self._hotkey_registered,
        }

    def start(self) -> None:
        if self._started or sys.platform != "win32":
            return
        self._started = True
        self._stop_evt.clear()
        self.services.bus.add_listener(self._on_bus_event)
        self._ui_thread = threading.Thread(target=self._run_tk_loop, name="ul10n-overlay-ui", daemon=True)
        self._ui_thread.start()
        self._hk_thread = threading.Thread(target=self._run_hotkey_loop, name="ul10n-hotkey", daemon=True)
        self._hk_thread.start()

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self._stop_evt.set()
        self.services.bus.remove_listener(self._on_bus_event)
        self._cmd_q.put(("quit", None))
        if sys.platform == "win32" and self._hk_thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(self._hk_thread_id, _WM_QUIT, 0, 0)
            except Exception:
                pass

    def toggle(self) -> bool:
        if not self._started:
            self.start()
        self._cmd_q.put(("toggle", None))
        return not self._visible

    def show(self) -> None:
        if not self._started:
            self.start()
        self._cmd_q.put(("show", None))

    def hide(self) -> None:
        self._cmd_q.put(("hide", None))

    def _on_bus_event(self, payload: dict[str, Any]) -> None:
        if payload.get("type") == "live_entry":
            self._cmd_q.put(("refresh", None))

    def _run_hotkey_loop(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._hk_thread_id = int(kernel32.GetCurrentThreadId())
        ok = False
        for label, mods, vk in _HOTKEY_CANDIDATES:
            if user32.RegisterHotKey(None, _HOTKEY_ID, mods, vk):
                self.hotkey_label = label
                ok = True
                break
        self._hotkey_registered = ok
        if not ok:
            return
        msg = ctypes.wintypes.MSG()
        try:
            while not self._stop_evt.is_set():
                res = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if res <= 0:
                    break
                if msg.message == _WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                    self._cmd_q.put(("toggle", None))
        finally:
            try:
                user32.UnregisterHotKey(None, _HOTKEY_ID)
            except Exception:
                pass
            self._hotkey_registered = False

    def _run_tk_loop(self) -> None:
        try:
            import tkinter as tk
            from tkinter import ttk
        except Exception:
            return

        # Restrained graphite-neutral palette matching UL10nForge desktop aesthetic
        bg = "#121418"
        surface = "#191c22"
        elevated = "#22262f"
        border = "#2e3440"
        fg = "#eef1f5"
        muted = "#8b94a3"
        accent = "#2ea87e"
        accent_hover = "#39be90"
        accent_soft = "#163128"
        warn = "#d99b38"
        danger = "#d96363"

        root = tk.Tk()
        root.title(f"UL10nForge · 游戏内译文 ({self.hotkey_label})")
        root.configure(bg=bg)
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.96)
            root.attributes("-toolwindow", True)
        except Exception:
            pass

        win_w, win_h = 540, 620
        screen_w = root.winfo_screenwidth()
        x_pos = max(20, screen_w - win_w - 24)
        root.geometry(f"{win_w}x{win_h}+{x_pos}+44")
        root.minsize(450, 480)
        root.withdraw()

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "HUD.Treeview",
            background=surface,
            fieldbackground=surface,
            foreground=fg,
            bordercolor=border,
            rowheight=30,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "HUD.Treeview.Heading",
            background=elevated,
            foreground=muted,
            relief="flat",
            font=("Microsoft YaHei UI", 8, "bold"),
        )
        style.map(
            "HUD.Treeview",
            background=[("selected", accent_soft)],
            foreground=[("selected", "#ffffff")],
        )

        ui_state: dict[str, Any] = {
            "filter": "live",       # live | all | flagged
            "translucent": False,
            "entries": {},          # iid -> Entry
            "selected_id": None,
            "loaded_tr": "",
            "project_id": None,
            "busy": False,
        }

        # ── Top Bar ───────────────────────────────────────────────────────
        header = tk.Frame(root, bg=surface, padx=12, pady=9)
        header.pack(fill="x")
        lbl_title = tk.Label(
            header,
            text="实时译文悬浮窗",
            bg=surface,
            fg=fg,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        lbl_title.pack(side="left")

        btn_hide = tk.Button(
            header,
            text=f"收起 ({self.hotkey_label})",
            bg=elevated,
            fg=muted,
            activebackground=border,
            activeforeground=fg,
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
        )
        btn_hide.pack(side="right")

        btn_alpha = tk.Button(
            header,
            text="半透明",
            bg=elevated,
            fg=muted,
            activebackground=border,
            activeforeground=fg,
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
        )
        btn_alpha.pack(side="right", padx=(0, 6))

        # ── Filter Tabs + Search Row ──────────────────────────────────────
        toolbar = tk.Frame(root, bg=bg, padx=10, pady=8)
        toolbar.pack(fill="x")

        tab_frame = tk.Frame(toolbar, bg=bg)
        tab_frame.pack(side="left", padx=(0, 8))
        tab_btns: dict[str, tk.Button] = {}
        for key, label in (("live", "实时"), ("all", "全部"), ("flagged", "待修")):
            b = tk.Button(
                tab_frame,
                text=label,
                relief="flat",
                padx=9,
                pady=3,
                cursor="hand2",
                font=("Microsoft YaHei UI", 8),
            )
            b.pack(side="left", padx=(0, 4))
            tab_btns[key] = b

        search_var = tk.StringVar()
        ent_search = tk.Entry(
            toolbar,
            textvariable=search_var,
            bg=surface,
            fg=fg,
            insertbackground=fg,
            relief="flat",
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor=accent,
            font=("Microsoft YaHei UI", 9),
        )
        ent_search.pack(side="left", fill="x", expand=True, ipady=4)

        btn_refresh = tk.Button(
            toolbar,
            text="刷新",
            bg=elevated,
            fg=muted,
            activebackground=border,
            activeforeground=fg,
            relief="flat",
            padx=8,
            pady=3,
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
        )
        btn_refresh.pack(side="right", padx=(6, 0))

        # ── Entry Table ───────────────────────────────────────────────────
        list_wrap = tk.Frame(root, bg=bg, padx=10)
        list_wrap.pack(fill="both", expand=True)
        tree = ttk.Treeview(
            list_wrap,
            columns=("orig", "trans"),
            show="headings",
            style="HUD.Treeview",
            selectmode="browse",
        )
        tree.heading("orig", text="原文")
        tree.heading("trans", text="译文")
        tree.column("orig", width=240, anchor="w")
        tree.column("trans", width=255, anchor="w")
        scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # ── Bottom Quick-Edit Card ────────────────────────────────────────
        editor = tk.Frame(
            root,
            bg=surface,
            padx=12,
            pady=10,
            highlightbackground=border,
            highlightthickness=1,
        )
        editor.pack(fill="x", padx=10, pady=10)

        orig_head = tk.Frame(editor, bg=surface)
        orig_head.pack(fill="x")
        tk.Label(orig_head, text="原文", bg=surface, fg=muted, font=("Microsoft YaHei UI", 8)).pack(side="left")
        lbl_entry_meta = tk.Label(orig_head, text="", bg=surface, fg=muted, font=("Microsoft YaHei UI", 8))
        lbl_entry_meta.pack(side="right")

        txt_orig = tk.Text(
            editor,
            height=2,
            bg=bg,
            fg=muted,
            relief="flat",
            wrap="word",
            font=("Microsoft YaHei UI", 9),
            padx=7,
            pady=5,
        )
        txt_orig.pack(fill="x", pady=(3, 7))
        txt_orig.configure(state="disabled")

        tk.Label(
            editor,
            text="译文（修改后按 Ctrl+Enter 立即保存并在游戏内生效）",
            bg=surface,
            fg=muted,
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w")
        txt_tr = tk.Text(
            editor,
            height=3,
            bg=bg,
            fg=fg,
            insertbackground=fg,
            relief="flat",
            wrap="word",
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor=accent,
            font=("Microsoft YaHei UI", 9),
            padx=7,
            pady=5,
        )
        txt_tr.pack(fill="x", pady=(3, 9))

        foot = tk.Frame(editor, bg=surface)
        foot.pack(fill="x")

        btn_retrans = tk.Button(
            foot,
            text="重译",
            bg=elevated,
            fg=fg,
            activebackground=border,
            activeforeground=fg,
            relief="flat",
            padx=10,
            pady=5,
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
        )
        btn_retrans.pack(side="left", padx=(0, 6))

        btn_undo = tk.Button(
            foot,
            text="还原上一版",
            bg=elevated,
            fg=muted,
            activebackground=border,
            activeforeground=fg,
            relief="flat",
            padx=9,
            pady=5,
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
            state="disabled",
        )
        btn_undo.pack(side="left", padx=(0, 6))

        btn_skip = tk.Button(
            foot,
            text="忽略",
            bg=elevated,
            fg=muted,
            activebackground="#3a2326",
            activeforeground=danger,
            relief="flat",
            padx=9,
            pady=5,
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
        )
        btn_skip.pack(side="left")

        btn_save = tk.Button(
            foot,
            text="保存并生效",
            bg=accent,
            fg="#06231a",
            activebackground=accent_hover,
            activeforeground="#06231a",
            relief="flat",
            padx=14,
            pady=5,
            cursor="hand2",
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        btn_save.pack(side="right")

        lbl_status = tk.Label(
            editor,
            text=f"游戏中随时按 {self.hotkey_label} 呼出或收起",
            bg=surface,
            fg=muted,
            anchor="w",
            font=("Microsoft YaHei UI", 8),
        )
        lbl_status.pack(fill="x", pady=(6, 0))

        def _set_status(msg: str, color: str = muted) -> None:
            lbl_status.configure(text=msg, fg=color)

        def _style_tabs() -> None:
            cur = ui_state["filter"]
            for k, btn in tab_btns.items():
                if k == cur:
                    btn.configure(bg=accent_soft, fg="#9be5c8", activebackground=accent_soft)
                else:
                    btn.configure(bg=elevated, fg=muted, activebackground=border)

        def _active_project():
            pid = self.services.active_project_id
            if pid:
                p = self.services.global_store.get_project(pid)
                if p:
                    return p
            projects = self.services.global_store.list_projects()
            return projects[0] if projects else None

        def _is_user_typing_dirty() -> bool:
            if not ui_state["selected_id"]:
                return False
            return txt_tr.get("1.0", "end-1c") != ui_state["loaded_tr"]

        def _load_entry_into_editor(entry) -> None:
            ui_state["selected_id"] = entry.id
            ui_state["loaded_tr"] = entry.translation or ""
            txt_orig.configure(state="normal")
            txt_orig.delete("1.0", "end")
            txt_orig.insert("1.0", entry.original)
            txt_orig.configure(state="disabled")
            txt_tr.delete("1.0", "end")
            txt_tr.insert("1.0", entry.translation or "")
            meta_bits = []
            if entry.source == "xuat_live":
                meta_bits.append("实时")
            if entry.locked:
                meta_bits.append("已锁定")
            if entry.review_reason:
                meta_bits.append(entry.review_reason[:24])
            lbl_entry_meta.configure(text=" · ".join(meta_bits))
            has_prev = bool((entry.prev_translation or "").strip())
            btn_undo.configure(state="normal" if has_prev else "disabled", fg=fg if has_prev else muted)

        def _refresh_list(keep_selection: bool = True) -> None:
            proj = _active_project()
            if not proj:
                lbl_title.configure(text="尚未打开游戏项目")
                tree.delete(*tree.get_children())
                ui_state["entries"].clear()
                ui_state["project_id"] = None
                return

            ui_state["project_id"] = proj.id
            store = self.services.project_store(proj.id)
            flt = ui_state["filter"]
            entries, total = store.query_entries(
                status=flt,
                search=search_var.get().strip(),
                offset=0,
                limit=100,
            )
            mode_lbl = {"live": "实时", "all": "全部", "flagged": "待修"}.get(flt, flt)
            lbl_title.configure(text=f"{proj.name} · {mode_lbl} {total}")

            prev_sel = ui_state["selected_id"] if keep_selection else None
            tree.delete(*tree.get_children())
            ui_state["entries"].clear()
            for e in entries:
                iid = str(e.id)
                ui_state["entries"][iid] = e
                orig_flat = e.original.replace("\n", " ↵ ")
                tr_flat = (e.translation or "（待翻译）").replace("\n", " ↵ ")
                tree.insert("", "end", iid=iid, values=(orig_flat, tr_flat))

            if prev_sel and str(prev_sel) in ui_state["entries"]:
                tree.selection_set(str(prev_sel))
                if not _is_user_typing_dirty():
                    _load_entry_into_editor(ui_state["entries"][str(prev_sel)])

        def _on_select(_ev=None) -> None:
            sel = tree.selection()
            if not sel:
                return
            entry = ui_state["entries"].get(sel[0])
            if not entry:
                return
            _load_entry_into_editor(entry)

        def _save_current(_ev=None):
            pid = ui_state.get("project_id")
            eid = ui_state.get("selected_id")
            if not pid or not eid:
                _set_status("请先在列表中选择一条文本", warn)
                return "break"
            new_tr = txt_tr.get("1.0", "end-1c").strip()
            try:
                updated = self.services.save_manual_translation(pid, int(eid), new_tr)
                if updated:
                    _load_entry_into_editor(updated)
                    _set_status("已保存并热同步到游戏", accent)
                    _refresh_list(keep_selection=True)
                else:
                    _set_status("未找到该词条", danger)
            except Exception as exc:
                _set_status(f"保存失败：{exc}", danger)
            return "break"

        def _retranslate_current() -> None:
            if ui_state["busy"]:
                return
            pid = ui_state.get("project_id")
            eid = ui_state.get("selected_id")
            if not pid or not eid:
                _set_status("请先选择一条文本", warn)
                return
            ui_state["busy"] = True
            btn_retrans.configure(state="disabled", text="重译中…")
            _set_status("正在重新翻译并同步…", muted)

            def _bg():
                try:
                    updated, ok, note = self.services.retranslate_entry_now(pid, int(eid))
                    self._cmd_q.put(("retrans_done", (updated, ok, note)))
                except Exception as exc:
                    self._cmd_q.put(("retrans_done", (None, False, str(exc))))

            threading.Thread(target=_bg, daemon=True).start()

        def _undo_current() -> None:
            pid = ui_state.get("project_id")
            eid = ui_state.get("selected_id")
            if not pid or not eid:
                return
            updated = self.services.restore_entry_translation(pid, int(eid))
            if updated:
                _load_entry_into_editor(updated)
                _set_status("已还原上一版译文并热同步", accent)
                _refresh_list(keep_selection=True)
            else:
                _set_status("暂无可还原的历史译文", warn)

        def _skip_current() -> None:
            pid = ui_state.get("project_id")
            eid = ui_state.get("selected_id")
            if not pid or not eid:
                return
            updated = self.services.skip_entry(pid, int(eid))
            if updated:
                ui_state["selected_id"] = None
                ui_state["loaded_tr"] = ""
                _set_status("已忽略该词条", muted)
                _refresh_list(keep_selection=False)

        def _switch_filter(flt: str) -> None:
            ui_state["filter"] = flt
            _style_tabs()
            _refresh_list(keep_selection=False)

        def _toggle_alpha() -> None:
            ui_state["translucent"] = not ui_state["translucent"]
            alpha = 0.80 if ui_state["translucent"] else 0.96
            try:
                root.attributes("-alpha", alpha)
            except Exception:
                pass
            btn_alpha.configure(text="不透明" if ui_state["translucent"] else "半透明")

        def _hide_win() -> None:
            self._visible = False
            root.withdraw()

        def _show_win() -> None:
            self._visible = True
            _refresh_list(keep_selection=True)
            root.deiconify()
            root.attributes("-topmost", True)
            root.lift()
            try:
                root.focus_force()
            except Exception:
                pass

        for k, b in tab_btns.items():
            b.configure(command=lambda key=k: _switch_filter(key))
        _style_tabs()

        btn_hide.configure(command=_hide_win)
        btn_alpha.configure(command=_toggle_alpha)
        btn_refresh.configure(command=lambda: _refresh_list(keep_selection=True))
        btn_save.configure(command=_save_current)
        btn_retrans.configure(command=_retranslate_current)
        btn_undo.configure(command=_undo_current)
        btn_skip.configure(command=_skip_current)
        tree.bind("<<TreeviewSelect>>", _on_select)
        txt_tr.bind("<Control-Return>", _save_current)
        root.bind("<Escape>", lambda _e: _hide_win())
        root.protocol("WM_DELETE_WINDOW", _hide_win)

        search_job = {"id": None}

        def _on_search_change(*_args) -> None:
            if search_job["id"]:
                root.after_cancel(search_job["id"])
            search_job["id"] = root.after(160, lambda: _refresh_list(keep_selection=True))

        search_var.trace_add("write", _on_search_change)

        def _drain_commands() -> None:
            need_refresh = False
            while True:
                try:
                    cmd, payload = self._cmd_q.get_nowait()
                except queue.Empty:
                    break
                if cmd == "quit":
                    root.destroy()
                    return
                if cmd == "toggle":
                    if self._visible:
                        _hide_win()
                    else:
                        _show_win()
                elif cmd == "show":
                    _show_win()
                elif cmd == "hide":
                    _hide_win()
                elif cmd == "refresh" and self._visible:
                    need_refresh = True
                elif cmd == "retrans_done":
                    ui_state["busy"] = False
                    btn_retrans.configure(state="normal", text="重译")
                    updated, ok, note = payload
                    if updated is not None:
                        _load_entry_into_editor(updated)
                        _refresh_list(keep_selection=True)
                        _set_status("重译完成并已热同步到游戏" if ok else f"重译提示：{note}", accent if ok else warn)
                    else:
                        _set_status(f"重译失败：{note}", danger)
            if need_refresh:
                _refresh_list(keep_selection=True)
            root.after(85, _drain_commands)

        root.after(85, _drain_commands)
        try:
            root.mainloop()
        except Exception:
            pass
