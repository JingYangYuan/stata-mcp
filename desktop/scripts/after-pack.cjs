const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

module.exports = async function afterPack(context) {
  if (context.electronPlatformName !== "darwin") {
    return;
  }
  const productName = context.packager.appInfo.productFilename;
  const appPath = path.join(context.appOutDir, `${productName}.app`);
  if (!fs.existsSync(appPath)) {
    throw new Error(`afterPack: expected macOS bundle not found at ${appPath}`);
  }

  try {
    execFileSync("xattr", ["-cr", appPath], { stdio: "inherit" });
  } catch {
    // xattr may not exist or have nothing to clear
  }

  try {
    execFileSync("codesign", ["--remove-signature", appPath], { stdio: "ignore" });
  } catch {
    // ignore if unsigned
  }

  execFileSync("codesign", ["--force", "--deep", "--sign", "-", "--timestamp=none", appPath], {
    stdio: "inherit",
  });
  execFileSync("codesign", ["--verify", "--deep", "--strict", "--verbose=2", appPath], {
    stdio: "inherit",
  });
};
