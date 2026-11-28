import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  buildAugmentedEnv,
  buildTerminalLaunchPlan,
  CoreService,
  detectInstalledAgents,
  detectInstalledTerminals,
  findPythonCommand,
  inspectResearchFolder,
  posixSingleQuote,
  resolveCoreRoot,
} from "../app/core-service.mjs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const afterPack = require("../scripts/after-pack.cjs");

const testsDir = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.resolve(testsDir, "..");
const repoRoot = path.resolve(desktopDir, "..");

test("buildAugmentedEnv sets UTF-8 flags and platform PATH entries", () => {
  const macEnv = buildAugmentedEnv({
    platform: "darwin",
    env: { PATH: "/usr/bin" },
    homeDir: "/Users/tester",
  });
  assert.equal(macEnv.PYTHONUTF8, "1");
  assert.equal(macEnv.PYTHONUNBUFFERED, "1");
  assert.ok(macEnv.PATH.includes("/opt/homebrew/bin"));
  assert.ok(macEnv.PATH.includes("/Users/tester/.local/bin"));

  const winEnv = buildAugmentedEnv({
    platform: "win32",
    env: { PATH: "C:\\Windows\\System32", LOCALAPPDATA: "C:\\Users\\t\\AppData\\Local" },
    homeDir: "C:\\Users\\t",
  });
  assert.equal(winEnv.PYTHONIOENCODING, "utf-8");
  assert.ok(winEnv.PATH.includes("Python312"));
});

test("findPythonCommand prefers state.json python when present", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "stata-mcp-desk-"));
  const fakePy = path.join(tmp, "custom-python");
  fs.writeFileSync(fakePy, "");
  fs.writeFileSync(path.join(tmp, "state.json"), JSON.stringify({ python: fakePy }));

  const found = findPythonCommand({
    platform: "darwin",
    stateHome: tmp,
    existsSync: (p) => fs.existsSync(p),
  });
  assert.equal(found.cmd, fakePy);
  fs.rmSync(tmp, { recursive: true, force: true });
});

test("resolveCoreRoot points to repo root when unpackaged", () => {
  const root = resolveCoreRoot({
    isPackaged: false,
    resourcesPath: null,
    appDir: path.join(desktopDir, "app"),
  });
  assert.equal(root, repoRoot);
});

test("CoreService can lint Stata code via stata_mcp_analysis.py", async () => {
  const svc = new CoreService({
    isPackaged: false,
    appDir: path.join(desktopDir, "app"),
  });
  const res = await svc.lintDo({
    code: "forvalues i = 1/`=_N' {\n  replace x = 1 in `i'\n}\n",
  });
  assert.equal(res.anti_loop_count, 1);
  assert.ok(res.issues.some((i) => i.rule_id === "ANTI_LOOP_OBS_INDEX"));
});

test("afterPack no-ops cleanly on non-darwin targets", async () => {
  await afterPack({ electronPlatformName: "win32" });
});

test("renderer HTML contains all 3 tabs and every ID referenced by renderer.mjs", () => {
  const html = fs.readFileSync(path.join(desktopDir, "app", "renderer", "index.html"), "utf8");
  const js = fs.readFileSync(path.join(desktopDir, "app", "renderer", "renderer.mjs"), "utf8");

  for (const tab of ["config", "terminal", "monitor"]) {
    assert.ok(html.includes(`id="tab-${tab}"`), `missing tab-${tab}`);
    assert.ok(html.includes(`data-tab="${tab}"`), `missing data-tab=${tab}`);
  }

  const ids = [...js.matchAll(/\$\("([A-Za-z0-9_-]+)"\)/g)].map((m) => m[1]);
  assert.ok(ids.length > 20);
  for (const id of new Set(ids)) {
    assert.ok(html.includes(`id="${id}"`), `renderer.mjs references missing DOM id: ${id}`);
  }

  for (const extraId of [
    "global-progress-bar",
    "app-snackbar",
    "advanced-drawer",
    "env-mirror-links",
    "pypi-mirror-presets",
    "in-pip-index",
    "term-app-select",
    "term-agents-grid",
    "monitor-timeline",
    "log-modal",
  ]) {
    assert.ok(html.includes(`id="${extraId}"`), `missing UI id: ${extraId}`);
  }
  for (const removedId of ["btn-write-project-mcp", "btn-copy-research-prompt"]) {
    assert.equal(html.includes(`id="${removedId}"`), false, `removed button should not remain: ${removedId}`);
  }
  assert.ok(
    fs.existsSync(path.join(repoRoot, "server", "src", "stata_mcp_server.py")),
    "bundled server/src/stata_mcp_server.py should exist in repo",
  );
  assert.ok(html.includes("data-theme-choice"));
});

