import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";

export function buildAugmentedEnv({
  platform = process.platform,
  env = process.env,
  homeDir = os.homedir(),
} = {}) {
  const next = { ...env };
  const p = platform === "win32" ? path.win32 : path.posix;
  const sep = platform === "win32" ? ";" : ":";
  let currentPath = "";
  if (platform === "win32") {
    for (const k of Object.keys(next)) {
      if (k.toLowerCase() === "path") {
        if (!currentPath && next[k]) currentPath = next[k];
        delete next[k];
      }
    }
  } else {
    currentPath = next.PATH || next.Path || "";
  }
  const parts = currentPath ? currentPath.split(sep).filter(Boolean) : [];
  const extras = [];

  if (platform === "darwin") {
    extras.push(
      "/opt/homebrew/bin",
      "/usr/local/bin",
      p.join(homeDir, ".local", "bin"),
      p.join(homeDir, ".cargo", "bin"),
      "/Library/Frameworks/Python.framework/Versions/Current/bin",
      "/usr/bin",
      "/bin",
    );
  } else if (platform === "win32") {
    const localApp = env.LOCALAPPDATA || p.join(homeDir, "AppData", "Local");
    extras.push(
      p.join(homeDir, ".local", "bin"),
      p.join(localApp, "Programs", "Python", "Python313"),
      p.join(localApp, "Programs", "Python", "Python312"),
      p.join(localApp, "Programs", "Python", "Python311"),
      p.join(localApp, "Programs", "Python", "Python310"),
      p.join(localApp, "Programs", "Python", "Python39"),
    );
  } else {
    extras.push("/usr/local/bin", p.join(homeDir, ".local", "bin"));
  }

  const merged = [];
  const seen = new Set();
  for (const item of [...extras, ...parts]) {
    if (!item || seen.has(item)) continue;
    seen.add(item);
    merged.push(item);
  }
  next.PATH = merged.join(sep);
  next.PYTHONUTF8 = "1";
  next.PYTHONIOENCODING = "utf-8";
  next.PYTHONUNBUFFERED = "1";
  return next;
}

export function resolveCoreRoot({ isPackaged, resourcesPath, appDir }) {
  if (isPackaged && resourcesPath) {
    const candidate = path.join(resourcesPath, "stata-mcp-core");
    if (fs.existsSync(candidate)) return candidate;
  }
  return path.resolve(appDir, "..", "..");
}

export function resolveStateHome(env = process.env, homeDir = os.homedir()) {
  const explicit = (env.STATA_MCP_HOME || "").trim();
  if (explicit) return path.resolve(explicit);
  return path.join(homeDir, ".stata-mcp");
}

export function findPythonCommand({
  platform = process.platform,
  env = process.env,
  homeDir = os.homedir(),
  stateHome = resolveStateHome(env, homeDir),
  existsSync = fs.existsSync,
} = {}) {
  const statePath = path.join(stateHome, "state.json");
  if (existsSync(statePath)) {
    try {
      const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
      if (state.python && existsSync(state.python)) {
        return { cmd: state.python, argsPrefix: [] };
      }
    } catch {
      // ignore invalid state.json
    }
  }

  const p = platform === "win32" ? path.win32 : path.posix;
  if (platform === "win32") {
    const venvPy = p.join(stateHome, "venv", "Scripts", "python.exe");
    if (existsSync(venvPy)) return { cmd: venvPy, argsPrefix: [] };
    const localApp = env.LOCALAPPDATA || p.join(homeDir, "AppData", "Local");
    for (const ver of ["313", "312", "311", "310", "39"]) {
      const p1 = p.join(localApp, "Programs", "Python", `Python${ver}`, "python.exe");
      if (existsSync(p1)) return { cmd: p1, argsPrefix: [] };
      const p2 = p.join("C:\\", `Python${ver}`, "python.exe");
      if (existsSync(p2)) return { cmd: p2, argsPrefix: [] };
    }
    return { cmd: "python", argsPrefix: [] };
  }

  const venvPy = p.join(stateHome, "venv", "bin", "python");
  if (existsSync(venvPy)) return { cmd: venvPy, argsPrefix: [] };
  for (const cand of [
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
    "/usr/bin/python3",
  ]) {
    if (existsSync(cand)) return { cmd: cand, argsPrefix: [] };
  }
  return { cmd: "python3", argsPrefix: [] };
}

export function httpGetJson(host, port, urlPath, timeoutMs = 2000) {
  return new Promise((resolve) => {
    const req = http.request(
      {
        host: host || "127.0.0.1",
        port: Number(port || 4001),
        path: urlPath,
        method: "GET",
        headers: { Accept: "application/json" },
        timeout: timeoutMs,
      },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => {
          if (res.statusCode !== 200) {
            resolve(null);
            return;
          }
          try {
            resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
          } catch {
            resolve(null);
          }
        });
      },
    );
    req.on("error", () => resolve(null));
    req.on("timeout", () => {
      req.destroy();
      resolve(null);
    });
    req.end();
  });
}

