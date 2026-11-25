/* UL10nForge v2 — Zero-build modern ES Module frontend */

const state = {
  tab: "workbench",
  projects: [],
  project: null,
  stats: null,
  context: null,
  settings: null,
  runtime: null,
  task: null,
  liveFeed: [],
  editor: {
    source: "all",
    status: "all",
    search: "",
    offset: 0,
    limit: 40,
    total: 0,
    entries: [],
  },
  glossarySubtab: "project-glossary",
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const STATUS_LABELS = {
  pending: "待翻译",
  translated: "已翻译",
  edited: "已修改",
  flagged: "需处理",
  failed: "失败",
  skipped: "不翻译",
};

const TASK_STAGE_LABELS = {
  auto: "自动汉化",
  translate: "批量翻译",
  review: "智能审校",
  context: "语境识别",
};

/* ── HTTP & Toast Helpers ──────────────────────────────────────────── */

async function api(path, options = {}) {
  const init = {
    headers: { "Content-Type": "application/json" },
    ...options,
  };
  if (init.body && typeof init.body !== "string") {
    init.body = JSON.stringify(init.body);
  }
  const resp = await fetch(path, init);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || `请求失败 (${resp.status})`);
  }
  return data;
}

function toast(message, type = "ok") {
  const stack = $("#toast-stack");
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
  stack.appendChild(el);
  setTimeout(() => el.remove(), 3400);
}

function escapeHtml(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function applyTheme(theme) {
  const t = theme === "light" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", t);
  $("#icon-sun").hidden = t === "dark";
  $("#icon-moon").hidden = t === "light";
  $$("#seg-theme button").forEach((b) => b.classList.toggle("active", b.dataset.val === t));
}

/* ── Navigation ────────────────────────────────────────────────────── */

function switchTab(tab) {
  state.tab = tab;
  $$(".nav-item").forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tab));

  const hasProject = Boolean(state.project);
  const showEmpty = !hasProject && (tab === "workbench" || tab === "editor");

  $("#view-empty").hidden = !showEmpty;
  $("#tab-workbench").hidden = showEmpty || tab !== "workbench";
  $("#tab-editor").hidden = showEmpty || tab !== "editor";
  $("#tab-glossary").hidden = tab !== "glossary";
  $("#tab-settings").hidden = tab !== "settings";

  if (tab === "editor" && hasProject) loadEntries();
  if (tab === "glossary") loadGlossaryView();
  if (tab === "settings") renderSettings();
}

/* ── Data Loading & Rendering ──────────────────────────────────────── */

async function bootstrap() {
  const [projRes, setRes, taskRes] = await Promise.all([
    api("/api/projects"),
    api("/api/settings"),
    api("/api/tasks/current"),
  ]);

  state.settings = setRes.settings;
  state.runtime = setRes.runtime;
  applyTheme(state.settings.theme);
  renderRuntime();
  renderSettings();

  state.projects = projRes.projects || [];
  renderProjectSelector();

  if (projRes.active_project_id) {
    await selectProject(projRes.active_project_id);
  } else {
    switchTab("workbench");
  }

  if (taskRes.task && taskRes.task.status === "running") {
    applyTaskState(taskRes.task);
  }

  connectEvents();
}

function renderProjectSelector() {
  const sel = $("#project-select");
  if (!state.projects.length) {
    sel.innerHTML = `<option value="">尚未打开游戏</option>`;
    return;
  }
  sel.innerHTML = state.projects
    .map(
      (p) =>
        `<option value="${escapeHtml(p.id)}" ${state.project && state.project.id === p.id ? "selected" : ""}>${escapeHtml(p.name)}</option>`
    )
    .join("");
}

async function selectProject(projectId) {
  const [data, recent] = await Promise.all([
    api(`/api/projects/${projectId}`),
    api(`/api/projects/${projectId}/entries?limit=12`),
  ]);
  state.project = data.project;
  state.stats = data.stats;
  state.context = data.context;
  state.liveFeed = recent.entries || [];
  state.editor.offset = 0;
  renderProjectSelector();
  renderWorkbench();
  switchTab(state.tab);
}

function renderWorkbench() {
  const p = state.project;
  const st = state.stats || {
    total: 0,
    done: 0,
    pending: 0,
    flagged: 0,
    failed: 0,
    translated: 0,
    edited: 0,
    reviewed: 0,
    attention: 0,
    skipped: 0,
    static: 0,
    live: 0,
    percent: 0,
  };
  if (!p) return;

  $("#wb-game-name").textContent = p.name;
  const parts = [];
  if (p.unity_version) parts.push(`Unity ${p.unity_version}`);
  if (p.runtime_type && p.runtime_type !== "unknown") parts.push(p.runtime_type.toUpperCase());
  if (p.arch) parts.push(p.arch);
  $("#wb-game-meta").textContent = parts.length ? parts.join(" · ") : "已就绪 · 支持词典导入与实时翻译";

  const hookPill = $("#wb-hook-pill");
  hookPill.textContent = p.hook_deployed ? "已接入 · 可直接游玩" : "待启动时自动接入";
  hookPill.classList.toggle("deployed", Boolean(p.hook_deployed));

  const protWarn = $("#wb-protection-warn");
  if (protWarn) {
    if (p.protection_warning) {
      protWarn.textContent = `⚠ ${p.protection_warning}`;
      protWarn.hidden = false;
    } else {
      protWarn.hidden = true;
    }
  }

  $("#btn-uninstall-hook").hidden = !p.hook_deployed;

  const loaderWrap = $("#wb-loader-wrap");
  if (loaderWrap) {
    const isIl2cpp = p.runtime_type === "il2cpp" && p.arch === "x64";
    loaderWrap.hidden = !isIl2cpp;
    if (isIl2cpp) $("#wb-loader-select").value = p.loader || "bepinex";
  }

  // Progress ring
  const pct = Math.max(0, Math.min(100, st.percent || 0));
  const circumference = 263.89;
  $("#wb-ring-fg").style.strokeDashoffset = String(circumference * (1 - pct / 100));
  $("#wb-pct").textContent = `${pct}%`;

  $("#st-total").textContent = st.total ?? 0;
  $("#st-done").textContent = st.done ?? 0;
  $("#st-pending").textContent = st.pending ?? 0;
  $("#st-flagged").textContent = (st.flagged || 0) + (st.failed || 0);

  const badge = $("#nav-entry-badge");
  badge.hidden = !st.total;
  badge.textContent = st.total;

  // Sync editor filter counts
  $$("[data-count]").forEach((el) => {
    const key = el.dataset.count;
    el.textContent = st[key] ?? 0;
  });

  renderContextCard();
  renderLiveFeed();
}

function renderContextCard() {
  const ctx = state.context || {};
  const box = $("#wb-context-body");
  const syncBtn = $("#btn-sync-ctx-glossary");
  const chars = (ctx.characters || []).slice(0, 8);
  const terms = (ctx.terms || []).slice(0, 8);
  if (syncBtn) syncBtn.hidden = !(chars.length || terms.length);

  const hasInfo = Boolean(ctx.genre || ctx.setting || ctx.summary || ctx.style);
  if (!hasInfo) {
    box.innerHTML = `<p class="muted-hint">尚未生成语境档案。点击「自动识别」将根据游戏文本归纳类型、文风与专名。</p>`;
    return;
  }
  const tags = [ctx.genre, ctx.setting, ctx.style].filter(Boolean);
  box.innerHTML = `
    <div class="ctx-tags">${tags.map((t) => `<span class="ctx-tag">${escapeHtml(t)}</span>`).join("")}</div>
    ${ctx.summary ? `<p class="ctx-summary">${escapeHtml(ctx.summary)}</p>` : ""}
    ${chars.length ? `<div class="muted-hint">角色：${escapeHtml(chars.join("、"))}</div>` : ""}
    ${terms.length ? `<div class="ctx-terms-wrap">${terms.map((t) => `<span class="ctx-term-chip">${escapeHtml(t)}</span>`).join("")}</div>` : ""}
  `;
}

