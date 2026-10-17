# perimeter/ — the bank's perimeter, as a local stand-in (owner: D)

A real bank's traffic passes a CDN with DDoS protection (for example Cloudflare), then a WAF and an API gateway (for example AWS WAF and API Gateway), before it reaches the bank. This folder plays those three layers in one process on `127.0.0.1:8000` (plus `RF_PORT_OFFSET`), in front of the edge on 8080, so the demo shows the whole chain. No real Cloudflare or AWS service is used, and nothing leaves the machine.

```
lab --> perimeter :8000 --> edge :8080 --> Shield router :8001 --> bank :9000 --> books, vault
        CDN, WAF,           RiskFabric's edge, or the bank's own edge for bank_only and bank_realistic
        API gateway
```

## Run
```bash
python -m perimeter                 # serve
python -m perimeter limit --set     # the global ceiling from this host's measured capacity
```

## The layers (`layers.py`)
| Layer | Checks | Answer |
|---|---|---|
| CDN and DDoS | IP blocklist (`perimeter:blocklist`); per-IP rate above `cfg:perimeter_ip_rps` (default 20 per second) | 403; 429 with `Retry-After: 1` |
| WAF | only `POST /pay` and `POST /verify`; JSON only; at most 8 KB; SQL injection, script injection and path traversal in any string value | 403 |
| API gateway | global ceiling `cfg:perimeter_limit_rps`; the payment and verify schema | 429 with `Retry-After: 1`; 400 |

`GET /perimeter/stats` counts what each layer stopped, by reason. `GET /perimeter/health` answers 200.

**What it can't do, on purpose:** none of the layers sees payment outcomes or links identities. Bots that each stay under the per-IP rate and send clean JSON pass all three. That gap is what RiskFabric's edge and Sentinel fill, and the stats make it visible in the demo.

## The request limit (`python -m perimeter limit`)
The ceiling is what this host can actually process, measured, never guessed:

`ceiling = 0.8 x cores x 1,000,000 / (edge + Sentinel + decision + bank CPU microseconds per request)`

- **Where the inputs come from:** the CPU figures and the core count come from C's capacity run, `python -m eval.scenarios.runner capacity`, saved as `eval/results/CAP__shield__<stamp>.json`.
- **How each request is costed:** as if it passed the edge, so the ceiling holds even on a day with no attack to shed.
- **The 0.8:** leaves 20% headroom for the OS, Valkey and bursts.
- **Where to run it:** on the LinuxONE VM, after the capacity run there. It refuses figures measured on another host.
- **Without a capacity run:** the ceiling stays off.

## Tests
`python -m pytest perimeter`: every layer rule, the forwarding, the stats, the Valkey policy and the ceiling rule.
