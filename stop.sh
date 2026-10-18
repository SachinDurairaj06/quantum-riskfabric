#!/usr/bin/env bash
# Stop the RiskFabric stack and free its ports.
if command -v fuser >/dev/null 2>&1; then
  fuser -k 8000/tcp 8001/tcp 8080/tcp 8500/tcp 9000/tcp >/dev/null 2>&1 || true
else
  pkill -f "riskfabric.*-m (perimeter|decision|bank|console|sentinel)|-m devstack" >/dev/null 2>&1 || true
fi
echo "RiskFabric stack stopped."
