const api = window.stataMcpDesktop;

const MCP_TOOLS_CATALOG = [
  { name: "stata_run_file", cat: "执行与作业", desc: "运行 .do 文件（支持异步 job_id、lint=True 预检、失败自动报错行号）" },
  { name: "stata_run_selection", cat: "执行与作业", desc: "运行选中 Stata 代码片段，短任务直出结果、长任务异步返回 job_id" },
  { name: "stata_run_lines", cat: "执行与作业", desc: "运行指定行区间 [start_line, end_line]，报错自动映射回原文件行号" },
  { name: "stata_job_status", cat: "执行与作业", desc: "查询作业状态、步骤数、耗时及失败时的 error_code / error_line" },
  { name: "stata_job_tail", cat: "执行与作业", desc: "按 offset 增量读取步骤预览 (steps) 或完整日志 (full)" },
  { name: "stata_job_list", cat: "执行与作业", desc: "列出最近 Stata 作业及摘要" },
  { name: "stata_job_cancel", cat: "执行与作业", desc: "向引擎发送 /stop_execution 终止运行中的作业" },
  { name: "stata_session", cat: "会话管理", desc: "列出或销毁并行 Stata 会话 (session_id)" },
  { name: "stata_help", cat: "Help 参考", desc: "自动解析别名与 SMCL 格式的官方/SSC help 文档（支持按章节提取）" },
  { name: "stata_search_help", cat: "Help 参考", desc: "按关键词检索已安装的 base / SSC help 文件与命令别名" },
  { name: "stata_which", cat: "Help 参考", desc: "检查命令是否已安装（built-in 或 .ado 路径与版本头）" },
  { name: "stata_diagnose_error", cat: "报错定位", desc: "定位失败作业（或任意日志）在 .do 中的精确行号、上下文与修复建议" },
  { name: "stata_lint_do", cat: "破除循环", desc: "拦截 1/_N、levelsof+foreach、preserve 循环、缺失值 > 陷阱与语法错误" },
  { name: "stata_vectorize_guide", cat: "破除循环", desc: "提供 11 类典型 Stata 向量化替代方案（bysort/egen/frames/mata）" },
  { name: "stata_inspect_data", cat: "数据探查", desc: "直连 /view_data 查看内存数据集行数、列名、类型与样本预览" },
  { name: "stata_describe", cat: "数据探查", desc: "只读运行 describe + summarize + misstable summarize 核验变量" },
  { name: "stata_stored_results", cat: "数据探查", desc: "查看内存中的 e()、r()、c() 与已保存估计结果 estimates dir" },
];

function $(id) {
  return document.getElementById(id);
}

function escapeHtml(str) {
  return String(str ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function appendConsole(text) {
  const box = $("action-console");
  if (box.textContent === "等待操作…") box.textContent = "";
  box.textContent += text;
  box.scrollTop = box.scrollHeight;
}

function switchTab(tabId) {
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tabId);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `tab-${tabId}`);
  });
  if (tabId === "jobs") refreshJobs();
  if (tabId === "doctor") refreshLogs();
  if (tabId === "config") refreshConfigSnippet();
}

async function refreshOverview() {
  const res = await api.getOverview({ stataPath: $("in-stata-path").value.trim() || undefined });
  const doc = res.doctor || {};
  const rt = doc.runtime || {};
  const eng = rt.engine || {};
  const state = doc.state || {};

  const gwOk = Boolean(rt.healthy);
  const engOk = Boolean(eng.healthy);

  $("sidebar-gateway-pill").textContent = `Gateway :${rt.port || 4001} ${gwOk ? "● 在线" : "○ 未运行"}`;
  $("sidebar-gateway-pill").className = `status-pill ${gwOk ? "ok" : "bad"}`;

  $("sidebar-engine-pill").textContent = `Engine :${eng.port || 4002} ${engOk ? "● 在线" : "○ 未运行"}`;
  $("sidebar-engine-pill").className = `status-pill ${engOk ? "ok" : "bad"}`;

  $("ov-gateway-status").textContent = gwOk ? "运行中" : "未运行";
  $("ov-gateway-meta").textContent = `${rt.host || "127.0.0.1"}:${rt.port || 4001}` + (rt.pid ? ` (PID ${rt.pid})` : "");

  $("ov-engine-status").textContent = engOk ? "运行中" : "未运行";
  $("ov-engine-meta").textContent = `:${eng.port || 4002}` + (eng.pid ? ` (PID ${eng.pid})` : "");

  const cands = doc.stata_candidates || [];
  const select = $("in-stata-select");
  select.innerHTML = "";
  if (cands.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "未自动发现 Stata，请手动选择";
    select.appendChild(opt);
  } else {
    for (const c of cands) {
      const opt = document.createElement("option");
      opt.value = c.path;
      opt.textContent = `${c.path} (${(c.edition || "").toUpperCase()} ${c.version || ""})`;
      select.appendChild(opt);
    }
    if (!$("in-stata-path").value.trim()) {
      $("in-stata-path").value = state.stata_path || cands[0].path;
    }
  }

  $("ov-stata-summary").textContent = cands[0] ? `${(cands[0].edition || "").toUpperCase()} ${cands[0].version || ""}` : "未找到";
  $("ov-python-meta").textContent = `Python ${doc.python?.version || "?"} · uv: ${doc.tools?.uv ? "已装" : "无"} · git: ${doc.tools?.git ? "已装" : "无"}`;
  $("ov-home-path").textContent = res.stateHome || doc.home || "~/.stata-mcp";
  $("ov-jobs-meta").textContent = `已记录 ${doc.jobs_count ?? 0} 个作业`;

  if (state.port) $("in-port").value = state.port;
  if (state.default_timeout) $("in-timeout").value = state.default_timeout;

  $("doctor-json-box").textContent = JSON.stringify(doc, null, 2);
}

