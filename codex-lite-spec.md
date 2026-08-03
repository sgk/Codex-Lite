# Codex Lite 仕様書

## 1. 概要

Codex Lite は、Windows の単一ウィンドウから、WSL 上の複数プロジェクトと Codex チャットを扱う軽量クライアントである。Windows 側は表示と操作を担う WPF アプリ、WSL 側は状態管理と Codex 連携を担う Python デーモンとする。

Codex の推論やツール実行は再実装せず、Codex CLI の `app-server` を利用する。Codex アプリの既存チャットについては、ローカルの state SQLite から必要最小限のメタデータを読み取り、公開されたJSONL履歴ファイルから表示内容を構築する。

本書は将来構想ではなく、現在の実装を記述する。

## 2. 構成

```text
Windows
  CodexLite.exe / CodexLite.dll（C#、.NET 8、WPF）
    │ wsl.exeで起動
    │ HTTP/JSON + SSE（127.0.0.1、動的ポート）
    ▼
WSL
  run-daemon.sh
    └─ Python仮想環境
        └─ Starletteデーモン
            ├─ Codex Lite SQLite
            ├─ Codex state SQLite（読み取り専用）
            ├─ Codex JSONL履歴（読み取り専用）
            └─ codex app-server（標準入出力JSON-RPC）
```

Windowsアプリはビルド成果物に同梱された `run-daemon.sh` と `daemon/` だけを使用し、開発用チェックアウトなど別経路へのフォールバックは行わない。

## 3. 対象環境

- Windows 11、WSL 2
- WSLディストリビューションはWindows側で設定された既定環境を起動時に自動検出する
- Windows側: .NET 8 WPF
- WSL側: Python 3.11以上、Starlette、uvicorn、SQLite
- WSLから実行可能で、ユーザーによる認証が完了した Codex CLI

プロジェクトは実在する絶対パスに限る。Linuxパスに加え、設定で許可されている場合は `/mnt/c` 上のパスも扱う。

## 4. Windowsクライアント

### 4.1 画面構成

左ペインにプロジェクトとチャットのツリー、右ペインに選択対象の内容を表示する。右ペインには次のタブがある。

- 「会話」: ユーザー指示、Codexの作業中メッセージ、ツール実行状況、最終回答を時系列表示する。
- 「ファイル」: プロジェクト配下のディレクトリツリーとファイル内容を読み取り専用で表示する。Markdownは専用表示を使い、リンクからプロジェクト内の別ファイルへ移動できる。基本的なMarkdown表は罫線付きの表として表示する。VS Codeまたは既定アプリで開く操作も提供する。ファイルツリーで選択したファイルまたはディレクトリはWindows Explorerへコピーでき、Explorerでコピーした項目は選択中のプロジェクト内ディレクトリへ貼り付けられる。貼り付け時に同名項目が存在する場合は上書きしない。
- 「オートメーション」: 選択チャットに紐づく定期実行を作成、編集、有効化、即時実行、削除する。
- 「診断」: app-server、実行中Run、Codex、権限設定、デーモン診断情報を表示する。

下部には現在の状態、Explorer・VS Codeでプロジェクトを開くボタン、設定ボタンを置く。

### 4.2 プロジェクトとチャット

プロジェクトはディレクトリ指定で登録するほか、Codex履歴から候補を検出して一括登録できる。表示名の変更、登録解除、ドラッグによる並べ替えに対応する。登録解除はCodex側のプロジェクトやファイルを削除しない。

チャットはプロジェクト配下へ表示し、選択、絞り込み、名前変更、アーカイブ、ドラッグによる並べ替えができる。並べ替え順はWindowsクライアントのUI状態として保存する。プロジェクトだけを選ぶと新規チャット入力欄を表示し、最初の送信時にスレッドを作成する。新設したチャットは、閉じていたプロジェクトを展開したうえで選択状態にする。既存チャットを選ぶと履歴と入力欄を表示する。

履歴は新しい範囲を先に読み、上端へ移動したときに過去分を追加する。外部で履歴が更新されても、利用者が末尾付近を見ていない限りスクロール位置を末尾へ強制移動しない。

