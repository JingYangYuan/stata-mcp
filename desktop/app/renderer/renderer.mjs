const api = window.stataMcpDesktop;

const MCP_TOOLS_CATALOG = [
  { name: "stata_inspect_data", cat: "数据探查", desc: "[步骤1] 直连 /view_data 查看内存数据集行数、列名、类型与样本预览" },
  { name: "stata_describe", cat: "数据探查", desc: "[步骤1] 只读运行 describe + summarize + misstable summarize（支持内存或指定 .dta 文件）" },
  { name: "stata_stored_results", cat: "数据探查", desc: "[步骤1/3] 查看内存中的 e()、r()、c() 与已保存估计结果 estimates dir" },
  { name: "stata_which", cat: "Help 参考", desc: "[步骤2] 检查命令是否已安装（自动解析别名、built-in 或 .ado 路径与版本头）" },
  { name: "stata_search_help", cat: "Help 参考", desc: "[步骤2] 按关键词检索已安装的 base / SSC help 文件与命令别名" },
  { name: "stata_help", cat: "Help 参考", desc: "[步骤2] 自动解析别名与 SMCL 格式的官方/SSC help 文档（支持按章节提取）" },
  { name: "stata_run_file", cat: "执行与作业", desc: "[步骤3] 运行 .do 文件（短任务直出，长任务异步 job_id，支持 lint=True，出错自带行号定位）" },
  { name: "stata_run_selection", cat: "执行与作业", desc: "[步骤3] 运行选中代码片段，短任务直出结果、长任务返回 job_id" },
  { name: "stata_job_status", cat: "执行与作业", desc: "[步骤3] 查询作业状态、步骤数、耗时与失败时的 error_code / error_line" },
  { name: "stata_job_tail", cat: "执行与作业", desc: "[步骤3] 按 offset 增量读取步骤预览 (steps) 或完整日志 (full)" },
  { name: "stata_job_list", cat: "执行与作业", desc: "[步骤3] 列出最近 Stata 作业及摘要" },
  { name: "stata_job_cancel", cat: "执行与作业", desc: "[步骤3] 向引擎发送 /stop_execution 终止运行中的作业" },
  { name: "stata_session", cat: "会话管理", desc: "[步骤3] 列出或销毁并行 Stata 会话 (session_id)" },
  { name: "stata_diagnose_error", cat: "报错定位", desc: "[步骤4] 定位失败作业（或任意日志）在 .do 中的精确行号、上下文与修复建议" },
  { name: "stata_run_lines", cat: "执行与作业", desc: "[步骤4] 运行指定行区间 [start_line, end_line] 快速复测，报错自动映射回原文件行号" },
  { name: "stata_lint_do", cat: "破除循环", desc: "[步骤5] 拦截 1/_N、levelsof+foreach、前向填充、mvencode、宽表年份、preserve 循环等低效模式" },
  { name: "stata_vectorize_guide", cat: "破除循环", desc: "[步骤5] 提供 15 类经真实 Stata 19.5 验证的向量化替代方案（bysort / egen / frames / mata 等）" },
];

const SAMPLE_LINT_CODE = `levelsof industry, local(inds)
foreach g of local inds {
    summarize sales if industry == \`g'
    replace ind_mean = r(mean) if industry == \`g'
}
keep if sales > 0`;

const stateUI = {
  transport: "stdio",
  toolsCategory: "全部",
  jobsFilter: "all",
  selectedJobId: "",
  jobsCache: [],
  helpSection: "",
  logName: "gateway.log",
  allRecipes: [],
  actionRunning: false,
  userEditedPort: false,
  userEditedTimeout: false,
};

function $(id) {
  return document.getElementById(id);
}

function escapeHtml(str) {
  return String(str ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

let _snackbarTimer = null;
function showSnackbar(message, durationMs = 2200) {
  const el = $("app-snackbar");
  if (!el) return;
  el.textContent = message;
  el.classList.add("visible");
  if (_snackbarTimer) clearTimeout(_snackbarTimer);
  _snackbarTimer = setTimeout(() => {
    el.classList.remove("visible");
  }, durationMs);
}

let _busyDepth = 0;
function setGlobalBusy(active) {
  _busyDepth = Math.max(0, _busyDepth + (active ? 1 : -1));
  const bar = $("global-progress-bar");
  if (bar) bar.classList.toggle("active", _busyDepth > 0);
}

function flashCopied(btn, originalLabel) {
  if (!btn) return;
  const prev = originalLabel || btn.textContent;
  btn.textContent = "已复制 ✓";
  showSnackbar("已复制到剪贴板");
  setTimeout(() => {
    btn.textContent = prev;
  }, 1400);
}

async function copyWithFeedback(text, btn) {
  await api.copyText(text);
  flashCopied(btn);
}

/* ── Theme, Motion, Density & Platform ──────────────────────────────── */
function applyTheme(choice) {
  const valid = ["light", "system", "dark"].includes(choice) ? choice : "system";
  document.documentElement.setAttribute("data-theme", valid);
  localStorage.setItem("stata-mcp-theme", valid);
  document.querySelectorAll(".theme-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.themeChoice === valid);
  });
}

function applyMotion(choice) {
  const valid = ["full", "reduced"].includes(choice) ? choice : "full";
  document.documentElement.setAttribute("data-motion", valid);
  localStorage.setItem("stata-mcp-motion", valid);
  document.querySelectorAll("[data-motion-choice]").forEach((b) => {
    b.classList.toggle("active", b.dataset.motionChoice === valid);
  });
}

function applyDensity(choice) {
  const valid = ["comfortable", "compact"].includes(choice) ? choice : "comfortable";
  document.documentElement.setAttribute("data-density", valid);
  localStorage.setItem("stata-mcp-density", valid);
  document.querySelectorAll("[data-density-choice]").forEach((b) => {
    b.classList.toggle("active", b.dataset.densityChoice === valid);
  });
}

function attachMaterialRipple() {
  document.addEventListener("pointerdown", (e) => {
    if (document.documentElement.getAttribute("data-motion") === "reduced") return;
    const target = e.target.closest(".btn, .nav-btn, .workflow-step");
    if (!target || target.disabled) return;
    const rect = target.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height);
    const ripple = document.createElement("span");
    ripple.className = "md-ripple";
    ripple.style.width = `${size}px`;
    ripple.style.height = `${size}px`;
    ripple.style.left = `${e.clientX - rect.left - size / 2}px`;
    ripple.style.top = `${e.clientY - rect.top - size / 2}px`;
    target.appendChild(ripple);
    setTimeout(() => ripple.remove(), 520);
  });
}

