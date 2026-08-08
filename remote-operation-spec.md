# Codex Lite リモート操作 仕様・検討メモ

## 1. 文書の位置付け

本書は、外出先の別マシン上の Codex Lite またはWebページから、自宅などの対象PC上の Codex Lite を操作する将来機能の仕様案と検討事項を記録する。

現在実装済みの動作は `codex-lite-spec.md` を正本とする。本書の内容は未実装であり、採用が確定した事項と検討中の事項を区別して記載する。Slackを操作UIとして使う案の詳細は `slack-remote-spec.md` に残し、本書で定義する共通の配送・実行機構に対する将来のUI adapterとして扱う。

## 2. 目的

- 外出先のWebページまたは別PCの Codex Lite から対象PCを選び、Codexへ指示を送る。
- 既存チャットへの指示、新規チャット、実行中の追加指示、停止、承認、進捗確認、最終回答の取得を可能にする。
- 固定IP、ポート開放、VPN、NAT traversal、常時稼働の独自中継サーバーを前提にしない。
- 対象PCへの着信接続を作らず、対象PCからクラウドへの外向きHTTPS接続だけで動作させる。
- Codex Lite daemon と `codex app-server` をインターネットへ公開しない。
- クラウド側はサーバーレスサービスを利用し、待機中の常時稼働コンテナを必要としない。

## 3. 基本構成

第一候補は、Firebaseを共有メールボックスとして使う構成とする。

```text
外出先
  Web PWA / 別PCの Codex Lite
    │
    │ Firebase Authentication + HTTPS
    ▼
Firebase
  Firestore
    ├─ device / project / chat metadata
    ├─ task
    ├─ task event
    └─ approval / cancellation
    ▲
    │ realtime listener + HTTPS
    │
対象PC
  Remote Agent
    │ localhostのみ
    ▼
  Codex Lite daemon（127.0.0.1）
    │ stdio
    ▼
  codex app-server
```

対象PCのRemote Agentは、自分宛てのFirestoreタスクを監視する。タスクを受信したときだけローカルのCodex Lite daemonへ渡す。daemonおよびapp-serverは既存方針どおり必要時に遅延起動できる。

Cloud RunまたはCloud Run functionsは、デバイス登録、ペアリング、権限が必要な検証、定期整理など、クライアントから直接実行させるべきでない短い処理に限定する。指示や結果の常時中継、長時間のWebSocket接続には使わない。minimum instancesは0を前提とする。

## 4. リモート機能の有効状態

リモート操作の有効化に自動的な期限は設けない。

- 利用者が明示的にON/OFFを切り替える。
- ONの間は軽量なRemote Agentがクラウドへの外向き接続を維持する。
- daemonとapp-serverまで常時起動する必要はない。
- OFFの間は新しいタスクを取得・実行しない。
- 対象PCの電源が切れている場合、外出先から本機構だけで電源を入れることはできない。

タスクの自動有効期限も初期版では必須としない。対象PCがオフラインの場合は`queued`のまま保持し、利用者が明示的にキャンセルできるようにする。古いタスクの誤実行防止が必要になった場合は、タスクごとの任意期限または再確認を追加検討する。

## 5. Firestoreを介した配送

初期案では、指示、実行状態、適度にまとめた途中経過、承認要求、最終回答をFirestore経由で受け渡す。

### 5.1 指示

1. 操作側クライアントが対象device、project、chatと指示を指定する。
2. Firestoreへ`queued`タスクを登録する。
3. 対象PCのRemote Agentが自分宛てのタスクだけを検出する。
4. Remote Agentがトランザクションでタスクをclaimする。
5. Remote Agentがローカルdaemonの専用リモート操作口へ指示を渡す。

### 5.2 結果

1. daemonのRunイベントをRemote Agentが受け取る。
2. Remote Agentが状態変化と表示対象イベントをFirestoreへ登録する。
3. 操作側クライアントがFirestoreの更新を購読し、進捗と結果を表示する。
4. 再接続時はFirestore上の状態とイベント連番から表示を再開する。

FirestoreへCodexの全履歴、Codex private DB、JSONL全体を複製しない。クラウドへ送るのはリモート操作で必要になったデータだけとする。

低レベルのdeltaを1件ずつ書き込むと書き込み回数と表示更新が過大になるため、次のいずれかで集約する。

- 0.5秒から1秒程度の短い間隔で本文deltaをまとめる。
- commentary、コマンド、ツール、承認要求、最終回答など、完成した表示項目単位で保存する。
- 大きいコマンド出力には単体サイズとRunごとの総量上限を設ける。

## 6. データモデル案

### devices

```text
devices/{deviceId}
```

- ownerUserId
- displayName
- clientVersion
- remoteEnabled
- status
- lastSeenAt
- createdAt
- updatedAt