### 4.3 メッセージ送信

Enterで送信し、改行操作は修飾キーを使う。送信履歴は上下キーで再利用できる。ファイル選択、ドラッグ＆ドロップ、クリップボード画像から添付を追加できる。添付パスはWSLパスへ変換してapp-serverへ渡す。非画像ファイルは、Codexが同名ファイルを別の場所から探索せず、変換後の絶対パスを直接確認するよう送信内容にも明示する。

送信後はSSEでイベントを受け取り、作業中の説明、ツール実行、回答の差分、完了・失敗を逐次反映する。実行中は停止できる。また、同じチャットの実行中に入力した指示は `turn/steer` により追加指示として送る。画面上は実行中でもapp-server側ですでにターンが完了していた場合、入力内容を失敗させず同じスレッドの次ターンとして開始する。停止時はapp-serverのスレッド状態が `idle` になるまで追加指示と次ターン開始を無効にし、停止前後のターンIDを混在させない。

同時実行はチャットをまたいで管理し、既定の上限は4である。

クリップボード画像などの一時添付ファイルは `%LOCALAPPDATA%\CodexLite\attachments` に保存する。Windowsクライアント起動時に、7日より古い一時添付ファイルを削除する。削除に失敗したファイルがあっても起動は継続する。

### 4.4 使用状況

プロジェクト選択時の新規チャット画面に、Codexから取得した5時間容量と1週間容量を数値とグラフで表示する。利用者は手動で再読み込みできる。この機能はapp-serverモードでのみ利用できる。

### 4.5 設定

次のUI設定をWindows側に保存し、次回起動時に復元する。

- ウィンドウ位置とサイズ
- 左ペイン幅、展開中のプロジェクト、選択状態、プロジェクト順
- 文字サイズ、ファイル表示の折り返し
- Codex HOMEの選択

Codex HOMEは「自動」「Windows側 `.codex`」「WSL側 `$HOME/.codex`」から選ぶ。自動ではWindowsユーザープロファイルの `.codex` が存在すればそれを優先する。変更時はデーモンを再起動し、プロジェクトツリーを読み直す。複数のHOMEを同時に統合しない。

Codexの実行設定はWSL側の `settings.json` に保存し、スレッド作成時と送信前にapp-serverへ適用する。

モデルと承認レベルはチャット入力欄から選択する。モデル候補はデーモンの `/models` を介して app-server の `model/list` から取得し、新しいモデルIDが返ればクライアントの変更なしで表示する。「既定」は空のモデル値としてCodex側の既定に従う。app-serverでモデル一覧を取得できない場合に限り、デーモンが持つ最小限の静的候補を表示する。DeepSeek APIキーがWSL側のユーザー専用ファイル（`~/.config/codex-lite/deepseek.env`）に設定されている場合は `deepseek-v4-flash` を候補へ追加する。

OpenAIとDeepSeekの切り替えは、チャットごとのモデル選択に応じて、それぞれ専用のCodex app-serverと内部スレッドを使い分ける。DeepSeekを選択した場合はDeepSeekのResponses API設定とモデルカタログを使う。プロバイダーを切り替えたときは、切替先を最後に使用してから増えたユーザー指示と最終回答だけを表示会話の文脈として渡し、切替先自身が保持する履歴、提供元固有のreasoning、作業中表示、ツール詳細、過去に合成した引き継ぎ文脈を重複して渡さない。切替先を初めて使う場合も、引き継ぎ文脈はメッセージ境界を保って上限内に収める。実行中のRunを切断する切り替えは行わず、APIキーの内容はログ・診断・画面へ出さない。

自動圧縮は app-server 起動時の Codex config として有効化する。既定では
`model_auto_compact_token_limit=100000` と
`model_auto_compact_token_limit_scope="total"` を指定し、同じ app-server 上で継続・新規作成される全チャットセッションに適用する。`CODEX_LITE_AUTO_COMPACT_TOKEN_LIMIT=0` を指定した場合は Codex Lite から自動圧縮設定を渡さない。

