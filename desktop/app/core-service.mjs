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
      p.join(homeDir, ".npm-global", "bin"),
      p.join(homeDir, ".cargo", "bin"),
      "/Library/Frameworks/Python.framework/Versions/Current/bin",
      "/usr/bin",
      "/bin",
    );
  } else if (platform === "win32") {
    const localApp = env.LOCALAPPDATA || p.join(homeDir, "AppData", "Local");
    const roamingApp = env.APPDATA || p.join(homeDir, "AppData", "Roaming");
    extras.push(
      p.join(homeDir, ".local", "bin"),
      p.join(roamingApp, "npm"),
      p.join(localApp, "Programs", "Python", "Python313"),
      p.join(localApp, "Programs", "Python", "Python312"),
      p.join(localApp, "Programs", "Python", "Python311"),
      p.join(localApp, "Programs", "Python", "Python310"),
      p.join(localApp, "Programs", "Python", "Python39"),
    );
  } else {
    extras.push("/usr/local/bin", p.join(homeDir, ".local", "bin"), p.join(homeDir, ".npm-global", "bin"));
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

export function posixSingleQuote(str) {
  return `'${String(str ?? "").replace(/'/g, "'\\''")}'`;
}

export function findExecutableInPath(
  binName,
  {
    platform = process.platform,
    env = process.env,
    homeDir = os.homedir(),
    existsSync = fs.existsSync,
  } = {},
) {
  const clean = String(binName || "").trim();
  if (!clean) return "";
  const augmented = buildAugmentedEnv({ platform, env, homeDir });
  const p = platform === "win32" ? path.win32 : path.posix;
  const sep = platform === "win32" ? ";" : ":";
  const dirs = String(augmented.PATH || "").split(sep).filter(Boolean);
  const candidates =
    platform === "win32" && !/\.(exe|cmd|bat)$/i.test(clean)
      ? [clean, `${clean}.exe`, `${clean}.cmd`, `${clean}.bat`]
      : [clean];
  for (const dir of dirs) {
    for (const file of candidates) {
      const full = p.join(dir, file);
      try {
        if (existsSync(full)) return full;
      } catch {
        // ignore invalid path entry
      }
    }
  }
  return "";
}

export function detectInstalledAgents({
  platform = process.platform,
  env = process.env,
  homeDir = os.homedir(),
  existsSync = fs.existsSync,
} = {}) {
  const p = platform === "win32" ? path.win32 : path.posix;
  const localApp = env.LOCALAPPDATA || p.join(homeDir, "AppData", "Local");
  const programFiles = env.ProgramFiles || "C:\\Program Files";

  const firstExisting = (pathsList) => {
    for (const item of pathsList) {
      if (!item) continue;
      try {
        if (existsSync(item)) return item;
      } catch {
        // ignore
      }
    }
    return "";
  };

  const resolveCli = (bins) => {
    for (const b of bins) {
      const hit = findExecutableInPath(b, { platform, env, homeDir, existsSync });
      if (hit) return hit;
    }
    return "";
  };

  const macAppPaths = (appName) => [
    p.join("/Applications", appName),
    p.join(homeDir, "Applications", appName),
  ];

  const defs = [
    {
      id: "claude",
      name: "Claude Code",
      kind: "cli",
      desc: "终端打开目录并运行 claude",
      bins: ["claude"],
      command: "claude",
    },
    {
      id: "codex",
      name: "Codex CLI",
      kind: "cli",
      desc: "终端打开目录并运行 codex",
      bins: ["codex"],
      command: "codex",
    },
    {
      id: "gemini",
      name: "Gemini CLI",
      kind: "cli",
      desc: "终端打开目录并运行 gemini",
      bins: ["gemini"],
      command: "gemini",
    },
    {
      id: "antigravity",
      name: "Antigravity",
      kind: "app",
      desc: "使用 Antigravity 打开科研目录",
      bins: ["agy", "antigravity"],
      apps:
        platform === "darwin"
          ? macAppPaths("Antigravity.app")
          : platform === "win32"
            ? [
                p.join(localApp, "Programs", "Antigravity", "Antigravity.exe"),
                p.join(programFiles, "Antigravity", "Antigravity.exe"),
              ]
            : [],
    },
    {
      id: "cursor",
      name: "Cursor",
      kind: "app",
      desc: "使用 Cursor 打开科研目录",
      bins: ["cursor"],
      apps:
        platform === "darwin"
          ? macAppPaths("Cursor.app")
          : platform === "win32"
            ? [
                p.join(localApp, "Programs", "cursor", "Cursor.exe"),
                p.join(localApp, "Programs", "Cursor", "Cursor.exe"),
              ]
            : [],
    },
    {
      id: "vscode",
      name: "VS Code",
      kind: "app",
      desc: "使用 VS Code 打开科研目录",
      bins: ["code"],
      apps:
        platform === "darwin"
          ? macAppPaths("Visual Studio Code.app")
          : platform === "win32"
            ? [
                p.join(localApp, "Programs", "Microsoft VS Code", "Code.exe"),
                p.join(programFiles, "Microsoft VS Code", "Code.exe"),
              ]
            : [],
    },
    {
      id: "windsurf",
      name: "Windsurf",
      kind: "app",
      desc: "使用 Windsurf 打开科研目录",
      bins: ["windsurf"],
      apps:
        platform === "darwin"
          ? macAppPaths("Windsurf.app")
          : platform === "win32"
            ? [p.join(localApp, "Programs", "Windsurf", "Windsurf.exe")]
            : [],
    },
    {
      id: "trae",
      name: "Trae",
      kind: "app",
      desc: "使用 Trae 打开科研目录",
      bins: ["trae"],
      apps:
        platform === "darwin"
          ? macAppPaths("Trae.app")
          : platform === "win32"
            ? [p.join(localApp, "Programs", "Trae", "Trae.exe")]
            : [],
    },
    {
      id: "shell",
      name: "打开终端",
      kind: "cli",
      desc: "在当前目录打开所选默认终端",
      alwaysInstalled: true,
      command: "",
    },
  ];

  const items = defs.map((d) => {
    if (d.alwaysInstalled) {
      return {
        id: d.id,
        name: d.name,
        kind: d.kind,
        desc: d.desc,
        installed: true,
        target: "",
        command: d.command || "",
      };
    }
    const appTarget = d.apps ? firstExisting(d.apps) : "";
    const cliTarget = resolveCli(d.bins || []);
    const target = d.kind === "app" ? appTarget || cliTarget : cliTarget;
    return {
      id: d.id,
      name: d.name,
      kind: d.kind,
      desc: d.desc,
      installed: Boolean(target),
      target,
      command: d.kind === "cli" ? d.command || d.bins[0] : "",
    };
  });

  return items.sort((a, b) => Number(b.installed) - Number(a.installed));
}

export function detectInstalledTerminals({
  platform = process.platform,
  env = process.env,
  homeDir = os.homedir(),
  existsSync = fs.existsSync,
} = {}) {
  const p = platform === "win32" ? path.win32 : path.posix;
  const localApp = env.LOCALAPPDATA || p.join(homeDir, "AppData", "Local");
  const programFiles = env.ProgramFiles || "C:\\Program Files";
  const programFilesX86 = env["ProgramFiles(x86)"] || "C:\\Program Files (x86)";

  const firstExisting = (pathsList) => {
    for (const item of pathsList) {
      if (!item) continue;
      try {
        if (existsSync(item)) return item;
      } catch {
        // ignore
      }
    }
    return "";
  };

  const resolveCli = (bins) => {
    for (const b of bins) {
      const hit = findExecutableInPath(b, { platform, env, homeDir, existsSync });
      if (hit) return hit;
    }
    return "";
  };

  const macAppPaths = (appName) => [
    p.join("/Applications", appName),
    p.join(homeDir, "Applications", appName),
  ];

  let defs = [];
  if (platform === "darwin") {
    defs = [
      { id: "ghostty", name: "Ghostty", apps: macAppPaths("Ghostty.app") },
      { id: "iterm2", name: "iTerm2", apps: macAppPaths("iTerm.app") },
      { id: "wezterm", name: "WezTerm", apps: macAppPaths("WezTerm.app"), bins: ["wezterm"] },
      { id: "kitty", name: "Kitty", apps: macAppPaths("kitty.app"), bins: ["kitty"] },
      { id: "alacritty", name: "Alacritty", apps: macAppPaths("Alacritty.app"), bins: ["alacritty"] },
      { id: "warp", name: "Warp", apps: macAppPaths("Warp.app") },
      {
        id: "terminal",
        name: "Terminal.app（系统终端）",
        apps: ["/System/Applications/Utilities/Terminal.app"],
        alwaysInstalled: true,
        defaultTarget: "/System/Applications/Utilities/Terminal.app",
      },
    ];
  } else if (platform === "win32") {
    defs = [
      {
        id: "wt",
        name: "Windows Terminal",
        bins: ["wt.exe"],
        apps: [p.join(localApp, "Microsoft", "WindowsApps", "wt.exe")],
      },
      {
        id: "pwsh",
        name: "PowerShell 7",
        bins: ["pwsh.exe"],
        apps: [p.join(programFiles, "PowerShell", "7", "pwsh.exe")],
      },
      {
        id: "gitbash",
        name: "Git Bash",
        apps: [
          p.join(programFiles, "Git", "git-bash.exe"),
          p.join(programFilesX86, "Git", "git-bash.exe"),
          p.join(localApp, "Programs", "Git", "git-bash.exe"),
        ],
      },
      {
        id: "wezterm",
        name: "WezTerm",
        bins: ["wezterm-gui.exe", "wezterm.exe"],
        apps: [p.join(programFiles, "WezTerm", "wezterm-gui.exe")],
      },
      {
        id: "alacritty",
        name: "Alacritty",
        bins: ["alacritty.exe"],
        apps: [p.join(programFiles, "Alacritty", "alacritty.exe")],
      },
      {
        id: "powershell",
        name: "Windows PowerShell",
        bins: ["powershell.exe"],
        alwaysInstalled: true,
        defaultTarget: "powershell.exe",
      },
      {
        id: "cmd",
        name: "命令提示符 (CMD)",
        bins: ["cmd.exe"],
        alwaysInstalled: true,
        defaultTarget: "cmd.exe",
      },
    ];
  } else {
    defs = [
      { id: "ghostty", name: "Ghostty", bins: ["ghostty"] },
      { id: "wezterm", name: "WezTerm", bins: ["wezterm"] },
      { id: "kitty", name: "Kitty", bins: ["kitty"] },
      { id: "alacritty", name: "Alacritty", bins: ["alacritty"] },
      { id: "gnome-terminal", name: "GNOME Terminal", bins: ["gnome-terminal"] },
      { id: "konsole", name: "Konsole", bins: ["konsole"] },
      { id: "xfce4-terminal", name: "XFCE Terminal", bins: ["xfce4-terminal"] },
      { id: "x-terminal-emulator", name: "系统默认终端", bins: ["x-terminal-emulator"] },
      { id: "xterm", name: "XTerm", bins: ["xterm"] },
    ];
  }

  const items = defs.map((d) => {
    const appTarget = d.apps ? firstExisting(d.apps) : "";
    const cliTarget = d.bins ? resolveCli(d.bins) : "";
    const target = appTarget || cliTarget || d.defaultTarget || "";
    const installed = Boolean(d.alwaysInstalled || appTarget || cliTarget);
    return {
      id: d.id,
      name: d.name,
      installed,
      target,
      platform,
    };
  });

  const installedItems = items.filter((i) => i.installed);
  const missingItems = items.filter((i) => !i.installed);
  return [...installedItems, ...missingItems];
}

export function buildTerminalLaunchPlan({
  platform = process.platform,
  terminal,
  targetDir,
  cmdToRun = "",
  env = process.env,
  isAppRunning = false,
  hasGhosttySdef = false,
  preferredPowerShell = "powershell.exe",
  minttyPath = "",
  gitBashBin = "",
} = {}) {
  const cleanCmd = String(cmdToRun || "").trim();
  const termId = terminal?.id || "terminal";
  const termTarget = terminal?.target || "";

  if (platform === "darwin") {
    const userShell = (env.SHELL || "").trim() || "/bin/zsh";
    const shellBase = path.posix.basename(userShell);
    const shellFlags = shellBase === "fish" ? ["-l", "-c"] : ["-i", "-l", "-c"];
    const wrappedCmd = `cd ${posixSingleQuote(targetDir)} && ${cleanCmd}; exec ${posixSingleQuote(userShell)} -l`;

    if (termId === "ghostty") {
      const appPath = termTarget || "/Applications/Ghostty.app";
      const openNaPlan = {
        command: "open",
        args: cleanCmd
          ? ["-na", appPath, "--args", `--working-directory=${targetDir}`, "-e", userShell, ...shellFlags, wrappedCmd]
          : ["-na", appPath, "--args", `--working-directory=${targetDir}`],
        spawnOpts: {},
        mode: "detached",
      };
      if (isAppRunning && hasGhosttySdef) {
        const script = [
          "on run argv",
          "  set targetDir to item 1 of argv",
          "  set cmdText to item 2 of argv",
          '  tell application "Ghostty"',
          "    set cfg to new surface configuration",
          "    set initial working directory of cfg to targetDir",
          '    if cmdText is not "" then',
          "      set initial input of cfg to (cmdText & linefeed)",
          "    end if",
          "    new window with configuration cfg",
          "    activate",
          "  end tell",
          "end run",
        ].join("\n");
        return {
          command: "osascript",
          args: ["-e", script, targetDir, cleanCmd],
          spawnOpts: {},
          mode: "control",
          fallback: openNaPlan,
        };
      }
      return openNaPlan;
    }

    if (termId === "terminal") {
      const shellCmd = cleanCmd
        ? `cd ${posixSingleQuote(targetDir)} && ${cleanCmd}`
        : `cd ${posixSingleQuote(targetDir)}`;
      const script = [
        "on run argv",
        "  set cmdText to item 1 of argv",
        '  set wasRunning to (application "Terminal" is running)',
        '  tell application "Terminal"',
        "    if not wasRunning then",
        "      launch",
        "      repeat 10 times",
        "        if (count of windows) > 0 then exit repeat",
        "        delay 0.05",
        "      end repeat",
        "      if (count of windows) > 0 then",
        "        do script cmdText in front window",
        "      else",
        "        do script cmdText",
        "      end if",
        "      activate",
        "    else",
        "      do script cmdText",
        "      activate",
        "    end if",
        "  end tell",
        "end run",
      ].join("\n");
      return {
        command: "osascript",
        args: ["-e", script, shellCmd],
        spawnOpts: {},
        mode: "control",
      };
    }

    if (termId === "iterm2") {
      const shellCmd = cleanCmd
        ? `cd ${posixSingleQuote(targetDir)} && ${cleanCmd}`
        : `cd ${posixSingleQuote(targetDir)}`;
      const script = [
        "on run argv",
        "  set cmdText to item 1 of argv",
        '  set wasRunning to (application "iTerm" is running)',
        '  tell application "iTerm"',
        "    if not wasRunning then",
        "      launch",
        "      repeat 10 times",
        "        if (count of windows) > 0 then exit repeat",
        "        delay 0.05",
        "      end repeat",
        "      if (count of windows) > 0 then",
        "        tell current session of current window to write text cmdText",
        "      else",
        "        set newWin to (create window with default profile)",
        "        tell current session of newWin to write text cmdText",
        "      end if",
        "      activate",
        "    else",
        "      set newWin to (create window with default profile)",
        "      tell current session of newWin to write text cmdText",
        "      activate",
        "    end if",
        "  end tell",
        "end run",
      ].join("\n");
      return {
        command: "osascript",
        args: ["-e", script, shellCmd],
        spawnOpts: {},
        mode: "control",
      };
    }

    if (termId === "wezterm") {
      const bin = termTarget.endsWith(".app")
        ? path.posix.join(termTarget, "Contents", "MacOS", "wezterm")
        : termTarget || "wezterm";
      return {
        command: bin,
        args: cleanCmd
          ? ["start", "--cwd", targetDir, "--", userShell, ...shellFlags, wrappedCmd]
          : ["start", "--cwd", targetDir],
        spawnOpts: {},
        mode: "detached",
      };
    }

    if (termId === "kitty") {
      const bin = termTarget.endsWith(".app")
        ? path.posix.join(termTarget, "Contents", "MacOS", "kitty")
        : termTarget || "kitty";
      return {
        command: bin,
        args: cleanCmd
          ? ["--directory", targetDir, userShell, ...shellFlags, wrappedCmd]
          : ["--directory", targetDir],
        spawnOpts: {},
        mode: "detached",
      };
    }

    if (termId === "alacritty") {
      const bin = termTarget.endsWith(".app")
        ? path.posix.join(termTarget, "Contents", "MacOS", "alacritty")
        : termTarget || "alacritty";
      return {
        command: bin,
        args: cleanCmd
          ? ["--working-directory", targetDir, "-e", userShell, ...shellFlags, wrappedCmd]
          : ["--working-directory", targetDir],
        spawnOpts: {},
        mode: "detached",
      };
    }

    if (termId === "warp") {
      if (!cleanCmd) {
        return {
          command: "open",
          args: ["-a", termTarget || "/Applications/Warp.app", targetDir],
          spawnOpts: {},
          mode: "detached",
        };
      }
      const warpLaunchYaml = [
        "name: stata-mcp-launch",
        "windows:",
        "  - tabs:",
        "      - layout:",
        `          cwd: ${JSON.stringify(targetDir)}`,
        "          commands:",
        `            - exec: ${JSON.stringify(cleanCmd)}`,
        "",
      ].join("\n");
      return {
        command: "open",
        args: ["warp://launch/stata-mcp-launch.yaml"],
        spawnOpts: {},
        mode: "detached",
        warpLaunchYaml,
      };
    }
  }

  if (platform === "win32") {
    const psEscapedDir = targetDir.replace(/'/g, "''");
    const psCmd = cleanCmd
      ? `Set-Location -LiteralPath '${psEscapedDir}'; ${cleanCmd}`
      : `Set-Location -LiteralPath '${psEscapedDir}'`;

    if (termId === "wt") {
      return {
        command: termTarget || "wt.exe",
        args: cleanCmd
          ? ["-d", targetDir, preferredPowerShell, "-NoExit", "-Command", psCmd]
          : ["-d", targetDir],
        spawnOpts: { windowsHide: false },
        mode: "detached",
      };
    }

    if (termId === "pwsh") {
      return {
        command: "cmd.exe",
        args: ["/c", "start", "", "/D", targetDir, termTarget || "pwsh.exe", "-NoExit", "-Command", psCmd],
        spawnOpts: { cwd: targetDir, windowsHide: true },
        mode: "detached",
      };
    }

    if (termId === "powershell") {
      return {
        command: "cmd.exe",
        args: ["/c", "start", "", "/D", targetDir, "powershell.exe", "-NoExit", "-Command", psCmd],
        spawnOpts: { cwd: targetDir, windowsHide: true },
        mode: "detached",
      };
    }

    if (termId === "gitbash") {
      if (cleanCmd && minttyPath) {
        return {
          command: minttyPath,
          args: ["-e", gitBashBin || "/bin/bash", "-lic", `${cleanCmd}; exec bash -i`],
          spawnOpts: { cwd: targetDir, windowsHide: false },
          mode: "detached",
        };
      }
      if (cleanCmd) {
        return {
          command: termTarget,
          args: [`--cd=${targetDir}`, "-c", `${cleanCmd}; exec bash -i`],
          spawnOpts: { cwd: targetDir, windowsHide: false },
          mode: "detached",
        };
      }
      return {
        command: termTarget,
        args: [`--cd=${targetDir}`],
        spawnOpts: { cwd: targetDir, windowsHide: false },
        mode: "detached",
      };
    }

    if (termId === "wezterm") {
      return {
        command: termTarget,
        args: cleanCmd
          ? ["start", "--cwd", targetDir, "--", preferredPowerShell, "-NoExit", "-Command", psCmd]
          : ["start", "--cwd", targetDir],
        spawnOpts: { windowsHide: false },
        mode: "detached",
      };
    }

    if (termId === "alacritty") {
      return {
        command: termTarget,
        args: cleanCmd
          ? ["--working-directory", targetDir, "-e", preferredPowerShell, "-NoExit", "-Command", psCmd]
          : ["--working-directory", targetDir],
        spawnOpts: { windowsHide: false },
        mode: "detached",
      };
    }

    // cmd
    return {
      command: "cmd.exe",
      args: cleanCmd
        ? ["/c", "start", "", "/D", targetDir, "cmd.exe", "/K", cleanCmd]
        : ["/c", "start", "", "/D", targetDir, "cmd.exe"],
      spawnOpts: { cwd: targetDir, windowsHide: true },
      mode: "detached",
    };
  }

  // Linux fallback & modern terminals
  const userShell = (env.SHELL || "").trim() || "/bin/bash";
  const shellCmd = cleanCmd
    ? `cd ${posixSingleQuote(targetDir)} && ${cleanCmd}; exec ${posixSingleQuote(userShell)}`
    : `cd ${posixSingleQuote(targetDir)}; exec ${posixSingleQuote(userShell)}`;
  const bin = termTarget || termId;

  if (termId === "ghostty") {
    return {
      command: bin,
      args: cleanCmd
        ? [`--working-directory=${targetDir}`, "-e", "bash", "-lc", shellCmd]
        : [`--working-directory=${targetDir}`],
      spawnOpts: {},
      mode: "detached",
    };
  }
  if (termId === "wezterm") {
    return {
      command: bin,
      args: cleanCmd
        ? ["start", "--cwd", targetDir, "--", "bash", "-lc", shellCmd]
        : ["start", "--cwd", targetDir],
      spawnOpts: {},
      mode: "detached",
    };
  }
  if (termId === "kitty") {
    return {
      command: bin,
      args: cleanCmd ? ["--directory", targetDir, "bash", "-lc", shellCmd] : ["--directory", targetDir],
      spawnOpts: {},
      mode: "detached",
    };
  }
  if (termId === "alacritty") {
    return {
      command: bin,
      args: cleanCmd
        ? ["--working-directory", targetDir, "-e", "bash", "-lc", shellCmd]
        : ["--working-directory", targetDir],
      spawnOpts: {},
      mode: "detached",
    };
  }
  if (termId === "gnome-terminal") {
    return {
      command: bin,
      args: [`--working-directory=${targetDir}`, "--", "bash", "-lc", shellCmd],
      spawnOpts: {},
      mode: "detached",
    };
  }
  if (termId === "konsole") {
    return {
      command: bin,
      args: ["--workdir", targetDir, "-e", "bash", "-lc", shellCmd],
      spawnOpts: {},
      mode: "detached",
    };
  }
  if (termId === "xfce4-terminal") {
    return {
      command: bin,
      args: [`--working-directory=${targetDir}`, "-e", `bash -lc ${posixSingleQuote(shellCmd)}`],
      spawnOpts: {},
      mode: "detached",
    };
  }
  return {
    command: bin,
    args: ["-e", "bash", "-lc", shellCmd],
    spawnOpts: {},
    mode: "detached",
  };
}

export function inspectResearchFolder(dirPath) {
  const raw = String(dirPath || "").trim();
  if (!raw) {
    return { ok: false, error: "请选择项目文件夹" };
  }
  const resolved = path.resolve(raw);
  try {
    const st = fs.statSync(resolved);
    if (!st.isDirectory()) {
      return { ok: false, error: "所选路径不是文件夹" };
    }
  } catch {
    return { ok: false, error: "文件夹不存在或无权访问" };
  }

  const skipDirs = new Set([".git", "node_modules", "venv", ".venv", "__pycache__", ".stata-mcp"]);
  const doFiles = [];
  const dtaFiles = [];
  let doCount = 0;
  let dtaCount = 0;
  let visited = 0;
  const maxEntries = 600;

  const scan = (currentDir, relBase, depth) => {
    if (visited >= maxEntries) return;
    let entries = [];
    try {
      entries = fs.readdirSync(currentDir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const ent of entries) {
      if (visited >= maxEntries) break;
      visited += 1;
      const rel = relBase ? `${relBase}/${ent.name}` : ent.name;
      if (ent.isDirectory()) {
        if (depth < 2 && !skipDirs.has(ent.name)) {
          scan(path.join(currentDir, ent.name), rel, depth + 1);
        }
      } else if (ent.isFile()) {
        const lower = ent.name.toLowerCase();
        if (lower.endsWith(".do")) {
          doCount += 1;
          if (doFiles.length < 6) doFiles.push(rel);
        } else if (lower.endsWith(".dta")) {
          dtaCount += 1;
          if (dtaFiles.length < 6) dtaFiles.push(rel);
        }
      }
    }
  };
  scan(resolved, "", 0);

  const mcpJsonPath = path.join(resolved, ".mcp.json");
  const hasMcpJson = fs.existsSync(mcpJsonPath);
  let hasStataMcpEntry = false;
  if (hasMcpJson) {
    try {
      const parsed = JSON.parse(fs.readFileSync(mcpJsonPath, "utf8"));
      hasStataMcpEntry = Boolean(parsed?.mcpServers && parsed.mcpServers["stata-mcp"]);
    } catch {
      hasStataMcpEntry = false;
    }
  }

  return {
    ok: true,
    path: resolved,
    name: path.basename(resolved) || resolved,
    doCount,
    dtaCount,
    doFiles,
    dtaFiles,
    hasMcpJson,
    hasStataMcpEntry,
  };
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

  readLogTail(logName = "server.log", lines = 120) {
    const allowed = new Set(["gateway.log", "server.log", "gateway.console.log", "server.console.log"]);
    const target = allowed.has(logName) ? logName : "server.log";
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

  detectAgents() {
    return detectInstalledAgents({
      platform: this.platform,
      env: this.env,
    });
  }

  detectTerminals() {
    return detectInstalledTerminals({
      platform: this.platform,
      env: this.env,
    });
  }

  inspectProjectDir(folderPath) {
    return inspectResearchFolder(folderPath);
  }

  _runControlCommand(command, args, { timeoutMs = 6000 } = {}) {
    return new Promise((resolve) => {
      try {
        const child = spawn(command, args, {
          env: this.env,
          windowsHide: true,
          stdio: ["ignore", "pipe", "pipe"],
        });
        let stdout = "";
        let stderr = "";
        let finished = false;
        const timer = setTimeout(() => {
          if (finished) return;
          finished = true;
          this._killChildTree(child);
          resolve({ ok: false, code: -1, stdout, stderr: stderr || "Command timed out" });
        }, timeoutMs);
        child.stdout.on("data", (c) => {
          stdout += c.toString("utf8");
        });
        child.stderr.on("data", (c) => {
          stderr += c.toString("utf8");
        });
        child.on("error", (err) => {
          if (finished) return;
          finished = true;
          clearTimeout(timer);
          resolve({ ok: false, code: -1, stdout, stderr: err.message });
        });
        child.on("close", (code) => {
          if (finished) return;
          finished = true;
          clearTimeout(timer);
          resolve({ ok: (code ?? 0) === 0, code: code ?? 0, stdout, stderr: stderr.trim() });
        });
      } catch (err) {
        resolve({ ok: false, code: -1, stdout: "", stderr: err.message });
      }
    });
  }

  _spawnDetached(command, args, opts = {}) {
    return new Promise((resolve) => {
      try {
        const child = spawn(command, args, {
          detached: true,
          stdio: "ignore",
          env: this.env,
          ...opts,
        });
        let settled = false;
        child.once("error", (err) => {
          if (settled) return;
          settled = true;
          resolve({ ok: false, error: err.message });
        });
        child.once("spawn", () => {
          if (settled) return;
          settled = true;
          child.unref();
          resolve({ ok: true });
        });
      } catch (err) {
        resolve({ ok: false, error: err.message });
      }
    });
  }

  async launchAgentInFolder({
    folderPath,
    agentId = "shell",
    customCommand = "",
    terminalId = "",
    ensureService = true,
  } = {}) {
    const info = inspectResearchFolder(folderPath);
    if (!info.ok) {
      return { ok: false, error: info.error };
    }
    const targetDir = info.path;
    let serviceStarted = false;

    if (ensureService) {
      const state = this._loadState();
      const host = state.host || "127.0.0.1";
      const port = Number(state.port || 4001);
      const healthy = await httpGetJson(host, port, "/health", 1500);
      if (!healthy) {
        const stateFile = path.join(this.resolveStateHome(), "state.json");
        if (!fs.existsSync(stateFile)) {
          return {
            ok: false,
            needsInstall: true,
            error: "Stata MCP 运行时尚未安装，请先在「配置」页点击「一键安装」。",
          };
        }
        const startRes = await this.runAction("start");
        if (!startRes.ok) {
          return {
            ok: false,
            error: `自动启动 Stata MCP 服务失败：${startRes.stderr || startRes.stdout || "请查看控制台日志"}`,
          };
        }
        serviceStarted = true;
      }
    }

    const agents = this.detectAgents();
    const matched = agents.find((a) => a.id === agentId);
    const cleanCustom = String(customCommand || "").trim();

    if (matched && matched.kind === "app") {
      if (!matched.installed || !matched.target) {
        return { ok: false, error: `未在本机检测到 ${matched.name}` };
      }
      let launchRes;
      if (this.platform === "darwin") {
        if (matched.target.endsWith(".app")) {
          launchRes = await this._spawnDetached("open", ["-a", matched.target, targetDir]);
        } else {
          launchRes = await this._spawnDetached(matched.target, [targetDir]);
        }
      } else if (this.platform === "win32") {
        if (/\.exe$/i.test(matched.target)) {
          launchRes = await this._spawnDetached(matched.target, [targetDir], { windowsHide: false });
        } else {
          launchRes = await this._spawnDetached("cmd.exe", ["/c", "start", "", matched.target, targetDir], {
            windowsHide: true,
          });
        }
      } else {
        launchRes = await this._spawnDetached(matched.target, [targetDir]);
      }
      if (!launchRes.ok) return launchRes;
      return { ok: true, launched: matched.name, folderPath: targetDir, serviceStarted };
    }

    const cmdToRun =
      agentId === "custom"
        ? cleanCustom
        : matched?.id === "shell"
          ? ""
          : matched?.command || "";
    if (agentId === "custom" && !cmdToRun) {
      return { ok: false, error: "请输入要执行的自定义终端命令" };
    }
    if (matched && matched.kind === "cli" && matched.id !== "shell" && !matched.installed) {
      return { ok: false, error: `未在 PATH 中检测到 ${matched.name} (${matched.command})` };
    }

    const terminals = this.detectTerminals();
    const installedTerminals = terminals.filter((t) => t.installed);
    const selectedTerminal =
      (terminalId ? installedTerminals.find((t) => t.id === terminalId) : null) || installedTerminals[0];
    if (!selectedTerminal) {
      return { ok: false, error: "未检测到可用的终端应用" };
    }

    let isAppRunning = false;
    let hasGhosttySdef = false;
    if (this.platform === "darwin" && selectedTerminal.id === "ghostty") {
      const pgrepRes = await this._runControlCommand("pgrep", ["-x", "ghostty"], { timeoutMs: 2000 });
      isAppRunning = pgrepRes.ok;
      hasGhosttySdef = Boolean(
        selectedTerminal.target &&
          fs.existsSync(path.join(selectedTerminal.target, "Contents", "Resources", "Ghostty.sdef")),
      );
    }

    let preferredPowerShell = "powershell.exe";
    let minttyPath = "";
    let gitBashBin = "";
    if (this.platform === "win32") {
      const pwshTerm = installedTerminals.find((t) => t.id === "pwsh");
      if (pwshTerm?.target) preferredPowerShell = pwshTerm.target;
      if (selectedTerminal.id === "gitbash" && selectedTerminal.target) {
        const gitRoot = path.win32.dirname(selectedTerminal.target);
        const candMintty = path.win32.join(gitRoot, "usr", "bin", "mintty.exe");
        const candBash = path.win32.join(gitRoot, "bin", "bash.exe");
        if (fs.existsSync(candMintty)) minttyPath = candMintty;
        if (fs.existsSync(candBash)) gitBashBin = candBash;
      }
    }

    const plan = buildTerminalLaunchPlan({
      platform: this.platform,
      terminal: selectedTerminal,
      targetDir,
      cmdToRun,
      env: this.env,
      isAppRunning,
      hasGhosttySdef,
      preferredPowerShell,
      minttyPath,
      gitBashBin,
    });

    if (plan.warpLaunchYaml) {
      const warpCfgDir = path.join(os.homedir(), ".warp", "launch_configurations");
      fs.mkdirSync(warpCfgDir, { recursive: true });
      fs.writeFileSync(path.join(warpCfgDir, "stata-mcp-launch.yaml"), plan.warpLaunchYaml, "utf8");
    }

    let termRes;
    if (plan.mode === "control") {
      const ctrlRes = await this._runControlCommand(plan.command, plan.args);
      if (ctrlRes.ok) {
        termRes = { ok: true };
      } else if (plan.fallback) {
        termRes = await this._spawnDetached(plan.fallback.command, plan.fallback.args, plan.fallback.spawnOpts);
      } else {
        termRes = { ok: false, error: ctrlRes.stderr || "拉起终端失败" };
      }
    } else {
      termRes = await this._spawnDetached(plan.command, plan.args, plan.spawnOpts);
    }

    if (!termRes.ok) return termRes;
    return {
      ok: true,
      launched: agentId === "custom" ? cmdToRun : matched?.name || selectedTerminal.name,
      terminalId: selectedTerminal.id,
      terminalName: selectedTerminal.name,
      folderPath: targetDir,
      serviceStarted,
    };
  }
}
