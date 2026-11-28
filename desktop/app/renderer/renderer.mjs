const api = window.stataMcpDesktop;

const PLAYFUL_TAGLINES = [
  "终端里的赛博包工头，喝口咖啡的工夫活全干完了",
  "放权一时爽，Token 火葬场，但出活是真香",
  "沉默卷王已就位，给个方向就闷头敲到天亮",
  "百万上下文大记忆恢复术，整个目录塞脑子里也不忘",
  "反重力起飞，写代码不靠手速全靠念力",
  "Tab 键按到冒烟，顺便祈祷今天额度别限流",
  "老牌正宫宇宙战舰，插件一装稳坐钓鱼台",
  "别人限流我补位，性价比冲浪战神",
  "字节卷王出击，白嫖真香还要什么自行车",
  "不聊 AI 聊情怀，黑底彩字一开气场直接拉满",
  "哪怕只敲一句 ls，终端也得是全楼最帅的那个",
  "祖传 .do 别怕，今天把你的 1/_N 循环全扬了",
  "祝今天跑回归一把过，核心变量稳稳三颗星 ***",
  "别慌，报错不过是 Stata 在跟你撒娇",
  "90% 时间审阅 AI 代码，10% 时间端咖啡假装很忙",
  "工具天天换，IDE 天天卷，唯一不变的是今晚的截稿日",
];

function pickRandomTagline(exclude = "") {
  const pool = PLAYFUL_TAGLINES.filter((line) => line !== exclude);
  const source = pool.length ? pool : PLAYFUL_TAGLINES;
  return source[Math.floor(Math.random() * source.length)];
}

const stateUI = {
  activeTab: "config",
  transport: "stdio",
  jobsFilter: "all",
  jobsCache: [],
  expandedJobs: new Set(),
  jobDetailsCache: new Map(),
  logName: "server.log",
  actionRunning: false,
  userEditedPort: false,
  userEditedTimeout: false,
  configSnippetText: "{}",
  agents: [],
  customCards: [],
  editingCardId: null,
  terminals: [],
  selectedTerminalId: "",
  currentProjectInfo: null,
  pinnedProjects: [],
  recentProjects: [],
  pollTimer: null,
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

async function copyWithFeedback(text, btn) {
  await api.copyText(text);
  showSnackbar("已复制到剪贴板");
  if (!btn) return;
  const prev = btn.textContent;
  btn.textContent = "已复制 ✓";
  setTimeout(() => {
    btn.textContent = prev;
  }, 1400);
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

function initThemeAndPlatform() {
  if (api?.platform) {
    document.documentElement.classList.add(`platform-${api.platform}`);
  }
  applyTheme(localStorage.getItem("stata-mcp-theme") || "system");
  document.querySelectorAll(".theme-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      applyTheme(btn.dataset.themeChoice);
    });
  });
}

/* ── Tabs & Drawers / Modals ────────────────────────────────────────── */
function openAdvancedDrawer() {
  $("drawer-backdrop").classList.remove("hidden");
  $("advanced-drawer").classList.remove("hidden");
  refreshConfigSnippet();
}

function closeAdvancedDrawer() {
  $("drawer-backdrop").classList.add("hidden");
  $("advanced-drawer").classList.add("hidden");
}

function openLogModal() {
  $("log-modal-backdrop").classList.remove("hidden");
  $("log-modal").classList.remove("hidden");
  refreshLogs();
}

function closeLogModal() {
  $("log-modal-backdrop").classList.add("hidden");
  $("log-modal").classList.add("hidden");
}

function switchTab(tabId) {
  const validTabs = ["config", "terminal", "monitor"];
  const target = validTabs.includes(tabId) ? tabId : "config";
  stateUI.activeTab = target;
  localStorage.setItem("stata-mcp-active-tab", target);
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === target);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `tab-${target}`);
  });
  if (target === "terminal") refreshTerminalPage();
  if (target === "monitor") refreshJobs();
}

/* ── Console Output ─────────────────────────────────────────────────── */
function appendConsole(text) {
  $("console-card").classList.remove("hidden");
  const box = $("action-console");
  if (box.textContent === "等待操作…") box.textContent = "";
  box.textContent += text;
  box.scrollTop = box.scrollHeight;
}

/* ── Page 1: Config & Overview ──────────────────────────────────────── */
function _syncStataSelectToInput(cands) {
  const select = $("in-stata-select");
  const cur = $("in-stata-path").value.trim().toLowerCase();
  if (!cur) return cands[0] || null;
  const matched = cands.find((c) => (c.path || "").toLowerCase() === cur) || null;
  if (matched) select.value = matched.path;
  return matched;
}

