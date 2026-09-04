# Codex Lite リモート操作 引き継ぎ

更新日: 2026-08-28
記録時の HEAD: `8efcc0d`

## この文書の役割

別スレッドでリモート操作機能の導入・実装を再開するための状態記録である。

文書の優先順位は次のとおり。

1. `../codex-lite-spec.md`: Codex Lite 全体の正本
2. `../remote-operation-spec.md`: リモート操作の仕様と検討事項
3. `README.md`: Firebase の導入・起動手順
4. 本書: 現在の実装状態と次に行う作業

## 確定している方針

- 外出先の Web ページ、将来的には別マシンの Codex Lite から対象 PC の Codex Lite を操作する。
- Firebase を共有メールボックスとして使い、指示、状態、途中経過、承認要求、結果を Firestore 経由で配送する。
- 対象 PC は Firebase へ外向き HTTPS 接続する。daemon や app-server を外部公開せず、ポート開放もしない。
- Cloud Run は常時中継には使わない。将来、端末登録、ペアリング、権限検証、定期整理などの短い管理処理が必要になった場合だけ候補とする。使う場合も minimum instances は 0 とする。
- リモート操作に自動的な有効期限を設けない。利用者が明示的に ON/OFF する。
- 通信路は Firebase の HTTPS を使う。
- 初期版はHTTPSとFirestore Rulesで保護し、本文のアプリ層暗号化は提供しない。将来、WindowsクライアントとWebのペアリング時に鍵を自動設定する方式で追加する。
- 任意コマンド、汎用ターミナル、ファイル取得、添付、ローカルログや JSONL の転送は対象外。

## 構成

```text
外出先ブラウザー (remote/web)
        │ Firebase Authentication + Firestore
        ▼
Firebase（指示、状態、応答イベント）
        ▲
        │ 外向き接続
対象 PC の Remote Agent (remote/agent)
        │ localhost HTTP
        ▼
Codex Lite daemon (127.0.0.1 のみ)
        │ stdio
        ▼
codex app-server
```

## 現在の実装

### Web PWA: `remote/web`

- Vite + TypeScript。
- Firebase Authentication のGoogle認証。
- 端末、プロジェクト、チャットの選択。
- 既存チャットへの送信と新規チャット作成。
- タスク状態、応答イベント、最終結果の表示。
- 実行停止、実行中の追加指示、承認要求への許可／拒否。
- 本文の暗号化UIは初期版では提供しない。
- チャットごとに検証済み履歴から直近のユーザー／assistant表示用メッセージを同期し、Web右ペーンで表示する。JSONL全体、ツール呼び出し、内部推論、ローカルパスや秘密情報は同期しない。
- Web App Manifest と Service Worker。

### Remote Agent: `remote/agent`

- Firestore の自端末宛て queued task を監視する Node.js/TypeScript プロセス。
- Firestore transaction で task を claim し、複数 Agent による二重取得を防ぐ。
- `~/.local/share/codex-lite/daemon-endpoint.json` から実行中 daemon を検出する。
- daemon が見つからない場合、`scripts/run-daemon.sh` から必要時に起動する。
- 許可済み操作だけを localhost の daemon 専用 API へ渡す。
- daemon の SSE イベントを Firestore へ転送する。
- 同じ出力項目の連続 delta を最大約 750 ms 単位にまとめる。
- heartbeat とプロジェクト／チャット一覧の同期。Windows側から選択中のチャット、選択中のプロジェクト、プロジェクトツリー順を受け取り、catalogとFirestoreのsyncOrderへ反映する。
- Firebase ログイン情報は対話入力、または無人起動用環境変数から受け取る。OS 保護領域との統合は未実装。

### 共有コード: `remote/shared`

- Web と Agent の Firestore データ型。

### daemon

- `../daemon/codex_lite_daemon/remote_gateway.py` を追加済み。
- Remote Agent 用 API:
  - `GET /remote/v1/catalog`
  - `POST /remote/v1/sync-priority`
  - `POST /remote/v1/tasks/{task_id}/execute`
  - `GET /remote/v1/runs/{run_id}/events`
- 許可済み操作:
  - `send_message`
  - `create_chat`
  - `steer_run`
  - `cancel_run`
  - `resolve_approval`
- SQLite の `remote_tasks` で cloud task ID を冪等キーとして記録する。
- 完了済み task ID の再要求には保存済み結果を返す。開始済みで結果不明の task は推測で再実行しない。
- daemon は起動時に endpoint ファイルを mode 0600 で書き、正常終了時は PID が一致する場合だけ削除する。

### Firebase 構成

実装上の主なパスは次のとおり。

```text
users/{uid}/devices/{deviceId}
users/{uid}/devices/{deviceId}/projects/{projectId}
users/{uid}/devices/{deviceId}/chats/{chatId}
users/{uid}/devices/{deviceId}/tasks/{taskId}
users/{uid}/devices/{deviceId}/tasks/{taskId}/events/{eventId}
```

