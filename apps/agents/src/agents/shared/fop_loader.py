from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

FOPS_DIR = Path(__file__).resolve().parents[3] / "fops"
SAFE_MERCHANT_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def load_fops_for_merchant(merchant_id: str) -> list[dict[str, Any]]:
    """Load Phase 0 hardcoded FOPs for a merchant slug.

    Phase 1 moves this data into Postgres with parsing, versioning, and confirmation.
    """

    if SAFE_MERCHANT_SLUG.fullmatch(merchant_id) is None:
        return []

    fop_file = FOPS_DIR / f"{merchant_id}.yaml"
    if not fop_file.exists():
        return []

    raw = yaml.safe_load(fop_file.read_text(encoding="utf-8")) or {}
    fops = raw.get("fops", [])
    if not isinstance(fops, list):
        msg = f"Expected 'fops' to be a list in {fop_file}"
        raise ValueError(msg)

    return [dict(fop) for fop in fops]
