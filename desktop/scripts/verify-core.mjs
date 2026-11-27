import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.resolve(scriptDir, "..");
const coreDir = path.join(desktopDir, "resources", "stata-mcp-core");

const EXPECTED_FILES = [
  "install.py",
  "stata_mcp_local/__init__.py",
  "stata_mcp_local/cli.py",
  "stata_mcp_local/config_emit.py",
  "stata_mcp_local/paths.py",
  "stata_mcp_local/runtime.py",
  "stata_mcp_local/source.py",
  "stata_mcp_local/stata.py",
  "stata_mcp_local/ui.py",
  "bin/stata-mcp-bridge.py",
  "bin/stata-mcp-start.py",
  "bin/stata-mcp-jobs.py",
  "bin/stata_mcp_analysis.py",
];

for (const rel of EXPECTED_FILES) {
  const full = path.join(coreDir, rel);
  if (!fs.existsSync(full) || !fs.statSync(full).isFile()) {
    throw new Error(`verify:core failed — missing ${full}`);
  }
}

console.log(`Verified ${EXPECTED_FILES.length} core files in ${coreDir}`);