function initPlatformAndTheme() {
  if (api.platform) {
    document.body.setAttribute("data-platform", api.platform);
  }
  applyTheme(localStorage.getItem("stata-mcp-theme") || "system");
  const prefersReduced = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
  applyMotion(localStorage.getItem("stata-mcp-motion") || (prefersReduced ? "reduced" : "full"));
  applyDensity(localStorage.getItem("stata-mcp-density") || "comfortable");

  document.querySelectorAll(".theme-btn").forEach((btn) => {
    btn.addEventListener("click", () => applyTheme(btn.dataset.themeChoice));
  });
  document.querySelectorAll("[data-motion-choice]").forEach((btn) => {
    btn.addEventListener("click", () => {
      applyMotion(btn.dataset.motionChoice);
      showSnackbar(btn.dataset.motionChoice === "reduced" ? "已切换为减弱动效" : "已启用 Material 标准动效");
    });
  });
  document.querySelectorAll("[data-density-choice]").forEach((btn) => {
    btn.addEventListener("click", () => {
      applyDensity(btn.dataset.densityChoice);
      showSnackbar(btn.dataset.densityChoice === "compact" ? "已切换为紧凑布局" : "已切换为舒适布局");
    });
  });
  attachMaterialRipple();
}

/* ── Tab Navigation ─────────────────────────────────────────────────── */
function switchTab(tabId) {
  const validTabs = ["overview", "config", "jobs", "help", "lint", "doctor"];
  const target = validTabs.includes(tabId) ? tabId : "overview";
  localStorage.setItem("stata-mcp-active-tab", target);
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === target);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `tab-${target}`);
  });
  if (target === "config") refreshConfigSnippet();
  if (target === "jobs") refreshJobs();
  if (target === "doctor") refreshLogs();
  if (target === "lint" && !stateUI.allRecipes.length) loadAllRecipes();
}

function openHelpForTopic(topic) {
  if (!topic) return;
  $("help-topic-input").value = topic;
  switchTab("help");
  lookupHelp();
}

/* ── Console Drawer ─────────────────────────────────────────────────── */
function appendConsole(text) {
  const drawer = $("console-drawer");
  if (drawer && !drawer.open) drawer.open = true;
  const box = $("action-console");
  if (box.textContent === "等待操作…") box.textContent = "";
  box.textContent += text;
  box.scrollTop = box.scrollHeight;
}

/* ── 1. Overview & 3-Step Wizard ────────────────────────────────────── */
function _syncStataSelectToInput(cands) {
  const select = $("in-stata-select");
  const cur = $("in-stata-path").value.trim().toLowerCase();
  if (!cur) return cands[0] || null;
  const matched = cands.find((c) => (c.path || "").toLowerCase() === cur) || null;
  if (matched) select.value = matched.path;
  return matched;
}

