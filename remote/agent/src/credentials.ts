export function firebaseGoogleAccessToken(): string {
  const accessToken = process.env.CODEX_LITE_REMOTE_GOOGLE_ACCESS_TOKEN?.trim();
  if (!accessToken) {
    throw new Error("Google認証が必要です。WindowsアプリからOAuth認証を完了してからAgentを起動してください。");
  }
  return accessToken;
}