### projects

```text
devices/{deviceId}/projects/{projectId}
```

- displayName
- repositoryName
- currentBranch
- lastSeenAt

ローカル絶対パスは保存しない。対象PCのローカル設定だけに`projectId`との対応を保存する。

### chats

```text
devices/{deviceId}/projects/{projectId}/chats/{chatId}
```

- codexThreadIdまたはリモート配送用内部ID
- title
- status
- lastUpdatedAt

### tasks

```text
tasks/{taskId}
```

- requestedByUserId
- targetDeviceId
- projectId
- chatId
- operation
- payloadまたはencryptedPayload
- status
- claimedByDeviceId
- createdAt
- claimedAt
- startedAt
- completedAt
- updatedAt
- errorCode

### task events

```text
tasks/{taskId}/events/{eventId}
```

- sequence
- type
- payloadまたはencryptedPayload
- createdAt

`sequence`はタスク内で単調増加させ、再接続時の重複排除と欠落検出に使う。

## 7. タスク状態と冪等性

最低限、次の状態を持つ。

| 状態 | 意味 |
|---|---|
| `queued` | 対象PCの取得待ち |
| `claimed` | 対象PCが取得済み |
| `running` | Codexが実行中 |
| `waiting_for_approval` | 利用者の承認待ち |
| `completed` | 正常完了 |
| `failed` | エラー終了 |
| `cancelled` | 利用者または対象PCにより中止 |
| `connection_lost` | 実行中にクラウドとの対応付けを失った |

claimはFirestoreトランザクションで原子的に行う。同一タスクの再配送や再接続が発生しても同じ指示を二重送信しないよう、`taskId`をローカルでも冪等キーとして記録する。

通信切断後は、Remote AgentがローカルRunと`taskId`の対応を照合し、完了済みなら結果を再登録する。実行済みか不明な指示を推測で再送しない。

## 8. リモート操作APIの境界

既存のローカルHTTP API全体をクラウドへ透過的に公開しない。Remote Agentまたはdaemonに、許可した操作だけを扱う専用境界を設ける。

初期版の対象候補:

- 対象PC、プロジェクト、チャットの一覧
- 新規チャット
- 指示送信
- 実行中の追加指示
- 実行停止
- 承認、拒否
- 実行状態、適度にまとめた進捗、最終回答の取得

初期版の対象外:

- 汎用ターミナル
- 任意コマンド実行API
- ファイルブラウザと任意ファイル取得
- project root外の読み取り
- ローカル絶対パスの表示
- 環境変数、認証情報、Cookie、token、API key、`auth.json`の取得
- daemon診断情報やローカルログの無制限転送
- Codex private DBまたはJSONLそのものの転送

## 9. 認証と認可

- 操作側WebはFirebase Authenticationを使う。
- ユーザーは自分に紐付いたdevice、project、chat、taskだけを読み書きできるようFirestore Security Rulesで制限する。
- Remote Agentは利用者ログインとは別のdevice credentialを持つ。
- device credentialをリポジトリ、通常設定ファイル、ログ、診断へ出さない。
- Windowsでは長期credentialや暗号鍵をWindows Credential ManagerなどのOS保護領域へ保存する。
- 対象device IDは表示名ではなく内部IDで判定する。
- Remote Agentは`targetDeviceId`が自端末と一致するタスクだけをclaimする。

デバイス登録とcredential発行は、Cloud Run functions等の管理処理を使う候補とする。具体的な認証方式とペアリング手順は未決定である。

## 10. ペイロード暗号化

### 10.1 目的と位置付け

HTTPSだけでは、Firebaseプロジェクトのデータを閲覧できる管理者から保存済み本文を隠せない。そのため、Firestoreへ保存する指示と応答に簡易なアプリケーション層暗号化を加える案を有力候補とする。

これは現時点では採用候補であり、詳細な鍵管理と実装方式は確定していない。暗号化を初期実装へ含めない場合でも、データ形式は後から暗号化ペイロードへ移行できる形にする。

### 10.2 単純な共通鍵方式

個人利用の初期版では、対象PCごとに共通鍵を1本持つ方式を第一候補とする。

1. 対象PCが256bitのランダムな共通鍵を生成する。
2. QRコードまたは一度だけ表示するペアリング情報で、操作側のWebまたは別PCへ鍵を渡す。
3. 以後の本文ペイロードをAES-256-GCMで暗号化する。
4. Firestoreには暗号文、nonce、key ID、暗号方式だけを保存する。
5. 鍵を失った場合や端末を失効させる場合は、対象PCで鍵を再生成して再ペアリングする。

暗号化対象:

