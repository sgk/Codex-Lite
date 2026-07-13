#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${CODEX_LITE_INSTALL_DIR:-$HOME/.local/share/codex-lite}"

mkdir -p "$TARGET"
python3 -m venv "$TARGET/.venv"
"$TARGET/.venv/bin/pip" install --upgrade pip
"$TARGET/.venv/bin/pip" install -e "$ROOT/daemon"

cat > "$TARGET/run-daemon.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$ROOT/daemon"
exec "$TARGET/.venv/bin/python" -m codex_lite_daemon.main
EOF
chmod +x "$TARGET/run-daemon.sh"

echo "Installed Codex Lite daemon to $TARGET"