async function refreshOverview() {
  const refreshBtn = $("btn-refresh-overview");
  if (refreshBtn) {
    refreshBtn.disabled = true;
    refreshBtn.textContent = "检测中…";
  }
  setGlobalBusy(true);
  try {
    const res = await api.getOverview({ stataPath: $("in-stata-path").value.trim() || undefined });
    if (res.appVersion) {
      $("brand-sub-version").textContent = `v${res.appVersion} · 17 Tools`;
    }
    const doc = res.doctor || {};
    const rt = doc.runtime || {};
    const eng = rt.engine || {};
    const st = doc.state || {};
    const cands = doc.stata_candidates || [];

    const gwOk = Boolean(rt.healthy);
    const engOk = Boolean(eng.healthy);
    const installed = Boolean(doc.installed);

    // Sidebar live dots
    $("sidebar-gateway-pill").className = `status-dot-row ${gwOk ? "ok" : "bad"}`;
    $("sidebar-gateway-pill").querySelector(".dot-label").textContent = `Gateway :${rt.port || 4001}`;
    $("sidebar-engine-pill").className = `status-dot-row ${engOk ? "ok" : "bad"}`;
    $("sidebar-engine-pill").querySelector(".dot-label").textContent = `Engine :${eng.port || 4002}`;

    // Overall pill
    const overall = $("ov-overall-badge");
    if (gwOk && engOk) {
      overall.textContent = "服务运行中";
      overall.className = "pill-badge ok";
    } else if (installed) {
      overall.textContent = "已安装 · 未启动";
      overall.className = "pill-badge warn";
    } else {
      overall.textContent = "待安装";
      overall.className = "pill-badge";
    }

    // Step 1
    const select = $("in-stata-select");
    select.innerHTML = "";
    if (!cands.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "未自动发现 Stata，请在下方手动指定根目录";
      select.appendChild(opt);
      $("step1-badge").textContent = "需指定目录";
      $("step1-badge").className = "pill-badge warn";
    } else {
      for (const c of cands) {
        const opt = document.createElement("option");
        opt.value = c.path;
        opt.textContent = `${c.path} (${(c.edition || "").toUpperCase()} ${c.version || ""})`;
        select.appendChild(opt);
      }
      if (!$("in-stata-path").value.trim()) {
        $("in-stata-path").value = st.stata_path || cands[0].path;
      }
      const activeCand = _syncStataSelectToInput(cands);
      if (activeCand && activeCand.valid !== false) {
        $("step1-badge").textContent = `已就绪 · ${(activeCand.edition || "").toUpperCase()} ${activeCand.version || ""}`.trim();
        $("step1-badge").className = "pill-badge ok";
      } else if ($("in-stata-path").value.trim()) {
        $("step1-badge").textContent = "目录无效或未检测到 Stata";
        $("step1-badge").className = "pill-badge warn";
      } else {
        $("step1-badge").textContent = "需指定目录";
        $("step1-badge").className = "pill-badge warn";
      }
    }

    $("chip-python").textContent = `Python ${doc.python?.version || "?"}`;
    $("chip-uv").textContent = `uv: ${doc.tools?.uv ? "已装" : "未装 (可选)"}`;
    $("chip-git").textContent = `git: ${doc.tools?.git ? "已装" : "未装"}`;
    $("chip-home").textContent = res.stateHome || doc.home || "~/.stata-mcp";

    // Step 2
    if (installed) {
      $("step2-badge").textContent = `已安装 v${st.installer_version || "1.2.4"}`;
      $("step2-badge").className = "pill-badge ok";
    } else {
      $("step2-badge").textContent = "尚未安装";
      $("step2-badge").className = "pill-badge";
    }
    if (st.port && !stateUI.userEditedPort) $("in-port").value = st.port;
    if (st.default_timeout && !stateUI.userEditedTimeout) $("in-timeout").value = st.default_timeout;

    // Step 3
    $("ov-gateway-status").textContent = gwOk ? "运行中" : "未运行";
    $("ov-gateway-meta").textContent = `${rt.host || "127.0.0.1"}:${rt.port || 4001}` + (rt.pid ? ` · PID ${rt.pid}` : "");
    $("ov-engine-status").textContent = engOk ? "运行中" : "未运行";
    $("ov-engine-meta").textContent = `:${eng.port || 4002}` + (eng.pid ? ` · PID ${eng.pid}` : "");
    if (gwOk && engOk) {
      $("step3-badge").textContent = "双进程在线";
      $("step3-badge").className = "pill-badge ok";
    } else if (gwOk || engOk) {
      $("step3-badge").textContent = "部分在线";
      $("step3-badge").className = "pill-badge warn";
    } else {
      $("step3-badge").textContent = "未运行";
      $("step3-badge").className = "pill-badge";
    }

    // Warn when another process already answers on port without our installation
    const portConflict = !installed && gwOk;
    $("port-conflict-banner").classList.toggle("hidden", !portConflict);
    if (portConflict) {
      $("port-conflict-text").textContent = `${rt.host || "127.0.0.1"}:${rt.port || 4001} 已有非本程序启动的服务在响应，直接启动可能会冲突。`;
    }

    // Doctor Tab Metrics
    $("doc-platform").textContent = `${doc.platform || "?"} · ${doc.arch || "?"}`;
    $("doc-stata-count").textContent = `发现 ${cands.length} 个 Stata 候选`;
    $("doc-python").textContent = `Python ${doc.python?.version || "?"}`;
    $("doc-tools").textContent = `uv: ${doc.tools?.uv ? "✓" : "×"} · git: ${doc.tools?.git ? "✓" : "×"}`;
    $("doc-home").textContent = res.stateHome || doc.home || "~/.stata-mcp";
    $("doc-installed").textContent = installed ? "state.json 正常" : "尚未安装";
    $("doc-jobs").textContent = `${doc.jobs_count ?? 0} 个作业`;
    $("doc-problems").textContent = doc.problems ? `${doc.problems} 项需注意` : "状态正常";
    $("doctor-json-box").textContent = JSON.stringify(doc, null, 2);
  } catch (err) {
    $("ov-overall-badge").textContent = "检测异常";
    $("ov-overall-badge").className = "pill-badge bad";
    appendConsole(`\n[错误] 刷新状态失败: ${err?.message || err}\n`);
  } finally {
    setGlobalBusy(false);
    if (refreshBtn) {
      refreshBtn.disabled = false;
      refreshBtn.textContent = "刷新状态";
    }
  }
}

