import assert from "node:assert/strict";
import test from "node:test";
import { OrderedWriter } from "./ordered-writer.js";

test("タイマーが取り出した本文の保存を完了通知が追い越さない", async () => {
  const written: string[] = [];
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const writer = new OrderedWriter<string>(async value => {
    if (value === "output") await gate;
    written.push(value);
  });
  const first = writer.write("output");
  const terminal = writer.write("done");
  let drained = false;
  const drain = writer.drain().then(() => { drained = true; });
  await Promise.resolve();
  assert.equal(drained, false);
  assert.deepEqual(written, []);
  release();
  await Promise.all([first, terminal, drain]);
  assert.deepEqual(written, ["output", "done"]);
});

test("書き込み失敗時は後続の完了通知を送らず原因を返す", async () => {
  const writes: string[] = [];
  const writer = new OrderedWriter<string>(async value => {
    writes.push(value);
    throw new Error("storage unavailable");
  });
  await assert.rejects(writer.write("output"), /storage unavailable/);
  await assert.rejects(writer.write("done"), /storage unavailable/);
  await assert.rejects(writer.drain(), /storage unavailable/);
  assert.deepEqual(writes, ["output"]);
});
