"""Add the benchmark's canary string to every scenario file and transcript (release step, run after the port; no model
calls). Idempotent: a file that already carries the canary is left as it is.

    uv run python scripts/release/add_canary.py            # add it where it is missing
    uv run python scripts/release/add_canary.py --check    # list the files without it; exit 1 if any

What gets it (the string is collab_intel.release.CANARY):
- every scenario folder under benchmark/ (a folder with an episode.yaml): a first-line comment in `episode.yaml`, a
  first `canary` key in `record.json` (and in `prefill.json`, the replayed turns of a takeover), and a line under the
  title of `source.md` and `notes.md` (the verbatim upstream rows and excerpts some folders keep in raw/, source_raw/
  and raw_excerpt.txt are left as they are, and so are LICENSE and NOTICE);
- the showcase transcripts in results/transcripts/: a first `canary` key in each run `.json` and a line under the title
  of each run `.md`;
- the results files results/*_results.json (a first `canary` key) and results/*_results.md (a line), which
  scripts/score.py also writes with it.
Everything else in a file is kept byte for byte, so the takeover scenarios' copies of an original's `source.md` stay
byte-identical to it. The harness reads record.json fields by name and YAML comments are dropped by the parser, so no
prompt changes (tests/test_release.py runs scenarios with the canary and checks that no prompt contains it).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from collab_intel.release import CANARY, CANARY_GUID  # noqa: E402


def json_with_key(text: str, key: str, value: str) -> str | None:
    """`text` (a JSON object) with `"key": value` as its first member and every other byte unchanged; None if the key is
    already there with this value. Raises if the key holds another value or the text is not a JSON object."""
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("not a JSON object")
    if key in obj:
        if obj[key] == value:
            return None
        raise ValueError(f"{key!r} is already set to {obj[key]!r}")
    m = re.match(r"\s*\{(\s*)", text)
    i, ws = m.end(0), m.group(1)
    member = f"{json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}"
    if text[i] == "}":
        new = text[:i] + member + text[i:]
    elif "\n" in ws:
        new = text[:i] + member + "," + ws + text[i:]
    else:
        new = text[:i] + member + ", " + text[i:]
    if json.loads(new) != {key: value, **obj}:
        raise AssertionError("insertion changed the content")
    return new


def text_with_line(text: str, line: str) -> str | None:
    """`text` with `line` as a paragraph of its own right under the title (the first line, if it is a `# ` heading;
    else at the top); None if `line` is already in it. Everything else is kept byte for byte, and a file that starts
    with another file's text (a realistic-load source.md starts with its original's) still does after both are edited."""
    if line in text:
        return None
    if not text.startswith("# "):
        return line + "\n\n" + text if text.strip() else line + "\n"
    head, _, rest = text.partition("\n")
    return head + "\n\n" + line + "\n" + (rest if rest.startswith("\n") or not rest else "\n" + rest)


def yaml_with_comment(text: str, line: str) -> str | None:
    """`text` with `# line` as its first line; None if `line` is already in it."""
    if line in text:
        return None
    return f"# {line}\n" + text


def targets(root: Path) -> list[tuple[Path, str]]:
    """(file, kind) for every file that should carry the canary; kind is json, md or yaml."""
    out: list[tuple[Path, str]] = []
    for ep in sorted((root / "benchmark").rglob("episode.yaml")):
        d = ep.parent
        if any(x == "runs" or x.startswith("runs_") for x in d.relative_to(root).parts):
            continue  # run folders are not scenario files
        out.append((ep, "yaml"))
        for name, kind in (("record.json", "json"), ("prefill.json", "json"), ("source.md", "md"), ("notes.md", "md")):
            if (d / name).is_file():
                out.append((d / name, kind))
    tr = root / "results/transcripts"
    if tr.is_dir():
        for f in sorted(tr.rglob("*")):
            if f.is_file() and f.name != "README.md" and f.suffix in (".json", ".md"):
                out.append((f, f.suffix[1:]))
    for f in sorted((root / "results").glob("*_results.json")):
        out.append((f, "json"))
    for f in sorted((root / "results").glob("*_results.md")):
        out.append((f, "md"))
    return out


def stamp(path: Path, kind: str, line: str = CANARY, key: str = "canary") -> bool:
    """Add the canary to one file; True if the file changed."""
    text = path.read_text()
    if kind == "json":
        new = json_with_key(text, key, line)
    elif kind == "yaml":
        new = yaml_with_comment(text, line)
    else:
        new = text_with_line(text, line)
    if new is None:
        return False
    path.write_text(new)
    return True


def has_canary(path: Path, kind: str) -> bool:
    text = path.read_text()
    if kind == "json":
        return json.loads(text).get("canary") == CANARY
    return CANARY in text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(ROOT), help="repository root (default: this checkout)")
    ap.add_argument("--check", action="store_true", help="only list the files without the canary; exit 1 if any")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    files = targets(root)
    if a.check:
        missing = [f for f, k in files if not has_canary(f, k)]
        for f in missing:
            print(f"no canary: {f.relative_to(root)}")
        print(f"{len(files) - len(missing)} of {len(files)} files carry the canary (GUID {CANARY_GUID})")
        return 1 if missing else 0
    changed = sum(stamp(f, k) for f, k in files)
    print(f"canary added to {changed} files; {len(files) - changed} already had it (GUID {CANARY_GUID})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