承認モードはCodexデスクトップアプリに合わせ、チャット入力欄から次の3つを選択する。

- 承認を求める: `permissions=:workspace`、`approvalPolicy=on-request`、`approvalsReviewer=user`
- 自動で承認: `permissions=:workspace`、`approvalPolicy=on-request`、`approvalsReviewer=auto_review`
- フルアクセス: `permissions=:danger-full-access`、`approvalPolicy=never`、`approvalsReviewer=user`

承認モードはサンドボックス境界と承認要求の扱いを一体として切り替える。別のアクセスポリシーを診断画面から変更する経路は設けない。思考レベルはモデル一覧が返す `supportedReasoningEfforts` を使い、モデルごとの候補をチャット入力欄に表示する。候補を取得できない場合は「既定」「低」「中」「高」「最大」を表示し、選択値はapp-serverの `thread/settings/update` の `effort` に渡す。

## 5. デーモン

### 5.1 起動と終了

Windowsアプリは `wsl.exe -d <ディストリビューション> -- bash -c ...` で同梱スクリプトを起動する。起動時に login shell は使わない。これは `.bash_profile` 等で ssh-agent や ssh-add がパスフレーズ入力待ちになり、デーモン起動が止まることを避けるためである。スクリプトは `$HOME/.local/share/codex-lite/daemon-venv` を作成または再利用し、同梱 `pyproject.toml` のハッシュが変わった場合だけパッケージを再導入する。

開発ビルドでは `windows/CodexLite/bin` をビルド成果物、`runtime/CodexLite` を実行用配置先として分離する。ビルドが成功するまでは実行中クライアントを停止しない。成功後の配置は独立したWindowsプロセスへ引き渡し、隣接ディレクトリへのコピー完了後にクライアントを正常終了して実行用ディレクトリを入れ替え、最新クライアントを起動する。デスクトップショートカットは実行用配置先を参照する。

初回起動時間と Python バージョン差の影響を抑えるため、デーモンのHTTP層は Starlette と標準 uvicorn を使い、FastAPI/Pydantic や `uvicorn[standard]` のようなネイティブ拡張を含みやすい依存には頼らない。配布ZIPには仮想環境そのものを同梱しない。仮想環境は起動先WSLのPythonで作成し、Python 3.11以上の複数バージョンに対応する。

デーモンは必ず `127.0.0.1` にbindし、既定ではOSが選んだ空きポートを使う。待受完了時、標準出力へ `{"event":"ready","host":"127.0.0.1","port":...}` を1行出す。Windowsアプリはこの行を読んでHTTP接続先を決める。標準エラーはログとして回収する。

Windowsアプリは定期的にhealth checkを行い、デーモンが応答しなければ再起動する。アプリ終了時は `/shutdown` を呼び、標準入力を閉じる。デーモンは標準入力EOFも終了要求として扱い、app-serverとデータベースを閉じる。

### 5.2 app-server

通常モードは `codex app-server --listen stdio://` を子プロセスとして起動し、改行区切りJSON-RPCで通信する。app-serverは一覧や履歴を表示しただけでは起動せず、スレッド作成、送信、名前変更などapp-server操作が必要になった時点で遅延起動する。デーモンごとに1プロセスを共有し、チャット数やプロジェクト数だけ増殖させない。

主に使用する操作は次のとおりである。

- 初期化: `initialize`、`initialized`
- スレッド: `thread/start`、`thread/name/set`、`thread/archive`
- ターン: `turn/start`、`turn/interrupt`、`turn/steer`
- 使用容量の取得

app-serverの通知はデーモン内のRunへ対応付け、WindowsクライアントへSSEで配信する。Runは `queued`、`running`、`succeeded`、`failed`、`cancelled` の状態を持つ。

### 5.3 オートメーション

デーモン内のスケジューラーが有効なオートメーションを監視する。実行予定は、1分以上の分単位間隔、毎時の指定分（0〜59分）、毎日の指定時刻（ローカル時刻）から選択できる。Windowsクライアントでは方式と時・分をドロップダウンで選択させ、選択内容の具体例と動作説明を表示する。期限になると指定チャットへ保存済みプロンプトを送る。多重起動を防ぐ `running` 状態を持ち、次回時刻、最終実行時刻、最終エラーを記録する。手動の「今すぐ実行」も同じ実行サービスを使う。

