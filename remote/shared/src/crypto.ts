import type { CryptoContext, EncryptedPayload } from "./types.js";

const encoder = new TextEncoder();
const decoder = new TextDecoder();

export function decodeSharedKey(value: string): Uint8Array {
  const bytes = fromBase64(value.trim());
  if (bytes.length !== 32) throw new Error("共有鍵は32バイト（256bit）である必要があります。");
  return bytes;
}

export function generateSharedKey(): string {
  return toBase64(crypto.getRandomValues(new Uint8Array(32)));
}

export async function encryptJson(keyBytes: Uint8Array, context: CryptoContext, value: unknown): Promise<EncryptedPayload> {
  const key = await crypto.subtle.importKey("raw", arrayBuffer(keyBytes), "AES-GCM", false, ["encrypt"]);
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv: arrayBuffer(nonce), additionalData: arrayBuffer(aad(context)) },
    key,
    encoder.encode(JSON.stringify(value)),
  );
  return {
    version: 1,
    algorithm: "AES-256-GCM",
    keyId: await keyId(keyBytes),
    nonce: toBase64(nonce),
    ciphertext: toBase64(new Uint8Array(ciphertext)),
  };
}

export async function decryptJson<T>(keyBytes: Uint8Array, context: CryptoContext, value: EncryptedPayload): Promise<T> {
  if (value.version !== 1 || value.algorithm !== "AES-256-GCM") throw new Error("未対応の暗号化形式です。");
  if (value.keyId !== await keyId(keyBytes)) throw new Error("共有鍵が一致しません。");
  const key = await crypto.subtle.importKey("raw", arrayBuffer(keyBytes), "AES-GCM", false, ["decrypt"]);
  const plaintext = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: arrayBuffer(fromBase64(value.nonce)), additionalData: arrayBuffer(aad(context)) },
    key,
    arrayBuffer(fromBase64(value.ciphertext)),
  );
  return JSON.parse(decoder.decode(plaintext)) as T;
}

function aad(context: CryptoContext): Uint8Array {
  return encoder.encode(JSON.stringify([1, context.taskId, context.deviceId, context.direction, context.sequence]));
}

async function keyId(key: Uint8Array): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", arrayBuffer(key)));
  return toBase64(digest.slice(0, 9)).replace(/[+/=]/g, "_");
}

function toBase64(value: Uint8Array): string {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function fromBase64(value: string): Uint8Array {
  const binary = atob(value);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function arrayBuffer(value: Uint8Array): ArrayBuffer {
  return value.slice().buffer as ArrayBuffer;
}