function renderLiveFeed() {
  const box = $("#live-feed");
  if (!state.liveFeed.length) {
    box.innerHTML = `<div class="feed-empty">接入游戏并启动后，画面中出现的文本会自动实时翻译并显示在这里；点击任意一条即可立即修改译文。</div>`;
    return;
  }
  box.innerHTML = state.liveFeed
    .slice(0, 12)
    .map(
      (item) => `
      <div class="feed-row" data-orig="${escapeHtml(item.original)}">
        <span class="feed-orig">${escapeHtml(item.original)}</span>
        <span class="feed-tr">${escapeHtml(item.translation || "（等待译文）")}</span>
        <span class="badge ${item.source === "xuat_live" ? "live" : "static"}">${item.source === "xuat_live" ? "实时" : "静态"}</span>
        <span class="badge ${escapeHtml(item.status)}">${STATUS_LABELS[item.status] || item.status}</span>
      </div>
    `
    )
    .join("");

  $$(".feed-row").forEach((row) => {
    row.addEventListener("click", () => {
      state.editor.search = row.dataset.orig || "";
      state.editor.source = "all";
      state.editor.status = "all";
      state.editor.offset = 0;
      $("#editor-search").value = state.editor.search;
      $$("#editor-sources button").forEach((b) => b.classList.toggle("active", b.dataset.source === "all"));
      $$("#editor-filters .pill").forEach((b) => b.classList.toggle("active", b.dataset.status === "all"));
      switchTab("editor");
    });
  });
}

function renderRuntime() {
  const rt = state.runtime;
  if (!rt) return;
  const tr = rt.roles?.translate || {};
  const rs = rt.roles?.reason || {};

  const dot = $("#engine-dot");
  dot.className = "dot " + (tr.state || "idle");

  const labelMap = {
    ready: tr.mode === "api" ? "云端翻译已配置" : `翻译引擎就绪`,
    starting: "正在加载模型…",
    idle: "本地引擎待命",
    missing: "未检测到模型",
    unconfigured: "请配置在线 API",
  };
  $("#engine-label").textContent = labelMap[tr.state] || "引擎待命";
  $("#engine-sub").textContent = tr.mode === "api" ? tr.label : (tr.backend ? `${tr.backend} 加速` : "点击预热或设置");

  // Settings model mini cards
  $("#card-tr-name").textContent = tr.label || "未检测到翻译模型";
  const trBadge = $("#card-tr-state");
  trBadge.textContent = { ready: "运行中", starting: "启动中…", idle: "未启动", missing: "缺失" }[tr.state] || tr.state;
  trBadge.className = `badge ${tr.state || ""}`;

  $("#card-rs-name").textContent = rs.label || "未检测到推理模型";
  const rsBadge = $("#card-rs-state");
  rsBadge.textContent = { ready: "运行中", starting: "启动中…", idle: "按需待命", missing: "缺失" }[rs.state] || rs.state;
  rsBadge.className = `badge ${rs.state || ""}`;

  if (rt.gpu_name) {
    $("#set-gpu-hint").textContent = `已识别显卡：${rt.gpu_name}（支持自动加速与 CPU 回退）`;
  }
}

let taskPollTimer = null;
let doneHideTimer = null;
let liveSyncTimer = null;
let liveSyncInFlight = false;
let liveSyncQueued = false;
let lastProgressSyncAt = 0;

function formatEta(remaining, speedPerMin) {
  if (!remaining || remaining <= 0 || !speedPerMin || speedPerMin <= 0) return "";
  const secs = Math.max(1, Math.round((remaining / speedPerMin) * 60));
  if (secs < 60) return ` · 约剩 ${secs}秒`;
  const mins = Math.floor(secs / 60);
  const remSec = secs % 60;
  return remSec ? ` · 约剩 ${mins}分${remSec}秒` : ` · 约剩 ${mins}分钟`;
}

function scheduleLiveSync({ immediate = false } = {}) {
  if (!state.project) return;
  const run = async () => {
    liveSyncTimer = null;
    if (!state.project) return;
    if (liveSyncInFlight) {
      liveSyncQueued = true;
      return;
    }
    liveSyncInFlight = true;
    const pid = state.project.id;
    try {
      const [data, recent] = await Promise.all([
        api(`/api/projects/${pid}`),
        api(`/api/projects/${pid}/entries?limit=12`),
      ]);
      if (!state.project || state.project.id !== pid) return;
      state.project = data.project;
      state.stats = data.stats;
      state.context = data.context;
      state.liveFeed = recent.entries || [];
      renderWorkbench();
      if (state.tab === "editor" && !document.querySelector(".entry-textarea.dirty")) {
        await loadEntries({ skipWorkbench: true });
      }
    } catch {
      // ignore transient sync errors
    } finally {
      liveSyncInFlight = false;
      if (liveSyncQueued) {
        liveSyncQueued = false;
        scheduleLiveSync();
      }
    }
  };
  clearTimeout(liveSyncTimer);
  if (immediate) {
    run();
  } else {
    liveSyncTimer = setTimeout(run, 350);
  }
}

function applyTaskState(nextTask, { fromHttpStart = false } = {}) {
  if (!nextTask) {
    state.task = null;
    renderTaskProgress();
    return;
  }
  const prev = state.task;
  // Guard against SSE-vs-HTTP race: never overwrite a newer seq or terminal state of same task
  if (prev && prev.task_id === nextTask.task_id) {
    const prevTerminal = prev.status !== "running";
    if (prevTerminal && nextTask.status === "running") {
      return;
    }
    if ((nextTask.seq ?? 0) < (prev.seq ?? 0)) {
      return;
    }
    if (fromHttpStart && prevTerminal) {
      return;
    }
  }
  const transitionedToDone =
    nextTask.status === "completed" &&
    (!prev || prev.task_id !== nextTask.task_id || prev.status === "running");

  state.task = nextTask;
  renderTaskProgress({ showDoneBanner: transitionedToDone });

  // Safety poll while running so progress bar can NEVER get stuck even if SSE drops
  if (state.task.status === "running") {
    if (!taskPollTimer) {
      taskPollTimer = setInterval(async () => {
        try {
          const res = await api("/api/tasks/current");
          if (!res.task || res.task.status !== "running") {
            clearInterval(taskPollTimer);
            taskPollTimer = null;
            applyTaskState(res.task);
            scheduleLiveSync({ immediate: true });
          } else {
            applyTaskState(res.task);
            const now = Date.now();
            if (now - lastProgressSyncAt > 2000) {
              lastProgressSyncAt = now;
              scheduleLiveSync();
            }
          }
        } catch {
          // ignore transient poll errors
        }
      }, 1500);
    }
  } else if (taskPollTimer) {
    clearInterval(taskPollTimer);
    taskPollTimer = null;
  }
}

