#!/usr/bin/env bash
# One command: start the full RiskFabric stack on the VM and drive a 5-minute attack scenario through it.
# Watch it live in a browser at http://localhost:8500 (through your SSH tunnel) while this runs.
set -u
cd "$(dirname "$0")"
export RF_DATA="${RF_DATA:-/home/linux1/riskfabric-data}"
export RF_HOST="${RF_HOST:-127.0.0.1}"
LOGDIR="$RF_DATA/run"; mkdir -p "$LOGDIR"

free_ports() {
  if command -v fuser >/dev/null 2>&1; then
    fuser -k 8000/tcp 8001/tcp 8080/tcp 8500/tcp 9000/tcp >/dev/null 2>&1 || true
  else
    pkill -f "riskfabric.*-m (perimeter|decision|bank|console|sentinel)|-m devstack" >/dev/null 2>&1 || true
  fi
}

echo "[1/3] freeing ports..." ; free_ports ; sleep 2
echo "[2/3] starting the stack (log: $LOGDIR/devstack.log)..."
nohup python -m devstack up --system shield --allow-reset > "$LOGDIR/devstack.log" 2>&1 &
for _ in $(seq 1 40); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8500/ 2>/dev/null)" = "200" ] && break
  sleep 1
done
C=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8500/ 2>/dev/null)
B=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9000/bank/books 2>/dev/null)
echo "      console:$C bank:$B   ->  open http://localhost:8500 (through your SSH tunnel)"
if [ "$C" != "200" ]; then echo "stack did not come up; see $LOGDIR/devstack.log" >&2; exit 1; fi

echo "[3/3] sending a 5-minute S4 attack scenario (bots + hidden laundering) through the perimeter..."
python -m lab run --scenario S4 --run-id judge-demo --system shield \
    --base-rps 10 --entry perimeter --duration-s 300 \
    --replay "$RF_DATA/replay/ibm_cards_test.csv"

echo "done. the stack is still running for you to explore. stop it with: ./stop.sh"
