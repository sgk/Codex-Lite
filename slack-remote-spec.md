# Slack 経由 Codex リモート操作 仕様案

## 1. 目的

複数の利用者が、それぞれ複数 PC、複数プロジェクト、複数 Codex チャットを持つ環境で、Slack から対象を選んで各 PC 上の Codex Lite / Codex app-server へ指示を送れるようにする。

全員がリモート勤務で、社内 LAN、社内サーバー、固定 IP、ポート開放、VPN、常時稼働 Bridge は前提にしない。社員 PC への着信接続は作らず、PC 側からクラウドサービスへの外向き HTTPS 接続だけで動作させる。

本機能は Slack を自然言語指示、進捗確認、承認操作の UI として使うものであり、Slack を汎用ターミナルや任意コマンド実行口として扱わない。

## 2. 基本方針

方式は「常時稼働サーバーなしの Serverless Bridge」とする。

```text
Slack
  │ HTTPS Events / Interactivity / Web API
  ▼
Serverless Slack Handler
  │
  ▼
Cloud Database / Queue
  ▲
  │ HTTPS polling または managed realtime listener
  │
PC Client
  │ localhost / stdio / named pipe
  ▼
Codex Lite daemon / codex app-server
```

Slack のイベント受信、署名検証、モーダル表示、タスク登録、Slack への投稿はサーバーレス関数が担当する。PC Client は自分宛てのタスクを取得し、ローカルの Codex Lite daemon または `codex app-server` へ渡す。

Slack token はサーバーレス側にのみ置き、PC Client には配布しない。PC Client は Slack Web API を直接呼ばず、結果や進捗をクラウド DB または専用 HTTPS API へ送る。Slack への投稿はサーバーレス側が行う。

## 3. 採用候補

初期実装は Google Cloud を第一候補とする。

- Cloud Run functions または Cloud Functions
- Firestore
- Secret Manager
- Cloud Logging

固定費を抑えるため、常時インスタンスは持たない。Cloud Run functions はリクエスト処理時間に対する従量課金を前提にし、minimum instances は 0 とする。

AWS Lambda + DynamoDB、Azure Functions + Cosmos DB でも同じ構成は可能だが、初期版では provider 抽象化を過度に作らない。

## 4. Slack 側 UI

Slack App は 1 つだけ作成する。使用する機能は次を基本とする。

- App Home
- Bot
- Modal
- Button
- Select menu
- Events API
- Interactivity
- Web API
- 必要に応じた DM と message shortcut

Socket Mode は初期版では使わない。Socket Mode は公開 HTTPS endpoint を不要にする代わりに常時接続プロセスが必要になるため、本仕様の「常時稼働 Bridge なし」と相性が悪い。

Slack からの HTTP リクエストには 3 秒以内に応答する。`trigger_id` を使うモーダル表示は有効期限が短いため、イベント受付関数内で即時に実行する。

## 5. App Home

App Home は Slack ユーザーごとに分離し、原則として自分に紐付く PC、プロジェクト、セッションだけを表示する。

表示例:

```text
Codex Remote

接続中の PC
────────────────
● Desktop-A
● Notebook-B
○ Old-Laptop

最近のセッション
────────────────
Project Alpha / API エラー修正 / 10 分前
Project Beta / テスト失敗調査 / 2 時間前

実行中
────────────────
Project Alpha / API エラー修正 / 実行中

[新しい指示]
```

App Home は次のタイミングで更新する。

- ユーザーが App Home を開いた
- PC の online / offline 状態が変化した
- タスク状態が変化した
- 新しいセッションが追加された

Slack API のレート制限を避けるため、同一ユーザーへの連続更新は 5 秒から 10 秒程度で集約する。

## 6. 新しい指示フロー

ユーザーが「新しい指示」を押すと、モーダルで対象を選ぶ。