async function refreshOverview() {
  const btn = $("btn-refresh-overview");
  if (btn) btn.disabled = true;
  setGlobalBusy(true);
  try {
    const res = await api.getOverview({ stataPath: $("in-stata-path").value.trim() || undefined });
    if (res.appVersion) {
      $("brand-sub-version").textContent = `v${res.appVersion} · 单端口`;
    }
    const doc = res.doctor || {};
    const rt = doc.runtime || {};
    const eng = rt.engine || {};
    const st = doc.state || {};
    const cands = doc.stata_candidates || [];
    const gwOk = Boolean(rt.healthy);
    const engOk = Boolean(eng.healthy);
    const installed = Boolean(doc.installed);
    const port = rt.port || st.port || 4001;

    // Sidebar status indicator
    $("sidebar-service-pill").className = `status-dot-row ${gwOk && engOk ? "ok" : "bad"}`;
    $("sidebar-service-pill").querySelector(".dot-label").textContent = `Stata MCP :${port}`;

    // Header badge + service row meta
    const badge = $("cfg-status-badge");
    if (gwOk && engOk) {
      badge.textContent = "运行中";
      badge.className = "pill-badge ok";
      $("cfg-service-meta").textContent = `127.0.0.1:${port} · 在线 (v${st.installer_version || res.appVersion || "1.4.0"})`;
    } else if (installed) {
      badge.textContent = "已安装 · 未启动";
      badge.className = "pill-badge warn";
      $("cfg-service-meta").textContent = `127.0.0.1:${port} · 未启动 (v${st.installer_version || "1.4.0"})`;
    } else {
      badge.textContent = "待安装";
      badge.className = "pill-badge";
      $("cfg-service-meta").textContent = `127.0.0.1:${port} · 尚未部署运行时`;
    }

    // Stata candidates selector
    const select = $("in-stata-select");
    select.innerHTML = "";
    if (!cands.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "未自动发现 Stata，请在下方手动选择路径";
      select.appendChild(opt);
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
      _syncStataSelectToInput(cands);
    }

    $("cfg-env-meta").textContent =
      `Python ${doc.python?.version || "?"} · uv ${doc.tools?.uv ? "可用" : "可选"} · 内置引擎 v0.5.3`;

    $("btn-install").textContent = installed ? "重装 / 升级" : "一键安装";
    if (st.port && !stateUI.userEditedPort) $("in-port").value = st.port;
    if (st.default_timeout && !stateUI.userEditedTimeout) $("in-timeout").value = st.default_timeout;

    // External port conflict check
    const portConflict = !installed && gwOk;
    $("port-conflict-banner").classList.toggle("hidden", !portConflict);
    if (portConflict) {
      $("port-conflict-text").textContent = `${rt.host || "127.0.0.1"}:${port} 已被其他服务占用`;
    }

    $("doctor-json-box").textContent = JSON.stringify(doc, null, 2);
  } catch (err) {
    $("cfg-status-badge").textContent = "检测失败";
    $("cfg-status-badge").className = "pill-badge bad";
    appendConsole(`\n[错误] 刷新状态失败: ${err?.message || err}\n`);
  } finally {
    setGlobalBusy(false);
    if (btn) btn.disabled = false;
  }
}

const ACTION_BTN_IDS = ["btn-install", "btn-start", "btn-stop", "btn-verify", "btn-refresh-bin", "btn-uninstall"];

function setActionBusy(busy) {
  stateUI.actionRunning = busy;
  setGlobalBusy(busy);
  for (const id of ACTION_BTN_IDS) {
    const el = $(id);
    if (el) el.disabled = busy;
  }
}

async function runAction(action, extraOpts = {}) {
  if (stateUI.actionRunning) {
    showSnackbar("上一项操作仍在执行中，请稍候");
    return;
  }
  setActionBusy(true);
  $("console-card").classList.remove("hidden");
  const statusEl = $("console-status-text");
  statusEl.textContent = `正在执行 ${action}…`;
  statusEl.className = "drawer-status running";
  try {
    const opts = {
      stataPath: $("in-stata-path").value.trim(),
      port: Number($("in-port").value || 4001),
      defaultTimeout: Number($("in-timeout").value || 21600),
      pipIndex: $("in-pip-index").value.trim(),
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
      showSnackbar(`${action} 完成`);
      if (action === "install" || action === "refresh") {
        stateUI.userEditedPort = false;
        stateUI.userEditedTimeout = false;
      }
    } else {
      statusEl.textContent = `${action} 失败 (${res.code})`;
      statusEl.className = "drawer-status bad";
    }
    await refreshOverview();
    await refreshConfigSnippet();
  } catch (err) {
    appendConsole(`\n[异常] ${err?.message || err}\n`);
    statusEl.textContent = `${action} 异常`;
    statusEl.className = "drawer-status bad";
  } finally {
    setActionBusy(false);
  }
}

