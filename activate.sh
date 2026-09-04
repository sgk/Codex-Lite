#!/usr/bin/env bash

if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  _ACTIVATE_PREV_OPTS=$(set +o)
fi

set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
VENV_DIR="${ROOT_DIR}/daemon/.venv"

if [ -f "${VENV_DIR}/bin/activate" ]; then
  # shellcheck source=/dev/null
  source "${VENV_DIR}/bin/activate"
else
  echo "venv not found: ${VENV_DIR}/bin/activate" >&2
  echo "create it with: python3 -m venv daemon/.venv && daemon/.venv/bin/pip install -e 'daemon[dev]'" >&2
fi

export CODEX_LITE_REPO="${ROOT_DIR}"
export CLOUDSDK_CONFIG="${ROOT_DIR}/.gcloud"
export XDG_CONFIG_HOME="${ROOT_DIR}/.firebase-cli-config"

mkdir -p "${CLOUDSDK_CONFIG}" "${XDG_CONFIG_HOME}"

echo "Activated Codex Lite venv at ${VENV_DIR}"
echo "GCP config: ${CLOUDSDK_CONFIG}"
echo "Firebase CLI auth/config: ${XDG_CONFIG_HOME}"

if [ -n "${_ACTIVATE_PREV_OPTS:-}" ]; then
  eval "${_ACTIVATE_PREV_OPTS}"
  unset _ACTIVATE_PREV_OPTS
fi
