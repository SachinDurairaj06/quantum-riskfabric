"""The core books (prd/03 section 4.5): the record of money the bank actually moved.

Only ALLOW posts, once per `txn_id` (`bank:posted:{txn_id}`), so a retried settle never posts twice. A posting is one
line in $RF_DATA/bank/postings.jsonl plus a debit and a credit on `bank:bal:{acct}`. There is no overdraft logic:
balances may go negative, which is out of scope and documented in bank/README.md.
"""
from __future__ import annotations

import json
import os
import time


class Books:
    def __init__(self, r, postings_path: str, banklog):
        self.r, self.log, self.path = r, banklog, postings_path
        os.makedirs(os.path.dirname(postings_path), exist_ok=True)
        self.posted_count, self.posted_value = 0, 0.0
        if os.path.exists(postings_path):  # a restart mid-run keeps counting from the file
            with open(postings_path, encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        self.posted_count += 1
                        self.posted_value += float(json.loads(line).get("amount", 0))
        self.fallback_count, self.fallback_value = 0, 0.0
        self.fh = open(postings_path, "a", encoding="utf-8", buffering=1)

    def close(self) -> None:
        self.fh.close()

    async def post(self, p: dict, req_id: str, via: str, path: str, fallback: bool = False) -> bool:
        """True if this call posted the payment; False if it was already posted."""
        if not await self.r.set(f"bank:posted:{p['txn_id']}", "1", nx=True):
            return False
        amount = float(p["amount"])
        pipe = self.r.pipeline(transaction=False)
        pipe.incrbyfloat(f"bank:bal:{p['from_acct']}", -amount)
        pipe.incrbyfloat(f"bank:bal:{p['to_acct']}", amount)
        await pipe.execute()
        self.fh.write(json.dumps({"ts": time.time(), "txn_id": p["txn_id"], "req_id": req_id, "from_acct": p["from_acct"],
                                  "to_acct": p["to_acct"], "amount": amount, "currency": p["currency"], "via": via,
                                  "path": path}, separators=(",", ":")) + "\n")
        self.posted_count += 1
        self.posted_value += amount
        if fallback:
            self.fallback_count += 1
            self.fallback_value += amount
        return True

    async def settle(self, body: dict) -> dict:
        p, action, req_id = body["payment"], body.get("action"), body.get("req_id") or ""
        posted = False
        if action == "ALLOW":
            posted = await self.post(p, req_id, body.get("via") or "shield", body.get("path") or "")
        self.log.add(txn_id=p["txn_id"], req_id=req_id, entry="settle", action=action, posted=posted)
        return {"txn_id": p["txn_id"], "posted": posted, "duplicate": action == "ALLOW" and not posted}