async function runAction(action) {
  const opts = {
    stataPath: $("in-stata-path").value.trim(),
    port: Number($("in-port").value || 4001),
    defaultTimeout: Number($("in-timeout").value || 21600),
    mirror: $("in-mirror").value.trim(),
    refresh: $("chk-reclone").checked,
  };
  const res = await api.runAction(action, opts);
  appendConsole(`\n[退出码 ${res.code}]\n`);
  await refreshOverview();
}

async function refreshConfigSnippet() {
  const res = await api.getConfigSnippet({
    transport: $("cfg-transport").value,
    root: $("cfg-root").value.trim() || "mcpServers",
    key: $("cfg-key").value.trim() || "stata-mcp",
    timeoutMs: Number($("cfg-timeout-ms").value || 0) || undefined,
  });
  $("cfg-snippet-box").textContent = res.snippet || res.stderr || "{}";
}

function renderToolsCatalog() {
  $("tools-catalog").innerHTML = MCP_TOOLS_CATALOG.map(
    (t) => `<div class="tool-item">
      <div class="tool-name">${escapeHtml(t.name)}</div>
      <div class="tool-cat">${escapeHtml(t.cat)}</div>
      <div class="tool-desc">${escapeHtml(t.desc)}</div>
    </div>`,
  ).join("");
}

let selectedJobId = "";

async function refreshJobs() {
  const jobs = await api.listJobs(30);
  const listEl = $("jobs-list");
  if (!jobs.length) {
    listEl.innerHTML = "<div class='hint-text'>暂无作业记录</div>";
    return;
  }
  listEl.innerHTML = jobs
    .map((j) => {
      const errBadge = j.error_line ? ` · L${j.error_line}` : "";
      return `<div class="job-row ${j.job_id === selectedJobId ? "active" : ""}" data-job="${escapeHtml(j.job_id)}">
        <div class="job-row-top">
          <span>${escapeHtml(j.job_id)}</span>
          <span class="badge ${escapeHtml(j.status)}">${escapeHtml(j.status)}${escapeHtml(errBadge)}</span>
        </div>
        <div class="card-meta">${escapeHtml(j.source_file || j.file_path || "")}</div>
        <div class="card-meta">步数 ${j.step ?? 0} · ${escapeHtml(j.started_at_text || "")}</div>
      </div>`;
    })
    .join("");

  listEl.querySelectorAll(".job-row").forEach((el) => {
    el.addEventListener("click", () => loadJobDetail(el.dataset.job));
  });

  if (!selectedJobId && jobs[0]) {
    loadJobDetail(jobs[0].job_id);
  }
}

async function loadJobDetail(jobId) {
  selectedJobId = jobId;
  const res = await api.getJobDetail(jobId);
  if (!res.ok) return;
  const { meta, journal, diagnosis } = res;
  $("job-detail-title").textContent = `${meta.job_id} (${meta.status})`;
  $("btn-cancel-job").disabled = meta.status !== "running";
  $("job-journal-box").textContent = journal || "(空日志)";

  const diagBox = $("job-diagnosis-box");
  if (diagnosis) {
    diagBox.classList.remove("hidden");
    diagBox.innerHTML = `
      <div><strong>报错定位：</strong>${escapeHtml(diagnosis.file_path || "")}:${escapeHtml(diagnosis.line ?? "?")}
        (${escapeHtml(diagnosis.error_code)} ${escapeHtml(diagnosis.error_title || "")})</div>
      <div><strong>错误信息：</strong>${escapeHtml(diagnosis.error_message)}</div>
      <div><strong>修复建议：</strong>${escapeHtml(diagnosis.fix_hint)}</div>
      ${diagnosis.source_context ? `<pre>${escapeHtml(diagnosis.source_context)}</pre>` : ""}
    `;
  } else {
    diagBox.classList.add("hidden");
    diagBox.innerHTML = "";
  }
}

async function lookupHelp() {
  const topic = $("help-topic-input").value.trim();
  const section = $("help-section-select").value;
  if (!topic) return;
  $("help-output-box").textContent = "查询中…";
  const res = await api.lookupHelp({ topic, section });
  if (res.found) {
    $("help-meta-bar").textContent = `解析命令: ${res.resolved_topic} (${res.source}) · 可选章节: ${(res.sections_available || []).join(", ")}`;
    $("help-output-box").textContent = res.content;
  } else {
    $("help-meta-bar").textContent = `未找到 ${topic}`;
    $("help-output-box").textContent = res.error || "可点击“搜索”尝试关键词检索。";
  }
}

