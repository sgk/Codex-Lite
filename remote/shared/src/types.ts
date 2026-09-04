export const remoteOperations = [
  "send_message",
  "create_chat",
  "steer_run",
  "cancel_run",
  "resolve_approval",
  "get_runtime",
  "update_runtime",
] as const;

export type RemoteOperation = (typeof remoteOperations)[number];
export type TaskStatus = "queued" | "claimed" | "running" | "waiting_for_approval" | "completed" | "failed" | "cancelled" | "connection_lost";

export interface EncryptedPayload {
  version: 1;
  algorithm: "AES-256-GCM";
  keyId: string;
  nonce: string;
  ciphertext: string;
}

export interface RemoteTaskPayload {
  projectId?: string;
  chatId?: string;
  content?: string;
  title?: string;
  runId?: string;
  requestId?: string;
  decision?: "accept" | "acceptForSession" | "decline" | "cancel";
  model?: string;
  reasoningEffort?: string;
  permissionProfile?: string;
  approvalPolicy?: string;
  approvalsReviewer?: string;
  attachments?: RemoteAttachment[];
}

export interface RemoteAttachment {
  name: string;
  mimeType: string;
  kind: "image" | "file";
  dataBase64: string;
}

export interface CryptoContext {
  taskId: string;
  deviceId: string;
  direction: "controller_to_target" | "target_to_controller";
  sequence: number;
}

export interface FirebasePublicConfig {
  apiKey: string;
  authDomain: string;
  projectId: string;
  storageBucket?: string;
  messagingSenderId?: string;
  appId: string;
}
