import assert from "node:assert/strict";
import test from "node:test";
import { SingleFlight } from "./single-flight.js";

function gate() {
  let release!: () => void;
  const promise = new Promise<void>((resolve) => { release = resolve; });
  return { promise, release };
}

test("並行するカタログ同期は同じ完了を待ち、変更監視を高速ループさせない", async () => {
  const flight = new SingleFlight();
  const blocked = gate();
  let syncs = 0;
  let syncedRevision = 0;
  let waits = 0;
  let watchFinished = false;
  const sync = () => flight.run(async () => {
    syncs++;
    await blocked.promise;
    syncedRevision = 1;
  });
  const first = sync();
  const second = sync();
  assert.equal(first, second);
  const watch = (async () => {
    // The daemon returns immediately while the watcher still has an old
    // revision. This was an unbounded HTTP loop during an overlapping sync.
    while (syncedRevision !== 1 && waits < 1000) {
      waits++;
      await sync();
    }
    watchFinished = true;
  })();
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(waits, 1);
  assert.equal(syncs, 1);
  assert.equal(watchFinished, false);
  blocked.release();
  await Promise.all([first, second, watch]);
  assert.equal(waits, 1);
  assert.equal(syncedRevision, 1);
  await sync();
  assert.equal(syncs, 2);
});

test("同期失敗は全待機者へ伝え、失敗後に明示された次の同期だけを開始する", async () => {
  const flight = new SingleFlight();
  const blocked = gate();
  const failure = new Error("catalog failed");
  let starts = 0;
  const first = flight.run(async () => { starts++; await blocked.promise; throw failure; });
  const second = flight.run(async () => { starts++; });
  assert.equal(first, second);
  const checked = Promise.all([
    assert.rejects(first, (error) => error === failure),
    assert.rejects(second, (error) => error === failure),
  ]);
  blocked.release();
  await checked;
  assert.equal(starts, 1);
  await flight.run(async () => { starts++; });
  assert.equal(starts, 2);
});

test("同期処理が即座に例外を返しても保留状態を残さない", async () => {
  const flight = new SingleFlight();
  await assert.rejects(flight.run(() => { throw new Error("invalid catalog"); }), /invalid catalog/);
  let completed = false;
  await flight.run(async () => { completed = true; });
  assert.equal(completed, true);
});