async function refreshConfigSnippet() {
  const res = await api.getConfigSnippet({
    transport: stateUI.transport,
    root: $("cfg-root").value.trim() || "mcpServers",
    key: $("cfg-key").value.trim() || "stata-mcp",
    timeoutMs: Number($("cfg-timeout-ms").value || 0) || undefined,
  });
  stateUI.configSnippetText = res.snippet || res.stderr || "{}";
  $("cfg-snippet-box").textContent = stateUI.configSnippetText;
  return stateUI.configSnippetText;
}

/* ── Page 2: Research Terminal ──────────────────────────────────────── */
function loadSavedProjects() {
  try {
    stateUI.pinnedProjects = JSON.parse(localStorage.getItem("stata-mcp-pinned-projects") || "[]");
  } catch {
    stateUI.pinnedProjects = [];
  }
  try {
    stateUI.recentProjects = JSON.parse(localStorage.getItem("stata-mcp-recent-projects") || "[]");
  } catch {
    stateUI.recentProjects = [];
  }
}

function saveProjectsState() {
  localStorage.setItem("stata-mcp-pinned-projects", JSON.stringify(stateUI.pinnedProjects));
  localStorage.setItem("stata-mcp-recent-projects", JSON.stringify(stateUI.recentProjects));
}

function recordRecentProject(dirPath) {
  const clean = String(dirPath || "").trim();
  if (!clean) return;
  localStorage.setItem("stata-mcp-last-project", clean);
  stateUI.recentProjects = [clean, ...stateUI.recentProjects.filter((p) => p !== clean)].slice(0, 6);
  saveProjectsState();
}

function renderProjectPills() {
  const active = $("term-folder-input").value.trim();
  const pinnedSet = new Set(stateUI.pinnedProjects);
  const all = [...stateUI.pinnedProjects];
  for (const r of stateUI.recentProjects) {
    if (!pinnedSet.has(r)) all.push(r);
  }
  $("btn-toggle-pin-project").textContent = pinnedSet.has(active) ? "取消收藏" : "收藏";

  const box = $("term-projects-list");
  if (!all.length) {
    box.innerHTML = `<span class="setup-meta">尚无常用科研目录，点击右侧「选择文件夹…」添加</span>`;
    return;
  }
  box.innerHTML = all
    .map((dir) => {
      const name = dir.split(/[/\\]/).filter(Boolean).pop() || dir;
      const isPinned = pinnedSet.has(dir);
      const isActive = dir === active;
      return `<button type="button" class="project-pill ${isActive ? "active" : ""}" data-project-dir="${escapeHtml(dir)}" title="${escapeHtml(dir)}">
        <span>${isPinned ? "★ " : ""}${escapeHtml(name)}</span>
      </button>`;
    })
    .join("");

  box.querySelectorAll(".project-pill").forEach((btn) => {
    btn.addEventListener("click", () => selectResearchFolder(btn.dataset.projectDir));
  });
}

async function selectResearchFolder(folderPath) {
  const clean = String(folderPath || "").trim();
  $("term-folder-input").value = clean;
  if (!clean) {
    stateUI.currentProjectInfo = null;
    $("term-folder-meta").innerHTML = "";
    renderProjectPills();
    return;
  }
  const info = await api.inspectProject(clean);
  stateUI.currentProjectInfo = info;
  if (!info.ok) {
    $("term-folder-meta").innerHTML = `<span class="info-chip">${escapeHtml(info.error || "无效目录")}</span>`;
    renderProjectPills();
    return;
  }
  recordRecentProject(info.path);
  $("term-folder-input").value = info.path;
  $("term-folder-meta").innerHTML = [
    `<span class="info-chip">${info.doCount} 个 .do</span>`,
    `<span class="info-chip">${info.dtaCount} 个 .dta</span>`,
  ].join("");
  renderProjectPills();
}

function getSelectedTerminal() {
  const installed = stateUI.terminals.filter((t) => t.installed);
  return installed.find((t) => t.id === stateUI.selectedTerminalId) || installed[0] || null;
}

function renderTerminalSelector() {
  const select = $("term-app-select");
  if (!select) return;
  const installed = stateUI.terminals.filter((t) => t.installed);
  select.innerHTML = "";
  if (!installed.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "未检测到终端";
    select.appendChild(opt);
    select.disabled = true;
    stateUI.selectedTerminalId = "";
    return;
  }
  select.disabled = false;
  for (const t of installed) {
    const opt = document.createElement("option");
    opt.value = t.id;
    opt.textContent = t.name;
    select.appendChild(opt);
  }
  const saved = localStorage.getItem("stata-mcp-terminal-app") || "";
  const chosen = installed.find((t) => t.id === saved) || installed[0];
  stateUI.selectedTerminalId = chosen.id;
  select.value = chosen.id;
  localStorage.setItem("stata-mcp-terminal-app", chosen.id);
}

