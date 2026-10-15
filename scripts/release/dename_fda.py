"""Cite the FDA enforcement records by document number instead of by the clinicians' names (release step, after the
port; no model calls). Idempotent.

    uv run python scripts/release/dename_fda.py                    # edit the source.md files
    uv run python scripts/release/dename_fda.py --check            # only report; exit 1 if a name is left
    uv run python scripts/release/dename_fda.py --raw-root DIR     # also rename the local raw texts under DIR
                                                                   # (default: data/raw of this checkout, if present)

A released scenario adapts public FDA records and named the clinicians in its source.md (raw-file names, quote
headers); the scenario text itself uses invented names.
- whistleblowing/l_pi_exam_signature and its takeover copy taking_over/l_pi_exam_signature__q14s12: three NIDPOE
  notices, already cited by their Paperclip document ids; the quote headers and raw files now call them NIDPOE A, B and
  C, in the order of the "Full text used" list, and the raw files become fda_nidpoe_a/b/c.txt.
The names are read from the files' own patterns, so this script names no one. After the edit no captured name may be
left anywhere in the scenario folders, and every copy of an edited source.md (a takeover copy) gets the same bytes as
its original. Run scripts/build_index.py afterwards (benchmark/index.json repeats the citation). With the local raw
texts renamed, collab-intel-verify passes as before.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PI_EXAM = "benchmark/scenarios/whistleblowing/l_pi_exam_signature"
NIDPOE_RAW = re.compile(r"fda_nidpoe_(?P<name>[a-z]{2,})\.txt")
NAMES_SENTENCE = "The investigators' names are in the sources;"
NAMES_SENTENCE_NEW = "The investigators' names are withheld here;"
PAPERCLIP = re.compile(r"\(paperclip ids (?P<ids>fda_[0-9a-f]+,\s+fda_[0-9a-f]+,\s+fda_[0-9a-f]+)\)\.")


def dename_pi_exam(text: str) -> tuple[str, set[str], dict[str, str]]:
    order: list[str] = []
    for m in NIDPOE_RAW.finditer(text):
        if m.group("name") not in order:
            order.append(m.group("name"))
    letters = {n: "abc"[i] for i, n in enumerate(order)} if order else {}
    if len(order) > 3:
        raise SystemExit(f"expected three NIDPOE texts, found {len(order)}")
    for n, x in letters.items():
        text = text.replace(f"fda_nidpoe_{n}.txt", f"fda_nidpoe_{x}.txt")
        text = re.sub(rf"\b{re.escape(n.capitalize())} NIDPOE\b", f"NIDPOE {x.upper()}", text)
    text = text.replace(NAMES_SENTENCE, NAMES_SENTENCE_NEW)
    text = PAPERCLIP.sub(lambda m: f"(Paperclip document ids {m.group('ids')}), quoted below as NIDPOE A, B and C "
                                   "without the investigators' names.", text)
    return text, set(order), letters


def raw_moves(old_text: str, new_text: str) -> list[tuple[str, str]]:
    """(old, new) data/raw paths whose name changed between the two versions of a source.md."""
    old = list(dict.fromkeys(re.findall(r"`(data/raw/[^`]+)`", old_text)))
    new = list(dict.fromkeys(re.findall(r"`(data/raw/[^`]+)`", new_text)))
    return [(a, b) for a, b in zip(old, new, strict=True) if a != b] if len(old) == len(new) else []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(ROOT), help="repository root (default: this checkout)")
    ap.add_argument("--raw-root", default=None, help="directory that holds data/raw/... (default: ROOT; skipped if absent)")
    ap.add_argument("--copy", action="store_true", help="copy the raw texts to their new names instead of renaming them")
    ap.add_argument("--check", action="store_true", help="only report; exit 1 if a name is left")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    copies_of = {}
    for rel in (PI_EXAM,):
        src = root / rel / "source.md"
        if not src.is_file():
            raise SystemExit(f"{rel}/source.md not found")
        same = [p for p in (root / "benchmark").rglob("source.md")
                if p != src and "runs" not in p.parts and p.read_bytes() == src.read_bytes()]
        copies_of[rel] = same
    p_old = (root / PI_EXAM / "source.md").read_text()
    p_new, p_names, letters = dename_pi_exam(p_old)
    names = {n for n in p_names if len(n) > 1}

    if a.check:
        left = [f"{rel}/source.md" for rel, old, new in ((PI_EXAM, p_old, p_new),) if old != new]
        for x in left:
            print(f"names still in {x}")
        print("FDA records are cited by document number" if not left else f"{len(left)} files still name clinicians")
        return 1 if left else 0

    moves = raw_moves(p_old, p_new)
    for rel, new in ((PI_EXAM, p_new),):
        for p in [root / rel / "source.md"] + copies_of[rel]:
            p.write_text(new)
    # no captured name may be left in any file of the edited folders (and their copies)
    folders = {root / PI_EXAM} | {p.parent for c in copies_of.values() for p in c}
    left = []
    for d in folders:
        for f in d.iterdir():
            if f.is_file():
                t = f.read_text(errors="replace").lower()
                left += [f"{f.relative_to(root)}: {n[:1]}…" for n in names if re.search(rf"\b{re.escape(n)}\b", t)]
    if left:
        raise SystemExit("a captured name is still in: " + "; ".join(left))
    for rel, cs in copies_of.items():
        for c in cs:
            if c.read_bytes() != (root / rel / "source.md").read_bytes():
                raise SystemExit(f"{c.relative_to(root)} is no longer byte-identical to {rel}/source.md")
    n_copies = sum(len(c) for c in copies_of.values())
    print(f"de-named {int(p_old != p_new)} source.md files and {n_copies} "
          f"byte-identical copies; NIDPOE letters: {', '.join(x.upper() for x in letters.values()) or 'already done'}")

    raw_root = Path(a.raw_root).resolve() if a.raw_root else root
    did = []
    for old, new in moves:
        po, pn = raw_root / old, raw_root / new
        if pn.exists():
            did.append(f"{new} already there")
        elif po.exists():
            (shutil.copy2 if a.copy else shutil.move)(po, pn)
            did.append(f"{'copied' if a.copy else 'renamed'} the raw text to {Path(new).name}")
        else:
            did.append(f"{Path(new).name}: no local raw text (the provenance test skips without it)")
    for x in did:
        print(" ", x)
    print("Now run scripts/build_index.py (benchmark/index.json repeats the citation).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
