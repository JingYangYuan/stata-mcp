import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.resolve(scriptDir, "..");
const repoRoot = path.resolve(desktopDir, "..");
const outDir = path.join(desktopDir, "resources", "stata-mcp-core");

const REQUIRED_ITEMS = [
  "install.py",
  "LICENSE",
  "README.md",
  "bin",
  "stata_mcp_local",
  "docs",
];

function copyFiltered(src, dest) {
  const stat = fs.statSync(src);
  const name = path.basename(src);
  if (name === "__pycache__" || name === ".DS_Store" || name.endsWith(".pyc") || name.endsWith(".pyo")) {
    return;
  }
  if (stat.isDirectory()) {
    fs.mkdirSync(dest, { recursive: true });
    for (const child of fs.readdirSync(src)) {
      copyFiltered(path.join(src, child), path.join(dest, child));
    }
    return;
  }
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.copyFileSync(src, dest);
}

fs.rmSync(outDir, { recursive: true, force: true });
fs.mkdirSync(outDir, { recursive: true });

for (const item of REQUIRED_ITEMS) {
  const src = path.join(repoRoot, item);
  if (!fs.existsSync(src)) {
    throw new Error(`Missing required core item: ${src}`);
  }
  copyFiltered(src, path.join(outDir, item));
}

console.log(`Prepared stata-mcp-core in ${outDir}`);
