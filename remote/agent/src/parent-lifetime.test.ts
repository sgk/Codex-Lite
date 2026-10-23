import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { PassThrough } from "node:stream";
import test from "node:test";
import { watchParentInput } from "./parent-lifetime.js";

test("親の入力が開いている間は継続し、EOFだけで停止する", async () => {
  const input = new PassThrough();
  let stopped = false;
  watchParentInput(input, () => { stopped = true; });
  input.write("input");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(stopped, false);
  input.end();
  await once(input, "end");
  assert.equal(stopped, true);
});

test("WSLのNodeプロセスは親が標準入力を閉じたら実際に終了する", { timeout: 8000 }, async () => {
  const child = spawn(process.execPath, ["--import", "tsx", "--input-type=module", "-e",
    "import { watchParentInput } from './agent/src/parent-lifetime.ts'; watchParentInput(); setInterval(() => {}, 1000); console.log('ready');"],
  { stdio: ["pipe", "pipe", "pipe"] });
  try {
    await once(child.stdout, "data");
    const exited = once(child, "exit");
    child.stdin.end();
    const [code, signal] = await exited;
    assert.equal(code, 0);
    assert.equal(signal, null);
  } finally {
    if (child.exitCode === null) child.kill();
  }
});
