"""Replace "project lead" with "the authors" in the scenarios' notes.md build logs and in the harness comment that uses
it (release step, after the port; no model calls). Idempotent.

    uv run python scripts/release/neutralise_notes.py           # edit
    uv run python scripts/release/neutralise_notes.py --check   # only report; exit 1 if the phrase is left

The notes.md files are the scenarios' build logs (benchmark/README.md says so); some record steering by "the project
lead". The edit keeps the grammar: "The project lead wants" -> "The authors want", "the project lead's steering" -> "the
authors' steering", "(project lead, 2026-10-03)" -> "(the authors, 2026-10-03)", and "per the / # project lead" across
a comment line break -> "per the / # authors". Files edited: every notes.md under benchmark/ and
src/collab_intel/harness/llm.py. --check also lists any other tracked text file that still says "project lead".
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERBS = {"is": "are", "was": "were", "has": "have", "wants": "want", "says": "say", "asks": "ask", "asked": "asked",
         "prefers": "prefer", "needs": "need", "thinks": "think", "likes": "like", "decides": "decide",
         "decided": "decided", "wanted": "wanted", "agrees": "agree", "requires": "require", "requested": "requested"}
PHRASE = re.compile(
    r"(?P<the>\b[Tt]he(?P<sep>\s+(?:#\s*)?))?(?P<p>[Pp])roject lead(?P<poss>'s)?(?P<verb>\s+(?:" + "|".join(VERBS) + r")\b)?")


def _sub(m: re.Match) -> str:
    if m.group("the"):
        head = m.group("the")[:3] + m.group("sep") + "authors"
    else:
        head = ("The" if m.group("p") == "P" else "the") + " authors"
    out = head + ("'" if m.group("poss") else "")
    if m.group("verb"):
        ws, verb = re.match(r"(\s+)(\w+)", m.group("verb")).groups()
        out += ws + VERBS[verb]
    return out


def neutralise(text: str) -> str:
    return PHRASE.sub(_sub, text)


def files(root: Path) -> list[Path]:
    out = sorted(p for p in (root / "benchmark").rglob("notes.md")
                 if not any(x == "runs" or x.startswith("runs_") for x in p.relative_to(root).parts))
    llm = root / "src/collab_intel/harness/llm.py"
    return out + ([llm] if llm.exists() else [])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(ROOT), help="repository root (default: this checkout)")
    ap.add_argument("--check", action="store_true", help="only report; exit 1 if the phrase is left")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    if a.check:
        try:
            names = subprocess.run(["git", "grep", "-l", "-i", "-I", "project lead"], cwd=root, capture_output=True,
                                   text=True).stdout.split()
        except OSError:
            names = []
        left = sorted({str(p.relative_to(root)) for p in files(root) if re.search("project lead", p.read_text(), re.I)}
                      | {n for n in names if not n.startswith("scripts/release/")})
        for n in left:
            print(f"'project lead' in {n}")
        print(f"{len(left)} files say 'project lead'")
        return 1 if left else 0
    changed = 0
    for p in files(root):
        t = p.read_text()
        new = neutralise(t)
        if new != t:
            p.write_text(new)
            changed += 1
    left = [p for p in files(root) if re.search("project lead", p.read_text(), re.I)]
    print(f"edited {changed} files; {len(left)} still say 'project lead'")
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())