- `firestore.rules`、`firestore.indexes.json`、`firebase.json` を追加済み。
- Security Rules はログイン利用者本人の UID 以下にアクセスを限定する。
- 現在の MVP では Web と Agent が同じ Firebase ユーザーでログインする。
- device ごとの独立 credential と個別失効はまだない。

## 実装時に確認済みの結果

- daemon の pytest: 131 件成功。
- AES-GCM unit test: 1 件成功。
- Firebase Emulator を使う Firestore Rules test: 3 件成功。
- `remote` の npm build 成功。
- npm audit: 脆弱性 0 件。
- Web bundle が 500 kB を超える警告はあったが、ビルド自体は成功。

この引き継ぎ文書の作成時はコードを変更していないため、再ビルド・再テストは行っていない。

## まだ導入・確認していないこと

- 実 Firebase プロジェクトの作成と接続。
- Firebase CLI へのログイン。
- Firestore Rules、indexes、Firebase Hosting の deploy。
- 実 Firebase を介した Web → Agent → daemon → Web の E2E 確認。
- Windows クライアントの再ビルドと実行用ディレクトリへの配置。現在配置済みの Windows 実行版に、新しい daemon endpoint と remote API が入っているとは限らない。
- 長時間切断、Agent 再起動、daemon 再起動を含む再接続の網羅的な確認。
- 実行中に接続を失った task のイベントを完全に再開する処理の検証。
- Firestore の古い task/event の保持期限と自動削除。
- Windows UI からのリモート操作 ON/OFF。
- 端末登録、ペアリング、device credential の発行・更新・失効。
- Agent用credentialのWindows Credential Manager保存と端末単位の失効。
- WebとWindowsクライアントのペアリング、およびペアリング時の本文暗号化鍵の自動設定。
- Cloud Run / Functions を使う管理 API。
- 別マシンの Codex Lite を操作クライアントにする adapter。現在ある操作 UI は Web PWA のみ。

## Firebase 導入時の手動作業

詳細は `README.md` を参照する。概要は次のとおり。

1. Firebase Console でプロジェクトと Web App を作る。
2. Authentication のGoogleプロバイダを有効化する。
3. Firestore Database を作る。
4. Web App 設定を `web/public/firebase-config.json` に置く。
5. 必要なら `agent-defaults.json` のheartbeat間隔を調整する。端末IDと表示名は各PCの初回起動時にWSL側 `~/.local/share/codex-lite/remote-device.json` へ生成し、以後はこのファイルだけから読む。
6. `.firebaserc.example` を `.firebaserc` にコピーして project ID を記入する。
7. 利用者本人が Firebase CLI にブラウザーで手動ログインする。
8. Rules、indexes、Hosting を deploy する。

実設定ファイルは Git の追跡対象外。ログインが必要になった時点で利用者に依頼し、Cookie や token を抽出・再利用してはならない。

## 次のスレッドでの推奨作業順

1. `../codex-lite-spec.md`、`../remote-operation-spec.md`、本書、`README.md` の順に読む。
2. `git status` を確認し、既存の利用者変更を保護する。
3. Firebase Console での準備が必要なところまで進め、ログインや Console 操作は利用者に依頼する。
4. Rules、indexes、Hosting を deploy する。
5. Windows クライアントのビルドは、利用者から明示的に依頼された場合だけ行う。
6. Agent を起動し、端末状態と catalog 同期を確認する。
7. メッセージ、新規チャット、途中経過、最終回答、追加指示、停止、承認を順に E2E 確認する。
8. オフライン復帰、Agent/daemon 再起動、同一 task の再受信で二重実行されないことを確認する。
9. 実運用で必要と判明したものから、Agent用credentialの発行・失効、端末登録、データ削除、ペアリング時の暗号化鍵設定を追加する。

## 守るべき安全境界

- daemon は `127.0.0.1` のみに bind する。
- Remote Agent は daemon を外部公開せず、Firebase へ外向き接続する。
- API key、token、Cookie、`auth.json`、ブラウザーの認証状態を読み取り、抽出、転送、再利用しない。
- Codex private DB は通常機能では read-only metadata sync に限り、直接書き込まない。
- Firestore へローカル絶対パス、環境変数、秘密情報、private DB、JSONL 全体を送らない。
- リモート API に任意コマンドや任意ファイル取得の逃げ道を追加しない。
- フォールバック経路を無断で増やさず、使用された経路を診断可能にする。
- ビルド、配置、起動、再起動、deploy は、それぞれ利用者の依頼・確認範囲を守る。

## 関連コミット

- `b42f166 Firebase経由のリモート操作を実装`
- `80bb2e3 Firebase導入手順を補足`