```text
対象 PC
[ Desktop-A ▼ ]

対象プロジェクト
[ Project Alpha ▼ ]

対象セッション
[ API エラー修正 ▼ ]

指示
[                                    ]
[                                    ]

[キャンセル] [送信]
```

選択順は PC、プロジェクト、セッションとする。セッション選択には「新しいセッションを開始」を含める。

表示名は UI 用に限り、配送や権限判定には必ず内部 ID を使う。

## 7. Slack スレッドと Codex セッション

1 つの Codex チャットセッションに 1 つの Slack スレッドを割り当てる。

```text
Slack parent message + thread_ts
  ↕
Codex session / codexThreadId
```

親メッセージには次を表示する。

```text
Project Alpha
PC: Desktop-A
Branch: main
Session: API エラー修正
Status: 実行中
```

同じ Slack スレッドへの返信は、同じ Codex セッションへの追加指示として扱う。ユーザーは毎回 PC、プロジェクト、セッションを指定しない。

## 8. PC Client

各 PC には軽量な PC Client を入れる。PC Client は次を担当する。

- デバイス登録
- デバイス heartbeat
- ローカルプロジェクトの登録
- Codex セッション一覧の同期
- 自分宛てタスクの取得
- 原子的な claim
- Codex Lite daemon または `codex app-server` への指示送信
- 進捗、承認要求、完了結果の登録
- 中止要求の監視

PC Client はローカルの Codex 実行口だけに接続する。

```text
127.0.0.1
localhost
stdio
Unix domain socket
Windows named pipe
```

`codex app-server` や Codex Lite daemon をインターネットへ公開してはならない。

## 9. クラウドへ保存する情報

クラウド DB へ保存してよい情報は、Slack から対象選択と配送を行うためのメタデータに限定する。

- Slack workspace ID
- Slack user ID
- 内部 user ID
- device ID
- device 表示名
- OS 種別
- client version
- online / offline 判定用 timestamp
- project ID
- project 表示名
- repository 名
- current branch
- session ID
- Codex thread ID
- session title
- session status
- task ID
- task status
- Slack channel ID
- Slack thread ts
- 作成、更新、期限 timestamp

クラウド DB に保存しない情報:

- ローカル絶対パス
- 環境変数
- API key
- access token
- Cookie
- 秘密鍵
- `.env` の内容
- `auth.json` の内容
- ソースコード全文
- プロンプト全文を含む詳細ログ
- Codex private DB の内容

ローカル絶対パスは PC Client のローカル設定にのみ保存する。Slack やクラウド DB では `projectId` で参照する。

## 10. データモデル

### users

```text
users/{userId}
```

主な項目:

- slackWorkspaceId
- slackUserId
- displayName
- role
- createdAt
- updatedAt

### devices

```text
devices/{deviceId}
```

主な項目:

- ownerUserId
- ownerSlackUserId
- displayName
- os
- clientVersion
- status
- lastSeenAt
- createdAt
- updatedAt

hostname は利用者に表示する必要がある場合だけ保存する。保存する場合も秘密情報として扱い、ログへ不用意に出さない。

### projects

```text
devices/{deviceId}/projects/{projectId}
```

主な項目:

- displayName
- repositoryName
- currentBranch
- lastSeenAt
- createdAt
- updatedAt

`localPath` は保存しない。

### sessions

```text
devices/{deviceId}/projects/{projectId}/sessions/{sessionId}
```

主な項目:

- codexThreadId
- title
- status
- branch
- lastUpdatedAt
- createdAt
- updatedAt

### tasks

```text
tasks/{taskId}
```

主な項目:

- requestedByUserId
- requestedBySlackUserId
- targetDeviceId
- projectId
- sessionId
- instructionRef または encryptedInstruction
- status
- slackChannelId
- slackThreadTs
- claimedByDeviceId
- claimExpiresAt
- createdAt
- startedAt
- completedAt
- expiresAt
- errorCode
- errorSummary

