"""The vault: the bank's sealed data store in $RF_DATA/vault/ (never in git).

You copy the IBM Jupyter Lab sample files in, then `python -m vault seal` records every file's SHA-256, size, header
and row count in MANIFEST.json. From then on `verify` re-hashes everything and names any file that changed, went
missing or was added. `build` reads only sealed, unchanged files, so the bank's accounts always trace back to a sealed
digest.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import time

from .schemas import classify

MANIFEST = "MANIFEST.json"
BUILD = "BUILD.json"
OWN_FILES = {MANIFEST, BUILD, MANIFEST + ".tmp", BUILD + ".tmp"}
CHUNK = 1 << 20


class VaultError(Exception):
    """The vault is missing, empty, unsealed or changed."""


def root(data_dir: str) -> str:
    return os.path.join(data_dir, "vault")


def _scan(path: str) -> tuple:
    """SHA-256, size in bytes and line count, in one pass over the file."""
    h, size, lines, last = hashlib.sha256(), 0, 0, b""
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            h.update(block)
            size += len(block)
            lines += block.count(b"\n")
            last = block[-1:]
    if size and last != b"\n":
        lines += 1
    return h.hexdigest(), size, lines


def _header(path: str) -> list:
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        return [c.strip() for c in next(csv.reader(fh), [])]


def _entry(vroot: str, rel: str) -> dict:
    path = os.path.join(vroot, rel)
    digest, size, lines = _scan(path)
    entry = {"file": rel, "sha256": digest, "bytes": size}
    if rel.lower().endswith(".csv"):
        header = _header(path)
        found = classify(rel, header)
        entry.update(kind=found["kind"], rows=max(0, lines - 1), columns=header)
        if found["missing"]:
            entry.update(expected_kind=found["expected"], missing_columns=found["missing"])
    else:
        entry["kind"] = "document"
    return entry


def files(vroot: str) -> list:
    """Every file under the vault except the vault's own records, as sorted relative paths."""
    out = []
    for dirpath, dirs, names in os.walk(vroot):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names):
            rel = os.path.relpath(os.path.join(dirpath, name), vroot).replace(os.sep, "/")
            if rel not in OWN_FILES and not name.startswith("."):
                out.append(rel)
    return out


def _digest(entries: list) -> str:
    h = hashlib.sha256()
    for e in sorted(entries, key=lambda e: e["file"]):
        h.update(f"{e['file']}\0{e['sha256']}\n".encode())
    return h.hexdigest()


def _write_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1)
    os.replace(tmp, path)


def seal(data_dir: str, reseal: bool = False) -> dict:
    vroot = root(data_dir)
    if not os.path.isdir(vroot):
        raise VaultError(f"{vroot} does not exist: copy the sample files there first")
    mpath = os.path.join(vroot, MANIFEST)
    previous = None
    if os.path.exists(mpath):
        if not reseal:
            raise VaultError("the vault is already sealed; use --reseal to record a deliberate change")
        previous = load_manifest(data_dir)["digest"]
    rels = files(vroot)
    if not rels:
        raise VaultError(f"{vroot} has no files to seal")
    entries = [_entry(vroot, rel) for rel in rels]
    manifest = {"sealed_ts": time.time(), "digest": _digest(entries), "replaces": previous, "files": entries}
    _write_json(mpath, manifest)
    return manifest


def load_manifest(data_dir: str) -> dict:
    mpath = os.path.join(root(data_dir), MANIFEST)
    if not os.path.exists(mpath):
        raise VaultError("the vault is not sealed yet: run `python -m vault seal`")
    with open(mpath, encoding="utf-8") as fh:
        return json.load(fh)


def verify(data_dir: str) -> dict:
    manifest = load_manifest(data_dir)
    vroot = root(data_dir)
    sealed = {e["file"]: e for e in manifest["files"]}
    present = set(files(vroot))
    changed = [f for f in sorted(present & sealed.keys()) if _scan(os.path.join(vroot, f))[0] != sealed[f]["sha256"]]
    missing = sorted(sealed.keys() - present)
    added = sorted(present - sealed.keys())
    return {"ok": not (changed or missing or added), "digest": manifest["digest"], "files": len(sealed),
            "changed": changed, "missing": missing, "added": added}


def of_kind(manifest: dict, kind: str) -> list:
    return [e for e in manifest["files"] if e.get("kind") == kind]


def path_of(data_dir: str, entry: dict) -> str:
    return os.path.join(root(data_dir), entry["file"])


def write_build(data_dir: str, report: dict) -> str:
    path = os.path.join(root(data_dir), BUILD)
    _write_json(path, report)
    return path