export class CoreService {
  constructor({
    isPackaged = false,
    resourcesPath = null,
    appDir = path.dirname(new URL(import.meta.url).pathname),
    platform = process.platform,
    emitLog = () => {},
  } = {}) {
    this.platform = platform;
    this.emitLog = emitLog;
    this.coreRoot = resolveCoreRoot({ isPackaged, resourcesPath, appDir });
    this.env = buildAugmentedEnv({ platform });
    this._activeAction = null;
  }

  resolveStateHome() {
    return resolveStateHome(this.env);
  }

  _installScript() {
    return path.join(this.coreRoot, "install.py");
  }

  _analysisScript() {
    const installed = path.join(this.resolveStateHome(), "bin", "stata_mcp_analysis.py");
    if (fs.existsSync(installed)) return installed;
    return path.join(this.coreRoot, "bin", "stata_mcp_analysis.py");
  }

  _loadState() {
    const stateFile = path.join(this.resolveStateHome(), "state.json");
    try {
      return JSON.parse(fs.readFileSync(stateFile, "utf8"));
    } catch {
      return {};
    }
  }

  _loadAppVersion() {
    try {
      const pkg = JSON.parse(fs.readFileSync(path.resolve(this.coreRoot, "desktop", "package.json"), "utf8"));
      if (pkg.version) return pkg.version;
    } catch {
      // fallback to state or package version
    }
    try {
      const pkg = JSON.parse(fs.readFileSync(new URL("../package.json", import.meta.url), "utf8"));
      if (pkg.version) return pkg.version;
    } catch {
      return "";
    }
    return "";
  }

