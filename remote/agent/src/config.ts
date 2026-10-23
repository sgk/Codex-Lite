import { randomUUID } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { hostname } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { FirebasePublicConfig } from "../../shared/src/types.js";

export interface AgentConfig {
  firebase: FirebasePublicConfig;
  deviceId: string;
  displayName: string;
  heartbeatSeconds: number;
  endpointFile: string;
  attachmentRoot: string;
}

export async function loadAgentConfig(): Promise<AgentConfig> {
  const packageRoot = path.dirname(fileURLToPath(import.meta.url));
  const firebase = parseFirebaseConnection(process.env.CODEX_LITE_REMOTE_FIREBASE_CONFIG);
  const defaults = JSON.parse(await readFile(path.join(packageRoot, "agent-defaults.json"), "utf8")) as Partial<AgentConfig>;
  const stateRoot = process.env.HOME?.trim();
  if (!stateRoot) throw new Error("WSLのHOMEが未設定です。");
  const identity = await loadOrCreateDeviceIdentity(path.join(stateRoot, ".local/share/codex-lite/remote-device.json"));
  return {
    firebase,
    deviceId: identity.deviceId,
    displayName: identity.displayName,
    heartbeatSeconds: bounded(defaults.heartbeatSeconds, 60, 900, 300),
    endpointFile: process.env.CODEX_LITE_DAEMON_ENDPOINT_FILE || path.join(stateRoot, ".local/share/codex-lite/daemon-endpoint.json"),
    attachmentRoot: path.join(stateRoot, ".local/share/codex-lite/remote-attachments"),
  };
}

export function parseFirebaseConnection(raw: string | undefined): FirebasePublicConfig {
  if (!raw?.trim()) throw new Error("クラウド同期先が未設定です。CODEX_LITE_REMOTE_FIREBASE_CONFIG が必要です。");
  let value: unknown;
  try { value = JSON.parse(raw); }
  catch { throw new Error("クラウド同期先のJSON形式が不正です。"); }
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("クラウド同期先の設定が不正です。");
  const config = value as Record<string, unknown>;
  for (const field of ["projectId", "apiKey", "appId"]) {
    if (typeof config[field] !== "string" || !config[field].trim()) throw new Error(`クラウド同期先の ${field} が未設定です。`);
  }
  if (config.authDomain !== undefined && config.authDomain !== null && typeof config.authDomain !== "string")
    throw new Error("クラウド同期先の authDomain が不正です。");
  return {
    projectId: config.projectId as string, apiKey: config.apiKey as string, appId: config.appId as string,
    ...(typeof config.authDomain === "string" ? { authDomain: config.authDomain } : {}),
  };
}

interface DeviceIdentity {
  deviceId: string;
  displayName: string;
}

export async function loadOrCreateDeviceIdentity(identityPath: string): Promise<DeviceIdentity> {
  try {
    return validateDeviceIdentity(JSON.parse(await readFile(identityPath, "utf8")) as Partial<DeviceIdentity>);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }

  const identity: DeviceIdentity = {
    deviceId: `pc-${randomUUID().replaceAll("-", "")}`,
    displayName: hostname().trim() || "Codex Lite PC",
  };
  await mkdir(path.dirname(identityPath), { recursive: true });
  try {
    await writeFile(identityPath, `${JSON.stringify(identity, null, 2)}\n`, { encoding: "utf8", flag: "wx", mode: 0o600 });
    return identity;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
    return validateDeviceIdentity(JSON.parse(await readFile(identityPath, "utf8")) as Partial<DeviceIdentity>);
  }
}

function validateDeviceIdentity(value: Partial<DeviceIdentity>): DeviceIdentity {
  const deviceId = value.deviceId?.trim() || "";
  const displayName = value.displayName?.trim() || "";
  if (!/^[A-Za-z0-9_-]{1,80}$/.test(deviceId)) throw new Error("remote-device.json のdeviceIdが不正です。");
  if (!displayName) throw new Error("remote-device.json のdisplayNameが未設定です。");
  return { deviceId, displayName };
}

function bounded(value: number | undefined, minimum: number, maximum: number, fallback: number): number {
  return Number.isFinite(value) ? Math.max(minimum, Math.min(maximum, Number(value))) : fallback;
}
