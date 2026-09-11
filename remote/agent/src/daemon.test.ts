import assert from "node:assert/strict";
import test from "node:test";
import { parseSseBlock, runEventsPath } from "./daemon.js";

test("再生イベントでもデーモンの発生時刻と秒以下精度を保持する", () => {
  const occurredAt = "2026-09-11T01:02:03.123456Z";
  const event = parseSseBlock(`id: 12\nevent: progress\ndata: ${JSON.stringify({ occurredAt, summary: "work" })}`);
  assert.equal(event?.observedAt, occurredAt);
  assert.equal(event?.sequence, 12);
  assert.throws(() => parseSseBlock('id: 13\nevent: progress\ndata: {"summary":"work"}'), /daemon timestamp/);
});

test("デスクトップRunの再接続では現在位置より後だけを購読する", () => {
  assert.equal(
    runEventsPath("run /1", 1195),
    "/remote/v1/runs/run%20%2F1/events?after=1195",
  );
});

test("Web起点の新規Runは先頭から購読する", () => {
  assert.equal(runEventsPath("run_1"), "/remote/v1/runs/run_1/events");
});