const ACTION_BTN_MAP = {
  install: "btn-install",
  refresh: "btn-refresh-bin",
  start: "btn-start",
  stop: "btn-stop",
  verify: "btn-verify",
  uninstall: "btn-uninstall",
};

function setActionBusy(busy, activeAction = "") {
  stateUI.actionRunning = busy;
  setGlobalBusy(busy);
  for (const id of Object.values(ACTION_BTN_MAP)) {
    const el = $(id);
    if (!el) continue;
    el.disabled = busy;
    el.classList.toggle("is-loading", busy && ACTION_BTN_MAP[activeAction] === id);
  }
}

async function runAction(action, extraOpts = {}) {
  if (stateUI.actionRunning) {
    appendConsole("\n[提示] 上一个操作仍在执行中，请稍候…\n");
    return;
  }
  setActionBusy(true, action);
  const statusEl = $("console-status-text");
  statusEl.textContent = `正在执行 ${action}…`;
  statusEl.className = "drawer-status running";
  $("console-drawer").open = true;
  try {
    const opts = {
      stataPath: $("in-stata-path").value.trim(),
      port: Number($("in-port").value || 4001),
      defaultTimeout: Number($("in-timeout").value || 21600),
      mirror: $("in-mirror").value.trim(),
      refresh: $("chk-reclone").checked,
      ...extraOpts,
    };
    const res = await api.runAction(action, opts);
    if (res.code !== 0 && res.stderr && !$("action-console").textContent.includes(res.stderr.trim())) {
      appendConsole(`\n${res.stderr.trim()}\n`);
    }
    appendConsole(`\n[退出码 ${res.code}]\n`);
    if (res.code === 0) {
      statusEl.textContent = `${action} 完成`;
      statusEl.className = "drawer-status ok";
      if (action === "install" || action === "refresh") {
        stateUI.userEditedPort = false;
        stateUI.userEditedTimeout = false;
      }
    } else {
      statusEl.textContent = `${action} 失败 (${res.code})`;
      statusEl.className = "drawer-status bad";
    }
    await refreshOverview();
  } catch (err) {
    appendConsole(`\n[异常] ${err?.message || err}\n`);
    statusEl.textContent = `${action} 异常`;
    statusEl.className = "drawer-status bad";
  } finally {
    setActionBusy(false);
  }
}

/* ── 2. Config & 17 Tools ───────────────────────────────────────────── */
async function refreshConfigSnippet() {
  const res = await api.getConfigSnippet({
    transport: stateUI.transport,
    root: $("cfg-root").value.trim() || "mcpServers",
    key: $("cfg-key").value.trim() || "stata-mcp",
    timeoutMs: Number($("cfg-timeout-ms").value || 0) || undefined,
  });
  $("cfg-snippet-box").textContent = res.snippet || res.stderr || "{}";
}

function renderToolsCatalog() {
  const cats = ["全部", ...new Set(MCP_TOOLS_CATALOG.map((t) => t.cat))];
  $("tools-cat-pills").innerHTML = cats
    .map(
      (c) =>
        `<button type="button" class="pill-btn ${stateUI.toolsCategory === c ? "active" : ""}" data-cat="${escapeHtml(c)}">${escapeHtml(c)}</button>`,
    )
    .join("");
  $("tools-cat-pills").querySelectorAll(".pill-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      stateUI.toolsCategory = btn.dataset.cat;
      renderToolsCatalog();
    });
  });

  const q = ($("tools-filter-input").value || "").trim().toLowerCase();
  const filtered = MCP_TOOLS_CATALOG.filter((t) => {
    if (stateUI.toolsCategory !== "全部" && t.cat !== stateUI.toolsCategory) return false;
    if (!q) return true;
    return t.name.toLowerCase().includes(q) || t.desc.toLowerCase().includes(q) || t.cat.toLowerCase().includes(q);
  });

  $("tools-catalog").innerHTML = filtered
    .map(
      (t) => `<div class="tool-item">
        <div class="tool-head">
          <span class="tool-name">${escapeHtml(t.name)}</span>
          <span class="tool-cat">${escapeHtml(t.cat)}</span>
        </div>
        <div class="tool-desc">${escapeHtml(t.desc)}</div>
      </div>`,
    )
    .join("") || `<div class="hint-text">没有匹配的 MCP 工具</div>`;
}

/* ── 3. Jobs & Error Localization ───────────────────────────────────── */
async function refreshJobs() {
  stateUI.jobsCache = await api.listJobs(40);
  renderJobsList();
}

