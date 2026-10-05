#!/bin/bash
# Launch the GitHub Actions runner once, using a just-in-time (JIT) config.
#
# ENCODED_JIT_CONFIG is passed in by app.py (derived from the MicroVM run-hook
# payload). Running with --jitconfig makes the runner register, process exactly
# one job, auto-deregister from GitHub, and exit — the ephemeral model.
set -euo pipefail

if [ -z "${ENCODED_JIT_CONFIG:-}" ]; then
  echo "Error: ENCODED_JIT_CONFIG is required (generate via GitHub's generate-jitconfig API)" >&2
  exit 1
fi

exec ./run.sh --jitconfig "$ENCODED_JIT_CONFIG"