オートメーション編集欄は、既存項目または「新規」で作成した下書きを選択するまで無効にする。

### 5.4 HTTP API

APIはWindowsクライアント用のローカルAPIであり、外部公開しない。

| 分類 | 主なAPI |
|---|---|
| 稼働・設定 | `GET /health`、`GET /diagnostics`、`GET/PATCH /settings`、`POST /shutdown` |
| 使用状況 | `GET /usage/capacity`（OpenAI/Codexの5時間・1週間容量、Codexクレジット、DeepSeek選択時の残高） |
| プロジェクト | `GET/POST /projects`、`GET/PATCH/DELETE /projects/{id}`、履歴候補の一覧・取込 |
| チャット | 一覧、作成、取得、名前変更、アーカイブ、削除 |
| メッセージ | 一覧、カーソル付きページ取得、送信 |
| Run | 取得、SSEイベント、停止、追加指示 |
| ファイル | ディレクトリ一覧、内容取得 |
| オートメーション | 一覧、作成、更新、即時実行、削除 |

エラーは `error.code`、`error.message`、必要に応じて `error.details` を持つJSONで返す。

## 6. Codexアプリのデータとの連携

### 6.1 読み取るデータ

Codex Liteは、選択中の本人の `CODEX_HOME` に対応する `sqlite` ディレクトリから `state*.sqlite` を探し、`threads` テーブルがあるDBを読み取り専用URIで開く。通常同期で読むのは次のthreadメタデータに限る。

- `id`: CodexスレッドID
- `cwd`: プロジェクトの作業ディレクトリ
- `title`、`preview`、`first_user_message`: 表示タイトルの決定材料
- `archived`、`archived_at`: アーカイブ状態
- `rollout_path`: 対応するJSONL履歴ファイル
- `created_at`、`updated_at`、`recency_at` と各ミリ秒版: 作成・更新時刻

DBは候補の存在、`threads` テーブル、必須カラムを検査してから読む。想定スキーマでない場合は推測で処理せず同期を停止し、理由だけを診断情報へ出す。認証情報、Cookie、トークン、APIキー、`auth.json` は読まない。

`cwd` はWindowsドライブ表記なら `/mnt/<ドライブ>/...` に変換し、実在するディレクトリだけを候補にする。`rollout_path` は実在する `.jsonl` で、選択中の `CODEX_HOME` の `sessions` または `archived_sessions` 配下にある場合だけ採用する。

対応するJSONLの `session_meta.payload.source.subagent.other` が `guardian` のスレッドは、Codexの自動承認判断に使われる内部セッションのため、プロジェクト候補とチャット一覧へ表示しない。その他のサブエージェントセッションは通常どおり同期する。

### 6.2 履歴の表示

チャット履歴はstate DBそのものから本文を読まず、検証済みのJSONLファイルを読み取って組み立てる。主に次を表示対象とする。

- ユーザーメッセージ
- assistantの作業中メッセージと最終回答
- 関数・ツール呼び出しの完了結果。コマンド実行は要約と詳細出力を分けて表示する
- Codexが公開したreasoning summary。非公開の推論内容や暗号化された推論データは表示しない

ライブ実行と履歴のツール表示では、取得できた実行メソッド、引数、コマンド、作業ディレクトリ、出力、終了状態を可能な限り省略せず表示する。連続する開始・終了イベントを単一の要約へ上書きせず、低レベルの出力差分は対応する実行項目の詳細へ追記する。認証情報、Cookie、token、API key、secretなどは表示前に伏せる。

壊れたJSON行や未対応イベントは読み飛ばす。JSONLのみから発見した履歴、アーカイブ済み履歴、現在の `CODEX_HOME/sessions` 外の履歴は表示専用とし、継続できない理由をUIへ返す。

### 6.3 同期と状態変更

