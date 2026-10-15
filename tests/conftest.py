"""Shared test set-up. The 3 GPQA-based scenarios ship encrypted (benchmark/gpqa_scenarios.zip, as GPQA asks); every
test parametrised with one of them is skipped until `uv run python scripts/unpack_gpqa.py` has put them in place."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from collab_intel.release import gpqa_packed  # noqa: E402

PACKED = gpqa_packed(ROOT)


def _mentions(v, packed: set[str]) -> bool:
    if isinstance(v, str):
        return v.rstrip("/") in packed
    if isinstance(v, dict):
        return any(_mentions(x, packed) for x in v.values())
    if isinstance(v, (list, tuple)):
        return any(_mentions(x, packed) for x in v)
    return False


def pytest_collection_modifyitems(config, items):
    if not PACKED:
        return
    skip = pytest.mark.skip(reason="a GPQA-based scenario that is still packed: run scripts/unpack_gpqa.py")
    for item in items:
        cs = getattr(item, "callspec", None)
        if cs is not None and _mentions(list(cs.params.values()), PACKED):
            item.add_marker(skip)