function loadCustomCards() {
  try {
    const raw = JSON.parse(localStorage.getItem("stata-mcp-custom-cards") || "[]");
    stateUI.customCards = Array.isArray(raw) ? raw : [];
  } catch {
    stateUI.customCards = [];
  }
}

function saveCustomCards() {
  localStorage.setItem("stata-mcp-custom-cards", JSON.stringify(stateUI.customCards));
}

function populateCardPresetSelect() {
  const select = $("in-card-preset");
  if (!select) return;
  select.innerHTML = "";
  for (const a of stateUI.agents) {
    const opt = document.createElement("option");
    opt.value = a.id;
    opt.textContent = `${a.name}（${a.installed ? "已就绪" : "未检测到"}）`;
    select.appendChild(opt);
  }
  const customOpt = document.createElement("option");
  customOpt.value = "custom";
  customOpt.textContent = "自定义终端命令";
  select.appendChild(customOpt);
}

function syncCardModalFieldsFromPreset(presetId, { updateName = true } = {}) {
  const matched = stateUI.agents.find((a) => a.id === presetId);
  const isApp = matched?.kind === "app";
  $("card-cmd-field").classList.toggle("hidden", isApp);
  if (isApp) {
    $("in-card-cmd").value = "";
  } else if (presetId === "custom") {
    if (updateName) $("in-card-cmd").value = "";
  } else {
    $("in-card-cmd").value = matched?.command || "";
  }
  if (updateName) {
    $("in-card-name").value = matched ? matched.name : "";
  }
}

function openCardModal(card = null) {
  populateCardPresetSelect();
  if (card) {
    stateUI.editingCardId = card.id;
    $("card-modal-title").textContent = "编辑启动卡片";
    const select = $("in-card-preset");
    const hasOption = [...select.options].some((o) => o.value === card.presetId);
    const presetVal = hasOption ? card.presetId : "custom";
    select.value = presetVal;
    syncCardModalFieldsFromPreset(presetVal, { updateName: false });
    $("in-card-name").value = card.name || "";
    if (card.kind !== "app") {
      $("in-card-cmd").value = card.command || "";
    }
    $("in-card-tagline").value = card.tagline || pickRandomTagline();
  } else {
    stateUI.editingCardId = null;
    $("card-modal-title").textContent = "新建启动卡片";
    const firstReady = stateUI.agents.find((a) => a.installed) || stateUI.agents[0];
    const presetVal = firstReady?.id || "custom";
    $("in-card-preset").value = presetVal;
    syncCardModalFieldsFromPreset(presetVal, { updateName: true });
    $("in-card-tagline").value = pickRandomTagline();
  }
  $("card-modal-backdrop").classList.remove("hidden");
  $("card-modal").classList.remove("hidden");
}

function closeCardModal() {
  $("card-modal-backdrop").classList.add("hidden");
  $("card-modal").classList.add("hidden");
  stateUI.editingCardId = null;
}

