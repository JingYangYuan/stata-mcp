const api = window.stataMcpDesktop;

const MCP_TOOLS_CATALOG = [
  { name: "stata_run_file", cat: "执行与作业", desc: "运行 .do 文件（短任务直出，长任务异步 job_id，支持 lint=True，出错自带行号定位）" },
  { name: "stata_run_selection", cat: "执行与作业", desc: "运行选中代码片段，短任务直出结果、长任务返回 job_id" },
  { name: "stata_run_lines", cat: "执行与作业", desc: "运行指定行区间 [start_line, end_line]，报错自动映射回原文件行号" },
  { name: "stata_job_status", cat: "执行与作业", desc: "查询作业状态、步骤数、耗时与失败时的 error_code / error_line" },
  { name: "stata_job_tail", cat: "执行与作业", desc: "按 offset 增量读取步骤预览 (steps) 或完整日志 (full)" },
  { name: "stata_job_list", cat: "执行与作业", desc: "列出最近 Stata 作业及摘要" },
  { name: "stata_job_cancel", cat: "执行与作业", desc: "向引擎发送 /stop_execution 终止运行中的作业" },
  { name: "stata_session", cat: "会话管理", desc: "列出或销毁并行 Stata 会话 (session_id)" },
  { name: "stata_help", cat: "Help 参考", desc: "自动解析别名与 SMCL 格式的官方/SSC help 文档（支持按章节提取）" },
  { name: "stata_search_help", cat: "Help 参考", desc: "按关键词检索已安装的 base / SSC help 文件与命令别名" },
  { name: "stata_which", cat: "Help 参考", desc: "检查命令是否已安装（built-in 或 .ado 路径与版本头）" },
  { name: "stata_diagnose_error", cat: "报错定位", desc: "定位失败作业（或任意日志）在 .do 中的精确行号、上下文与修复建议" },
  { name: "stata_lint_do", cat: "破除循环", desc: "拦截 1/_N、levelsof+foreach、preserve 循环、缺失值 > 陷阱与语法错误" },
  { name: "stata_vectorize_guide", cat: "破除循环", desc: "提供 11 类典型 Stata 向量化替代方案（bysort / egen / frames / mata）" },
  { name: "stata_inspect_data", cat: "数据探查", desc: "直连 /view_data 查看内存数据集行数、列名、类型与样本预览" },
  { name: "stata_describe", cat: "数据探查", desc: "只读运行 describe + summarize + misstable summarize 核验变量" },
  { name: "stata_stored_results", cat: "数据探查", desc: "查看内存中的 e()、r()、c() 与已保存估计结果 estimates dir" },
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

function flashCopied(btn, originalLabel) {
  if (!btn) return;
  const prev = originalLabel || btn.textContent;
  btn.textContent = "已复制 ✓";
  setTimeout(() => {
    btn.textContent = prev;
  }, 1400);
}

async function copyWithFeedback(text, btn) {
  await api.copyText(text);
  flashCopied(btn);
}

/* ── Theme & Platform ───────────────────────────────────────────────── */
function applyTheme(choice) {
  const valid = ["light", "system", "dark"].includes(choice) ? choice : "system";
  document.documentElement.setAttribute("data-theme", valid);
  localStorage.setItem("stata-mcp-theme", valid);
  document.querySelectorAll(".theme-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.themeChoice === valid);
  });
}

function initPlatformAndTheme() {
  if (api.platform) {
    document.body.setAttribute("data-platform", api.platform);
  }
  const saved = localStorage.getItem("stata-mcp-theme") || "system";
  applyTheme(saved);
  document.querySelectorAll(".theme-btn").forEach((btn) => {
    btn.addEventListener("click", () => applyTheme(btn.dataset.themeChoice));
  });
}