function renderTaskProgress({ showDoneBanner = false } = {}) {
  const bar = $("#global-progress");
  const fill = $("#gp-fill");
  const cancelBtn = $("#btn-cancel-task");
  const t = state.task;

  if (t && t.status === "running") {
    clearTimeout(doneHideTimer);
    doneHideTimer = null;
    bar.hidden = false;
    bar.classList.remove("is-done");
    cancelBtn.hidden = false;
    cancelBtn.disabled = false;
    cancelBtn.textContent = "停止";

    $("#gp-stage").textContent = TASK_STAGE_LABELS[t.kind] || "处理中";
    $("#gp-text").textContent = t.message || "处理中…";
    if (t.total > 0) {
      fill.classList.remove("indeterminate");
      const pct = Math.max(2, Math.min(100, t.percent || 0));
      fill.style.width = `${pct}%`;
      const speed = t.speed_per_min > 0 ? ` · ${t.speed_per_min} 条/分` : "";
      const eta = formatEta(t.total - t.current, t.speed_per_min);
      $("#gp-metrics").textContent = `${t.current} / ${t.total} (${pct}%)${speed}${eta}`;
    } else {
      fill.classList.add("indeterminate");
      fill.style.width = "36%";
      $("#gp-metrics").textContent = "准备中…";
    }
    return;
  }

  if (t && t.status === "completed" && showDoneBanner) {
    clearTimeout(doneHideTimer);
    bar.hidden = false;
    bar.classList.add("is-done");
    cancelBtn.hidden = true;
    fill.classList.remove("indeterminate");
    fill.style.width = "100%";
    $("#gp-stage").textContent = "已完成";
    $("#gp-text").textContent = t.message || "处理完成";
    $("#gp-metrics").textContent = "100%";
    doneHideTimer = setTimeout(() => {
      if (!state.task || state.task.status !== "running") {
        bar.hidden = true;
        bar.classList.remove("is-done");
      }
    }, 2400);
    return;
  }

  if (!doneHideTimer) {
    bar.hidden = true;
    bar.classList.remove("is-done");
    fill.classList.remove("indeterminate");
  }
}

/* ── Editor (View & Inline Edit Original / Translation) ────────────── */

function buildEntryBadgesHtml(e) {
  const srcBadge =
    e.source === "xuat_live"
      ? `<span class="badge live">实时</span>`
      : `<span class="badge static">静态</span>`;
  const revBadge =
    e.status !== "skipped" && e.review_level === "FIXED"
      ? `<span class="badge translated">已审校·已修正</span>`
      : e.status !== "skipped" && e.review_level === "PASS"
      ? `<span class="badge">已审校</span>`
      : "";
  return (
    srcBadge +
    `<span class="badge ${escapeHtml(e.status)}">${STATUS_LABELS[e.status] || e.status}</span>` +
    revBadge
  );
}

async function loadEntries({ skipWorkbench = false } = {}) {
  if (!state.project) return;
  const q = new URLSearchParams({
    source: state.editor.source,
    status: state.editor.status,
    search: state.editor.search,
    offset: String(state.editor.offset),
    limit: String(state.editor.limit),
  });
  const data = await api(`/api/projects/${state.project.id}/entries?${q}`);
  state.editor.entries = data.entries || [];
  state.editor.total = data.total || 0;
  state.stats = data.stats;
  if (!skipWorkbench) renderWorkbench();
  renderEditorList();
}

function renderEditorList() {
  const list = $("#editor-list");
  const entries = state.editor.entries;

  if (!entries.length) {
    const emptyHint =
      state.editor.source === "live"
        ? "暂无游戏内实时翻译记录。启动游戏游玩时，画面中出现的动态与剧情文本会自动捕获并实时显示在这里。"
        : "当前筛选下没有词条。可在工作台点击「扫描静态文本」、启动游戏实时捕获，或点击右上角「+ 添加」。";
    list.innerHTML = `<div class="feed-empty" style="margin:18px">${emptyHint}</div>`;
  } else {
    list.innerHTML = entries
      .map((e) => {
        const noteHtml = e.review_reason
          ? `<div class="entry-note">审校提示：${escapeHtml(e.review_reason)}</div>`
          : e.quality_note
          ? `<div class="entry-note fail">提示：${escapeHtml(e.quality_note)}</div>`
          : "";
        const isSkipped = e.status === "skipped";
        const hasPrev = Boolean(!isSkipped && (e.prev_translation || "").trim() && e.prev_translation !== e.translation);
        const skipToggleHtml = isSkipped
          ? `<button class="row-btn btn-unskip-row" title="恢复翻译此条">恢复</button>`
          : `<button class="row-btn btn-skip-row" title="标记为不翻译，游戏中保留原文">不翻</button>`;
        const taVal = isSkipped ? "" : e.translation;
        const taPlaceholder = isSkipped ? "已设为不翻译，游戏中将保留原文" : "输入译文…";
        return `
          <div class="entry-row" data-id="${e.id}" data-source="${escapeHtml(e.source || "")}">
            <div>
              <div class="entry-orig">${escapeHtml(e.original)}</div>
              ${noteHtml}
            </div>
            <div class="entry-tr-wrap">
              <textarea class="entry-textarea ${isSkipped ? "is-skipped" : ""}" rows="1" ${isSkipped ? "disabled" : ""} data-orig-val="${escapeHtml(taVal)}" placeholder="${taPlaceholder}">${escapeHtml(taVal)}</textarea>
              <div class="entry-badges">${buildEntryBadgesHtml(e)}</div>
            </div>
            <div class="entry-actions">
              ${isSkipped ? "" : `<button class="row-btn btn-save-row" title="保存并同步到游戏 (Ctrl+Enter)">保存</button>`}
              ${isSkipped ? "" : `<button class="row-btn btn-retrans-row" title="重新翻译">重译</button>`}
              ${hasPrev ? `<button class="row-btn btn-undo-row" title="还原为：${escapeHtml(e.prev_translation)}">还原</button>` : ""}
              ${skipToggleHtml}
            </div>
          </div>
        `;
      })
      .join("");
  }

  // Wire row interactions
  $$(".entry-row").forEach((row) => {
    const id = Number(row.dataset.id);
    const ta = row.querySelector(".entry-textarea");
    const saveBtn = row.querySelector(".btn-save-row");
    const retransBtn = row.querySelector(".btn-retrans-row");
    const undoBtn = row.querySelector(".btn-undo-row");
    const skipBtn = row.querySelector(".btn-skip-row");
    const unskipBtn = row.querySelector(".btn-unskip-row");

    const autoResize = () => {
      ta.style.height = "auto";
      ta.style.height = Math.max(36, ta.scrollHeight) + "px";
    };
    autoResize();

    let saveFlashTimer = null;
    const saveEntry = async ({ force = false } = {}) => {
      if (!saveBtn) return;
      const val = ta.value;
      const dirty = val !== ta.dataset.origVal;
      if (!dirty && !force) return;
      if (!dirty && !val.trim()) {
        ta.focus();
        return toast("请先输入译文", "err");
      }
      saveBtn.disabled = true;
      saveBtn.textContent = "保存中…";
      try {
        const res = await api(`/api/projects/${state.project.id}/entries/${id}`, {
          method: "PATCH",
          body: { translation: val },
        });
        ta.dataset.origVal = res.entry.translation;
        ta.classList.remove("dirty");
        saveBtn.classList.remove("save-active");
        state.stats = res.stats;
        renderWorkbench();
        const badges = row.querySelector(".entry-badges");
        badges.innerHTML = buildEntryBadgesHtml(res.entry);
        clearTimeout(saveFlashTimer);
        saveBtn.textContent = "已保存";
        saveFlashTimer = setTimeout(() => {
          if (saveBtn) saveBtn.textContent = "保存";
        }, 1200);
        toast("译文已保存并同步到游戏");
      } catch (err) {
        saveBtn.textContent = "保存";
        toast(err.message, "err");
      } finally {
        saveBtn.disabled = false;
      }
    };

    ta.addEventListener("input", () => {
      autoResize();
      const dirty = ta.value !== ta.dataset.origVal;
      ta.classList.toggle("dirty", dirty);
      if (saveBtn) {
        saveBtn.classList.toggle("save-active", dirty);
        if (dirty) {
          clearTimeout(saveFlashTimer);
          saveBtn.textContent = "保存";
        }
      }
    });

    ta.addEventListener("keydown", (ev) => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") {
        ev.preventDefault();
        saveEntry({ force: true });
      }
    });
    ta.addEventListener("blur", () => {
      if (ta.value !== ta.dataset.origVal) saveEntry({ force: false });
    });

    if (saveBtn) {
      // Prevent textarea blur from double-firing right before click
      saveBtn.addEventListener("mousedown", (ev) => ev.preventDefault());
      saveBtn.addEventListener("click", () => saveEntry({ force: true }));
    }

    if (retransBtn) {
      retransBtn.addEventListener("click", async () => {
        retransBtn.disabled = true;
        retransBtn.textContent = "翻译中…";
        try {
          const res = await api(`/api/projects/${state.project.id}/entries/${id}/retranslate`, {
            method: "POST",
            body: {},
          });
          ta.value = res.entry.translation;
          ta.dataset.origVal = res.entry.translation;
          autoResize();
          state.stats = res.stats;
          const badges = row.querySelector(".entry-badges");
          badges.innerHTML = buildEntryBadgesHtml(res.entry);
          renderWorkbench();
          toast(res.ok ? "重译完成" : `重译提醒：${res.note}`, res.ok ? "ok" : "err");
        } catch (err) {
          toast(err.message, "err");
        } finally {
          retransBtn.disabled = false;
          retransBtn.textContent = "重译";
        }
      });
    }

    if (undoBtn) {
      undoBtn.addEventListener("click", async () => {
        try {
          const res = await api(`/api/projects/${state.project.id}/entries/${id}/restore`, { method: "POST" });
          state.stats = res.stats;
          loadEntries();
          toast("已还原上一版译文并同步");
        } catch (err) {
          toast(err.message, "err");
        }
      });
    }

    if (skipBtn) {
      skipBtn.addEventListener("click", async () => {
        try {
          const res = await api(`/api/projects/${state.project.id}/entries/${id}/skip`, { method: "POST" });
          state.stats = res.stats;
          loadEntries();
          toast("已设为不翻译，游戏中将保留原文");
        } catch (err) {
          toast(err.message, "err");
        }
      });
    }

    if (unskipBtn) {
      unskipBtn.addEventListener("click", async () => {
        try {
          const res = await api(`/api/projects/${state.project.id}/entries/${id}/unskip`, { method: "POST" });
          state.stats = res.stats;
          loadEntries();
          toast("已恢复该词条");
        } catch (err) {
          toast(err.message, "err");
        }
      });
    }
  });

  // Pagination
  const { offset, limit, total } = state.editor;
  const start = total ? offset + 1 : 0;
  const end = Math.min(offset + limit, total);
  $("#editor-page-info").textContent = `显示 ${start}–${end} / 共 ${total} 条`;
  $("#btn-prev-page").disabled = offset <= 0;
  $("#btn-next-page").disabled = offset + limit >= total;
}