function saveCardFromModal() {
  const presetId = $("in-card-preset").value || "custom";
  const matched = stateUI.agents.find((a) => a.id === presetId);
  const kind = matched?.kind === "app" ? "app" : "cli";
  const name = $("in-card-name").value.trim() || matched?.name || "";
  if (!name) {
    showSnackbar("请输入卡片名称");
    return;
  }
  const command = kind === "app" ? "" : $("in-card-cmd").value.trim();
  const tagline = $("in-card-tagline").value.trim() || pickRandomTagline();
  const payload = {
    id: stateUI.editingCardId || `card-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
    presetId,
    kind,
    name,
    command,
    tagline,
  };
  if (stateUI.editingCardId) {
    stateUI.customCards = stateUI.customCards.map((c) => (c.id === stateUI.editingCardId ? payload : c));
    showSnackbar(`已更新卡片：${name}`);
  } else {
    stateUI.customCards = [...stateUI.customCards, payload];
    showSnackbar(`已添加卡片：${name}`);
  }
  saveCustomCards();
  closeCardModal();
  renderAgentsGrid();
}

function renderAgentsGrid() {
  const grid = $("term-agents-grid");
  if (!stateUI.customCards.length) {
    grid.innerHTML = `<div class="agents-empty">
      <div class="agents-empty-text">还没有启动卡片，按你的习惯自由创建（内置随机趣味卡片话语）</div>
      <button type="button" class="btn btn-secondary btn-sm js-empty-add-card">+ 新建卡片</button>
    </div>`;
    grid.querySelector(".js-empty-add-card")?.addEventListener("click", () => openCardModal(null));
    return;
  }

  const agentsById = new Map(stateUI.agents.map((a) => [a.id, a]));
  grid.innerHTML = stateUI.customCards
    .map((card) => {
      const matched = agentsById.get(card.presetId);
      const appMissing = card.kind === "app" && matched && !matched.installed;
      const cls = appMissing ? "missing" : "installed";
      const badgeText =
        card.kind === "app"
          ? appMissing
            ? "未检测到"
            : "应用已就绪"
          : card.command
            ? "CLI 命令"
            : "默认终端";
      const desc = card.tagline || pickRandomTagline();
      return `<div class="agent-card ${cls}" role="button" tabindex="0" data-card-id="${escapeHtml(card.id)}">
        <div class="agent-head">
          <span class="agent-name">${escapeHtml(card.name)}</span>
          <div class="agent-head-right">
            <span class="agent-badge">${escapeHtml(badgeText)}</span>
            <div class="agent-card-actions">
              <button type="button" class="card-action-btn" data-edit-card="${escapeHtml(card.id)}">编辑</button>
              <button type="button" class="card-action-btn danger" data-delete-card="${escapeHtml(card.id)}">删除</button>
            </div>
          </div>
        </div>
        <div class="agent-desc">${escapeHtml(desc)}</div>
      </div>`;
    })
    .join("");

  grid.querySelectorAll("[data-edit-card]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const card = stateUI.customCards.find((c) => c.id === btn.dataset.editCard);
      if (card) openCardModal(card);
    });
  });

  grid.querySelectorAll("[data-delete-card]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const targetId = btn.dataset.deleteCard;
      stateUI.customCards = stateUI.customCards.filter((c) => c.id !== targetId);
      saveCustomCards();
      renderAgentsGrid();
      showSnackbar("已删除卡片");
    });
  });

  grid.querySelectorAll(".agent-card[data-card-id]").forEach((cardEl) => {
    const triggerLaunch = () => {
      const card = stateUI.customCards.find((c) => c.id === cardEl.dataset.cardId);
      if (!card) return;
      if (card.kind === "app") {
        const matched = agentsById.get(card.presetId);
        if (matched && !matched.installed) {
          showSnackbar(`未在本机检测到 ${matched.name}`);
          return;
        }
        launchSelectedAgent(card.presetId);
      } else {
        launchSelectedAgent(card.command ? "custom" : "shell", card.command);
      }
    };
    cardEl.addEventListener("click", triggerLaunch);
    cardEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        triggerLaunch();
      }
    });
  });
}

async function launchSelectedAgent(agentId, customCommand = "") {
  const folderPath = $("term-folder-input").value.trim();
  if (!folderPath) {
    showSnackbar("请先选择科研项目文件夹");
    return;
  }
  setGlobalBusy(true);
  try {
    const ensureService = $("chk-ensure-service").checked;
    const res = await api.launchAgent({
      folderPath,
      agentId,
      customCommand,
      terminalId: stateUI.selectedTerminalId,
      ensureService,
    });
    if (!res.ok) {
      showSnackbar(res.error || "启动失败", 3500);
      if (res.needsInstall) switchTab("config");
      return;
    }
    const viaTerm = res.terminalName ? `（${res.terminalName}）` : "";
    if (res.serviceStarted) {
      refreshOverview();
      showSnackbar(`已自动启动 Stata MCP 并打开 ${res.launched}${viaTerm}`, 3000);
    } else {
      showSnackbar(`已在项目目录启动：${res.launched}${viaTerm}`, 2500);
    }
  } finally {
    setGlobalBusy(false);
  }
}

async function refreshTerminalPage() {
  loadSavedProjects();
  loadCustomCards();
  if (!$("term-folder-input").value.trim()) {
    const savedLast = localStorage.getItem("stata-mcp-last-project") || "";
    if (savedLast) $("term-folder-input").value = savedLast;
  }
  const [agents, terminals] = await Promise.all([
    api.detectAgents(),
    api.detectTerminals(),
    selectResearchFolder($("term-folder-input").value.trim()),
  ]);
  stateUI.agents = agents || [];
  stateUI.terminals = terminals || [];
  renderTerminalSelector();
  renderAgentsGrid();
}

/* ── Page 3: Empirical Monitor (Single-Column Expandable Timeline) ──── */
function statusBadgeClass(status) {
  if (status === "finished") return "ok";
  if (status === "running") return "warn";
  if (status === "failed" || status === "timed_out") return "bad";
  return "";
}

function formatDuration(sec) {
  if (sec === undefined || sec === null) return "—";
  const n = Number(sec);
  if (Number.isNaN(n)) return "—";
  return n >= 60 ? `${(n / 60).toFixed(1)}m` : `${n.toFixed(1)}s`;
}

async function loadJobDetailForCard(jobId) {
  const detail = await api.getJobDetail(jobId);
  if (detail.ok) {
    stateUI.jobDetailsCache.set(jobId, detail);
  }
  return detail;
}

function renderTimelineDetailHtml(detail) {
  if (!detail || !detail.ok) {
    return `<div class="setup-meta">正在读取日志…</div>`;
  }
  const d = detail.diagnosis;
  let diagHtml = "";
  if (d) {
    const loc = `${d.file || "?"}:${d.error_line || "?"}`;
    const snippet = (d.context_snippet || [])
      .map((s) => `${s.is_error ? ">>>" : "   "} ${String(s.line).padStart(4, " ")} | ${s.code}`)
      .join("\n");
    diagHtml = `<div class="diag-box">
      <div class="diag-title">报错定位：${escapeHtml(loc)} · ${escapeHtml(d.error_code || "")}</div>
      <div>${escapeHtml(d.error_message || "")}</div>
      ${d.fix_hint ? `<div style="margin-top:4px;">建议：${escapeHtml(d.fix_hint)}</div>` : ""}
      ${snippet ? `<pre class="diag-code">${escapeHtml(snippet)}</pre>` : ""}
    </div>`;
  }
  return `${diagHtml}<pre class="console-box">${escapeHtml(detail.journal || "(暂无步骤日志)")}</pre>`;
}

function renderMonitorTimeline() {
  const box = $("monitor-timeline");
  const filtered = stateUI.jobsCache.filter((j) => {
    if (stateUI.jobsFilter === "all") return true;
    return j.status === stateUI.jobsFilter;
  });
  if (!filtered.length) {
    box.innerHTML = `<div class="empty-card">当前筛选条件下暂无 Stata 作业记录</div>`;
    return;
  }

  box.innerHTML = filtered
    .map((j) => {
      const expanded = stateUI.expandedJobs.has(j.job_id);
      const cls = statusBadgeClass(j.status);
      const target = j.file_path || j.do_file || j.kind || j.job_id;
      const errTag = j.error_code ? ` · ${j.error_code}${j.error_line ? ` (Line ${j.error_line})` : ""}` : "";
      const cancelBtn =
        j.status === "running"
          ? `<button type="button" class="btn btn-danger btn-xs" data-cancel-job="${escapeHtml(j.job_id)}">中止</button>`
          : "";
      const detail = expanded ? stateUI.jobDetailsCache.get(j.job_id) : null;
      return `<article class="timeline-card" data-job-card="${escapeHtml(j.job_id)}">
        <div class="timeline-summary" data-toggle-job="${escapeHtml(j.job_id)}" role="button" tabindex="0">
          <div class="timeline-left">
            <div class="timeline-title">${escapeHtml(target)}</div>
            <div class="timeline-sub">${escapeHtml(j.job_id)} · ${j.steps ?? 0} 步 · ${formatDuration(j.elapsed_s)}${escapeHtml(errTag)}</div>
          </div>
          <div class="timeline-right">
            ${cancelBtn}
            <span class="pill-badge ${cls}">${escapeHtml(j.status || "unknown")}</span>
          </div>
        </div>
        ${expanded ? `<div class="timeline-detail">${renderTimelineDetailHtml(detail)}</div>` : ""}
      </article>`;
    })
    .join("");

  box.querySelectorAll("[data-cancel-job]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const jobId = btn.dataset.cancelJob;
      await api.cancelJob(jobId);
      showSnackbar(`已向作业 ${jobId} 发送中止请求`);
      await refreshJobs();
    });
  });

  box.querySelectorAll("[data-toggle-job]").forEach((row) => {
    const toggle = async () => {
      const jobId = row.dataset.toggleJob;
      if (stateUI.expandedJobs.has(jobId)) {
        stateUI.expandedJobs.delete(jobId);
        renderMonitorTimeline();
      } else {
        stateUI.expandedJobs.add(jobId);
        renderMonitorTimeline();
        await loadJobDetailForCard(jobId);
        renderMonitorTimeline();
      }
    };
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle();
      }
    });
  });
}

function scheduleMonitorPollIfNeeded() {
  if (stateUI.pollTimer) {
    clearTimeout(stateUI.pollTimer);
    stateUI.pollTimer = null;
  }
  const hasRunning = stateUI.jobsCache.some((j) => j.status === "running");
  if (stateUI.activeTab === "monitor" && hasRunning) {
    stateUI.pollTimer = setTimeout(() => refreshJobs(), 2000);
  }
}

async function refreshJobs() {
  stateUI.jobsCache = (await api.listJobs(40)) || [];
  for (const jobId of stateUI.expandedJobs) {
    const item = stateUI.jobsCache.find((j) => j.job_id === jobId);
    if (!stateUI.jobDetailsCache.has(jobId) || item?.status === "running") {
      await loadJobDetailForCard(jobId);
    }
  }
  renderMonitorTimeline();
  scheduleMonitorPollIfNeeded();
}

/* ── Logs Modal ─────────────────────────────────────────────────────── */
async function refreshLogs() {
  const res = await api.readLogTail(stateUI.logName, 140);
  $("log-tail-box").textContent = res.content || "(无内容)";
}

/* ── Events Wiring ──────────────────────────────────────────────────── */
function wireEvents() {
  // Navigation
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  // Page 1: Config
  $("btn-refresh-overview").addEventListener("click", refreshOverview);
  $("btn-open-advanced").addEventListener("click", openAdvancedDrawer);
  $("btn-open-mirrors-quick").addEventListener("click", openAdvancedDrawer);
  $("btn-open-client-cfg").addEventListener("click", openAdvancedDrawer);
  $("btn-close-advanced").addEventListener("click", closeAdvancedDrawer);
  $("drawer-backdrop").addEventListener("click", closeAdvancedDrawer);

  $("btn-install").addEventListener("click", () => runAction("install"));
  $("btn-start").addEventListener("click", () => runAction("start"));
  $("btn-stop").addEventListener("click", () => runAction("stop"));
  $("btn-verify").addEventListener("click", () => runAction("verify"));

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

  $("btn-use-alt-port").addEventListener("click", () => {
    stateUI.userEditedPort = true;
    $("in-port").value = "4011";
    $("port-conflict-banner").classList.add("hidden");
    openAdvancedDrawer();
    showSnackbar("已切换端口为 4011");
  });

  document.querySelectorAll("#cfg-transport-seg .seg-btn").forEach((b) => {
    b.addEventListener("click", () => {
      stateUI.transport = b.dataset.transport;
      document.querySelectorAll("#cfg-transport-seg .seg-btn").forEach((x) => x.classList.toggle("active", x === b));
      refreshConfigSnippet();
    });
  });

  $("btn-copy-config").addEventListener("click", async () => {
    const txt = await refreshConfigSnippet();
    copyWithFeedback(txt, $("btn-copy-config"));
  });

  // Console Drawer
  $("btn-copy-console").addEventListener("click", () =>
    copyWithFeedback($("action-console").textContent, $("btn-copy-console")),
  );
  $("btn-clear-console").addEventListener("click", () => {
    $("action-console").textContent = "等待操作…";
    $("console-status-text").textContent = "就绪";
    $("console-status-text").className = "drawer-status";
  });
  $("btn-close-console").addEventListener("click", () => {
    $("console-card").classList.add("hidden");
  });

  // Advanced Drawer: Mirrors, Ports, Safe Writer, Maintenance
  document.querySelectorAll("#env-mirror-links [data-open-url]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api.openExternalUrl(btn.dataset.openUrl);
        showSnackbar(`已在浏览器打开：${btn.textContent.trim()}`);
      } catch (err) {
        showSnackbar(`打开失败：${err?.message || err}`);
      }
    });
  });

  const syncPipIndexUI = (url) => {
    const clean = String(url || "").trim();
    $("in-pip-index").value = clean;
    localStorage.setItem("stata-mcp-pip-index", clean);
    document.querySelectorAll("#pypi-mirror-presets [data-pip-index]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.pipIndex === clean);
    });
  };
  document.querySelectorAll("#pypi-mirror-presets [data-pip-index]").forEach((btn) => {
    btn.addEventListener("click", () => {
      syncPipIndexUI(btn.dataset.pipIndex);
      showSnackbar(`已选用 PyPI 源：${btn.textContent.trim()}`);
    });
  });
  $("in-pip-index").addEventListener("input", () => syncPipIndexUI($("in-pip-index").value));
  syncPipIndexUI(localStorage.getItem("stata-mcp-pip-index") || "");

  $("in-port").addEventListener("input", () => (stateUI.userEditedPort = true));
  $("in-timeout").addEventListener("input", () => (stateUI.userEditedTimeout = true));
  $("cfg-root").addEventListener("input", refreshConfigSnippet);
  $("cfg-key").addEventListener("input", refreshConfigSnippet);
  $("cfg-timeout-ms").addEventListener("input", refreshConfigSnippet);

  $("btn-copy-config-drawer").addEventListener("click", async () => {
    const txt = await refreshConfigSnippet();
    copyWithFeedback(txt, $("btn-copy-config-drawer"));
  });
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

  $("btn-refresh-bin").addEventListener("click", () => {
    closeAdvancedDrawer();
    runAction("refresh");
  });
  $("btn-open-home").addEventListener("click", () => api.openStateFolder());
  $("btn-copy-doctor-json").addEventListener("click", () =>
    copyWithFeedback($("doctor-json-box").textContent, $("btn-copy-doctor-json")),
  );
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
    closeAdvancedDrawer();
    await runAction("uninstall");
  });

  // Page 2: Research Terminal
  $("btn-rescan-agents").addEventListener("click", refreshTerminalPage);
  $("btn-pick-project").addEventListener("click", async () => {
    const dir = await api.pickProjectDir();
    if (dir) selectResearchFolder(dir);
  });
  $("term-folder-input").addEventListener("change", () => {
    selectResearchFolder($("term-folder-input").value.trim());
  });
  $("btn-open-project-folder").addEventListener("click", async () => {
    const dir = $("term-folder-input").value.trim();
    if (!dir) {
      showSnackbar("请先选择项目文件夹");
      return;
    }
    try {
      await api.openFolder(dir);
    } catch (err) {
      showSnackbar(err?.message || "无法打开目录");
    }
  });
  $("btn-toggle-pin-project").addEventListener("click", () => {
    const dir = $("term-folder-input").value.trim();
    if (!dir) {
      showSnackbar("请先选择项目文件夹");
      return;
    }
    if (stateUI.pinnedProjects.includes(dir)) {
      stateUI.pinnedProjects = stateUI.pinnedProjects.filter((p) => p !== dir);
      showSnackbar("已取消收藏");
    } else {
      stateUI.pinnedProjects = [dir, ...stateUI.pinnedProjects.filter((p) => p !== dir)];
      showSnackbar("已收藏到科研目录");
    }
    saveProjectsState();
    renderProjectPills();
  });
  $("term-app-select").addEventListener("change", (e) => {
    stateUI.selectedTerminalId = e.target.value;
    localStorage.setItem("stata-mcp-terminal-app", stateUI.selectedTerminalId);
    renderAgentsGrid();
    const termName = getSelectedTerminal()?.name || stateUI.selectedTerminalId;
    showSnackbar(`默认终端已切换为 ${termName}`);
  });
  $("btn-add-agent-card").addEventListener("click", () => openCardModal(null));
  $("btn-close-card-modal").addEventListener("click", closeCardModal);
  $("btn-cancel-card-modal").addEventListener("click", closeCardModal);
  $("card-modal-backdrop").addEventListener("click", closeCardModal);
  $("in-card-preset").addEventListener("change", (e) => {
    syncCardModalFieldsFromPreset(e.target.value, { updateName: !stateUI.editingCardId });
  });
  $("btn-shuffle-tagline").addEventListener("click", () => {
    $("in-card-tagline").value = pickRandomTagline($("in-card-tagline").value);
  });
  $("btn-save-card").addEventListener("click", saveCardFromModal);
  $("btn-launch-custom-agent").addEventListener("click", () => {
    launchSelectedAgent("custom", $("in-custom-agent-cmd").value.trim());
  });
  $("in-custom-agent-cmd").addEventListener("keydown", (e) => {
    if (e.key === "Enter") launchSelectedAgent("custom", $("in-custom-agent-cmd").value.trim());
  });

  // Page 3: Monitor & Log Modal
  $("btn-reload-jobs").addEventListener("click", refreshJobs);
  document.querySelectorAll("#monitor-filter-seg .seg-btn").forEach((b) => {
    b.addEventListener("click", () => {
      stateUI.jobsFilter = b.dataset.jobFilter;
      document.querySelectorAll("#monitor-filter-seg .seg-btn").forEach((x) => x.classList.toggle("active", x === b));
      renderMonitorTimeline();
    });
  });

  $("btn-open-log-modal").addEventListener("click", openLogModal);
  $("btn-close-log-modal").addEventListener("click", closeLogModal);
  $("log-modal-backdrop").addEventListener("click", closeLogModal);
  $("btn-reload-logs").addEventListener("click", refreshLogs);
  $("btn-copy-log-tail").addEventListener("click", () =>
    copyWithFeedback($("log-tail-box").textContent, $("btn-copy-log-tail")),
  );
  document.querySelectorAll("#log-tabs-seg .seg-btn").forEach((b) => {
    b.addEventListener("click", () => {
      stateUI.logName = b.dataset.log;
      document.querySelectorAll("#log-tabs-seg .seg-btn").forEach((x) => x.classList.toggle("active", x === b));
      refreshLogs();
    });
  });

  // Live IPC streams + Esc key
  api.onLog(({ text }) => appendConsole(text));
  if (api.onStateChanged) {
    api.onStateChanged(() => refreshOverview());
  }
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeAdvancedDrawer();
      closeLogModal();
      closeCardModal();
    }
  });
}

initThemeAndPlatform();
wireEvents();
const savedTab = localStorage.getItem("stata-mcp-active-tab");
if (savedTab && ["config", "terminal", "monitor"].includes(savedTab) && savedTab !== "config") {
  switchTab(savedTab);
}
refreshOverview().finally(() => refreshConfigSnippet());
