# Codex Lite

Codex Lite は、Windows から WSL 上の Codex を軽量に利用するためのデスクトップクライアントです。Windows 側の WPF アプリと、WSL 側の Python デーモンで構成されています。

Codex Desktop を WSL と組み合わせた際に、PC 全体が重くなることがあったため、日常的なチャット、履歴閲覧、プロジェクト切り替えに必要な機能を小さな構成で提供する目的で作りました。Codex 自体を再実装するものではなく、Codex CLI の `app-server` を実行エンジンとして使用します。

主な機能は次のとおりです。

- 複数プロジェクトと複数チャットの一覧表示、名前変更、並べ替え、アーカイブ
- Codex アプリにある既存プロジェクトとチャット履歴の読み込み
- 新規チャット、メッセージ送信、実行停止、実行中の追加指示
- ファイル・画像の添付、クリップボード画像の貼り付け
- Codex の作業状況、ツール実行、回答のストリーミング表示
- 5時間・1週間の使用容量表示
- プロジェクト内ファイルの読み取り専用ツリーとプレビュー
- 一定間隔でプロンプトを実行するオートメーション
- アクセスポリシー、Codex の状態、実行状態を確認する診断画面

詳しい動作とデータの扱いは [codex-lite-spec.md](codex-lite-spec.md) を参照してください。Slack 経由のリモート操作構想は [slack-remote-spec.md](slack-remote-spec.md) に分けています。

## 必要な環境

- Windows 11
- WSL 2（起動時に既定のディストリビューションを自動検出します）
- WSL 側の Python 3.11 以上、`venv`、`pip`
- WSL 側で実行できる Codex CLI
- Windows 側の .NET 8 SDKまたは新しい SDK

Codex CLI はあらかじめユーザー自身で導入・認証してください。Codex Lite は認証情報を読み取ったり、ログインを代行したりしません。

## ビルド方法

WSL のプロジェクトルートで、Windows の .NET SDKを使ってビルドします。

```bash
'/mnt/c/Program Files/dotnet/dotnet.exe' build windows/CodexLite.sln
```

出力先は通常、次のディレクトリです。

```text
windows/CodexLite/bin/Debug/net8.0-windows/
```

ビルド時には `CodexLite.exe`、`CodexLite.dll` に加え、`run-daemon.sh` と `daemon/` が出力先へコピーされます。配布する場合は、この一式を同じディレクトリ構成のまままとめてください。

## 起動方法

開発中は、WSL上のDLLをWindowsの `dotnet.exe` で起動します。PowerShellで次を実行してください（パスとディストリビューション名は環境に合わせて変更します）。

```powershell
$dotnet = "C:\Program Files\dotnet\dotnet.exe"
$dll = "C:\path\to\Codex-Lite\windows\CodexLite\bin\Debug\net8.0-windows\CodexLite.dll"
Start-Process -FilePath $dotnet -ArgumentList @($dll) -WindowStyle Hidden
```

`\\wsl.localhost\...` 上の `CodexLite.exe` を直接起動すると、Windows がネットワーク上の実行ファイルと判断して警告する場合があります。開発時は上記のDLL起動を推奨します。Windows側へ出力一式をコピーした場合は、`CodexLite.exe` を直接起動できます。

起動すると、アプリが `wsl.exe` 経由で同梱の `run-daemon.sh` を実行します。初回は WSL の `$HOME/.local/share/codex-lite/daemon-venv` に仮想環境を作成し、必要なPythonパッケージを導入するため、完了まで時間がかかることがあります。以後は `pyproject.toml` が変わったときだけ再導入します。

## デーモンだけを起動する方法

開発や調査のためにデーモンを単独起動できます。

```bash
cd daemon
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
CODEX_LITE_PORT=0 python -m codex_lite_daemon.main
```

空きポートで起動すると、標準出力へ次の形式で待受情報を1行出力します。ログは標準エラーへ出力します。

```json
{"event":"ready","host":"127.0.0.1","port":41237}
```

Codexを呼ばずに動作確認する場合は、疑似実行モードを使用できます。

```bash
CODEX_LITE_RUNNER=fake CODEX_LITE_PORT=0 python -m codex_lite_daemon.main
```

## テスト

```bash
source activate.sh
cd daemon
pytest
```
