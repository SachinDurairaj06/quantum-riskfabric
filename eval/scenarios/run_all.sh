#!/usr/bin/env bash
# Runs the MUST matrix (prd/02 section 3: 36 runs of bank alone, the realistic bank and bank + Shield,
# 3 repeats), then rebuilds the summary.
# Usage: eval/scenarios/run_all.sh <base-rps R> [--should]
set -euo pipefail
cd "$(dirname "$0")/../.."
if [ $# -lt 1 ]; then echo "usage: $0 <base-rps> [--should]" >&2; exit 2; fi
R="$1"; shift
exec python -m eval.scenarios.runner all --base-rps "$R" "$@"
