#!/usr/bin/env node
// Starts the Python backend which also serves the built React app.
// Usage: npm run serve  (build first with `npm run build`)
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");
const backend = join(root, "backend");
const dist = join(root, "frontend", "dist");

if (!existsSync(dist)) {
  console.error("未找到前端构建产物 frontend/dist，先运行 `npm run build --prefix frontend`。");
  process.exit(1);
}

const py = process.env.PYTHON || process.env.PYTHON3 || "python3";
const args = [
  "-m", "signsub.server",
  "--host", process.env.SIGSUB_HOST || "127.0.0.1",
  "--port", process.env.SIGSUB_PORT || "8787",
];

const child = spawn(py, args, {
  cwd: root,
  env: { ...process.env, PYTHONPATH: [backend, process.env.PYTHONPATH].filter(Boolean).join(":") },
  stdio: "inherit",
});

const stop = (sig) => {
  if (!child.killed) child.kill(sig);
};
process.on("SIGINT", () => stop("SIGINT"));
process.on("SIGTERM", () => stop("SIGTERM"));
child.on("exit", (code) => process.exit(code ?? 0));
