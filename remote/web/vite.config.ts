import { defineConfig } from "vite";
import { execFileSync } from "node:child_process";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(fileURLToPath(import.meta.url));
const repositoryRoot = path.resolve(root, "../..");
const revision = execFileSync("git", ["rev-parse", "--short=8", "HEAD"], {
  cwd: repositoryRoot,
  encoding: "utf8",
}).trim();
const buildDate = new Date().toISOString();
const serviceWorkerPlaceholder = "__CODEX_LITE_BUILD_ID__";
export default defineConfig({
  root,
  publicDir: path.join(root, "public"),
  define: {
    __CODEX_LITE_REVISION__: JSON.stringify(revision),
    __CODEX_LITE_BUILD_DATE__: JSON.stringify(buildDate),
  },
  build: { outDir: path.join(root, "dist"), emptyOutDir: true },
  plugins: [{
    name: "codex-lite-service-worker-version",
    async writeBundle() {
      const serviceWorkerPath = path.join(root, "dist", "sw.js");
      const source = await readFile(serviceWorkerPath, "utf8");
      if (!source.includes(serviceWorkerPlaceholder)) throw new Error("Service Worker build placeholder is missing.");
      await writeFile(serviceWorkerPath, source.replaceAll(serviceWorkerPlaceholder, `${Date.now()}`), "utf8");
    },
  }],
});
