#!/usr/bin/env bash
# One command to bring the whole RiskFabric stack up on the LinuxONE VM (bank, edge, Sentinel, console).
# It frees the ports first, so "address already in use" can't happen, waits for health, and prints the URL.
#
#   ./run.sh                 # start everything, system = shield
#   RF_HOST=0.0.0.0 ./run.sh # also reachable from another machine at http://<vm-ip>:8500
#   ./run.sh bank_realistic  # a different system (bank_only | bank_realistic | shield)
set -u
cd "$(dirname "$0")"

export RF_DATA="${RF_DATA:-/home/linux1/riskfabric-data}"
export RF_HOST="${RF_HOST:-127.0.0.1}"
SYSTEM="${1:-shield}"
OFF="${RF_PORT_OFFSET:-0}"
PORTS=$((8000+OFF))/tcp\ $((8001+OFF))/tcp\ $((8080+OFF))/tcp\ $((8500+OFF))/tcp\ $((9000+OFF))/tcp

echo "RiskFabric: RF_DATA=$RF_DATA  RF_HOST=$RF_HOST  system=$SYSTEM"

# free the ports from any leftover run (fuser if present, else pkill our own processes)
if command -v fuser >/dev/null 2>&1; then
  fuser -k $PORTS >/dev/null 2>&1 || true
else
  pkill -f "riskfabric.*-m (perimeter|decision|bank|console|sentinel)|-m devstack" >/dev/null 2>&1 || true
fi
sleep 2

echo "starting the stack (Ctrl+C stops everything)..."
exec python -m devstack up --system "$SYSTEM" --allow-reset