/* ── Glossary & Memory ─────────────────────────────────────────────── */

const GLOSSARY_CATS = ["术语", "角色", "地名", "物品", "UI"];

async function refreshGlossaryCounts() {
  try {
    const pid = state.project ? encodeURIComponent(state.project.id) : "";
    const sum = await api(`/api/glossary/summary?project_id=${pid}`);
    $("#cnt-proj-glossary").textContent = sum.project_glossary ?? 0;
    $("#cnt-glob-glossary").textContent = sum.global_glossary ?? 0;
    $("#cnt-global-memory").textContent = sum.global_memory ?? 0;
  } catch {
    // ignore
  }
}

async function loadGlossaryView() {
  const sub = state.glossarySubtab;
  const isMem = sub === "global-memory";
  $("#panel-glossary").hidden = isMem;
  $("#panel-memory").hidden = !isMem;
  $("#btn-glossary-import").hidden = isMem;
  refreshGlossaryCounts();

  if (isMem) {
    const q = encodeURIComponent($("#memory-search").value.trim());
    const data = await api(`/api/memory?search=${q}`);
    const list = $("#memory-list");
    if (!data.items.length) {
      list.innerHTML = `<div class="feed-empty">暂无跨游戏翻译记忆。确认或修改译文后会自动沉淀到这里。</div>`;
      return;
    }
    list.innerHTML = data.items
      .map(
        (m) => `
        <div class="simple-row" data-mem-hash="${escapeHtml(m.src_hash)}">
          <span class="feed-orig" title="${escapeHtml(m.original)}">${escapeHtml(m.original)}</span>
          <input class="inline-edit-input mem-tr-input" value="${escapeHtml(m.translation)}" data-orig="${escapeHtml(m.translation)}" />
          <span class="badge">${m.source === "manual" ? "已修改" : "AI"}</span>
          <button class="btn-icon-sm danger-hover" data-del-mem="${escapeHtml(m.src_hash)}">删除</button>
        </div>
      `
      )
      .join("");
    $$(".mem-tr-input").forEach((inp) => {
      const saveMem = async () => {
        const val = inp.value.trim();
        if (!val || val === inp.dataset.orig) {
          inp.value = inp.dataset.orig;
          return;
        }
        const hash = inp.closest(".simple-row").dataset.memHash;
        try {
          await api(`/api/memory/${hash}`, { method: "PATCH", body: { translation: val } });
          inp.dataset.orig = val;
          const badge = inp.closest(".simple-row").querySelector(".badge");
          if (badge) badge.textContent = "已修改";
          toast("翻译记忆已更新");
        } catch (err) {
          toast(err.message, "err");
          inp.value = inp.dataset.orig;
        }
      };
      inp.addEventListener("blur", saveMem);
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          inp.blur();
        }
      });
    });
    $$("[data-del-mem]").forEach((b) =>
      b.addEventListener("click", async () => {
        await api(`/api/memory/${b.dataset.delMem}`, { method: "DELETE" });
        loadGlossaryView();
      })
    );
    return;
  }

  if (sub === "project-glossary" && !state.project) {
    $("#glossary-list").innerHTML = `<div class="feed-empty">请先打开一个游戏项目。</div>`;
    return;
  }
  const baseUrl = sub === "project-glossary" ? `/api/projects/${state.project.id}/glossary` : "/api/glossary";
  const q = encodeURIComponent($("#glossary-search").value.trim());
  const data = await api(`${baseUrl}?search=${q}`);
  const list = $("#glossary-list");
  if (!data.items.length) {
    list.innerHTML = `<div class="feed-empty">${q ? "未找到匹配的术语。" : "暂无术语。添加角色名或专有名词后，翻译与审校会自动保持一致。"}</div>`;
    return;
  }
  list.innerHTML = data.items
    .map((g) => {
      const catOpts = GLOSSARY_CATS.map(
        (c) => `<option value="${c}" ${g.category === c ? "selected" : ""}>${c}</option>`
      ).join("");
      return `
        <div class="simple-row" data-term-id="${g.id}">
          <input class="inline-edit-input term-src-edit" value="${escapeHtml(g.term)}" data-orig="${escapeHtml(g.term)}" />
          <input class="inline-edit-input term-dst-edit" value="${escapeHtml(g.translation)}" data-orig="${escapeHtml(g.translation)}" />
          <select class="inline-edit-select term-cat-edit" data-orig="${escapeHtml(g.category)}">${catOpts}</select>
          <button class="btn-icon-sm danger-hover" data-del-term="${g.id}">删除</button>
        </div>
      `;
    })
    .join("");

  $$("#glossary-list .simple-row").forEach((row) => {
    const id = row.dataset.termId;
    const srcInp = row.querySelector(".term-src-edit");
    const dstInp = row.querySelector(".term-dst-edit");
    const catSel = row.querySelector(".term-cat-edit");
    const saveTerm = async () => {
      const term = srcInp.value.trim();
      const translation = dstInp.value.trim();
      const category = catSel.value;
      if (term === srcInp.dataset.orig && translation === dstInp.dataset.orig && category === catSel.dataset.orig) {
        return;
      }
      if (!term || !translation) {
        srcInp.value = srcInp.dataset.orig;
        dstInp.value = dstInp.dataset.orig;
        return toast("原文和译名均不能为空", "err");
      }
      try {
        await api(`${baseUrl}/${id}`, { method: "PUT", body: { term, translation, category } });
        srcInp.dataset.orig = term;
        dstInp.dataset.orig = translation;
        catSel.dataset.orig = category;
        toast("术语已更新");
      } catch (err) {
        toast(err.message, "err");
        srcInp.value = srcInp.dataset.orig;
        dstInp.value = dstInp.dataset.orig;
        catSel.value = catSel.dataset.orig;
      }
    };
    [srcInp, dstInp].forEach((inp) => {
      inp.addEventListener("blur", saveTerm);
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          inp.blur();
        }
      });
    });
    catSel.addEventListener("change", saveTerm);
  });

  $$("[data-del-term]").forEach((b) =>
    b.addEventListener("click", async () => {
      await api(`${baseUrl}/${b.dataset.delTerm}`, { method: "DELETE" });
      loadGlossaryView();
    })
  );
}

