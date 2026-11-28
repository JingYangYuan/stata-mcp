import { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, nativeImage, nativeTheme, shell, Tray } from "electron";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { CoreService } from "./core-service.mjs";

const appDir = path.dirname(fileURLToPath(import.meta.url));
let mainWindow = null;
let tray = null;
let service = null;
let isQuitting = false;

function broadcastLog(payload) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("core:log", payload);
  }
}

function broadcastStateChanged() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("core:state-changed");
  }
}

function createWindow() {
  const iconPath = path.resolve(appDir, "..", "assets", "icon.png");
  const isMac = process.platform === "darwin";
  if (!isMac) {
    Menu.setApplicationMenu(null);
  }
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 820,
    minWidth: 960,
    minHeight: 660,
    show: false,
    autoHideMenuBar: !isMac,
    title: "Stata MCP Studio",
    backgroundColor: nativeTheme.shouldUseDarkColors ? "#131314" : "#f8f9fa",
    icon: iconPath,
    ...(isMac ? { titleBarStyle: "hiddenInset", trafficLightPosition: { x: 16, y: 16 } } : {}),
    webPreferences: {
      preload: path.join(appDir, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  if (!isMac) {
    mainWindow.setMenuBarVisibility(false);
  }
  mainWindow.once("ready-to-show", () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.show();
    }
  });
  mainWindow.loadFile(path.join(appDir, "renderer", "index.html"));
  mainWindow.on("close", (event) => {
    if (!isQuitting && tray && process.platform === "darwin") {
      event.preventDefault();
      mainWindow.hide();
    }
  });
}

function createTray() {
  try {
    const iconPath = path.resolve(appDir, "..", "assets", "icon.png");
    const img = nativeImage.createFromPath(iconPath).resize({ width: 18, height: 18 });
    tray = new Tray(img);
    tray.setToolTip("Stata MCP Studio");
    const menu = Menu.buildFromTemplate([
      {
        label: "显示主窗口",
        click: () => {
          if (mainWindow) {
            mainWindow.show();
            mainWindow.focus();
          }
        },
      },
      { type: "separator" },
      {
        label: "启动后台网关与引擎",
        click: async () => {
          await service?.runAction("start");
          broadcastStateChanged();
        },
      },
      {
        label: "停止后台服务",
        click: async () => {
          await service?.runAction("stop");
          broadcastStateChanged();
        },
      },
      { type: "separator" },
      {
        label: "退出 Stata MCP Studio",
        click: () => {
          isQuitting = true;
          app.quit();
        },
      },
    ]);
    tray.setContextMenu(menu);
    tray.on("click", () => {
      if (mainWindow) {
        if (mainWindow.isVisible()) mainWindow.focus();
        else mainWindow.show();
      }
    });
  } catch {
    tray = null;
  }
}

app.whenReady().then(() => {
  service = new CoreService({
    isPackaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
    appDir,
    emitLog: broadcastLog,
  });
  createWindow();
  createTray();

  app.on("activate", () => {
    if (mainWindow) {
      mainWindow.show();
      mainWindow.focus();
    } else if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("before-quit", () => {
  isQuitting = true;
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

ipcMain.handle("app:overview", (_e, opts) => service.getOverview(opts));
ipcMain.handle("app:run-action", (_e, action, opts) => service.runAction(action, opts));
ipcMain.handle("app:config-snippet", (_e, opts) => service.getConfigSnippet(opts));
ipcMain.handle("app:write-config", (_e, opts) => service.writeClientConfig(opts));
ipcMain.handle("app:list-jobs", (_e, limit) => service.listJobs(limit));
ipcMain.handle("app:job-detail", (_e, jobId) => service.getJobDetail(jobId));
ipcMain.handle("app:cancel-job", (_e, jobId) => service.cancelJob(jobId));
ipcMain.handle("app:help", (_e, opts) => service.lookupHelp(opts));
ipcMain.handle("app:search-help", (_e, opts) => service.searchHelp(opts));
ipcMain.handle("app:lint-do", (_e, opts) => service.lintDo(opts));
ipcMain.handle("app:vectorize-guide", (_e, opts) => service.vectorizeGuide(opts));
ipcMain.handle("app:read-log", (_e, logName, lines) => service.readLogTail(logName, lines));
ipcMain.handle("app:copy-text", (_e, text) => {
  clipboard.writeText(String(text || ""));
  return { ok: true };
});
ipcMain.handle("app:open-state-folder", async () => {
  const target = service.resolveStateHome();
  await shell.openPath(target);
  return { path: target };
});
ipcMain.handle("app:open-external", async (_e, rawUrl) => {
  const url = String(rawUrl || "").trim();
  if (!/^https?:\/\//i.test(url)) {
    throw new Error("Only http(s) URLs are allowed");
  }
  await shell.openExternal(url);
  return { ok: true, url };
});

ipcMain.handle("dialog:pick-stata-dir", async (event) => {
  const parent = BrowserWindow.fromWebContents(event.sender) || mainWindow;
  const res = await dialog.showOpenDialog(parent, {
    title: "选择 Stata 安装根目录",
    properties: ["openDirectory"],
  });
  return res.canceled ? "" : res.filePaths[0];
});

ipcMain.handle("dialog:pick-do-file", async (event) => {
  const parent = BrowserWindow.fromWebContents(event.sender) || mainWindow;
  const res = await dialog.showOpenDialog(parent, {
    title: "选择 Stata .do 文件",
    properties: ["openFile"],
    filters: [
      { name: "Stata Do File", extensions: ["do"] },
      { name: "All Files", extensions: ["*"] },
    ],
  });
  return res.canceled ? "" : res.filePaths[0];
});

ipcMain.handle("dialog:pick-config-file", async (event) => {
  const parent = BrowserWindow.fromWebContents(event.sender) || mainWindow;
  const res = await dialog.showOpenDialog(parent, {
    title: "选择 MCP 客户端 JSON 配置文件",
    properties: ["openFile"],
    filters: [
      { name: "JSON", extensions: ["json"] },
      { name: "All Files", extensions: ["*"] },
    ],
  });
  return res.canceled ? "" : res.filePaths[0];
});

ipcMain.handle("dialog:confirm", async (event, opts = {}) => {
  const parent = BrowserWindow.fromWebContents(event.sender) || mainWindow;
  const res = await dialog.showMessageBox(parent, {
    type: opts.type || "warning",
    title: opts.title || "确认操作",
    message: opts.message || "确定继续吗？",
    detail: opts.detail || "",
    buttons: [opts.confirmLabel || "确认", opts.cancelLabel || "取消"],
    defaultId: 1,
    cancelId: 1,
  });
  return res.response === 0;
});
