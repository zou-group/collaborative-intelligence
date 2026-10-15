"""Human vs LLM-judge agreement from the Second Opinion responses (no model calls).

    uv run python scripts/analysis/judge_check_agreement.py RESPONSES_DIR

RESPONSES_DIR holds one JSON per reader ({"initials", "answers": {item_id: {"label", "note"}}}), e.g. exported from the
artifact's `responses` collection. Compares each human label with the referee's and the second rater's labels in
results/analysis/judge_check_key.json ('not_sure' answers are left out) and reports raw agreement and Cohen's kappa,
overall and per dimension, plus agreement between readers on items two or more of them labelled. The key is withheld
from the repository (and ignored by git) until the check reports, so that the check stays blind.
"""
from __future__ import annotations

import collections
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def kappa(pairs: list[tuple[str, str]]) -> float | None:
    if not pairs:
        return None
    n = len(pairs)
    po = sum(a == b for a, b in pairs) / n
    ca, cb = collections.Counter(a for a, _ in pairs), collections.Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return None if pe == 1 else (po - pe) / (1 - pe)


def main(rdir: str) -> None:
    key_file = ROOT / "results/analysis/judge_check_key.json"
    if not key_file.exists():  # withheld from the public repository until the check reports (docs/human_checks)
        sys.exit(f"{key_file.relative_to(ROOT)} is not here: the answer key is withheld until the human check reports "
                 "(docs/human_checks/README.md); scripts/analysis/judge_check_sample.py writes it.")
    key = json.loads(key_file.read_text())
    items = {x["id"]: x for x in json.loads((ROOT / "results/analysis/judge_check_items.json").read_text())}
    readers = [json.loads(f.read_text()) for f in sorted(Path(rdir).rglob("*.json"))]
    by_dim: dict[str, dict[str, list]] = collections.defaultdict(lambda: {"referee": [], "second": []})
    per_item: dict[str, list[str]] = collections.defaultdict(list)
    for r in readers:
        for iid, a in (r.get("answers") or {}).items():
            lab = (a or {}).get("label")
            if not lab or lab == "not_sure" or iid not in key or iid not in items:  # items: the released packet
                continue
            d = items[iid]["dimension"]
            per_item[iid].append(lab)
            for j in ("referee", "second"):
                if key[iid].get(j):
                    by_dim[d][j].append((lab, key[iid][j]))
                    by_dim["all"][j].append((lab, key[iid][j]))
    print(f"{len(readers)} readers")
    for d in sorted(by_dim, key=lambda x: (x != "all", x)):
        row = []
        for j in ("referee", "second"):
            p = by_dim[d][j]
            agree = sum(a == b for a, b in p) / len(p) if p else None
            k = kappa(p)
            row.append(f"{j}: {'-' if agree is None else f'{100 * agree:.0f}%'} (kappa {'-' if k is None else f'{k:.2f}'}, n={len(p)})")
        print(f"{d:34s} " + " | ".join(row))
    hp = [(a, b) for labs in per_item.values() for a, b in itertools.combinations(labs, 2)]
    if hp:
        print(f"between readers: {100 * sum(a == b for a, b in hp) / len(hp):.0f}% (kappa {kappa(hp):.2f}, pairs={len(hp)})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "data/processed/second_opinion_responses"))
