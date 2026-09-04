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
  const firebase = JSON.parse(await readFile(path.join(packageRoot, "firebase-config.json"), "utf8")) as FirebasePublicConfig;
  const defaults = JSON.parse(await readFile(path.join(packageRoot, "agent-defaults.json"), "utf8")) as Partial<AgentConfig>;
  if (!firebase.apiKey || !firebase.projectId || !firebase.appId) throw new Error("firebase-config.json が未設定です。");
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
