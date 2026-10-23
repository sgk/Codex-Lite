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

Windows側の設定読み込み・接続先別のオン設定・サービスコードの構文、およびWindowsからWSL子プロセスをEOFで停止する経路は、独立したテストハーネスでも確認できます。Windows PowerShellでリポジトリ直下から実行します。Windowsクライアント本体のビルド・配置・起動は行いません。

```powershell
dotnet run --project scripts/tests/CloudSync.Tests/CloudSync.Tests.csproj -- (Get-Location).Path
```

このハーネスのWSL子プロセステストは `Ubuntu-24.04` と `/usr/bin/node` のある検証環境を対象にします。実際の認証やFirestoreへの読み書きは行いません。

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

クラウド同期機能の提供可否・接続先・OAuth設定は、実行用フォルダー（デスクトップの `Codex Lite`）直下の `cloud-sync.json` で指定します。雛形はリポジトリ直下の `cloud-sync.example.json` です。ファイルを編集したらアプリを再起動してください。

- `enabled`: 同期機能を提供する場合だけ `true`。ファイルがない場合や `false` の場合は同期機能を無効化します。
- `firebase`: 接続先の `projectId`、`apiKey`、`appId`。`authDomain` も同じFirebaseプロジェクトの設定を指定できます。Firestoreデータベースは `(default)` を使用します。
- `oauth`: デスクトップ用Google OAuthの `clientId` と `clientSecret`。

有効化したファイルの形式・必須項目が不正な場合は同期を停止し、設定画面に原因を表示します。旧 `remote-oauth.json`、同梱Agentの `firebase-config.json`、開発リポジトリや `.env` を代わりに読みません。

設定画面には「クラウド同期をオンにする」と「Google認証」を表示します。初期値は同期オフです。認証だけを行っても同期を勝手にオンにしません。オフにするとAgentを停止し、自動再接続・認証の自動更新も停止します。停止時には親プロセスから標準入力を閉じ、WSL側のNode.jsも終了させます。

オン／オフは既存のUI状態ファイルへ保存し、同じ接続先なら起動後も維持します。Refresh TokenはWindows Credential Managerへ接続先（FirebaseプロジェクトID・アプリID・OAuthクライアントID）別に保存します。接続先を変更した場合は同期オフから始め、以前の接続先の認証を流用しません。旧版の接続先を区別しない保存トークンも流用しないため、初回移行時は再認証が必要です。

認証後、アプリが同梱された `remote-agent/index.js` をWSLのNode.jsで起動します。GoogleアクセストークンとFirebase接続設定は標準入力で渡し、コマンドラインへ含めません。AgentへOAuthクライアントシークレットは渡しません。

### 配布と既存環境の移行

通常のビルド・配布ZIPにはAgentと無効状態の設定雛形だけを同梱し、特定の接続先やOAuth設定を自動で組み込みません。ビルド処理は `.env` やWeb UI用 `web/public/firebase-config.json` を読みません。社外配布版に社内設定が混入しないよう、具体的な設定の同梱には `scripts/build-debug.ps1` または `scripts/build-release.ps1` の `-CloudSyncConfigFile` で管理者が明示指定する必要があります。同梱せず、導入後に管理者が実行用フォルダーへファイルを設置することもできます。

通常のデスクトップ配置更新では、実行用フォルダーの既存 `cloud-sync.json` を保持します。明示指定して同梱した設定がある場合は、その設定で置き換えます。公開用ZIPの作成はインストール済み設定を取り込みません。

旧版から移行するときは、管理者が旧Firebase設定とOAuth設定を新しい `cloud-sync.json` の各項目へ移し、`enabled` を `true` にします。これは自動移行しません。ファイルを設置して再起動した後、設定画面でGoogle認証し、同期をオンにしてください。同期先のFirestore Rules・許可リストとGoogle認証の設定、同じFirebaseプロジェクトに接続するWeb UIも別途必要です。デスクトップの設定だけではWeb UIの接続先は変わりません。

管理者が移行を明示的に行う場合は、Windows PowerShellで `scripts/migrate-cloud-sync-config.ps1` を実行できます。この専用スクリプトだけが旧 `.env` とWeb用Firebase設定を読み、デスクトップの実行用フォルダーへ設定ファイルを作成します。設定値は出力せず、既存の `cloud-sync.json` がある場合は上書きせず停止します。通常のビルド・起動処理からは呼びません。

### OAuthクライアントシークレットの配布と復旧

クライアントシークレットの用途は、WindowsアプリでのGoogle認証コードの交換とトークン更新です。Remote Agentは取得したGoogleアクセストークンでFirebase Authenticationへログインし、Firestoreを通じてWeb UIと同期します。シークレット自体をWeb UIへ渡したり、Firestoreの同期先を決めたりするためのものではありません。

具体的な `cloud-sync.json` はGitへ登録しません。ただし、そのファイルを明示指定して同梱した配布ZIPをGitHub Releaseへ公開すると、ダウンロードした人は設定値を取得できます。「Gitに含まれない」ことは「配布物にも含まれない」ことを意味しません。旧方式で作成した `remote-oauth.json` 入りのリリースも同様です。

必ず「デスクトップアプリ」用のOAuthクライアントを使います。Googleもインストール型アプリはクライアントシークレットを秘密に保てないことを前提にしています。この設定は、ユーザー個別のRefresh Tokenやサーバー用秘密鍵とは区別します。それらを配布物へ同梱してはいけません。すべてのアプリ・認証方式でシークレットの同梱が必要という意味でもありません。

紛失時は、対象OAuthクライアントを管理するGCP権限があれば、同じクライアントIDに新しいシークレットを発行して復旧できます。OAuthクライアントそのものを削除・再作成する操作とは区別してください。

1. Google Cloud Consoleの「Google Auth Platform → クライアント」で、対象のデスクトップ用OAuthクライアントに新しいシークレットを追加します。値は安全に保存し、会話やログへ貼り付けません。
2. 実行用フォルダーの `cloud-sync.json` の `oauth.clientSecret` を更新します。同じOAuthクライアントならクライアントIDは維持します。
3. アプリを再起動します。新方式では設定ファイルの変更だけなら再ビルドは不要です。設定を同梱して配布している場合は、配布物と各インストール先の設定も更新します。GCP側で再発行しただけでは、既存のインストール先の設定は更新されません。
4. 新版でログインとトークン更新・クラウド同期を確認します。旧シークレットが有効な通常の移行では、旧版から新版へ移行してから旧シークレットを無効化・削除します。無効化後、旧設定のアプリは認証・トークン更新ができなくなります。

シークレットを紛失・更新しても、それ自体でFirestoreの同期データが削除されることはありません。保存済みRefresh Tokenが無効な場合は再認証が必要です。

参考: [Googleのデスクトップアプリ向けOAuth仕様](https://developers.google.com/identity/protocols/oauth2/native-app)、[OAuth設定とシークレットのローテーション](https://support.google.com/googleapi/answer/6158849?hl=en)。

## Remote Agentの起動

対象PCのWSLでソースから手動起動する場合は、Remote AgentはGoogle認証専用です。アクセストークンを `CODEX_LITE_REMOTE_GOOGLE_ACCESS_TOKEN`、Firebase接続設定のJSONを `CODEX_LITE_REMOTE_FIREBASE_CONFIG` で実行時に渡します。`start:agent` は配布時と同じAgentバンドルを生成してから起動します。標準入力が閉じるとAgentは終了します。通常はWindowsアプリの設定画面から同梱版を起動してください。

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
