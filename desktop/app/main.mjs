import { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, nativeImage, shell, Tray } from "electron";
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

function createWindow() {
  const iconPath = path.resolve(appDir, "..", "assets", "icon.png");
  mainWindow = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 920,
    minHeight: 660,
    title: "Stata MCP Studio",
    icon: iconPath,
    webPreferences: {
      preload: path.join(appDir, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
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
        click: () => service?.runAction("start"),
      },
      {
        label: "停止后台服务",
        click: () => service?.runAction("stop"),
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