/* ── Settings ──────────────────────────────────────────────────────── */

function renderSettings() {
  const s = state.settings;
  if (!s) return;
  const mode = s.translate_role?.mode || "local";
  $$("#seg-mode button").forEach((b) => b.classList.toggle("active", b.dataset.val === mode));
  $("#settings-local-panel").hidden = mode !== "local";
  $("#settings-api-panel").hidden = mode !== "api";

  $$("#seg-hardware button").forEach((b) => b.classList.toggle("active", b.dataset.val === s.hardware_mode));
  $("#chk-single-model").checked = Boolean(s.use_single_model);
  $("#chk-auto-sync").checked = Boolean(s.auto_sync_xuat);

  $("#api-tr-url").value = s.translate_role?.base_url || "";
  $("#api-tr-model").value = s.translate_role?.model || "";
  $("#api-tr-key").value = s.translate_role?.api_key || "";
  $("#api-rs-url").value = s.reason_role?.base_url || "";
  $("#api-rs-model").value = s.reason_role?.model || "";
  $("#api-rs-key").value = s.reason_role?.api_key || "";

  applyTheme(s.theme);
  renderRuntime();
}

async function saveSettingsFromUI() {
  const mode = $("#seg-mode button.active")?.dataset.val || "local";
  const hardware_mode = $("#seg-hardware button.active")?.dataset.val || "auto";
  const theme = $("#seg-theme button.active")?.dataset.val || "dark";
  const rsUrl = $("#api-rs-url").value.trim();
  const rsModel = $("#api-rs-model").value.trim();
  const rsKey = $("#api-rs-key").value.trim();
  const trUrl = $("#api-tr-url").value.trim();
  const trModel = $("#api-tr-model").value.trim();
  const trKey = $("#api-tr-key").value.trim();

  const payload = {
    theme,
    hardware_mode,
    use_single_model: $("#chk-single-model").checked,
    auto_sync_xuat: $("#chk-auto-sync").checked,
    translate_role: {
      ...(state.settings?.translate_role || {}),
      mode,
      base_url: trUrl,
      model: trModel,
      api_key: trKey,
    },
    reason_role: {
      ...(state.settings?.reason_role || {}),
      mode,
      base_url: rsUrl,
      model: rsModel,
      api_key: rsKey,
    },
  };
  const res = await api("/api/settings", { method: "PUT", body: payload });
  state.settings = res.settings;
  state.runtime = res.runtime;
  renderSettings();
  toast("设置已保存");
}

/* ── SSE Live Stream ───────────────────────────────────────────────── */

function connectEvents() {
  const es = new EventSource("/api/events");
  let openedOnce = false;
  es.onopen = async () => {
    if (!openedOnce) {
      openedOnce = true;
      return;
    }
    try {
      const cur = await api("/api/tasks/current");
      applyTaskState(cur.task);
      scheduleLiveSync({ immediate: true });
    } catch {
      // ignore reconnect sync errors
    }
  };
  es.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (msg.type === "hello") {
      state.runtime = msg.data.runtime;
      applyTaskState(msg.data.task);
      renderRuntime();
    } else if (msg.type === "runtime_status") {
      state.runtime = msg.data;
      renderRuntime();
    } else if (msg.type === "task_progress") {
      applyTaskState(msg.data);
      const now = Date.now();
      if (state.project && now - lastProgressSyncAt > 1200) {
        lastProgressSyncAt = now;
        scheduleLiveSync();
      }
    } else if (msg.type === "task_done") {
      applyTaskState(msg.data);
      if (msg.data.message) {
        toast(msg.data.message, msg.data.status === "failed" ? "err" : "ok");
      }
      if (state.project && msg.data.project_id === state.project.id) {
        scheduleLiveSync({ immediate: true });
      }
    } else if (msg.type === "live_entry") {
      const entry = msg.data.entry;
      if (state.project && msg.data.project_id === state.project.id && entry) {
        state.liveFeed = [entry, ...state.liveFeed.filter((x) => x.id !== entry.id)].slice(0, 30);
        if (msg.data.stats) {
          state.stats = msg.data.stats;
        }
        renderWorkbench();
        if (state.tab === "editor") {
          scheduleLiveSync();
        }
      }
    } else if (msg.type === "context_updated") {
      if (state.project && msg.data.project_id === state.project.id) {
        state.context = msg.data.context;
        renderContextCard();
      }
    }
  };
}

/* ── Modals & Actions ──────────────────────────────────────────────── */

function openModal(id) {
  $(`#${id}`).hidden = false;
}
function closeModal(id) {
  $(`#${id}`).hidden = true;
}

async function openGameDir(dirPath) {
  if (!dirPath || !dirPath.trim()) return;
  try {
    const res = await api("/api/projects/open", { method: "POST", body: { game_dir: dirPath.trim() } });
    const projRes = await api("/api/projects");
    state.projects = projRes.projects || [];
    await selectProject(res.project.id);
    switchTab("workbench");
    closeModal("modal-open-path");
    const imp = res.imported?.added || 0;
    toast(imp ? `已打开「${res.project.name}」，已读取词条 ${imp} 条` : `已打开「${res.project.name}」`);
  } catch (err) {
    toast(err.message, "err");
  }
}

async function nativePickAndOpen() {
  try {
    const res = await api("/api/projects/pick-folder", { method: "POST" });
    if (res.path) await openGameDir(res.path);
  } catch (err) {
    toast(err.message, "err");
  }
}