function renderJobsList() {
  const listEl = $("jobs-list");
  const rows =
    stateUI.jobsFilter === "failed"
      ? stateUI.jobsCache.filter((j) => j.status === "failed")
      : stateUI.jobsCache;

  if (!rows.length) {
    listEl.innerHTML = `<div class="hint-text">${stateUI.jobsFilter === "failed" ? "暂无失败作业" : "暂无作业记录"}</div>`;
    return;
  }

  listEl.innerHTML = rows
    .map((j) => {
      const fullPath = j.source_file || j.file_path || "";
      const baseName = fullPath.split(/[\\/]/).pop() || j.kind || "selection";
      const errBadge = j.error_line ? ` · L${j.error_line}` : "";
      return `<div class="job-row ${j.job_id === stateUI.selectedJobId ? "active" : ""}" data-job="${escapeHtml(j.job_id)}">
        <div class="job-row-top">
          <span>${escapeHtml(j.job_id)}</span>
          <span class="badge ${escapeHtml(j.status)}">${escapeHtml(j.status)}${escapeHtml(errBadge)}</span>
        </div>
        <div class="job-file" title="${escapeHtml(fullPath)}">${escapeHtml(baseName)}</div>
        <div class="job-meta">步数 ${j.step ?? 0} · ${escapeHtml(j.started_at_text || "")}</div>
      </div>`;
    })
    .join("");

  listEl.querySelectorAll(".job-row").forEach((el) => {
    el.addEventListener("click", () => loadJobDetail(el.dataset.job));
  });

  if ((!stateUI.selectedJobId || !rows.some((r) => r.job_id === stateUI.selectedJobId)) && rows[0]) {
    loadJobDetail(rows[0].job_id);
  }
}

async function loadJobDetail(jobId) {
  stateUI.selectedJobId = jobId;
  document.querySelectorAll(".job-row").forEach((el) => {
    el.classList.toggle("active", el.dataset.job === jobId);
  });
  const res = await api.getJobDetail(jobId);
  if (!res.ok) return;
  const { meta, journal, diagnosis } = res;
  $("job-detail-title").textContent = `${meta.job_id} (${meta.status})`;
  $("job-detail-sub").textContent = `${meta.source_file || meta.file_path || ""} · 共 ${meta.step ?? 0} 步`;
  $("btn-cancel-job").disabled = meta.status !== "running";
  $("job-journal-box").textContent = journal || "(空日志)";

  const diagBox = $("job-diagnosis-box");
  if (diagnosis) {
    diagBox.classList.remove("hidden");
    const helpBtn = diagnosis.suggested_help_topic
      ? `<button type="button" class="btn btn-secondary btn-sm" data-jump-help="${escapeHtml(diagnosis.suggested_help_topic)}">查看 stata_help("${escapeHtml(diagnosis.suggested_help_topic)}")</button>`
      : "";
    diagBox.innerHTML = `
      <div class="card-header-row">
        <strong>报错位置：${escapeHtml(diagnosis.file_path || "<selection>")}:${escapeHtml(diagnosis.line ?? "?")}（${escapeHtml(diagnosis.error_code)} ${escapeHtml(diagnosis.error_title || "")}）</strong>
        ${helpBtn}
      </div>
      <div><strong>错误信息：</strong>${escapeHtml(diagnosis.error_message)}</div>
      <div><strong>修复建议：</strong>${escapeHtml(diagnosis.fix_hint)}</div>
      ${diagnosis.source_context ? `<pre>${escapeHtml(diagnosis.source_context)}</pre>` : ""}
    `;
    diagBox.querySelectorAll("[data-jump-help]").forEach((b) => {
      b.addEventListener("click", () => openHelpForTopic(b.dataset.jumpHelp));
    });
  } else {
    diagBox.classList.add("hidden");
    diagBox.innerHTML = "";
  }
}

/* ── 4. Help Reader ─────────────────────────────────────────────────── */
async function lookupHelp() {
  const topic = $("help-topic-input").value.trim();
  if (!topic) return;
  $("help-search-results").classList.add("hidden");
  $("help-search-results").innerHTML = "";
  $("help-output-box").classList.remove("hidden");
  $("help-output-box").textContent = "查询中…";
  const res = await api.lookupHelp({ topic, section: stateUI.helpSection });
  const seeWrap = $("help-also-see");

  if (res.found) {
    $("help-meta-bar").textContent = `解析命令: ${res.resolved_topic} (${res.source}) · 章节: ${(res.sections_available || []).join(", ") || "默认"}`;
    $("help-output-box").textContent = res.content;
    const seeList = res.also_see || [];
    if (seeList.length) {
      seeWrap.classList.remove("hidden");
      seeWrap.innerHTML =
        `<span class="chips-label">相关：</span>` +
        seeList
          .slice(0, 8)
          .map((item) => {
            const cmd = item.replace(/^\[[^\]]+\]\s*/, "").trim().split(/\s+/)[0];
            return `<button type="button" class="chip-btn" data-topic="${escapeHtml(cmd)}">${escapeHtml(item)}</button>`;
          })
          .join("");
      seeWrap.querySelectorAll("[data-topic]").forEach((b) => {
        b.addEventListener("click", () => openHelpForTopic(b.dataset.topic));
      });
    } else {
      seeWrap.classList.add("hidden");
      seeWrap.innerHTML = "";
    }
  } else {
    seeWrap.classList.add("hidden");
    $("help-meta-bar").textContent = `未直接找到 "${topic}"，可点击“模糊搜索”`;
    $("help-output-box").textContent = res.error || "未找到对应 .sthlp 文档。";
  }
}

