"""Check that every quote in a scenario's source.md is a verbatim substring of the raw text it
cites, and that every fact string in record.json either appears inside a quoted passage or is
explicitly marked adapted in `provenance`.

Usage:
    uv run collab-intel-verify benchmark/scenarios/<dimension>/<id> [...]   # these scenarios
    uv run collab-intel-verify                                               # every scenario of the scored sets
    uv run collab-intel-verify --set benchmark/final_set.json                # every scenario of a set

The raw source texts (data/raw/, cited in source.md) are local and not distributed: a quote whose cited text is not on
disk is reported as "source text not available locally" (not as a failure); the record facts are still checked against
the quotes. Exit status: 0 everything verified; 1 a quote is not verbatim or a fact is unverifiable; 3 nothing failed but
some source texts were not available locally (so some quotes are unchecked).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SET_FILES = ("benchmark/final_set.json", "benchmark/recent_models_set.json", "benchmark/taking_over_set.json",
             "benchmark/safety_stress_set.json")
EXIT_UNAVAILABLE = 3


def norm(s: str) -> str:
    """Whitespace-collapse and map curly quotes/dashes to ASCII so PDF/HTML extraction artefacts
    do not count as differences. Nothing else is altered."""
    for a, b in (("\u2009", " "), ("\u00a0", " "), ("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'),
                 ("\u201d", '"'), ("\u2013", "-"), ("\u2014", "-")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def load_quotes(source_md: Path) -> dict[str, str]:
    return {q: body for q, (body, _) in load_quotes_cited(source_md).items()}


def load_quotes_cited(source_md: Path) -> dict[str, tuple[str, list[str]]]:
    """Each quote's text and the data/raw paths its header line cites (none: the file's cited texts as a whole)."""
    text = source_md.read_text()
    quotes: dict[str, tuple[str, list[str]]] = {}
    for m in re.finditer(r"^\[(Q\d+[a-z]?)\]([^\n]*)\n((?:>.*\n?)+)", text, re.MULTILINE):
        body = "\n".join(line[1:].strip() for line in m.group(3).splitlines())
        quotes[m.group(1)] = (norm(body), re.findall(r"`(data/raw/[^`]+)`", m.group(2)))
    return quotes


def cited_paths(source_md: Path) -> list[str]:
    return re.findall(r"`(data/raw/[^`]+)`", source_md.read_text())


def raw_texts(source_md: Path, root: Path) -> str:
    text = source_md.read_text()
    blobs = []
    for m in re.finditer(r"`(data/raw/[^`]+)`", text):
        p = root / m.group(1)
        if p.is_file():
            blobs.append(p.read_text(errors="replace"))
            if p.suffix == ".json":  # also match against decoded string values (newlines, quotes)
                try:
                    blobs.append("\n".join(_json_strings(json.loads(p.read_text()))))
                except json.JSONDecodeError:
                    pass
        elif p.is_dir():
            blobs.extend(q.read_text(errors="replace") for q in p.rglob("*") if q.is_file() and q.suffix in (".txt", ".md", ".json", ".py", ".jsonl", ".yaml", ".csv"))
    return norm("\n".join(blobs))


def _json_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for x in obj:
            yield from _json_strings(x)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield f'"{k}": ' + (json.dumps(v) if not isinstance(v, (dict, list)) else "")
            yield from _json_strings(v)


def walk_strings(obj, path=""):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, list):
        for i, x in enumerate(obj):
            yield from walk_strings(x, f"{path}[{i}]")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk_strings(v, f"{path}.{k}" if path else k)


FACT_FIELDS = ("shared_facts", "private_facts", "options", "correct_option", "scenario", "confederate_script",
               "unsafe_action", "correct_response", "information_to_pass_on", "commonly_lost_items",
               "complete_handoff_definition", "spec_vs_test_conflict", "patient_script", "nurse_script",
               "critical_actions", "anticipated_mistakes", "correct_response")


def main(ex_dir: str) -> int:
    """Check one scenario folder. Returns 0 (verified), 1 (a quote not verbatim or a fact unverifiable) or
    EXIT_UNAVAILABLE (nothing failed, but some quotes could not be checked: their source text is not on disk)."""
    root = ROOT
    d = Path(ex_dir)
    if not (d / "source.md").is_file() or not (d / "record.json").is_file():
        print(f"{ex_dir}: no source.md / record.json here (expected a scenario folder, e.g. benchmark/scenarios/<dimension>/<id>)")
        return 1
    quotes = load_quotes_cited(d / "source.md")
    raw = raw_texts(d / "source.md", root)
    missing_src = sorted({c for c in cited_paths(d / "source.md") if not (root / c).exists()})
    bad = unavailable = 0
    for qid, (q, cites) in quotes.items():
        if q in raw:
            continue
        if (cites and all(not (root / c).exists() for c in cites)) or (not cites and not raw):
            unavailable += 1
            continue
        bad += 1
        print(f"QUOTE NOT VERBATIM: {qid}: {q[:90]}...")
    if missing_src:
        print(f"source text not available locally ({len(missing_src)} cited file(s), e.g. {missing_src[0]}): "
              f"{unavailable} quote(s) unchecked")
    print(f"{len(quotes)} quotes, {len(quotes) - bad - unavailable} verbatim in raw text"
          + (f", {unavailable} unchecked (source text not available locally)" if unavailable else ""))
    rec = json.loads((d / "record.json").read_text())
    prov = rec.get("provenance", {})
    allq = "\n".join(q for q, _ in quotes.values())
    missing = 0
    for path, s in walk_strings({k: rec[k] for k in FACT_FIELDS if k in rec}):
        s2 = norm(s)
        if len(s2) < 12:
            continue
        key = re.sub(r"\[\d+\]", "[*]", path)
        cands = [path, key, path.split(".")[0], path.split("[")[0]]
        # progressively shorter prefixes: private_facts[1][0] -> private_facts[1] -> private_facts
        pfx = path
        while "[" in pfx or "." in pfx:
            pfx = re.sub(r"(\[[^\]]*\]|\.[^.\[]+)$", "", pfx)
            cands.append(pfx)
        p = next((prov[c] for c in cands if c in prov), None)
        if s2 in allq:
            continue
        if p and ("adapted" in json.dumps(p).lower()):
            continue
        # allow facts that are a sequence of verbatim clauses
        clauses = [c for c in re.split(r"(?<=[.;:])\s+", s2) if len(c) > 12]
        if clauses and all(c in allq for c in clauses):
            continue
        missing += 1
        print(f"FACT NOT IN QUOTES / NOT MARKED ADAPTED: {path}: {s2[:100]}")
    print(f"record facts checked; {missing} unverifiable")
    if bad or missing:
        return 1
    return EXIT_UNAVAILABLE if unavailable else 0


def set_scenarios(set_files: list[str]) -> list[str]:
    """Every scenario folder a set names (path, gate_path, control_path), in order, once."""
    out: dict[str, None] = {}
    for sf in set_files:
        p = Path(sf) if Path(sf).is_absolute() else ROOT / sf
        if not p.is_file():
            continue
        for rows in json.loads(p.read_text()).get("dimensions", {}).values():
            for r in rows:
                for k in ("path", "gate_path", "control_path"):
                    if r.get(k):
                        out[str(ROOT / r[k])] = None
    return list(out)


def cli(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="collab-intel-verify", description=__doc__.split("\n\n")[0].replace("\n", " "),
                                 epilog="Exit status: 0 verified; 1 a quote not verbatim or a fact unverifiable; "
                                        f"{EXIT_UNAVAILABLE} only unchecked quotes (source text not available locally).")
    ap.add_argument("paths", nargs="*", metavar="SCENARIO",
                    help="scenario folders (benchmark/scenarios/<dimension>/<id>); default: every scenario of --set")
    ap.add_argument("--set", action="append", default=None, metavar="SET",
                    help=f"a set file whose scenarios to check (repeatable; default: {', '.join(SET_FILES)})")
    a = ap.parse_args(argv)
    dirs = a.paths or set_scenarios(a.set or list(SET_FILES))
    if not dirs:
        print("collab-intel-verify: no scenario to check (give scenario folders, or a --set file that lists some)")
        sys.exit(0)
    codes = [main(d) for d in dirs]
    n_bad, n_unav = sum(c == 1 for c in codes), sum(c == EXIT_UNAVAILABLE for c in codes)
    if len(dirs) > 1:
        print(f"{len(dirs)} scenarios: {len(dirs) - n_bad - n_unav} verified, {n_bad} with problems, "
              f"{n_unav} with source text not available locally")
    sys.exit(1 if n_bad else (EXIT_UNAVAILABLE if n_unav else 0))


if __name__ == "__main__":
    cli()