指示本文をクラウド DB に保存する必要がある場合は、短期保持、暗号化、ログ出力禁止を必須とする。初期版では保存期間を短くし、完了後に本文を削除または参照不可にする方針とする。

## 11. タスク状態

タスクは最低限次の状態を持つ。

| 状態 | 意味 |
|---|---|
| queued | 対象 PC の受信待ち |
| claimed | 対象 PC が取得済み |
| running | Codex 実行中 |
| waiting_for_approval | ユーザー承認待ち |
| completed | 正常完了 |
| failed | エラー終了 |
| cancelled | ユーザーまたは PC により中止 |
| expired | 有効期限切れ |
| connection_lost | 実行中に PC との通信が途絶えた |

PC Client は `targetDeviceId == 自端末 ID` かつ `status == queued` のタスクだけを取得する。取得時はトランザクションで `claimed` へ変更し、`claimedByDeviceId` と `claimExpiresAt` を設定する。

同一タスクが複数回実行されないよう、claim は原子的に行う。claim 期限を過ぎたタスクは再試行可能にするが、Codex 側で同じ指示が二重送信されないよう task ID を冪等キーとして扱う。

## 12. オフライン時の扱い

PC がオフラインの場合、タスクは `queued` のまま保持する。ただし、古い指示が利用者の意図に反して後から実行されることを避けるため、タスクには有効期限を持たせる。

初期値:

```text
通常指示: 24 時間
承認待ち: 30 分
```

オフライン PC への送信時は Slack に次を表示する。

```text
対象 PC は現在オフラインです。
オンラインになり次第、有効期限内であれば実行します。

[中止]
```

長時間オフラインだった PC が戻った場合は、危険操作を含むタスクを自動実行せず、Slack 側で再確認を求める。

## 13. 承認操作

Codex が承認を要求した場合、サーバーレス側が Slack スレッドへ承認メッセージを投稿する。

```text
Codex が操作の承認を求めています。

操作:
npm test

理由:
変更後のテストを実行します。

[承認] [拒否] [詳細]
```

承認結果はクラウド DB の approval response として保存し、PC Client が取得して Codex へ返す。承認には有効期限を設定し、期限切れは拒否または timeout として扱う。

特に次は明示承認を必須とする。

- git commit / push / PR 作成
- ファイル削除
- 外部サービスへの POST / PUT / PATCH / DELETE
- 認証情報、Cookie、token を扱う可能性がある操作
- project root 外へのアクセス

## 14. Slack イベント処理

サーバーレス関数は次を処理する。

- App Home を開いた
- App Home 上のボタンを押した
- モーダルを送信した
- select menu を変更した
- 承認、拒否、中止ボタンを押した
- Slack スレッドへ返信した

同期処理は最小限にする。

1. Slack 署名検証
2. timestamp による replay 防止
3. payload の最小検証
4. 必要なモーダル表示または更新
5. タスク、承認、更新要求の登録
6. HTTP 200 応答

時間のかかる処理は非同期化する。

## 15. 関数構成

初期版の関数は次を想定する。

| 関数 | 役割 |
|---|---|
| slack-events | Events API、App Home open、message event の受付 |
| slack-actions | button、modal submit、select menu の処理 |
| slack-options | 動的選択肢の生成 |
| task-result | PC Client からの進捗、結果、承認要求の受付 |
| app-home-refresh | App Home 表示の生成と更新 |
| task-expirer | 期限切れ task / approval の整理 |

PC Client が Firestore へ直接書き込む構成では `task-result` を省けるが、認可と検証を一箇所に集めるため、初期版では専用 HTTPS API を置く方がよい。

## 16. セキュリティ

### Slack 署名検証

Slack からのすべての HTTP リクエストで署名を検証する。

- Slack Signing Secret を使う
- request timestamp を検査する
- 古い timestamp を拒否する
- replay 攻撃を防ぐ

### ユーザー認可