async function searchHelp() {
  const query = $("help-topic-input").value.trim();
  if (!query) return;
  $("help-also-see").classList.add("hidden");
  const res = await api.searchHelp({ query, limit: 30 });
  const items = res.results || [];
  $("help-meta-bar").textContent = `关键词 "${query}" 命中 ${res.count} 条（点击条目可直接阅读文档）`;
  const resultsEl = $("help-search-results");
  if (!items.length) {
    resultsEl.classList.add("hidden");
    resultsEl.innerHTML = "";
    $("help-output-box").classList.remove("hidden");
    $("help-output-box").textContent = "无匹配项";
    return;
  }
  resultsEl.innerHTML = items
    .map(
      (r) => `<button type="button" class="search-result-row" data-topic="${escapeHtml(r.target || r.topic)}">
        <div class="search-result-top">
          <span>${escapeHtml(r.topic)}${r.target && r.target !== r.topic ? ` → ${escapeHtml(r.target)}` : ""}</span>
          <span class="tool-cat">${escapeHtml(r.source)}</span>
        </div>
        <div class="search-result-title">${escapeHtml(r.title || "(点击查看 .sthlp 详情)")}</div>
      </button>`,
    )
    .join("");
  resultsEl.querySelectorAll("[data-topic]").forEach((b) => {
    b.addEventListener("click", () => openHelpForTopic(b.dataset.topic));
  });
  resultsEl.classList.remove("hidden");
  $("help-output-box").classList.add("hidden");
  $("help-output-box").textContent = items
    .map((r) => `${r.topic.padEnd(24)} -> ${r.target.padEnd(24)} [${r.source}] ${r.title || ""}`)
    .join("\n");
}

/* ── 5. Anti-Loop Lint & Vectorize Guide ────────────────────────────── */
async function runLint() {
  const filePath = $("lint-file-input").value.trim();
  const code = $("lint-code-input").value;
  const strict = $("chk-lint-strict").checked;
  setGlobalBusy(true);
  try {
    const res = await api.lintDo({ filePath, code, strict });
    const issues = res.issues || [];
    const score = res.idiomatic_score ?? 100;

    const banner = $("lint-score-banner");
    banner.classList.remove("hidden");
    const fillColor = score >= 80 ? "var(--ok)" : score >= 50 ? "var(--warn)" : "var(--err)";
    banner.innerHTML = `
      <div>
        <strong>地道度评分：<span class="score-num">0</span> / 100</strong>
        <div class="card-sub">${escapeHtml(res.summary || "")}</div>
      </div>
      <div class="score-meter"><div class="score-fill" style="width:0%;background:${fillColor}"></div></div>
    `;
    requestAnimationFrame(() => {
      const fill = banner.querySelector(".score-fill");
      if (fill) fill.style.width = `${score}%`;
      const numEl = banner.querySelector(".score-num");
      if (numEl) numEl.textContent = String(score);
    });

    const box = $("lint-result-box");
    if (!issues.length) {
      box.innerHTML = `<div class="issue-card info"><strong>未发现反循环或常见语法陷阱！</strong></div>`;
      return;
    }

    box.innerHTML = issues
      .map((iss, idx) => {
        const helpBtn = iss.help_topic
          ? `<button type="button" class="btn btn-ghost btn-sm" data-jump-help="${escapeHtml(iss.help_topic)}">查看 help ${escapeHtml(iss.help_topic)}</button>`
          : "";
        const copyBtn = iss.example_after
          ? `<button type="button" class="btn btn-ghost btn-sm" data-copy-idx="${idx}">复制改写</button>`
          : "";
        return `<div class="issue-card ${escapeHtml(iss.severity)}">
          <div class="card-header-row">
            <strong>L${escapeHtml(iss.line)} [${escapeHtml(iss.severity)}] ${escapeHtml(iss.rule_id)}</strong>
            <div class="header-actions">${helpBtn}${copyBtn}</div>
          </div>
          <div>${escapeHtml(iss.message)}</div>
          <div class="hint-text">建议：${escapeHtml(iss.suggestion)}</div>
          ${iss.example_after ? `<pre>${escapeHtml(iss.example_after)}</pre>` : ""}
        </div>`;
      })
      .join("");

    box.querySelectorAll("[data-jump-help]").forEach((b) => {
      b.addEventListener("click", () => openHelpForTopic(b.dataset.jumpHelp));
    });
    box.querySelectorAll("[data-copy-idx]").forEach((b) => {
      b.addEventListener("click", () => {
        const item = issues[Number(b.dataset.copyIdx)];
        if (item?.example_after) copyWithFeedback(item.example_after, b);
      });
    });
  } finally {
    setGlobalBusy(false);
  }
}

async function loadAllRecipes() {
  const res = await api.vectorizeGuide({ pattern: "all" });
  stateUI.allRecipes = res.recipes || [];
  renderRecipes();
}

