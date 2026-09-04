import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test, { after, before, beforeEach } from "node:test";
import { initializeTestEnvironment, assertFails, assertSucceeds, type RulesTestEnvironment } from "@firebase/rules-unit-testing";
import { doc, getDoc, serverTimestamp, setDoc } from "firebase/firestore";

let environment: RulesTestEnvironment;

before(async () => {
  environment = await initializeTestEnvironment({
    projectId: "demo-codex-lite",
    firestore: { rules: await readFile(path.resolve("firestore.rules"), "utf8") },
  });
});
beforeEach(async () => environment.clearFirestore());
after(async () => environment.cleanup());

async function seedAccess(): Promise<void> {
  await environment.withSecurityRulesDisabled(async (context) => {
    await setDoc(doc(context.firestore(), "system/access"), {
      entries: ["alice@example.com", "allowed.example"],
    });
  });
}

function allowedContext(uid = "alice", email = "alice@example.com") {
  return environment.authenticatedContext(uid, { email, email_verified: true });
}

beforeEach(seedAccess);

test("only the signed-in owner can read a device", async () => {
  await environment.withSecurityRulesDisabled(async (context) => {
    await setDoc(doc(context.firestore(), "users/alice/devices/home"), { status: "online" });
  });
  await assertSucceeds(getDoc(doc(allowedContext().firestore(), "users/alice/devices/home")));
  await assertFails(getDoc(doc(allowedContext("bob", "bob@allowed.example").firestore(), "users/alice/devices/home")));
  await assertFails(getDoc(doc(environment.unauthenticatedContext().firestore(), "users/alice/devices/home")));
});

test("owner can create only an allowed queued task shape", async () => {
  const firestore = allowedContext().firestore();
  await assertSucceeds(setDoc(doc(firestore, "users/alice/devices/home/tasks/task-1"), {
    operation: "send_message",
    status: "queued",
    payload: { projectId: "p", chatId: "c", content: "hello" },
    createdAt: serverTimestamp(),
    updatedAt: serverTimestamp(),
  }));
  await assertSucceeds(setDoc(doc(firestore, "users/alice/devices/home/tasks/runtime"), {
    operation: "update_runtime",
    status: "queued",
    payload: { projectId: "p", chatId: "c", model: "gpt-5-codex", reasoningEffort: "high", permissionProfile: ":workspace", approvalPolicy: "on-request", approvalsReviewer: "auto_review" },
    createdAt: serverTimestamp(),
    updatedAt: serverTimestamp(),
  }));
  await assertSucceeds(setDoc(doc(firestore, "users/alice/devices/home/tasks/image"), {
    operation: "send_message",
    status: "queued",
    payload: { projectId: "p", chatId: "c", content: "画像", attachments: [{ name: "screen.png", mimeType: "image/png", kind: "image", dataBase64: "cG5n" }] },
    createdAt: serverTimestamp(),
    updatedAt: serverTimestamp(),
  }));
  await assertFails(setDoc(doc(firestore, "users/alice/devices/home/tasks/task-2"), {
    operation: "read_file",
    status: "queued",
    payload: { path: "secret" },
    createdAt: serverTimestamp(),
    updatedAt: serverTimestamp(),
  }));
  await assertFails(setDoc(doc(firestore, "users/alice/devices/home/tasks/task-3"), {
    operation: "send_message",
    status: "queued",
    payload: {},
    encryptedPayload: {},
    createdAt: serverTimestamp(),
    updatedAt: serverTimestamp(),
  }));
  await assertFails(setDoc(doc(firestore, "users/alice/devices/home/tasks/task-4"), {
    operation: "send_message",
    status: "queued",
    payload: { content: "hello", path: "/not/allowed" },
    createdAt: serverTimestamp(),
    updatedAt: serverTimestamp(),
  }));
  await assertFails(setDoc(doc(firestore, "users/alice/devices/home/tasks/task-5"), {
    operation: "update_runtime",
    status: "queued",
    payload: { projectId: "p", permissionProfile: ":danger-full-access", approvalPolicy: "never", approvalsReviewer: "unknown" },
    createdAt: serverTimestamp(),
    updatedAt: serverTimestamp(),
  }));
});

test("another user cannot enqueue a task for the owner", async () => {
  const firestore = allowedContext("bob", "bob@allowed.example").firestore();
  await assertFails(setDoc(doc(firestore, "users/alice/devices/home/tasks/task-1"), {
    operation: "send_message",
    status: "queued",
    payload: { content: "hello" },
    createdAt: serverTimestamp(),
    updatedAt: serverTimestamp(),
  }));
  assert.ok(true);
});

test("owner can use only the flat chat collection", async () => {
  const owner = allowedContext().firestore();
  const other = allowedContext("bob", "bob@allowed.example").firestore();
  const flatChat = doc(owner, "users/alice/devices/home/chats/chat-1");
  await assertSucceeds(setDoc(flatChat, { projectId: "project-1", title: "Chat" }));
  await assertSucceeds(getDoc(flatChat));
  await assertFails(getDoc(doc(other, "users/alice/devices/home/chats/chat-1")));
  await assertFails(setDoc(doc(owner, "users/alice/devices/home/projects/project-1/chats/chat-1"), { title: "legacy" }));
});

test("owner can read and write selected-chat history chunks", async () => {
  const owner = allowedContext().firestore();
  const other = allowedContext("bob", "bob@allowed.example").firestore();
  const chunk = doc(owner, "users/alice/devices/home/historyChunks/chunk-1");
  await assertSucceeds(setDoc(chunk, {
    chatId: "chat-1",
    index: 0,
    hash: "abc",
    payload: JSON.stringify([{ id: "status-1", role: "status", content: "作業", createdAt: "now", kind: "work", activityDetails: "x".repeat(300_000) }]),
  }));
  await assertSucceeds(getDoc(chunk));
  await assertFails(getDoc(doc(other, "users/alice/devices/home/historyChunks/chunk-1")));
});

test("an email or domain in the access document is required", async () => {
  const exactEmail = allowedContext("alice", "alice@example.com").firestore();
  const allowedDomain = allowedContext("carol", "carol@allowed.example").firestore();
  const denied = allowedContext("mallory", "mallory@blocked.example").firestore();
  await assertSucceeds(getDoc(doc(exactEmail, "users/alice/devices/home")));
  await assertSucceeds(getDoc(doc(allowedDomain, "users/carol/devices/home")));
  await assertFails(getDoc(doc(denied, "users/mallory/devices/home")));
  await assertFails(getDoc(doc(environment.authenticatedContext("unverified", { email: "alice@example.com", email_verified: false }).firestore(), "users/unverified/devices/home")));
  await assertFails(getDoc(doc(exactEmail, "system/access")));
});