  _killChildTree(child) {
    if (!child) return;
    if (this.platform === "win32" && child.pid) {
      try {
        spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
          windowsHide: true,
          stdio: "ignore",
        });
        return;
      } catch {
        // fallback to child.kill()
      }
    }
    try {
      child.kill();
    } catch {
      // ignore
    }
  }

  _runPython(scriptPath, args, { stream = false, stdinText = null, timeoutMs = 180000 } = {}) {
    const py = findPythonCommand({
      platform: this.platform,
      env: this.env,
      stateHome: this.resolveStateHome(),
    });
    const fullArgs = [...py.argsPrefix, "-u", scriptPath, ...args];
    return new Promise((resolve) => {
      const child = spawn(py.cmd, fullArgs, {
        cwd: this.coreRoot,
        env: this.env,
        windowsHide: true,
        stdio: ["pipe", "pipe", "pipe"],
      });

      let stdout = "";
      let stderr = "";
      let finished = false;
      const timer = setTimeout(() => {
        if (!finished) {
          finished = true;
          this._killChildTree(child);
          const timeoutMsg = `\n[超时] 命令超过 ${Math.round(timeoutMs / 1000)}s 未结束，已终止。\n`;
          if (stream) this.emitLog({ stream: "stderr", text: timeoutMsg });
          resolve({ code: -1, stdout, stderr: `${stderr}\nCommand timed out after ${timeoutMs}ms` });
        }
      }, timeoutMs);

      child.stdout.on("data", (chunk) => {
        const text = chunk.toString("utf8");
        stdout += text;
        if (stream) this.emitLog({ stream: "stdout", text });
      });
      child.stderr.on("data", (chunk) => {
        const text = chunk.toString("utf8");
        stderr += text;
        if (stream) this.emitLog({ stream: "stderr", text });
      });
      child.on("error", (err) => {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        if (stream) this.emitLog({ stream: "stderr", text: `\n[启动失败] ${err.message}\n` });
        resolve({ code: -1, stdout, stderr: err.message });
      });
      child.on("close", (code) => {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        resolve({ code: code ?? 0, stdout, stderr });
      });

      if (stdinText !== null) {
        child.stdin.write(stdinText);
      }
      child.stdin.end();
    });
  }

  async getOverview(opts = {}) {
    const args = ["doctor", "--json"];
    if (opts.stataPath) args.push("--stata-path", opts.stataPath);
    const res = await this._runPython(this._installScript(), args, { timeoutMs: 30000 });
    let doctor = null;
    try {
      doctor = JSON.parse(res.stdout);
    } catch {
      doctor = {
        platform: this.platform,
        arch: process.arch,
        installed: false,
        problems: 1,
        error: res.stderr || res.stdout || "doctor failed",
      };
    }
    const state = doctor.state || this._loadState();
    const host = state.host || "127.0.0.1";
    const port = Number(state.port || 4001);
    const liveHealth = await httpGetJson(host, port, "/health", 1500);
    if (liveHealth) {
      doctor.live_health = liveHealth;
    }
    return {
      ok: res.code === 0,
      appVersion: this._loadAppVersion(),
      coreRoot: this.coreRoot,
      stateHome: this.resolveStateHome(),
      doctor,
      rawStderr: res.stderr,
    };
  }

  async runAction(action, opts = {}) {
    const allowed = new Set(["install", "refresh", "start", "stop", "verify", "uninstall"]);
    if (!allowed.has(action)) {
      return { ok: false, code: 2, stdout: "", stderr: `Unsupported action: ${action}` };
    }
    if (this._activeAction) {
      const msg = `操作 "${this._activeAction}" 正在执行中，请等待完成后再试。\n`;
      this.emitLog({ stream: "stderr", text: msg });
      return { ok: false, code: -2, stdout: "", stderr: msg.trim() };
    }
    this._activeAction = action;
    try {
      const args = [action];
      if (action === "install") {
        args.push("-y");
        if (opts.stataPath) args.push("--stata-path", opts.stataPath);
        if (opts.port) args.push("--port", String(opts.port));
        if (opts.pipIndex) args.push("--pip-index", opts.pipIndex);
        if (opts.mirror) args.push("--mirror", opts.mirror);
        if (opts.defaultTimeout) args.push("--default-timeout", String(opts.defaultTimeout));
        if (opts.refresh) args.push("--refresh");
      } else if (action === "refresh") {
        if (opts.port) args.push("--port", String(opts.port));
        if (opts.defaultTimeout) args.push("--default-timeout", String(opts.defaultTimeout));
      } else if (action === "start") {
        if (opts.port) args.push("--port", String(opts.port));
      } else if (action === "uninstall") {
        if (opts.purge) args.push("--purge");
      }

      const timeoutMs = action === "install" ? 600000 : action === "verify" ? 180000 : 120000;
      this.emitLog({ stream: "meta", text: `\n$ python install.py ${args.join(" ")}\n` });
      const res = await this._runPython(this._installScript(), args, {
        stream: true,
        timeoutMs,
      });
      return { ok: res.code === 0, ...res };
    } finally {
      this._activeAction = null;
    }
  }

  async getConfigSnippet(opts = {}) {
    const args = ["config", "--transport", opts.transport === "http" ? "http" : "stdio"];
    if (opts.root) args.push("--root", opts.root);
    if (opts.key) args.push("--key", opts.key);
    if (opts.timeoutMs) args.push("--timeout-ms", String(opts.timeoutMs));
    const res = await this._runPython(this._installScript(), args, { timeoutMs: 20000 });
    return { ok: res.code === 0, snippet: res.stdout, stderr: res.stderr };
  }

  async writeClientConfig(opts = {}) {
    if (!opts.filePath) {
      return { ok: false, message: "请选择目标 JSON 配置文件" };
    }
    const args = [
      "config",
      "--transport",
      opts.transport === "http" ? "http" : "stdio",
      "--write-config",
      opts.filePath,
    ];
    if (opts.root) args.push("--root", opts.root);
    if (opts.key) args.push("--key", opts.key);
    if (opts.timeoutMs) args.push("--timeout-ms", String(opts.timeoutMs));
    if (opts.dryRun) args.push("--dry-run");
    const res = await this._runPython(this._installScript(), args, { timeoutMs: 20000 });
    return { ok: res.code === 0, output: (res.stdout + "\n" + res.stderr).trim() };
  }

  listJobs(limit = 30) {
    const jobsDir = path.join(this.resolveStateHome(), "jobs");
    if (!fs.existsSync(jobsDir)) return [];
    const entries = fs
      .readdirSync(jobsDir, { withFileTypes: true })
      .filter((d) => d.isDirectory() && !d.name.startsWith(".") && !d.name.startsWith("_"))
      .map((d) => d.name)
      .sort()
      .reverse()
      .slice(0, limit);

    const rows = [];
    for (const id of entries) {
      const metaFile = path.join(jobsDir, id, "meta.json");
      try {
        const meta = JSON.parse(fs.readFileSync(metaFile, "utf8"));
        rows.push(meta);
      } catch {
        rows.push({ job_id: id, status: "unknown" });
      }
    }
    return rows;
  }

  async getJobDetail(jobId) {
    const safeId = String(jobId || "").trim();
    if (!/^[A-Za-z0-9._-]+$/.test(safeId)) {
      return { ok: false, error: "Invalid job_id" };
    }
    const jobDir = path.join(this.resolveStateHome(), "jobs", safeId);
    if (!fs.existsSync(jobDir)) {
      return { ok: false, error: `Job not found: ${safeId}` };
    }
    let meta = {};
    try {
      meta = JSON.parse(fs.readFileSync(path.join(jobDir, "meta.json"), "utf8"));
    } catch {
      meta = { job_id: safeId };
    }
    let journal = "";
    try {
      journal = fs.readFileSync(path.join(jobDir, "journal.log"), "utf8");
    } catch {
      journal = "";
    }
    let diagnosis = null;
    const errFile = path.join(jobDir, "error.json");
    if (fs.existsSync(errFile)) {
      try {
        diagnosis = JSON.parse(fs.readFileSync(errFile, "utf8"));
      } catch {
        diagnosis = null;
      }
    }
    if (!diagnosis && (meta.status === "failed" || journal.includes("r("))) {
      const res = await this._runPython(
        this._analysisScript(),
        ["diagnose", "--job-dir", jobDir, "--json"],
        { timeoutMs: 15000 },
      );
      try {
        const parsed = JSON.parse(res.stdout);
        if (parsed.found) diagnosis = parsed.diagnosis;
      } catch {
        // ignore
      }
    }
    return { ok: true, meta, journal, diagnosis };
  }

  async cancelJob(jobId) {
    const state = this._loadState();
    const host = state.host || "127.0.0.1";
    const enginePort = state.mode === "unified" ? Number(state.port || 4001) : Number(state.engine_port || state.port || 4001);
    return new Promise((resolve) => {
      const req = http.request(
        { host, port: enginePort, path: "/stop_execution", method: "POST", timeout: 5000 },
        (res) => {
          const chunks = [];
          res.on("data", (c) => chunks.push(c));
          res.on("end", () => {
            resolve({ ok: res.statusCode < 400, statusCode: res.statusCode, body: Buffer.concat(chunks).toString("utf8"), jobId });
          });
        },
      );
      req.on("error", (err) => resolve({ ok: false, error: err.message }));
      req.end();
    });
  }

  async lookupHelp({ topic = "", section = "" } = {}) {
    const state = this._loadState();
    const args = ["help", String(topic || "").trim(), "--json"];
    if (state.stata_path) args.push("--stata-path", state.stata_path);
    if (section) args.push("--section", section);
    const res = await this._runPython(this._analysisScript(), args, { timeoutMs: 15000 });
    try {
      return JSON.parse(res.stdout);
    } catch {
      return { found: false, topic, error: res.stderr || res.stdout };
    }
  }

  async searchHelp({ query = "", limit = 20 } = {}) {
    const state = this._loadState();
    const args = ["search", String(query || "").trim(), "--limit", String(limit), "--json"];
    if (state.stata_path) args.push("--stata-path", state.stata_path);
    const res = await this._runPython(this._analysisScript(), args, { timeoutMs: 20000 });
    try {
      return JSON.parse(res.stdout);
    } catch {
      return { query, count: 0, results: [], error: res.stderr };
    }
  }

  async lintDo({ filePath = "", code = "", strict = false } = {}) {
    const args = ["lint", "--json"];
    if (strict) args.push("--strict");
    if (filePath) {
      args.push("--file", filePath);
    } else {
      args.push("--stdin");
    }
    const res = await this._runPython(this._analysisScript(), args, {
      stdinText: filePath ? null : String(code || ""),
      timeoutMs: 15000,
    });
    try {
      return JSON.parse(res.stdout);
    } catch {
      return { summary: res.stderr || "lint failed", issues: [] };
    }
  }

  async vectorizeGuide({ pattern = "", filePath = "" } = {}) {
    const args = ["vectorize", "--json"];
    if (pattern) args.push("--pattern", pattern);
    if (filePath) args.push("--file", filePath);
    const res = await this._runPython(this._analysisScript(), args, { timeoutMs: 15000 });
    try {
      return JSON.parse(res.stdout);
    } catch {
      return { available_patterns: [], recipes: [] };
    }
  }

  readLogTail(logName = "gateway.log", lines = 120) {
    const allowed = new Set(["gateway.log", "server.log", "gateway.console.log", "server.console.log"]);
    const target = allowed.has(logName) ? logName : "gateway.log";
    const full = path.join(this.resolveStateHome(), "logs", target);
    if (!fs.existsSync(full)) {
      return { exists: false, path: full, content: "(日志文件尚不存在)" };
    }
    try {
      const all = fs.readFileSync(full, "utf8").split(/\r?\n/);
      return { exists: true, path: full, content: all.slice(-lines).join("\n") };
    } catch (err) {
      return { exists: false, path: full, content: err.message };
    }
  }
}
