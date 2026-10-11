#!/usr/bin/env bash
# Standalone Console preview; does not launch or call the production GUI.
set -euo pipefail
cd "$(dirname "$0")"
exec "${PREVIEW_PYTHON:-python3}" -m app.preview "$@"