Slack user ID と内部 user ID を紐付ける。ユーザーは原則として自分の PC、プロジェクト、セッション、タスクだけを操作できる。

管理者ロールは別途定義する。管理者操作は監査ログを必須とし、他人の PC に指示を送る操作は対象ユーザーの明示許可を必要とする。

### PC 認証

PC Client は利用者認証とは別に device credential を持つ。長期秘密情報を平文ファイルに保存しない。

保存先:

- Windows Credential Manager
- macOS Keychain
- Linux Secret Service

候補:

- OAuth Device Authorization Grant
- Firebase Authentication
- 短命 access token + refresh token
- 登録時に発行する device token

### ログ

Cloud Logging には構造化メタデータだけを出す。

出してよいもの:

- task ID
- device ID
- project ID
- session ID
- Slack user ID
- 状態遷移
- エラーコード
- 短いエラー要約

出してはいけないもの:

- prompt 本文
- ソースコード全文
- diff 全文
- 環境変数
- 認証情報
- token
- Cookie
- `auth.json` の内容

## 17. ファイル表示

初期版では Slack からのファイルブラウザを実装しない。Slack 経由で見せる内容は Codex の要約、短い差分要約、テスト結果要約に留める。

将来対応する場合も、PC Client が project root 配下の読み取りだけを許可し、サーバーレス側はサイズ上限と秘密情報マスクを通した結果だけを Slack に投稿する。大きなファイルやログ全文を Slack へ投稿しない。

## 18. 障害時の動作

### 対象 PC がオフライン

タスクは `queued` のまま保持し、有効期限内に PC が戻れば実行する。期限切れなら `expired` にする。

### app-server 起動失敗

タスクを `failed` にし、Slack へ短いエラーを投稿する。ローカルパスや秘密情報を含むエラー詳細は投稿しない。

### 実行中に PC が切断

heartbeat または進捗が一定時間途絶えた場合、タスクを `connection_lost` にする。PC が復帰したら、task ID を使ってローカル状態を照合し、完了済みなら結果を登録する。未実行なら再試行可能にする。

### Slack API エラー

Slack 投稿に失敗した場合、タスク結果は DB に残す。App Home または次回 thread 操作時に未配信結果を再通知する。

## 19. 初期実装範囲

初期版で実装する。

1. Slack App Home
2. PC 登録と online / offline 表示
3. プロジェクト一覧表示
4. Codex セッション一覧表示
5. モーダルからの指示送信
6. タスク登録
7. PC Client によるタスク取得と claim
8. Codex Lite daemon または `codex app-server` への指示送信
9. Slack スレッドへの応答投稿
10. 同じ Slack スレッドからの追加指示
11. タスク中止
12. 基本的なエラー表示

初期版では実装しない。

- 複雑なファイルブラウザ
- リアルタイムターミナル
- PC 間の直接通信
- VPN
- NAT traversal
- 独自常時 relay server
- 大容量ファイル転送
- 高度な管理者画面
- 複数クラウド provider 対応

## 20. 完成条件

初期版は次を満たせば完成とする。

1. PC Client を起動すると App Home に PC が表示される
2. PC を選ぶと、その PC のプロジェクト一覧が表示される
3. プロジェクトを選ぶと、Codex セッション一覧が表示される
4. Slack から指示を送ると、対象 PC だけが受信する
5. 対象 PC のローカル Codex 実行口へ指示が渡る
6. Codex の応答が Slack スレッドへ表示される
7. 同じ Slack スレッドへの返信が同じ Codex セッションへ追加指示として渡る
8. PC がオフラインの場合はタスクが保留される
9. 期限内に PC がオンラインへ戻ると保留タスクが実行される
10. ユーザーが Slack からタスクを中止できる
11. 誤った PC、プロジェクト、セッションへ配送されない
12. PC 側に Slack token を置かない
13. クラウド DB にローカル絶対パスや秘密情報を保存しない
14. 常時稼働 Bridge を必要としない