function openCmdPalette() {
  const actions = [
    { label: "打开 Unity 游戏文件夹", run: () => openModal("modal-open-path") },
    { label: "前往工作台", run: () => switchTab("workbench") },
    { label: "前往译文审校（查看与修改译文）", run: () => switchTab("editor") },
    { label: "前往术语与记忆", run: () => switchTab("glossary") },
    { label: "前往设置", run: () => switchTab("settings") },
    { label: "切换深色 / 浅色外观", run: () => $("#btn-theme").click() },
  ];
  if (state.project) {
    actions.unshift(
      { label: "一键自动汉化（扫描+语境+翻译+审校+接入）", run: () => $("#btn-auto-localize").click() },
      { label: "导出汉化文本（JSON / TXT / MD / CSV）与离线补丁", run: () => openModal("modal-export") },
      { label: "呼出 / 隐藏游戏内悬浮编辑窗 (Alt+L)", run: () => $("#btn-toggle-overlay").click() },
      { label: "批量查找替换译文", run: () => openModal("modal-replace") },
      { label: "启动当前游戏（自动接入并同步最新词典）", run: () => $("#btn-launch-game").click() },
      { label: "扫描静态文本", run: () => $("#btn-scan-texts").click() },
      { label: "开始批量翻译待翻词条", run: () => $("#btn-start-translate").click() },
      { label: "智能审校（筛选高风险词条并克制修正）", run: () => $("#btn-start-review").click() },
      { label: "深度全量审校（不跳过短文本）", run: () => $("#btn-review-full").click() },
      { label: "清空当前游戏翻译与扫描记录", run: () => openModal("modal-clear") },
      { label: "自动识别游戏语境", run: () => $("#btn-infer-context").click() }
    );
  }
  const render = (q = "") => {
    const low = q.trim().toLowerCase();
    const list = actions.filter((a) => !low || a.label.toLowerCase().includes(low));
    $("#cmd-list").innerHTML = list
      .map((a, i) => `<button class="cmd-item" data-idx="${i}">${escapeHtml(a.label)}</button>`)
      .join("");
    $$(".cmd-item").forEach((btn) =>
      btn.addEventListener("click", () => {
        closeModal("modal-cmd");
        list[Number(btn.dataset.idx)].run();
      })
    );
  };
  $("#cmd-input").value = "";
  render();
  openModal("modal-cmd");
  $("#cmd-input").focus();
  $("#cmd-input").oninput = (e) => render(e.target.value);
}

/* ── Event Bindings ────────────────────────────────────────────────── */