Codex Liteの `chats` テーブルは、Codex threadの一覧表示に必要なローカル投影である。state DBからの同期は読み取り専用であり、Codex private DBへ直接書き込まない。

新規スレッド、メッセージ送信、追加指示、停止、名前変更、アーカイブなどの状態変更はapp-serverを使う。アーカイブ後はローカル投影も更新する。プロジェクト登録名などCodex Lite固有のUI状態はCodex Lite DBだけで管理する。

## 7. Codex Liteの保存データ

既定の保存先はWSL ext4上の `$HOME/.local/share/codex-lite` である。

```text
$HOME/.local/share/codex-lite/
  codex-lite.db
  settings.json
  runs/
  daemon-venv/
```

SQLiteには次を保存する。

- `projects`: 登録済みプロジェクトのID、表示名、パス、時刻
- `chats`: プロジェクトとの対応、タイトル、CodexスレッドID、JSONLパス、継続可否、アーカイブ状態
- `messages`: fake/互換実行用メッセージと種別
- `runs`: 実行状態、PID、終了コード、ログパス、エラー
- `automations`: 名前、プロンプト、実行予定の種類と値、有効状態、実行時刻、エラー

主要状態をWindows側 `.codex` や `/mnt/c` に保存しない。ただし、利用者が設定でWindows側 `.codex` をCodex HOMEとして選んだ場合は、Codex自身の既存状態を読み取る。

## 8. ファイルAPIの安全境界

ファイルAPIは読み取り専用である。相対パスを正規化して登録プロジェクトのroot配下であることを検証し、root外へのパストラバーサルやシンボリックリンク経由の逸脱を拒否する。ファイルサイズとテキスト判定の制限を設け、編集・作成・削除APIは持たない。

診断情報には実行環境、パスの存在、Codexバージョン、Run、app-server状態などを含めるが、認証情報や秘密情報の内容は出力しない。

## 9. 設定値

デーモンは既定値、`$HOME/.config/codex-lite/config.toml`、環境変数の順に設定を上書きする。主な環境変数は次のとおりである。

| 環境変数 | 内容 | 既定 |
|---|---|---|
| `CODEX_LITE_HOST` | bind先 | `127.0.0.1` |
| `CODEX_LITE_PORT` | ポート。0は自動選択 | `0` |
| `CODEX_LITE_WSL_DISTRO` | WSL名（デーモン単独起動時の診断用） | `WSL_DISTRO_NAME`、未設定時は空文字列 |
| `CODEX_LITE_APP_DATA_DIR` | データディレクトリ | `$HOME/.local/share/codex-lite` |
| `CODEX_LITE_DATABASE` | SQLiteパス | データディレクトリ内 |
| `CODEX_LITE_RUN_LOG_DIR` | Runログ | データディレクトリ内 `runs` |
| `CODEX_LITE_CODEX_HOME` | Codex HOME | `$HOME/.codex` |
| `CODEX_LITE_CODEX_SQLITE_HOME` | state DBディレクトリ | Codex HOME内 `sqlite` |
| `CODEX_LITE_CODEX_PATH` | Codex実行ファイル | PATHから解決 |
| `CODEX_LITE_MAX_CONCURRENT_RUNS` | 同時実行数 | `4` |
| `CODEX_LITE_ALLOW_MNT_C_PROJECTS` | `/mnt/c`を許可 | `true` |
| `CODEX_LITE_RUNNER` | `app-server` または `fake` | `app-server` |
| `CODEX_LITE_PERMISSION_PROFILE` | 初期アクセスポリシー | `:danger-full-access` |
| `CODEX_LITE_APPROVAL_POLICY` | 初期承認方法 | `never` |
| `CODEX_LITE_MODEL` | 初期モデル。空文字列はCodex既定 | 空文字列 |

## 10. 現在の範囲外

- Codex Desktopの完全互換UI
- Codexの認証やログイン操作
- private Codex DBへの通常機能としての書き込み
- プロジェクトファイルの編集、作成、削除
- ブラウザ操作、Computer Use、各種外部サービス用UI
- Codex以外のAI実行エンジン
- 外部ネットワークへ公開するデーモン
