import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  buildAugmentedEnv,
  CoreService,
  detectInstalledAgents,
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
    "term-agents-grid",
    "monitor-timeline",
    "log-modal",
  ]) {
    assert.ok(html.includes(`id="${extraId}"`), `missing UI id: ${extraId}`);
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
