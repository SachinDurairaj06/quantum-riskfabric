#!/usr/bin/env bash
# Runs every check in team/CHECKS.md in order; stops nothing early. Exit 0 only if nothing failed.
# For one check: python -m eval.checks.run C-05
set -uo pipefail
cd "$(dirname "$0")/../.."
python -m eval.checks.run all