function bindEvents() {
  $$(".nav-item").forEach((btn) => btn.addEventListener("click", () => switchTab(btn.dataset.tab)));

  $("#project-select").addEventListener("change", (e) => {
    if (e.target.value) selectProject(e.target.value);
  });

  $("#btn-open-game").addEventListener("click", () => openModal("modal-open-path"));
  $("#btn-empty-pick").addEventListener("click", nativePickAndOpen);
  $("#btn-modal-native-pick").addEventListener("click", nativePickAndOpen);
  $("#btn-empty-open-path").addEventListener("click", () => openGameDir($("#input-empty-path").value));
  $("#input-empty-path").addEventListener("keydown", (e) => {
    if (e.key === "Enter") openGameDir(e.target.value);
  });
  $("#btn-modal-open-confirm").addEventListener("click", () => openGameDir($("#modal-path-input").value));

  $("#btn-theme").addEventListener("click", async () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    applyTheme(next);
    if (state.settings) {
      state.settings.theme = next;
      await api("/api/settings", { method: "PUT", body: { theme: next } }).catch(() => {});
    }
  });

  $("#btn-cmd").addEventListener("click", openCmdPalette);
  $("#engine-status-btn").addEventListener("click", () => switchTab("settings"));
  $("#btn-cancel-task").addEventListener("click", async () => {
    const btn = $("#btn-cancel-task");
    btn.disabled = true;
    btn.textContent = "停止中…";
    try {
      await api("/api/tasks/cancel", { method: "POST" });
      const cur = await api("/api/tasks/current");
      applyTaskState(cur.task);
    } catch {
      applyTaskState(null);
    } finally {
      btn.disabled = false;
      btn.textContent = "停止";
    }
  });

  // Workbench "更多 ▾" popover
  const moreMenu = $("#wb-more-menu");
  const closeMoreMenu = () => {
    if (moreMenu) moreMenu.hidden = true;
  };
  $("#btn-wb-more").addEventListener("click", (e) => {
    e.stopPropagation();
    moreMenu.hidden = !moreMenu.hidden;
  });
  moreMenu.addEventListener("click", (e) => {
    if (e.target.closest("button")) closeMoreMenu();
    else e.stopPropagation();
  });
  document.addEventListener("click", closeMoreMenu);

  // Workbench actions
  $("#btn-auto-localize").addEventListener("click", async () => {
    if (!state.project) return;
    try {
      const res = await api(`/api/projects/${state.project.id}/tasks/auto`, {
        method: "POST",
        body: { run_review: true, deploy_hook: true, review_limit: 200 },
      });
      applyTaskState(res.task, { fromHttpStart: true });
    } catch (err) {
      toast(err.message, "err");
    }
  });

  $("#wb-loader-select").addEventListener("change", async (e) => {
    if (!state.project) return;
    const loader = e.target.value;
    try {
      toast(`正在切换为 ${loader === "melonloader" ? "MelonLoader" : "BepInEx 6"}…`);
      await api(`/api/projects/${state.project.id}/deploy`, {
        method: "POST",
        body: { loader },
      });
      await selectProject(state.project.id);
      toast(`已切换加载器并完成接入`);
    } catch (err) {
      toast(err.message, "err");
    }
  });

  $("#btn-launch-game").addEventListener("click", async () => {
    if (!state.project) return;
    const btn = $("#btn-launch-game");
    const prevText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "启动中…";
    try {
      const borderless = Boolean($("#chk-launch-borderless")?.checked);
      const res = await api(`/api/projects/${state.project.id}/launch`, {
        method: "POST",
        body: { borderless },
      });
      if (res.auto_deployed) {
        await selectProject(state.project.id);
      }
      const prefix = res.auto_deployed ? "已自动接入插件并启动" : "已同步词典并启动";
      toast(`${prefix} ${res.executable}，游戏中按 Alt+L 可呼出悬浮改译窗`);
    } catch (err) {
      toast(err.message, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = prevText;
    }
  });

  $("#btn-open-clear").addEventListener("click", () => {
    if (!state.project) return;
    openModal("modal-clear");
  });

  const runClearRecords = async (rescan) => {
    if (!state.project) return;
    const clear_game_dict = Boolean($("#clear-game-dict")?.checked);
    const clear_context = Boolean($("#clear-context")?.checked);
    try {
      const res = await api(`/api/projects/${state.project.id}/clear`, {
        method: "POST",
        body: { clear_game_dict, clear_context, rescan },
      });
      closeModal("modal-clear");
      await selectProject(state.project.id);
      if (state.tab === "editor") loadEntries();
      const msg = rescan
        ? `已清空 ${res.cleared} 条旧记录，并重新扫出 ${res.rescanned} 条纯静态界面文本`
        : `已彻底清空 ${res.cleared} 条记录，当前已归零`;
      toast(msg);
    } catch (err) {
      toast(err.message, "err");
    }
  };
  $("#btn-confirm-clear-only").addEventListener("click", () => runClearRecords(false));
  $("#btn-confirm-clear-rescan").addEventListener("click", () => runClearRecords(true));

  $("#btn-delete-project").addEventListener("click", async () => {
    if (!state.project) return;
    if (!confirm(`确定从工作台移除「${state.project.name}」的记录？游戏原文件不受任何影响。`)) return;
    try {
      await api(`/api/projects/${state.project.id}`, { method: "DELETE" });
      const projRes = await api("/api/projects");
      state.projects = projRes.projects || [];
      if (projRes.active_project_id) {
        await selectProject(projRes.active_project_id);
      } else {
        state.project = null;
        state.stats = null;
        state.context = null;
        state.liveFeed = [];
        renderProjectSelector();
        switchTab("workbench");
      }
      toast("已移除该项目记录");
    } catch (err) {
      toast(err.message, "err");
    }
  });

  $("#btn-sync-ctx-glossary").addEventListener("click", async () => {
    if (!state.project) return;
    try {
      const res = await api(`/api/projects/${state.project.id}/context/sync-glossary`, { method: "POST" });
      toast(res.added > 0 ? `已将 ${res.added} 条语境专名导入本游戏术语表` : "语境中的专名已在术语表中");
    } catch (err) {
      toast(err.message, "err");
    }
  });

  // Export text (JSON / TXT / Bilingual / MD / CSV) & Standalone Patch
  const openExport = () => {
    if (!state.project) return;
    openModal("modal-export");
  };
  $("#btn-open-export").addEventListener("click", openExport);
  $("#btn-editor-export").addEventListener("click", openExport);

  $$("[data-export-fmt]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (!state.project) return;
      const fmt = btn.dataset.exportFmt;
      const scope = $("#export-scope")?.value || "done";
      const url = `/api/projects/${state.project.id}/export-text?fmt=${encodeURIComponent(fmt)}&scope=${encodeURIComponent(scope)}`;
      const a = document.createElement("a");
      a.href = url;
      a.download = "";
      document.body.appendChild(a);
      a.click();
      a.remove();
      toast(`已开始导出 ${fmt.toUpperCase()} 文件`);
    });
  });

  $("#btn-export-patch").addEventListener("click", () => {
    if (!state.project) return;
    const a = document.createElement("a");
    a.href = `/api/projects/${state.project.id}/export-patch`;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast("正在打包独立离线中文补丁 (.zip)…");
  });

  // Bulk find & replace
  $("#btn-open-replace").addEventListener("click", () => {
    if (!state.project) return;
    $("#replace-find").value = "";
    $("#replace-with").value = "";
    openModal("modal-replace");
    $("#replace-find").focus();
  });

  $("#btn-confirm-replace").addEventListener("click", async () => {
    if (!state.project) return;
    const find = $("#replace-find").value;
    const replace = $("#replace-with").value;
    if (!find) return toast("请输入要查找的内容", "err");
    try {
      const res = await api(`/api/projects/${state.project.id}/replace`, {
        method: "POST",
        body: { find, replace, only_unlocked: $("#replace-only-unlocked").checked },
      });
      closeModal("modal-replace");
      state.stats = res.stats;
      renderWorkbench();
      if (state.tab === "editor") loadEntries();
      toast(res.replaced > 0 ? `已批量替换 ${res.replaced} 条译文并同步` : "未匹配到包含该词组的译文");
    } catch (err) {
      toast(err.message, "err");
    }
  });

  $("#btn-scan-texts").addEventListener("click", async () => {
    if (!state.project) return;
    const btn = $("#btn-scan-texts");
    btn.disabled = true;
    btn.textContent = "扫描中…";
    try {
      const res = await api(`/api/projects/${state.project.id}/scan`, { method: "POST" });
      await selectProject(state.project.id);
      toast(res.added > 0 ? `静态文本扫描完成，新增 ${res.added} 条词条` : `静态文本扫描完成，当前共 ${res.stats.total} 条词条`);
    } catch (err) {
      toast(err.message, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = "扫描静态文本";
    }
  });

  $("#btn-uninstall-hook").addEventListener("click", async () => {
    if (!state.project || !confirm("确定卸载该游戏的汉化插件？原游戏文件不会受任何影响。")) return;
    try {
      await api(`/api/projects/${state.project.id}/uninstall`, { method: "POST" });
      await selectProject(state.project.id);
      toast("已干净卸载");
    } catch (err) {
      toast(err.message, "err");
    }
  });

  const startTranslate = async () => {
    if (!state.project) return;
    try {
      const res = await api(`/api/projects/${state.project.id}/tasks/translate`, {
        method: "POST",
        body: { include_failed: true },
      });
      applyTaskState(res.task, { fromHttpStart: true });
    } catch (err) {
      toast(err.message, "err");
    }
  };
  $("#btn-start-translate").addEventListener("click", startTranslate);
  $("#btn-editor-translate").addEventListener("click", startTranslate);

  $("#btn-start-review").addEventListener("click", async () => {
    if (!state.project) return;
    try {
      const res = await api(`/api/projects/${state.project.id}/tasks/review`, {
        method: "POST",
        body: { limit: 300, only_unreviewed: true, mode: "smart" },
      });
      applyTaskState(res.task, { fromHttpStart: true });
    } catch (err) {
      toast(err.message, "err");
    }
  });

  $("#btn-review-full").addEventListener("click", async () => {
    if (!state.project) return;
    try {
      const res = await api(`/api/projects/${state.project.id}/tasks/review`, {
        method: "POST",
        body: { limit: 500, only_unreviewed: false, mode: "full" },
      });
      applyTaskState(res.task, { fromHttpStart: true });
    } catch (err) {
      toast(err.message, "err");
    }
  });

  $("#btn-infer-context").addEventListener("click", async () => {
    if (!state.project) return;
    try {
      const res = await api(`/api/projects/${state.project.id}/tasks/context`, { method: "POST" });
      applyTaskState(res.task, { fromHttpStart: true });
    } catch (err) {
      toast(err.message, "err");
    }
  });

  const toggleOverlay = async () => {
    try {
      const res = await api("/api/overlay/toggle", { method: "POST" });
      toast(res.visible ? `已呼出悬浮编辑窗（游戏中也可按 ${res.hotkey} 切换）` : `已隐藏悬浮编辑窗（按 ${res.hotkey} 随时呼出）`);
    } catch (err) {
      toast(err.message, "err");
    }
  };
  $("#btn-toggle-overlay").addEventListener("click", toggleOverlay);
  $("#btn-editor-overlay").addEventListener("click", toggleOverlay);

  $("#btn-goto-editor").addEventListener("click", () => switchTab("editor"));
  $("#btn-quick-add").addEventListener("click", () => openModal("modal-import"));
  $("#btn-editor-add").addEventListener("click", () => openModal("modal-import"));

  $("#btn-confirm-import").addEventListener("click", async () => {
    if (!state.project) return;
    const content = $("#import-textarea").value;
    if (!content.trim()) return;
    try {
      const res = await api(`/api/projects/${state.project.id}/import-text`, {
        method: "POST",
        body: { content },
      });
      $("#import-textarea").value = "";
      closeModal("modal-import");
      state.stats = res.stats;
      renderWorkbench();
      if (state.tab === "editor") loadEntries();
      toast(`已添加 ${res.imported.added} 条词条`);
    } catch (err) {
      toast(err.message, "err");
    }
  });

  // Edit context modal
  $("#btn-edit-context").addEventListener("click", () => {
    const c = state.context || {};
    $("#ctx-genre").value = c.genre || "";
    $("#ctx-setting").value = c.setting || "";
    $("#ctx-style").value = c.style || "";
    $("#ctx-summary").value = c.summary || "";
    $("#ctx-characters").value = (c.characters || []).join("，");
    $("#ctx-notes").value = (c.notes || []).join("，");
    openModal("modal-context");
  });

  $("#btn-save-context").addEventListener("click", async () => {
    if (!state.project) return;
    const splitList = (v) => v.split(/[,，、]/).map((s) => s.trim()).filter(Boolean);
    const body = {
      game_name: state.project.name,
      genre: $("#ctx-genre").value.trim(),
      setting: $("#ctx-setting").value.trim(),
      style: $("#ctx-style").value.trim(),
      summary: $("#ctx-summary").value.trim(),
      characters: splitList($("#ctx-characters").value),
      notes: splitList($("#ctx-notes").value),
      terms: state.context?.terms || [],
    };
    const res = await api(`/api/projects/${state.project.id}/context`, { method: "PUT", body });
    state.context = res.context;
    renderContextCard();
    closeModal("modal-context");
    toast("游戏语境已保存");
  });

  // Editor filters & search
  let searchTimer = null;
  $("#editor-search").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.editor.search = e.target.value;
      state.editor.offset = 0;
      loadEntries();
    }, 200);
  });

  $$("#editor-sources button").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$("#editor-sources button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.editor.source = btn.dataset.source;
      state.editor.offset = 0;
      loadEntries();
    });
  });

  $$("#editor-filters .pill").forEach((pill) => {
    pill.addEventListener("click", () => {
      $$("#editor-filters .pill").forEach((b) => b.classList.remove("active"));
      pill.classList.add("active");
      state.editor.status = pill.dataset.status;
      state.editor.offset = 0;
      loadEntries();
    });
  });

  $("#btn-prev-page").addEventListener("click", () => {
    state.editor.offset = Math.max(0, state.editor.offset - state.editor.limit);
    loadEntries();
  });
  $("#btn-next-page").addEventListener("click", () => {
    state.editor.offset += state.editor.limit;
    loadEntries();
  });

  // Glossary & Memory
  $$(".subtab").forEach((btn) =>
    btn.addEventListener("click", () => {
      $$(".subtab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.glossarySubtab = btn.dataset.subtab;
      loadGlossaryView();
    })
  );

  $("#form-add-term").addEventListener("submit", async (e) => {
    e.preventDefault();
    const isProj = state.glossarySubtab === "project-glossary";
    if (isProj && !state.project) return toast("请先打开一个游戏项目", "err");
    const url = isProj ? `/api/projects/${state.project.id}/glossary` : "/api/glossary";
    try {
      await api(url, {
        method: "POST",
        body: {
          term: $("#term-src").value,
          translation: $("#term-dst").value,
          category: $("#term-cat").value,
        },
      });
      $("#term-src").value = "";
      $("#term-dst").value = "";
      loadGlossaryView();
      toast("术语已添加");
    } catch (err) {
      toast(err.message, "err");
    }
  });

  let glosTimer = null;
  $("#glossary-search").addEventListener("input", () => {
    clearTimeout(glosTimer);
    glosTimer = setTimeout(loadGlossaryView, 180);
  });

  $("#btn-glossary-import").addEventListener("click", () => {
    if (state.glossarySubtab === "project-glossary" && !state.project) {
      return toast("请先打开一个游戏项目", "err");
    }
    $("#glossary-import-text").value = "";
    openModal("modal-glossary-import");
    $("#glossary-import-text").focus();
  });

  $("#btn-confirm-glossary-import").addEventListener("click", async () => {
    const content = $("#glossary-import-text").value;
    if (!content.trim()) return toast("请输入要导入的术语内容", "err");
    const isProj = state.glossarySubtab === "project-glossary";
    const url = isProj ? `/api/projects/${state.project.id}/glossary/import` : "/api/glossary/import";
    try {
      const res = await api(url, { method: "POST", body: { content } });
      closeModal("modal-glossary-import");
      loadGlossaryView();
      toast(res.imported > 0 ? `已批量导入 ${res.imported} 条术语` : "未识别到「原文=译名」格式的行");
    } catch (err) {
      toast(err.message, "err");
    }
  });

  $("#btn-glossary-clear").addEventListener("click", async () => {
    const sub = state.glossarySubtab;
    if (sub === "project-glossary" && !state.project) {
      return toast("请先打开一个游戏项目", "err");
    }
    const labels = {
      "project-glossary": `本游戏（${state.project?.name || ""}）的全部术语`,
      "global-glossary": "全局术语库的全部术语",
      "global-memory": "跨游戏翻译记忆的全部记录",
    };
    if (!confirm(`确定一键清空${labels[sub]}？此操作不可撤销。`)) return;
    const url =
      sub === "project-glossary"
        ? `/api/projects/${state.project.id}/glossary`
        : sub === "global-glossary"
        ? "/api/glossary"
        : "/api/memory";
    try {
      const res = await api(url, { method: "DELETE" });
      loadGlossaryView();
      toast(`已清空 ${res.removed ?? 0} 条记录`);
    } catch (err) {
      toast(err.message, "err");
    }
  });

  let memTimer = null;
  $("#memory-search").addEventListener("input", () => {
    clearTimeout(memTimer);
    memTimer = setTimeout(loadGlossaryView, 220);
  });

  // Settings controls
  $$(".segmented").forEach((seg) => {
    seg.querySelectorAll("button").forEach((btn) => {
      btn.addEventListener("click", () => {
        seg.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        if (seg.id === "seg-mode") {
          const isLocal = btn.dataset.val === "local";
          $("#settings-local-panel").hidden = !isLocal;
          $("#settings-api-panel").hidden = isLocal;
        }
        if (seg.id === "seg-theme") applyTheme(btn.dataset.val);
      });
    });
  });

  $("#btn-save-settings").addEventListener("click", saveSettingsFromUI);

  $("#btn-warm-tr").addEventListener("click", async () => {
    toast("正在启动本地翻译引擎…");
    try {
      const res = await api("/api/runtime/start", { method: "POST", body: { role: "translate" } });
      state.runtime = res.runtime;
      renderRuntime();
      toast("翻译引擎已就绪");
    } catch (err) {
      toast(err.message, "err");
    }
  });

  $("#btn-stop-tr").addEventListener("click", async () => {
    const res = await api("/api/runtime/stop", { method: "POST", body: { role: "translate" } });
    state.runtime = res.runtime;
    renderRuntime();
    toast("翻译引擎已停止");
  });

  $("#btn-stop-rs").addEventListener("click", async () => {
    const res = await api("/api/runtime/stop", { method: "POST", body: { role: "reason" } });
    state.runtime = res.runtime;
    renderRuntime();
    toast("审校模型显存已释放");
  });

  $("#btn-test-tr-api").addEventListener("click", async () => {
    const btn = $("#btn-test-tr-api");
    btn.disabled = true;
    btn.textContent = "测试中…";
    try {
      const res = await api("/api/runtime/test", {
        method: "POST",
        body: {
          mode: "api",
          role: "translate",
          base_url: $("#api-tr-url").value.trim(),
          model: $("#api-tr-model").value.trim(),
          api_key: $("#api-tr-key").value.trim(),
        },
      });
      toast(`翻译接口连接成功：${res.reply}`);
    } catch (err) {
      toast(err.message, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = "测试连接";
    }
  });

  $("#btn-test-rs-api").addEventListener("click", async () => {
    const btn = $("#btn-test-rs-api");
    btn.disabled = true;
    btn.textContent = "测试中…";
    try {
      const rsUrl = $("#api-rs-url").value.trim();
      const rsModel = $("#api-rs-model").value.trim();
      const rsKey = $("#api-rs-key").value.trim();
      const res = await api("/api/runtime/test", {
        method: "POST",
        body: {
          mode: "api",
          role: "reason",
          base_url: rsUrl || $("#api-tr-url").value.trim(),
          model: rsModel || $("#api-tr-model").value.trim(),
          api_key: rsKey || $("#api-tr-key").value.trim(),
        },
      });
      toast(`审校接口连接成功：${res.reply}`);
    } catch (err) {
      toast(err.message, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = "测试审校接口";
    }
  });

  // Modal closing & global shortcuts
  $$("[data-close-modal]").forEach((btn) =>
    btn.addEventListener("click", () => closeModal(btn.dataset.closeModal))
  );
  $$(".modal-backdrop").forEach((bd) => {
    bd.addEventListener("click", (e) => {
      if (e.target === bd) bd.hidden = true;
    });
  });

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      openCmdPalette();
    } else if (e.key === "Escape") {
      $$(".modal-backdrop").forEach((m) => (m.hidden = true));
      closeMoreMenu();
    } else if (e.key === "/" && state.tab === "editor" && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "TEXTAREA") {
      e.preventDefault();
      $("#editor-search").focus();
    }
  });
}

bindEvents();
bootstrap().catch((err) => toast(`初始化失败：${err.message}`, "err"));
