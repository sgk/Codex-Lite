# Codex Lite Remote

`remote/` は、外出先のWebページから対象PCのCodex Liteへ指示を配送する独立した初期実装です。

## 含まれるもの

- `web/`: Firebase Authenticationでログインする操作用Web PWA
- `agent/`: Firestoreを監視し、localhostのdaemon専用APIへ許可済み操作だけを渡すRemote Agent
- `shared/`: WebとAgentで共用する型
- `firestore.rules`: ログイン利用者本人のデータだけへアクセスを限定するルール

利用者は Firestore の `system/access` ドキュメントで管理する。`entries` は小文字の文字列配列で、メールアドレス（例: `someone@example.com`）とドメイン（例: `example.com`）を区別せず列挙する。ログイン中のメールアドレス全体またはそのドメインが配列内の項目に一致し、かつGoogleがメールアドレスを確認済みの場合だけ利用できる。このドキュメントはクライアントから読み書きできないため、Firebase Consoleで管理する。
- `firebase.json`: Firestore RulesとFirebase Hostingの配置設定

指示、進捗、回答は `users/{uid}/devices/{deviceId}/tasks` 以下を通ります。既存チャットのWeb表示用には、検証済み履歴から作った直近の表示用メッセージだけをチャット文書へ同期します。ローカル絶対パス、JSONL全体、Codex private DB、認証情報は同期しません。

初期版で許可する操作は、既存チャットへの指示、新規チャットと最初の指示、追加指示、停止、承認・拒否、画像添付だけです。任意コマンドAPIとファイル取得は対象外です。

## ローカルでの確認

```bash
cd remote
npm install
npm test
npm run test:rules
npm run build
```

この操作だけではFirebaseへ接続または配置しません。

## Firebaseの準備（人間によるログインが必要）

1. Firebase Consoleでプロジェクトを作成する。
2. Webアプリを登録する。
3. AuthenticationでGoogleプロバイダを有効化する。
4. Firestore Databaseを作成する。初期Rulesは後のCLI配置で置き換える。
5. Webアプリ設定を `web/public/firebase-config.json` として保存する。雛形は同じ場所の `firebase-config.example.json`。
6. 必要なら `agent-defaults.json` のheartbeat間隔を調整する。端末IDと表示名はビルド設定へ入れず、各PCの初回起動時にWSL側の `~/.local/share/codex-lite/remote-device.json` へ生成し、以後はこのファイルだけを使う。
7. Firebase CLIで本人がログインし、`.firebaserc.example` を `.firebaserc` へコピーしてプロジェクトIDを設定する。
8. RulesとWebを配置する。

```bash
cd /home/sgk/Codex-Lite
source activate.sh
cd remote
npx --yes firebase-tools@15.27.0 login
npx --yes firebase-tools@15.27.0 deploy --only firestore:rules,firestore:indexes,hosting
```

`activate.sh` は GCP の設定を `.gcloud/`、Firebase CLI の認証・設定を `.firebase-cli-config/` に分離します。別プロジェクトへ切り替えるときは、現在のシェルを終了してから対象プロジェクトの `activate.sh` を source してください。認証情報の内容をリポジトリへ保存したり、ログへ出力したりしません。

Firebaseへのログインやプロジェクト作成はCodex Liteが代行しません。

## WindowsアプリからのRemote接続

Google Cloud ConsoleでOAuthクライアントを作成するときは、アプリケーションの種類を「デスクトップアプリ」にします。WindowsアプリはクライアントIDからGoogle標準の `com.googleusercontent.apps.<クライアントID本体>:/oauth2redirect` を生成して使います。

作成したクライアントIDとクライアントシークレットは、リポジトリ直下のGit管理外 `.env` に保存します。雛形は `.env.example` です。

```dotenv
GOOGLE_OAUTH_CLIENT_ID="YOUR_DESKTOP_CLIENT_ID.apps.googleusercontent.com"
GOOGLE_OAUTH_CLIENT_SECRET="YOUR_DESKTOP_CLIENT_SECRET"
```

ビルド処理は `.env` を読み、クライアントシークレットが設定されている場合だけRemote Agentをビルド・同梱し、同期UIを有効にします。シークレットが無い場合はRemote同期機能を含まないWindowsクライアントをビルドします。`.env` 自体は成果物へコピーせず、値をログや画面にも表示しません。

その後、Windowsアプリで「Remote接続」を押してGoogle認証を完了します。認証後、アプリが配布パッケージに同梱された `remote-agent/index.js` をWSLのNode.jsで起動し、認証トークンはプロセスの標準入力から一時的に渡されます。アプリは開発リポジトリやソース版 `remote/` を探索しません。コマンドライン、設定ファイル、ログには保存しません。

一度認証を完了すると、次回以降のWindowsアプリ起動時はWindows Credential Managerの保存済みRefresh TokenでRemote Agentへ自動再接続し、Firestoreのカタログ同期を再開します。Refresh Tokenが無効な場合だけ、設定画面から再認証してください。

## Remote Agentの起動

対象PCのWSLでソースから手動起動する場合は、Remote AgentはGoogle認証専用です。アクセストークンを環境変数へ設定して起動します。`start:agent` は配布時と同じAgentバンドルを生成してから起動します。通常はWindowsアプリの「Remote接続」から同梱版を起動してください。

```bash
cd remote
npm run start:agent
```

Agentは既に動いているdaemonを `~/.local/share/codex-lite/daemon-endpoint.json` から発見します。動いていなければ、このリポジトリの `scripts/run-daemon.sh` からdaemonを遅延起動します。いずれの場合も接続先は `127.0.0.1` だけです。

`CODEX_LITE_REMOTE_GOOGLE_ACCESS_TOKEN` はシェル履歴、サービス定義、ログへ保存しないでください。Windowsアプリで取得したrefresh tokenはWindows Credential Managerに保存し、次回接続時とアクセストークンの有効期限前に更新します。

## 本文の暗号化

初期版ではアプリ層の本文暗号化を提供しません。通信はHTTPS、アクセス制御はFirebase AuthenticationとFirestore Rulesで保護します。将来、WindowsクライアントとWebのペアリング時に鍵を自動設定する方式で追加します。

## 現在の制約

- 初期版は1利用者を想定し、WebとAgentは同じFirebase Authentication利用者でログインする。
- Agent用の個別credential発行と端末単位の失効は未実装。
- 既存チャット履歴はチャットごとに直近80件、本文は1件12,000文字、合計約240,000文字までの表示用投影として同期する。実行中のツール・思考進捗は2秒以上の間隔で、直近24項目の上限付き表示用投影として同期し、会話履歴へ表示する。実行終了後はdaemonに保存されたRun進捗の履歴投影へ切り替える。
- Cloud Run functionsによる登録・ペアリング処理は未実装。
- Firestoreイベントの保持期間と自動削除は未設定。
- Firebase EmulatorによるRulesの自動テストは追加済み。実Firebase環境でのE2E確認は未実施。

現在の実装状態と次の作業は `HANDOFF.md` を参照してください。
