import assert from "node:assert/strict";
import test from "node:test";
import { createServer, type Server } from "node:http";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import type { AgentConfig } from "./config.js";
import { LocalDaemon, parseSseBlock, runEventsPath } from "./daemon.js";

test("稼働中Agentはデーモン再起動後の正規接続先へAPIとSSEを送る", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "codex-lite-endpoint-"));
  const endpointFile = path.join(root, "endpoint.json");
  const requests: string[] = [];
  const servers: Server[] = [];
  t.after(async () => {
    await Promise.all(servers.map((server) => new Promise<void>((resolve, reject) => {
      if (!server.listening) { resolve(); return; }
      server.closeAllConnections();
      server.close((error) => error ? reject(error) : resolve());
    })));
    await rm(root, { recursive: true });
  });
  const start = async (name: string) => {
    const server = createServer((request, response) => {
      requests.push(`${name}:${request.method}:${request.url}`);
      if (request.url?.includes("/events")) {
        response.end('id: 1\nevent: progress\ndata: {"occurredAt":"2026-10-20T12:00:00Z"}\n\n');
      } else if (request.url?.includes("/execute")) {
        request.socket.destroy(); // An accepted command with a lost response must not be replayed.
      } else {
        response.end(JSON.stringify({ revision: name === "old" ? 10 : 1, projects: [] }));
      }
    });
    servers.push(server);
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    assert.ok(address && typeof address !== "string");
    return address.port;
  };
  const publish = (port: number) => writeFile(endpointFile, JSON.stringify({ host: "127.0.0.1", port, pid: port }));
  const oldPort = await start("old");
  await publish(oldPort);
  const daemon = new LocalDaemon({ endpointFile } as AgentConfig);
  await daemon.connect();
  assert.equal((await daemon.catalog()).revision, 10);
  servers[0].closeAllConnections();
  await new Promise<void>((resolve, reject) => servers[0].close((error) => error ? reject(error) : resolve()));
  await assert.rejects(daemon.catalog(), /daemon接続失敗.*(ECONNREFUSED|UND_ERR_SOCKET)/);
  const newPort = await start("new");
  await publish(newPort);
  assert.equal((await daemon.catalog()).revision, 1);
  assert.equal(await daemon.waitCatalogRevision(10), 1);
  for await (const event of daemon.events("run", 5)) assert.equal(event.sequence, 1);
  assert.deepEqual(requests, [
    "old:GET:/health", "old:GET:/remote/v1/catalog?includeHistory=false",
    "new:GET:/remote/v1/catalog?includeHistory=false",
    "new:GET:/remote/v1/catalog-revision/wait?after=10",
    "new:GET:/remote/v1/runs/run/events?after=5",
  ]);
  await assert.rejects(daemon.execute("task", "send", {}), /daemon接続失敗/);
  assert.equal(requests.filter((request) => request.includes("/execute")).length, 1);
  await writeFile(endpointFile, '{"host":"example.com","port":80}');
  await assert.rejects(daemon.catalog(), /不正/);
  await rm(endpointFile);
  await assert.rejects(daemon.catalog(), /読み取れません/);
  assert.equal(requests.length, 6); // No stale endpoint or alternate route on invalid/missing files.
});

test("再生イベントでもデーモンの発生時刻と秒以下精度を保持する", () => {
  const occurredAt = "2026-09-11T01:02:03.123456Z";
  const event = parseSseBlock(`id: 12\nevent: progress\ndata: ${JSON.stringify({ occurredAt, summary: "work" })}`);
  assert.equal(event?.observedAt, occurredAt);
  assert.equal(event?.sequence, 12);
  assert.throws(() => parseSseBlock('id: 13\nevent: progress\ndata: {"summary":"work"}'), /daemon timestamp/);
});

test("指示文スナップショットは再生カーソルを進めず元の保存時刻を保持する", () => {
  const occurredAt = "2026-10-24T01:02:03.123456Z";
  const event = parseSseBlock(`event: instructions\ndata: ${JSON.stringify({ occurredAt, messages: [{ id: "instruction-1", createdAt: occurredAt }] })}`);
  assert.equal(event?.sequence, 0);
  assert.equal(event?.event, "instructions");
  assert.equal(event?.observedAt, occurredAt);
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
