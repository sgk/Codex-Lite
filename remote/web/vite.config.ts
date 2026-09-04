import { defineConfig } from "vite";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(fileURLToPath(import.meta.url));
const serviceWorkerPlaceholder = "__CODEX_LITE_BUILD_ID__";
export default defineConfig({
  root,
  publicDir: path.join(root, "public"),
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
