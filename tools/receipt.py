"""RECEIPT — why the ladder cannot be skipped.

`--dry-run` writes a receipt; `--apply` refuses without one. That is the
whole mechanism that makes "the user has seen the diff" true rather than
aspirational: the approved diff is a hash of a specific plan on a specific
machine, and the machine can invalidate it.

Three things live here:

- the **plan hash** — a digest of the profile, the ordered actions and the
  key fingerprint. A changed machine, a changed plan or a changed key
  produces a different hash, and the receipt expires.
- the **receipt** — the small JSON file written by a dry run. It holds
  hashes and counts. Never a value, never a credential, never an absolute
  home path.
- the **manifest** — the record of which files this tool owns. It is what
  makes a second run a no-op instead of an overwrite.

Standard library only.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

SCHEMA = 1
PROBE_VERSION = 1

# Where the receipt lives: inside the repo, gitignored, and never published.
RECEIPT_REL = Path(".genesis-bootstrap") / "receipt.json"


def canonical(obj) -> str:
    """The canonical form a hash is taken over.

    Sorted keys, no incidental whitespace, no ASCII escaping — so the same
    plan hashes the same way on any machine, in any Python, at any time.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def digest(*parts) -> str:
    """`sha256:<hex>` over the canonical form of each part."""
    joined = "\n".join(canonical(p) for p in parts)
    return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()


def plan_hash(profile: dict, actions: list, key_fingerprint: str = "") -> str:
    """The hash of a plan.

    Each action contributes `(id, kind, target, desired, effect)`. Cosmetic
    differences — an unrelated file's mtime, the order of a set — do not
    change it; a real difference in what would be done does.

    The key fingerprint is in there on purpose. The receipt says *this plan,
    on this machine, with this key*. Replace the key between the dry run and
    the apply and the receipt expires, exactly as it does when the machine
    changes.
    """
    ordered = sorted(
        (
            {
                "id": a.get("id", ""),
                "kind": a.get("kind", ""),
                "target": a.get("target", ""),
                "desired": a.get("desired", {}),
                "effect": a.get("effect", ""),
            }
            for a in actions
        ),
        key=lambda a: (a["id"], a["target"]),
    )
    return digest(profile, ordered, key_fingerprint)


def profile_hash(profile: dict) -> str:
    """The hash of the machine, on its own."""
    return digest(profile)


def receipt_path(root: Path) -> Path:
    return Path(root) / RECEIPT_REL


def write_receipt(root: Path, receipt: dict) -> Path:
    """Write the receipt. Mode 0600 — it holds hashes, but it is still a
    record of what this machine did."""
    path = receipt_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def read_receipt(root: Path) -> Optional[dict]:
    """The receipt at this repo, or None."""
    path = receipt_path(root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def new_receipt(profile: dict, actions: list, tier: str,
                key_fingerprint: str = "") -> dict:
    """A receipt for this plan on this machine."""
    return {
        "schema": SCHEMA,
        "probe_version": PROBE_VERSION,
        "tier": tier,
        "plan_hash": plan_hash(profile, actions, key_fingerprint),
        "profile_hash": profile_hash(profile),
        "key_fingerprint": key_fingerprint,
        "actions": len(actions),
        "files": sum(1 for a in actions if a.get("kind") == "file"),
    }


# --------------------------------------------------------------- the manifest --

def manifest_path(config_dir: Path) -> Path:
    return Path(config_dir) / "manifest.json"


def load_manifest(config_dir: Path) -> dict:
    """The manifest: what this tool owns, and the hash it owned it at."""
    path = manifest_path(config_dir)
    if not path.exists():
        return {"schema": SCHEMA, "files": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": SCHEMA, "files": {}}
    if not isinstance(data, dict) or "files" not in data:
        return {"schema": SCHEMA, "files": {}}
    return data


def save_manifest(config_dir: Path, manifest: dict) -> None:
    path = manifest_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def file_hash(path: Path) -> Optional[str]:
    """The content hash of a file, or None when it is not there."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def owns(manifest: dict, path: Path) -> Optional[str]:
    """The hash this tool recorded for a path, or None if it does not own it."""
    return manifest.get("files", {}).get(str(path), {}).get("hash")


def record(manifest: dict, path: Path, content_hash: str, kind: str = "file") -> None:
    """Take ownership of a path at a given hash."""
    manifest.setdefault("files", {})[str(path)] = {
        "hash": content_hash, "kind": kind}
