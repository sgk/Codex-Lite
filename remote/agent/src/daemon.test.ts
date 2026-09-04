import assert from "node:assert/strict";
import test from "node:test";
import { runEventsPath } from "./daemon.js";

test("デスクトップRunの再接続では現在位置より後だけを購読する", () => {
  assert.equal(
    runEventsPath("run /1", 1195),
    "/remote/v1/runs/run%20%2F1/events?after=1195",
  );
});

test("Web起点の新規Runは先頭から購読する", () => {
  assert.equal(runEventsPath("run_1"), "/remote/v1/runs/run_1/events");
});
