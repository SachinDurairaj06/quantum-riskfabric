# Running the demo on the LinuxONE VM

Everything runs **on the VM**. You view it from your laptop browser through an SSH tunnel. Nothing to install on your laptop.

## One-time setup (already done on our VM)
```bash
export RF_DATA=/home/linux1/riskfabric-data         # where the bank data and models live (NOT in git)
pip install numpy pandas scikit-learn fastapi uvicorn httpx redis fakeredis
python -m vault seal && python -m vault build        # builds the bank accounts, tiers and replay from the IBM data
python -m scoring train  --source cards --path $RF_DATA/vault/transactions_history.csv
python -m scoring export --source cards --path $RF_DATA/vault/transactions_history.csv
```

## Run the live demo — one command (on the VM)
```bash
cd ~/riskfabric
export RF_DATA=/home/linux1/riskfabric-data
./demo.sh
```
`demo.sh` frees the ports, starts the whole stack (bank, edge, Sentinel, console), waits until it is healthy, then sends a 5-minute attack scenario (a bot flood with laundering hidden inside it) through the front door. It prints `console:200 bank:200` when ready. Stop it later with `./stop.sh`.

## View it from your laptop
Open **one** terminal on your laptop and leave it open (it forwards the VM's ports to your machine):
```bash
ssh -i <your-key.pem> -o ServerAliveInterval=30 \
    -L 8500:127.0.0.1:8500 -L 5001:127.0.0.1:5001 \
    linux1@<vm-ip>
```
Then in your laptop browser:
- **http://localhost:8500** — the RiskFabric console (payment funnel, load level, decisions, hold queue, quantum challenger)
- **http://localhost:5001** — the attack / banking simulator dashboard

The VM only listens on its own loopback, so the SSH tunnel is how the browser reaches it — no VPN or firewall change needed.

## What you are looking at
- The console only shows live data while traffic is flowing (during `demo.sh`'s 5-minute run). Use the time-window buttons (60s / 5m / 15m / 1h) at the top to see the whole run.
- The quantum section shows the pre-registered gate's verdict and the full evaluation matrix. "No advantage found" is the honest, expected result for 2-local quantum features on tabular data; the gate is calibrated to demand a reproducible edge before claiming one. `python -m quantum.research.evaluate control` runs a labelled calibration self-test.

## If something looks stuck
```bash
./stop.sh && ./demo.sh          # clean restart
curl -s -o /dev/null -w "console:%{http_code} bank:%{http_code}\n" http://127.0.0.1:8500/   # should be 200 200
```
