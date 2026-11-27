import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  buildAugmentedEnv,
  CoreService,
  findPythonCommand,
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

test("renderer HTML contains all 6 tabs and every ID referenced by renderer.mjs", () => {
  const html = fs.readFileSync(path.join(desktopDir, "app", "renderer", "index.html"), "utf8");
  const js = fs.readFileSync(path.join(desktopDir, "app", "renderer", "renderer.mjs"), "utf8");

  for (const tab of ["overview", "config", "jobs", "help", "lint", "doctor"]) {
    assert.ok(html.includes(`id="tab-${tab}"`), `missing tab-${tab}`);
    assert.ok(html.includes(`data-tab="${tab}"`), `missing data-tab=${tab}`);
  }

  const ids = [...js.matchAll(/\$\("([A-Za-z0-9_-]+)"\)/g)].map((m) => m[1]);
  assert.ok(ids.length > 20);
  for (const id of new Set(ids)) {
    assert.ok(html.includes(`id="${id}"`), `renderer.mjs references missing DOM id: ${id}`);
  }
});