/* ── Tab Navigation ─────────────────────────────────────────────────── */
function switchTab(tabId) {
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tabId);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `tab-${tabId}`);
  });
  if (tabId === "config") refreshConfigSnippet();
  if (tabId === "jobs") refreshJobs();
  if (tabId === "doctor") refreshLogs();
  if (tabId === "lint" && !stateUI.allRecipes.length) loadAllRecipes();
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
async function refreshOverview() {
  const res = await api.getOverview({ stataPath: $("in-stata-path").value.trim() || undefined });
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
    $("step1-badge").textContent = `已就绪 · ${(cands[0].edition || "").toUpperCase()} ${cands[0].version || ""}`;
    $("step1-badge").className = "pill-badge ok";
  }

  $("chip-python").textContent = `Python ${doc.python?.version || "?"}`;
  $("chip-uv").textContent = `uv: ${doc.tools?.uv ? "已装" : "未装 (可选)"}`;
  $("chip-git").textContent = `git: ${doc.tools?.git ? "已装" : "未装"}`;
  $("chip-home").textContent = res.stateHome || doc.home || "~/.stata-mcp";

  // Step 2
  if (installed) {
    $("step2-badge").textContent = `已安装 v${st.installer_version || "1.2.2"}`;
    $("step2-badge").className = "pill-badge ok";
  } else {
    $("step2-badge").textContent = "尚未安装";
    $("step2-badge").className = "pill-badge";
  }
  if (st.port) $("in-port").value = st.port;
  if (st.default_timeout) $("in-timeout").value = st.default_timeout;

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
}

async function runAction(action, extraOpts = {}) {
  $("console-status-text").textContent = `正在执行 ${action}…`;
  $("console-drawer").open = true;
  const opts = {
    stataPath: $("in-stata-path").value.trim(),
    port: Number($("in-port").value || 4001),
    defaultTimeout: Number($("in-timeout").value || 21600),
    mirror: $("in-mirror").value.trim(),
    refresh: $("chk-reclone").checked,
    ...extraOpts,
  };
  const res = await api.runAction(action, opts);
  appendConsole(`\n[退出码 ${res.code}]\n`);
  $("console-status-text").textContent = res.code === 0 ? `${action} 完成` : `${action} 失败 (${res.code})`;
  await refreshOverview();
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
  $("help-meta-bar").textContent = `关键词 "${query}" 命中 ${res.count} 条`;
  $("help-output-box").textContent =
    (res.results || [])
      .map((r) => `${r.topic.padEnd(24)} -> ${r.target.padEnd(24)} [${r.source}] ${r.title || ""}`)
      .join("\n") || "无匹配项";
}

/* ── 5. Anti-Loop Lint & Vectorize Guide ────────────────────────────── */
async function runLint() {
  const filePath = $("lint-file-input").value.trim();
  const code = $("lint-code-input").value;
  const strict = $("chk-lint-strict").checked;
  const res = await api.lintDo({ filePath, code, strict });
  const issues = res.issues || [];
  const score = res.idiomatic_score ?? 100;

  const banner = $("lint-score-banner");
  banner.classList.remove("hidden");
  const fillColor = score >= 80 ? "var(--ok)" : score >= 50 ? "var(--warn)" : "var(--err)";
  banner.innerHTML = `
    <div>
      <strong>地道度评分：${score} / 100</strong>
      <div class="card-sub">${escapeHtml(res.summary || "")}</div>
    </div>
    <div class="score-meter"><div class="score-fill" style="width:${score}%;background:${fillColor}"></div></div>
  `;

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
  api.onLog(({ text }) => appendConsole(text));

  $("btn-open-home").addEventListener("click", () => api.openStateFolder());
  $("btn-refresh-overview").addEventListener("click", refreshOverview);
  $("btn-start").addEventListener("click", () => runAction("start"));
  $("btn-stop").addEventListener("click", () => runAction("stop"));
  $("btn-install").addEventListener("click", () => runAction("install"));
  $("btn-refresh-bin").addEventListener("click", () => runAction("refresh"));
  $("btn-verify").addEventListener("click", () => runAction("verify"));
  $("btn-clear-console").addEventListener("click", () => {
    $("action-console").textContent = "";
    $("console-status-text").textContent = "已清空";
  });
  $("btn-copy-console").addEventListener("click", () =>
    copyWithFeedback($("action-console").textContent, $("btn-copy-console")),
  );

  $("in-stata-select").addEventListener("change", (e) => {
    if (e.target.value) $("in-stata-path").value = e.target.value;
  });
  $("btn-pick-stata").addEventListener("click", async () => {
    const dir = await api.pickStataDir();
    if (dir) $("in-stata-path").value = dir;
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
refreshOverview();
refreshConfigSnippet();