test("detectInstalledAgents finds installed apps and CLI binaries", () => {
  const found = detectInstalledAgents({
    platform: "darwin",
    env: { PATH: "/opt/homebrew/bin" },
    homeDir: "/Users/tester",
    existsSync: (p) => p === "/opt/homebrew/bin/claude" || p === "/Applications/Cursor.app",
  });
  const byId = Object.fromEntries(found.map((a) => [a.id, a]));
  assert.equal(byId.claude.installed, true);
  assert.equal(byId.cursor.installed, true);
  assert.equal(byId.shell.installed, true);
  assert.equal(byId.codex.installed, false);
});

test("inspectResearchFolder counts .do/.dta files and checks .mcp.json", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "stata-mcp-proj-"));
  try {
    fs.mkdirSync(path.join(tmp, "code"));
    fs.writeFileSync(path.join(tmp, "main.do"), "display 1\n", "utf8");
    fs.writeFileSync(path.join(tmp, "code", "clean.do"), "display 2\n", "utf8");
    fs.writeFileSync(path.join(tmp, "data.dta"), "", "utf8");
    fs.writeFileSync(
      path.join(tmp, ".mcp.json"),
      JSON.stringify({ mcpServers: { "stata-mcp": { command: "python3" } } }),
      "utf8",
    );
    const info = inspectResearchFolder(tmp);
    assert.equal(info.ok, true);
    assert.equal(info.doCount, 2);
    assert.equal(info.dtaCount, 1);
    assert.equal(info.hasMcpJson, true);
    assert.equal(info.hasStataMcpEntry, true);
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("posixSingleQuote safely escapes single quotes and spaces", () => {
  assert.equal(posixSingleQuote("/Users/test/my paper's dir"), "'/Users/test/my paper'\\''s dir'");
});

test("CoreService rejects concurrent runAction while an action is active", async () => {
  const svc = new CoreService({
    isPackaged: false,
    appDir: path.join(desktopDir, "app"),
  });
  svc._activeAction = "install";
  const res = await svc.runAction("install");
  assert.equal(res.ok, false);
  assert.equal(res.code, -2);
  svc._activeAction = null;
});

test("buildAugmentedEnv deduplicates Path/PATH keys on win32", () => {
  const winEnv = buildAugmentedEnv({
    platform: "win32",
    env: { Path: "C:\\Windows\\System32", LOCALAPPDATA: "C:\\Users\\t\\AppData\\Local" },
    homeDir: "C:\\Users\\t",
  });
  const pathKeys = Object.keys(winEnv).filter((k) => k.toLowerCase() === "path");
  assert.deepEqual(pathKeys, ["PATH"]);
  assert.ok(winEnv.PATH.includes("C:\\Windows\\System32"));
});

test("CoreService._runPython emits stderr log when command times out", async () => {
  const logs = [];
  const svc = new CoreService({
    isPackaged: false,
    appDir: path.join(desktopDir, "app"),
    emitLog: (payload) => logs.push(payload),
  });
  const tmpScript = path.join(os.tmpdir(), `stata-mcp-timeout-${Date.now()}.py`);
  fs.writeFileSync(tmpScript, "import time\ntime.sleep(5)\n", "utf8");
  try {
    const res = await svc._runPython(tmpScript, [], { stream: true, timeoutMs: 150 });
    assert.equal(res.code, -1);
    assert.ok(res.stderr.includes("timed out"));
    assert.ok(logs.some((l) => l.stream === "stderr" && l.text.includes("超时")));
  } finally {
    fs.rmSync(tmpScript, { force: true });
  }
});

test("renderer HTML contains no literal markdown backticks in UI text", () => {
  const html = fs.readFileSync(path.join(desktopDir, "app", "renderer", "index.html"), "utf8");
  const uiWithoutCodeSamples = html.replace(/<textarea\b[\s\S]*?<\/textarea>/gi, "");
  assert.equal(
    uiWithoutCodeSamples.includes("`"),
    false,
    "index.html should use <code> tags instead of literal backticks",
  );
});

test("detectInstalledTerminals detects macOS terminals and prioritizes Ghostty", () => {
  const found = detectInstalledTerminals({
    platform: "darwin",
    env: { PATH: "/usr/bin" },
    homeDir: "/Users/tester",
    existsSync: (p) => p === "/Applications/Ghostty.app" || p === "/Applications/iTerm.app",
  });
  const installedIds = found.filter((t) => t.installed).map((t) => t.id);
  assert.deepEqual(installedIds, ["ghostty", "iterm2", "terminal"]);
  assert.equal(found[0].id, "ghostty");
  assert.equal(found[0].target, "/Applications/Ghostty.app");
});

test("detectInstalledTerminals detects Windows terminals in priority order", () => {
  const existing = new Set([
    "C:\\Users\\t\\AppData\\Local\\Microsoft\\WindowsApps\\wt.exe",
    "C:\\Program Files\\PowerShell\\7\\pwsh.exe",
    "C:\\Program Files\\Git\\git-bash.exe",
    "C:\\Program Files\\WezTerm\\wezterm-gui.exe",
    "C:\\Program Files\\Alacritty\\alacritty.exe",
  ]);
  const found = detectInstalledTerminals({
    platform: "win32",
    env: {
      PATH: "C:\\Windows\\System32",
      LOCALAPPDATA: "C:\\Users\\t\\AppData\\Local",
      ProgramFiles: "C:\\Program Files",
      "ProgramFiles(x86)": "C:\\Program Files (x86)",
    },
    homeDir: "C:\\Users\\t",
    existsSync: (p) => existing.has(p),
  });
  const installedIds = found.filter((t) => t.installed).map((t) => t.id);
  assert.deepEqual(installedIds, ["wt", "pwsh", "gitbash", "wezterm", "alacritty", "powershell", "cmd"]);
});

test("detectInstalledTerminals detects Linux terminals in PATH", () => {
  const existing = new Set(["/usr/bin/ghostty", "/usr/bin/kitty", "/usr/bin/gnome-terminal"]);
  const found = detectInstalledTerminals({
    platform: "linux",
    env: { PATH: "/usr/bin" },
    homeDir: "/home/tester",
    existsSync: (p) => existing.has(p),
  });
  const installedIds = found.filter((t) => t.installed).map((t) => t.id);
  assert.deepEqual(installedIds, ["ghostty", "kitty", "gnome-terminal"]);
});

test("buildTerminalLaunchPlan for macOS Ghostty supports cold start and running AppleScript with fallback", () => {
  const ghostty = { id: "ghostty", name: "Ghostty", target: "/Applications/Ghostty.app" };
  const coldPlan = buildTerminalLaunchPlan({
    platform: "darwin",
    terminal: ghostty,
    targetDir: "/Users/tester/paper dir",
    cmdToRun: "claude",
    env: { SHELL: "/bin/zsh" },
    isAppRunning: false,
    hasGhosttySdef: true,
  });
  assert.equal(coldPlan.command, "open");
  assert.equal(coldPlan.mode, "detached");
  assert.deepEqual(coldPlan.args.slice(0, 4), ["-na", "/Applications/Ghostty.app", "--args", "--working-directory=/Users/tester/paper dir"]);
  assert.equal(coldPlan.args[4], "-e");
  assert.equal(coldPlan.args[5], "/bin/zsh");

  const warmPlan = buildTerminalLaunchPlan({
    platform: "darwin",
    terminal: ghostty,
    targetDir: "/Users/tester/paper dir",
    cmdToRun: "codex",
    env: { SHELL: "/bin/zsh" },
    isAppRunning: true,
    hasGhosttySdef: true,
  });
  assert.equal(warmPlan.command, "osascript");
  assert.equal(warmPlan.mode, "control");
  assert.ok(warmPlan.args[1].includes("new surface configuration"));
  assert.ok(warmPlan.args[1].includes("new window with configuration cfg"));
  assert.ok(warmPlan.fallback);
  assert.equal(warmPlan.fallback.command, "open");
});

test("buildTerminalLaunchPlan for macOS Terminal.app and iTerm2 avoids creating an empty window", () => {
  const termPlan = buildTerminalLaunchPlan({
    platform: "darwin",
    terminal: { id: "terminal", name: "Terminal.app", target: "/System/Applications/Utilities/Terminal.app" },
    targetDir: "/Users/tester/proj",
    cmdToRun: "claude",
  });
  assert.equal(termPlan.command, "osascript");
  assert.equal(termPlan.mode, "control");
  const termScript = termPlan.args[1];
  assert.ok(termScript.includes("wasRunning"));
  assert.ok(termScript.includes("do script cmdText in front window"));
  // In the already-running branch, do script must happen before activate
  const elseIdx = termScript.indexOf("else");
  const branchText = termScript.slice(elseIdx);
  assert.ok(branchText.indexOf("do script cmdText") < branchText.indexOf("activate"));

  const itermPlan = buildTerminalLaunchPlan({
    platform: "darwin",
    terminal: { id: "iterm2", name: "iTerm2", target: "/Applications/iTerm.app" },
    targetDir: "/Users/tester/proj",
    cmdToRun: "gemini",
  });
  assert.equal(itermPlan.command, "osascript");
  const itermScript = itermPlan.args[1];
  assert.ok(itermScript.includes("wasRunning"));
  assert.ok(itermScript.includes("current session of current window"));
  const itermElse = itermScript.slice(itermScript.indexOf("else", itermScript.indexOf("create window")));
  assert.ok(itermElse.indexOf("create window") < itermElse.indexOf("activate"));
});

test("buildTerminalLaunchPlan builds correct Windows terminal launch commands", () => {
  const targetDir = "D:\\Research\\Paper 1";

  const wtPlan = buildTerminalLaunchPlan({
    platform: "win32",
    terminal: { id: "wt", target: "wt.exe" },
    targetDir,
    cmdToRun: "claude",
    preferredPowerShell: "C:\\Program Files\\PowerShell\\7\\pwsh.exe",
  });
  assert.equal(wtPlan.command, "wt.exe");
  assert.deepEqual(wtPlan.args.slice(0, 3), ["-d", targetDir, "C:\\Program Files\\PowerShell\\7\\pwsh.exe"]);

  const pwshPlan = buildTerminalLaunchPlan({
    platform: "win32",
    terminal: { id: "pwsh", target: "C:\\Program Files\\PowerShell\\7\\pwsh.exe" },
    targetDir,
    cmdToRun: "codex",
  });
  assert.equal(pwshPlan.command, "cmd.exe");
  assert.equal(pwshPlan.spawnOpts.windowsHide, true);
  assert.ok(pwshPlan.args.includes("C:\\Program Files\\PowerShell\\7\\pwsh.exe"));

  const psPlan = buildTerminalLaunchPlan({
    platform: "win32",
    terminal: { id: "powershell", target: "powershell.exe" },
    targetDir,
    cmdToRun: "",
  });
  assert.equal(psPlan.command, "cmd.exe");
  assert.equal(psPlan.spawnOpts.windowsHide, true);
  assert.ok(psPlan.args.includes("powershell.exe"));

  const gitBashPlan = buildTerminalLaunchPlan({
    platform: "win32",
    terminal: { id: "gitbash", target: "C:\\Program Files\\Git\\git-bash.exe" },
    targetDir,
    cmdToRun: "gemini",
    minttyPath: "C:\\Program Files\\Git\\usr\\bin\\mintty.exe",
    gitBashBin: "C:\\Program Files\\Git\\bin\\bash.exe",
  });
  assert.equal(gitBashPlan.command, "C:\\Program Files\\Git\\usr\\bin\\mintty.exe");
  assert.deepEqual(gitBashPlan.args.slice(0, 3), ["-e", "C:\\Program Files\\Git\\bin\\bash.exe", "-lic"]);

  const wezPlan = buildTerminalLaunchPlan({
    platform: "win32",
    terminal: { id: "wezterm", target: "C:\\Program Files\\WezTerm\\wezterm-gui.exe" },
    targetDir,
    cmdToRun: "claude",
    preferredPowerShell: "powershell.exe",
  });
  assert.equal(wezPlan.command, "C:\\Program Files\\WezTerm\\wezterm-gui.exe");
  assert.deepEqual(wezPlan.args.slice(0, 4), ["start", "--cwd", targetDir, "--"]);

  const alacrittyPlan = buildTerminalLaunchPlan({
    platform: "win32",
    terminal: { id: "alacritty", target: "C:\\Program Files\\Alacritty\\alacritty.exe" },
    targetDir,
    cmdToRun: "claude",
    preferredPowerShell: "powershell.exe",
  });
  assert.equal(alacrittyPlan.command, "C:\\Program Files\\Alacritty\\alacritty.exe");
  assert.deepEqual(alacrittyPlan.args.slice(0, 3), ["--working-directory", targetDir, "-e"]);

  const cmdPlan = buildTerminalLaunchPlan({
    platform: "win32",
    terminal: { id: "cmd", target: "cmd.exe" },
    targetDir,
    cmdToRun: "codex",
  });
  assert.equal(cmdPlan.command, "cmd.exe");
  assert.equal(cmdPlan.spawnOpts.windowsHide, true);
  assert.deepEqual(cmdPlan.args, ["/c", "start", "", "/D", targetDir, "cmd.exe", "/K", "codex"]);
});