- 指示本文
- commentaryと途中経過
- コマンド、ツール、承認要求の詳細
- 最終回答
- 本文を含むエラー詳細

平文で保持する候補:

- task ID、device ID、project ID、chat ID
- task status
- event type、sequence
- timestamp
- encryption version、key ID、暗号方式

AES-GCMの追加認証データには、少なくともprotocol version、task ID、target device ID、通信方向、event sequenceを含め、別タスクや逆方向への暗号文の付け替えを検出する。nonceは同一鍵で再利用しない。

### 10.3 初期方式の制約

- 同じ共通鍵を持つ操作端末のうち1台だけを個別失効できない。
- 1台を失効させる場合は鍵を更新し、残りの端末も再ペアリングする。
- ブラウザの保存領域を消した場合は再ペアリングが必要になる。
- Webページを配信できる管理者が悪意あるJavaScriptへ差し替える脅威までは防げない。ブラウザが鍵を使用している間の平文や鍵操作をWebアプリから完全には隔離できないためである。
- Web配信者まで信頼しない要件が生じた場合は、署名済みCodex Liteクライアントでの復号、端末ごとの公開鍵、個別の鍵ラップを検討する。

この制約を許容できる個人利用では、公開鍵基盤を最初から構築するより共通鍵方式が単純である。

## 11. Cloud Run WebSocket案との比較

Cloud RunをWebSocket中継として、操作側と対象PCの間で本文を直接流す方式は初期版では採用しない。

- WebSocket接続中はCloud Runインスタンスがアクティブになる。
- 接続期限を考慮した再接続が必要になる。
- 複数インスタンス間の状態同期が別途必要になる。
- 切断中のタスク保持と再接続表示のため、結局は永続ストアが必要になる。

Firestore経由であれば、対象PCの着信ポートを開けず、切断中のタスク保持、claim、再接続、状態表示を同じモデルで扱える。

## 12. UIの共通化

配送と実行の中核を特定UIへ依存させない。

```text
Remote Core
  ├─ Web PWA adapter
  ├─ 別PC Codex Lite adapter
  └─ Slack adapter（将来候補）
```

初期UIはWeb PWAを第一候補とする。別PCのCodex Liteは、既存daemonへ直接接続するクライアントではなく、Remote Coreを利用する別の操作側クライアントとして扱う。

## 13. 初期実装範囲案

1. 1ユーザー、1対象PCの登録とペアリング
2. リモート操作の手動ON/OFF
3. 対象PCのonline/offline表示
4. プロジェクトとチャットの一覧
5. 既存チャットへの指示と新規チャット
6. Firestoreタスクのclaimとローカルdaemonへの配送
7. 実行状態、集約した途中経過、最終回答
8. 追加指示と停止
9. 承認と拒否
10. 再接続と冪等性
11. 簡易共通鍵暗号化を採用する場合のペアリング、暗号化、鍵更新

## 14. 未決事項

- Remote AgentをWindowsクライアント内に持つか、独立した常駐プロセスにするか。
- Web PWAをFirebase Hostingで配信するか、別の静的ホスティングを使うか。
- 操作側とRemote AgentがFirestoreへ直接読み書きする範囲と、Cloud Run functionsを通す範囲。
- Remote Agent用device credentialの発行、更新、失効方式。
- Firestore Security Rulesの具体的な所有権モデル。
- 暗号化を初期版から必須にするか、データ形式だけ準備して後から有効化するか。
- QRコードに共通鍵を直接含めるか、短時間だけ有効なペアリング手順にするか。
- ブラウザ側の鍵保存方法と、鍵消失時のUX。
- イベントの集約間隔、単体サイズ、Runごとの保持上限と保持期間。
- オフライン中に長期間残った`queued`タスクを自動実行するか、実行前に再確認するか。
- リモート承認で許可する操作範囲と、ローカル画面でのみ許可する操作範囲。
- 対象PCがスリープまたは電源OFFの場合の扱い。Wake-on-LAN等は本機能とは別に検討する。

## 15. 守るべき既存方針

- daemonは引き続き`127.0.0.1`だけにbindする。
- `codex app-server`はstdioまたはlocalhostだけで接続する。
- Codex、ChatGPT、ブラウザの認証情報、Cookie、token、API key、`auth.json`を読み取り、抽出、転送、再利用しない。
- Codex private DBは通常機能から書き換えない。
- クラウドへローカル絶対パス、環境変数、秘密情報、private DB、JSONL全体を送らない。
- ファイルAPIをリモートへ透過公開しない。
- クラウドログにはID、状態遷移、エラーコードなどのメタデータだけを出し、指示、応答、暗号鍵、nonceを含む暗号材料を不用意に出さない。
- フォールバックによる複数配送経路の自動試行は設けず、実際に使われた経路を診断できるようにする。