function renderRecipes() {
  const q = ($("recipe-filter-input").value || "").trim().toLowerCase();
  const items = stateUI.allRecipes.filter((r) => {
    if (!q) return true;
    return (
      r.title.toLowerCase().includes(q) ||
      r.why_better.toLowerCase().includes(q) ||
      (r.keywords || []).some((k) => k.toLowerCase().includes(q))
    );
  });

  $("recipes-grid").innerHTML = items
    .map(
      (r, idx) => `<div class="recipe-card">
        <div class="card-header-row">
          <strong>${escapeHtml(r.title)}</strong>
          <button type="button" class="btn btn-ghost btn-sm" data-copy-recipe="${idx}">复制地道写法</button>
        </div>
        <div class="card-sub">${escapeHtml(r.why_better)}</div>
        ${r.verified_note ? `<div class="verified-pill">✓ ${escapeHtml(r.verified_note)}</div>` : ""}
        <div class="compare-grid">
          <div class="compare-col">
            <div class="compare-col-label">避免：低效循环写法</div>
            <pre>${escapeHtml(r.bad_loop)}</pre>
          </div>
          <div class="compare-col">
            <div class="compare-col-label">推荐：向量化地道 Stata</div>
            <pre>${escapeHtml(r.idiomatic_stata)}</pre>
          </div>
        </div>
      </div>`,
    )
    .join("") || `<div class="hint-text">无匹配的向量化方案</div>`;

  $("recipes-grid").querySelectorAll("[data-copy-recipe]").forEach((b) => {
    b.addEventListener("click", () => {
      const rec = items[Number(b.dataset.copyRecipe)];
      if (rec) copyWithFeedback(rec.idiomatic_stata, b);
    });
  });
}

/* ── 6. Logs & Doctor ───────────────────────────────────────────────── */
async function refreshLogs() {
  const res = await api.readLogTail(stateUI.logName, 160);
  $("log-tail-box").textContent = res.content || "(空)";
}