async function searchHelp() {
  const query = $("help-topic-input").value.trim();
  if (!query) return;
  const res = await api.searchHelp({ query, limit: 25 });
  $("help-meta-bar").textContent = `关键词 "${query}" 命中 ${res.count} 条`;
  $("help-output-box").textContent = (res.results || [])
    .map((r) => `${r.topic.padEnd(24)} -> ${r.target.padEnd(24)} [${r.source}] ${r.title || ""}`)
    .join("\n") || "无匹配项";
}

async function runLint() {
  const filePath = $("lint-file-input").value.trim();
  const code = $("lint-code-input").value;
  const res = await api.lintDo({ filePath, code, strict: false });
  const issues = res.issues || [];
  const box = $("lint-result-box");
  if (!issues.length) {
    box.innerHTML = `<div class="issue-card"><strong>${escapeHtml(res.summary)}</strong></div>`;
    return;
  }
  box.innerHTML =
    `<div class="hint-text"><strong>${escapeHtml(res.summary)}</strong></div>` +
    issues
      .map(
        (iss) => `<div class="issue-card ${escapeHtml(iss.severity)}">
          <div><strong>L${escapeHtml(iss.line)} [${escapeHtml(iss.severity)}] ${escapeHtml(iss.rule_id)}</strong></div>
          <div>${escapeHtml(iss.message)}</div>
          <div class="hint-text">建议：${escapeHtml(iss.suggestion)}</div>
          ${iss.example_after ? `<pre>${escapeHtml(iss.example_after)}</pre>` : ""}
        </div>`,
      )
      .join("");
}

async function showRecipes() {
  const res = await api.vectorizeGuide({ pattern: "all" });
  const box = $("lint-result-box");
  box.innerHTML = (res.recipes || [])
    .map(
      (r) => `<div class="issue-card">
        <div><strong>${escapeHtml(r.title)}</strong></div>
        <div class="hint-text">${escapeHtml(r.why_better)}</div>
        <pre>${escapeHtml(r.idiomatic_stata)}</pre>
      </div>`,
    )
    .join("");
}

async function refreshLogs() {
  const name = $("log-file-select").value;
  const res = await api.readLogTail(name, 150);
  $("log-tail-box").textContent = res.content || "(空)";
}

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
  $("btn-clear-console").addEventListener("click", () => ($("action-console").textContent = ""));

  $("in-stata-select").addEventListener("change", (e) => {
    if (e.target.value) $("in-stata-path").value = e.target.value;
  });
  $("btn-pick-stata").addEventListener("click", async () => {
    const dir = await api.pickStataDir();
    if (dir) $("in-stata-path").value = dir;
  });

  $("btn-gen-config").addEventListener("click", refreshConfigSnippet);
  $("cfg-transport").addEventListener("change", refreshConfigSnippet);
  $("btn-copy-config").addEventListener("click", async () => {
    await api.copyText($("cfg-snippet-box").textContent);
    $("cfg-write-result").textContent = "已复制到剪贴板。";
  });
  $("btn-pick-config").addEventListener("click", async () => {
    const fp = await api.pickConfigFile();
    if (fp) $("cfg-target-file").value = fp;
  });
  $("btn-write-config-dry").addEventListener("click", async () => {
    const res = await api.writeClientConfig({
      filePath: $("cfg-target-file").value.trim(),
      transport: $("cfg-transport").value,
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
      transport: $("cfg-transport").value,
      root: $("cfg-root").value.trim(),
      key: $("cfg-key").value.trim(),
      timeoutMs: Number($("cfg-timeout-ms").value || 0) || undefined,
      dryRun: false,
    });
    $("cfg-write-result").textContent = res.output || res.message || "";
  });

  $("btn-reload-jobs").addEventListener("click", refreshJobs);
  $("btn-cancel-job").addEventListener("click", async () => {
    if (!selectedJobId) return;
    await api.cancelJob(selectedJobId);
    await refreshJobs();
    await loadJobDetail(selectedJobId);
  });

  $("btn-lookup-help").addEventListener("click", lookupHelp);
  $("btn-search-help").addEventListener("click", searchHelp);
  $("btn-pick-do").addEventListener("click", async () => {
    const fp = await api.pickDoFile();
    if (fp) $("lint-file-input").value = fp;
  });
  $("btn-run-lint").addEventListener("click", runLint);
  $("btn-show-recipes").addEventListener("click", showRecipes);

  $("btn-reload-logs").addEventListener("click", refreshLogs);
  $("log-file-select").addEventListener("change", refreshLogs);
  $("btn-uninstall").addEventListener("click", () => runAction("uninstall"));
}

renderToolsCatalog();
wireEvents();
refreshOverview();
refreshConfigSnippet();
