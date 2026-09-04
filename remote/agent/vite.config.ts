import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";

const remoteRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function runtimeConfigAssets(): Plugin {
  return {
    name: "codex-lite-remote-agent-config",
    async generateBundle() {
      this.emitFile({
        type: "asset",
        fileName: "firebase-config.json",
        source: await readFile(path.join(remoteRoot, "web/public/firebase-config.json")),
      });
      this.emitFile({
        type: "asset",
        fileName: "agent-defaults.json",
        source: await readFile(path.join(remoteRoot, "agent-defaults.json")),
      });
      this.emitFile({
        type: "asset",
        fileName: "package.json",
        source: '{"type":"module"}\n',
      });
    },
  };
}

export default defineConfig({
  plugins: [runtimeConfigAssets()],
  build: {
    ssr: path.join(remoteRoot, "agent/src/index.ts"),
    target: "node20",
    outDir: path.join(remoteRoot, "agent-dist"),
    emptyOutDir: true,
    minify: false,
    rollupOptions: {
      output: {
        format: "es",
        entryFileNames: "index.js",
        chunkFileNames: "chunks/[name]-[hash].js",
      },
    },
  },
  ssr: {
    noExternal: true,
  },
});
