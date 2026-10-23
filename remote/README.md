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

### OAuthクライアントシークレットの配布と復旧

クライアントシークレットの用途は、WindowsアプリでのGoogle認証コードの交換とトークン更新です。Remote Agentは取得したGoogleアクセストークンでFirebase Authenticationへログインし、Firestoreを通じてWeb UIと同期します。シークレット自体をWeb UIへ渡したり、Firestoreの同期先を決めたりするためのものではありません。

`.env` はGit管理外ですが、Remote同期対応のビルドには、その値を含む `remote-oauth.json` が同梱されます。その配布ZIPをGitHub Releaseへ公開すると、ダウンロードした人は設定値を取得できます。「Gitに含まれない」ことは「配布物にも含まれない」ことを意味しません。

必ず「デスクトップアプリ」用のOAuthクライアントを使います。Googleもインストール型アプリはクライアントシークレットを秘密に保てないことを前提にしています。この設定は、ユーザー個別のRefresh Tokenやサーバー用秘密鍵とは区別します。それらを配布物へ同梱してはいけません。すべてのアプリ・認証方式でシークレットの同梱が必要という意味でもありません。

紛失時は、対象OAuthクライアントを管理するGCP権限があれば、同じクライアントIDに新しいシークレットを発行して復旧できます。OAuthクライアントそのものを削除・再作成する操作とは区別してください。

1. Google Cloud Consoleの「Google Auth Platform → クライアント」で、対象のデスクトップ用OAuthクライアントに新しいシークレットを追加します。値は安全に保存し、会話やログへ貼り付けません。
2. Git管理外 `.env` の `GOOGLE_OAUTH_CLIENT_SECRET` を更新します。同じOAuthクライアントならクライアントIDは維持します。
3. 新しい設定でWindowsクライアントを再ビルド・配置し、配布している場合は配布物も更新します。GCP側で再発行しただけでは、既存のインストール先の設定は更新されません。
4. 新版でログインとトークン更新・クラウド同期を確認します。旧シークレットが有効な通常の移行では、旧版から新版へ移行してから旧シークレットを無効化・削除します。無効化後、旧設定のアプリは認証・トークン更新ができなくなります。

シークレットを紛失・更新しても、それ自体でFirestoreの同期データが削除されることはありません。保存済みRefresh Tokenが無効な場合は再認証が必要です。

参考: [Googleのデスクトップアプリ向けOAuth仕様](https://developers.google.com/identity/protocols/oauth2/native-app)、[OAuth設定とシークレットのローテーション](https://support.google.com/googleapi/answer/6158849?hl=en)。

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
- 既存チャット履歴は文書上限より小さい安定チャンクへ分割して同期する。実行中はチャット文書へ稼働状態と完成作業の累計件数だけを低頻度で同期する。完成したcommentaryと最終回答は履歴へ同期し、reasoningやツール詳細はクラウドへ保存しない。
- Cloud Run functionsによる登録・ペアリング処理は未実装。
- Firestoreイベントの保持期間と自動削除は未設定。
- Firebase EmulatorによるRulesの自動テストは追加済み。実Firebase環境でのE2E確認は未実施。

現在の実装状態と次の作業は `HANDOFF.md` を参照してください。