/* ── Wire All Events ────────────────────────────────────────────────── */
function wireEvents() {
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });
  document.querySelectorAll("#workflow-stepper [data-goto-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.filterCat) {
        stateUI.toolsCategory = btn.dataset.filterCat;
        renderToolsCatalog();
      }
      if (btn.dataset.jobsFilter) {
        stateUI.jobsFilter = btn.dataset.jobsFilter;
        document.querySelectorAll("#jobs-filter-seg .seg-btn").forEach((x) =>
          x.classList.toggle("active", x.dataset.jobFilter === stateUI.jobsFilter),
        );
      }
      switchTab(btn.dataset.gotoTab);
    });
  });
  api.onLog(({ text }) => appendConsole(text));
  api.onStateChanged?.(() => refreshOverview());

  $("btn-open-home").addEventListener("click", () => api.openStateFolder());
  $("btn-refresh-overview").addEventListener("click", refreshOverview);
  $("btn-start").addEventListener("click", () => runAction("start"));
  $("btn-stop").addEventListener("click", () => runAction("stop"));
  $("btn-install").addEventListener("click", () => runAction("install"));
  $("btn-refresh-bin").addEventListener("click", () => runAction("refresh"));
  $("btn-verify").addEventListener("click", () => runAction("verify"));
  $("btn-clear-console").addEventListener("click", () => {
    $("action-console").textContent = "";
    const statusEl = $("console-status-text");
    statusEl.textContent = "已清空";
    statusEl.className = "drawer-status";
  });
  $("btn-copy-console").addEventListener("click", () =>
    copyWithFeedback($("action-console").textContent, $("btn-copy-console")),
  );

  $("in-stata-select").addEventListener("change", (e) => {
    if (e.target.value) {
      $("in-stata-path").value = e.target.value;
      refreshOverview();
    }
  });
  $("btn-pick-stata").addEventListener("click", async () => {
    const dir = await api.pickStataDir();
    if (dir) {
      $("in-stata-path").value = dir;
      refreshOverview();
    }
  });
  $("in-port").addEventListener("input", () => {
    stateUI.userEditedPort = true;
  });
  $("in-timeout").addEventListener("input", () => {
    stateUI.userEditedTimeout = true;
  });
  $("btn-use-alt-port").addEventListener("click", () => {
    stateUI.userEditedPort = true;
    $("in-port").value = "4011";
    $("ov-advanced-panel").open = true;
    $("port-conflict-banner").classList.add("hidden");
  });

  // Config tab
  document.querySelectorAll("#cfg-transport-seg .seg-btn").forEach((b) => {
    b.addEventListener("click", () => {
      stateUI.transport = b.dataset.transport;
      document.querySelectorAll("#cfg-transport-seg .seg-btn").forEach((x) => x.classList.toggle("active", x === b));
      refreshConfigSnippet();
    });
  });
  $("btn-gen-config").addEventListener("click", refreshConfigSnippet);
  $("btn-copy-config").addEventListener("click", () =>
    copyWithFeedback($("cfg-snippet-box").textContent, $("btn-copy-config")),
  );
  $("btn-pick-config").addEventListener("click", async () => {
    const fp = await api.pickConfigFile();
    if (fp) $("cfg-target-file").value = fp;
  });
  $("btn-write-config-dry").addEventListener("click", async () => {
    const res = await api.writeClientConfig({
      filePath: $("cfg-target-file").value.trim(),
      transport: stateUI.transport,
      root: $("cfg-root").value.trim(),
      key: $("cfg-key").value.trim(),
      timeoutMs: Number($("cfg-timeout-ms").value || 0) || undefined,
      dryRun: true,
    });
    $("cfg-write-result").textContent = res.output || res.message || "";
  });
  $("btn-write-config").addEventListener("click", async () => {
    const res = await api.writeClientConfig({
      filePath: $("cfg-target-file").value.trim(),
      transport: stateUI.transport,
      root: $("cfg-root").value.trim(),
      key: $("cfg-key").value.trim(),
      timeoutMs: Number($("cfg-timeout-ms").value || 0) || undefined,
      dryRun: false,
    });
    $("cfg-write-result").textContent = res.output || res.message || "";
  });
  $("tools-filter-input").addEventListener("input", renderToolsCatalog);

  // Jobs tab
  $("btn-reload-jobs").addEventListener("click", refreshJobs);
  document.querySelectorAll("#jobs-filter-seg .seg-btn").forEach((b) => {
    b.addEventListener("click", () => {
      stateUI.jobsFilter = b.dataset.jobFilter;
      document.querySelectorAll("#jobs-filter-seg .seg-btn").forEach((x) => x.classList.toggle("active", x === b));
      renderJobsList();
    });
  });
  $("btn-copy-journal").addEventListener("click", () =>
    copyWithFeedback($("job-journal-box").textContent, $("btn-copy-journal")),
  );
  $("btn-cancel-job").addEventListener("click", async () => {
    if (!stateUI.selectedJobId) return;
    await api.cancelJob(stateUI.selectedJobId);
    await refreshJobs();
    await loadJobDetail(stateUI.selectedJobId);
  });

  // Help tab
  $("btn-lookup-help").addEventListener("click", lookupHelp);
  $("btn-search-help").addEventListener("click", searchHelp);
  $("help-topic-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") lookupHelp();
  });
  $("help-quick-chips").querySelectorAll("[data-topic]").forEach((b) => {
    b.addEventListener("click", () => openHelpForTopic(b.dataset.topic));
  });
  $("help-section-pills").querySelectorAll("[data-section]").forEach((b) => {
    b.addEventListener("click", () => {
      stateUI.helpSection = b.dataset.section;
      $("help-section-pills").querySelectorAll(".pill-btn").forEach((x) => x.classList.toggle("active", x === b));
      lookupHelp();
    });
  });
  $("btn-copy-help").addEventListener("click", () =>
    copyWithFeedback($("help-output-box").textContent, $("btn-copy-help")),
  );

  // Lint & Recipes tab
  document.querySelectorAll("#lint-mode-seg .seg-btn").forEach((b) => {
    b.addEventListener("click", () => {
      const mode = b.dataset.lintMode;
      document.querySelectorAll("#lint-mode-seg .seg-btn").forEach((x) => x.classList.toggle("active", x === b));
      $("lint-view-check").classList.toggle("hidden", mode !== "check");
      $("lint-view-recipes").classList.toggle("hidden", mode !== "recipes");
      if (mode === "recipes" && !stateUI.allRecipes.length) loadAllRecipes();
    });
  });
  $("btn-pick-do").addEventListener("click", async () => {
    const fp = await api.pickDoFile();
    if (fp) $("lint-file-input").value = fp;
  });
  $("btn-clear-do").addEventListener("click", () => ($("lint-file-input").value = ""));
  $("btn-load-sample").addEventListener("click", () => {
    $("lint-file-input").value = "";
    $("lint-code-input").value = SAMPLE_LINT_CODE;
  });
  $("btn-run-lint").addEventListener("click", runLint);
  $("recipe-filter-input").addEventListener("input", renderRecipes);

  // Doctor & Logs tab
  $("btn-reload-logs").addEventListener("click", () => {
    refreshOverview();
    refreshLogs();
  });
  document.querySelectorAll("#log-tabs-seg .seg-btn").forEach((b) => {
    b.addEventListener("click", () => {
      stateUI.logName = b.dataset.log;
      document.querySelectorAll("#log-tabs-seg .seg-btn").forEach((x) => x.classList.toggle("active", x === b));
      refreshLogs();
    });
  });
  $("btn-copy-log-tail").addEventListener("click", () =>
    copyWithFeedback($("log-tail-box").textContent, $("btn-copy-log-tail")),
  );
  $("btn-copy-doctor-json").addEventListener("click", (e) => {
    e.preventDefault();
    copyWithFeedback($("doctor-json-box").textContent, $("btn-copy-doctor-json"));
  });
  $("btn-uninstall").addEventListener("click", async () => {
    const confirmed = await api.confirmAction({
      type: "warning",
      title: "卸载 Stata MCP 运行时",
      message: "确定要停止并移除 ~/.stata-mcp 中的运行时（venv/server/bin）吗？",
      detail: "此操作不会触及你的 Stata 软件或客户端配置文件。",
      confirmLabel: "确定卸载",
      cancelLabel: "取消",
    });
    if (!confirmed) return;
    switchTab("overview");
    await runAction("uninstall");
  });
}

initPlatformAndTheme();
renderToolsCatalog();
wireEvents();
const savedTab = localStorage.getItem("stata-mcp-active-tab");
if (savedTab && savedTab !== "overview") switchTab(savedTab);
refreshOverview().finally(() => refreshConfigSnippet());
