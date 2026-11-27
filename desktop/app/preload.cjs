const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("stataMcpDesktop", {
  getOverview: (opts) => ipcRenderer.invoke("app:overview", opts),
  runAction: (action, opts) => ipcRenderer.invoke("app:run-action", action, opts),
  getConfigSnippet: (opts) => ipcRenderer.invoke("app:config-snippet", opts),
  writeClientConfig: (opts) => ipcRenderer.invoke("app:write-config", opts),
  listJobs: (limit) => ipcRenderer.invoke("app:list-jobs", limit),
  getJobDetail: (jobId) => ipcRenderer.invoke("app:job-detail", jobId),
  cancelJob: (jobId) => ipcRenderer.invoke("app:cancel-job", jobId),
  lookupHelp: (opts) => ipcRenderer.invoke("app:help", opts),
  searchHelp: (opts) => ipcRenderer.invoke("app:search-help", opts),
  lintDo: (opts) => ipcRenderer.invoke("app:lint-do", opts),
  vectorizeGuide: (opts) => ipcRenderer.invoke("app:vectorize-guide", opts),
  readLogTail: (logName, lines) => ipcRenderer.invoke("app:read-log", logName, lines),
  pickStataDir: () => ipcRenderer.invoke("dialog:pick-stata-dir"),
  pickDoFile: () => ipcRenderer.invoke("dialog:pick-do-file"),
  pickConfigFile: () => ipcRenderer.invoke("dialog:pick-config-file"),
  copyText: (text) => ipcRenderer.invoke("app:copy-text", text),
  openStateFolder: () => ipcRenderer.invoke("app:open-state-folder"),
  onLog: (listener) => {
    const handler = (_event, payload) => listener(payload);
    ipcRenderer.on("core:log", handler);
    return () => ipcRenderer.removeListener("core:log", handler);
  },
});
