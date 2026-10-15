"""Build the versioned archive of the run transcripts behind the results (release step, after the port; no model calls).
The archive is not published yet; publishing it (Zenodo or Hugging Face) is the authors' step, and then its URL and sha256
go into scripts/fetch_runs.py.

    uv run python scripts/release/build_runs_archive.py --source ../research-repo --out ../runs-archive
        [--version v3] [--forbid TEXT ...] [--no-zip] [--repo-url URL] [--doi DOI]

--source is a checkout that holds the runs (`<scenario>/runs/<model>/`, gitignored): the development repository, whose
scenario folders may sit elsewhere, or a release checkout with its runs. The scenario folders come from this checkout's
set files (benchmark/final_set.json, recent_models_set.json, taking_over_set.json, safety_stress_set.json); each is
paired with the source's folder by the same set file, dimension and scenario id, so the archive uses the release paths.

What goes in: for every folder of the scored sets (the scenario, its alone-gate folder and its baseline folder) and
every model of the set, every file under `runs/<model>/` with suffix .json (the run), .md (its readable copy) or
.judges (the judge labels a rescore asked for), including `continued_from/` (the originals of continued runs). Dry runs
(canned replies) are left out. Each file is processed:
- paths of the build machine are replaced by placeholders: `/nfs/home/<user>/<repo>` -> `<workspace>`, other
  `/nfs/home/<user>` (and `/home/<user>`, `/Users/<user>`) -> `<home>`, pytest's `/tmp/pytest-of-<user>` ->
  `<pytest-tmp>`, `/tmp/claude-<uid>/...` -> `<tmp>`, and the source checkout's own path -> `<workspace>`;
- the benchmark canary is added (a first `canary` key in each run .json, a line under the title of each .md; .judges
  sidecars are
  left as they are, since scripts/score.py merges them into the run's judges); the GPQA-based folders' files also get
  GPQA's canary;
- every model folder name has ':' written as '_' (Windows cannot hold ':'); `path_map.tsv` lists each renamed folder
  with its original name, and scripts/fetch_runs.py restores them.
The runs of the 3 GPQA-based scenarios go into `gpqa_runs.zip` inside the archive, encrypted with the GPQA archive's
password, as GPQA asks. Then the script writes README.md (contents, licences, the model-output notice, the canary),
MANIFEST.sha256 (sha256 of every file) and the zip `collab-intel-runs-<version>.zip` with its `.sha256`, and fails if a
home path, the current user name, a --forbid string or anything that looks like an API key is left in any file.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import re
import sys
import tomllib
import zipfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))
from add_canary import json_with_key, text_with_line  # noqa: E402
from zipcrypto import write_encrypted_zip  # noqa: E402

from collab_intel.release import CANARY, GPQA_CANARY, GPQA_FOLDERS, GPQA_PASSWORD  # noqa: E402

SETS = ("final_set.json", "recent_models_set.json", "taking_over_set.json", "safety_stress_set.json")
SUFFIXES = (".json", ".md", ".judges")
WINDOWS_BAD = re.compile(r'[<>:"|?*\\]|[. ]$')
SECRETS = re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|"
                     r"ghp_[A-Za-z0-9]{30,}|xox[bp]-[A-Za-z0-9-]{10,}")
GPQA_LINE = f"GPQA canary string: {GPQA_CANARY}"


def model_slug(model: str) -> str:
    return model.replace("/", "__")  # as the harness names run folders


def is_dry_run(t: dict) -> bool:
    """A transcript made with canned replies (as collab_intel.harness.cli.is_dry_run_transcript decides)."""
    if t.get("dry_run") or (t.get("harness_config") or {}).get("dry_run"):
        return True
    if any(isinstance(c, dict) and c.get("model") == "dry-run" for c in t.get("classifier_calls") or []):
        return True
    return any(str(m.get("text") or "").startswith("[dry-run reply]") for m in t.get("messages") or []
               if m.get("seat") == t.get("evaluated_seat"))


def folder_map(release: Path, source: Path) -> tuple[dict[str, str], dict[str, set[str]]]:
    """{release folder: source folder} for every folder of the scored sets, and {release folder: models}."""
    fmap: dict[str, str] = {}
    models: dict[str, set[str]] = {}
    for name in SETS:
        rel = json.loads((release / "benchmark" / name).read_text())
        src_file = source / "benchmark" / name
        src = json.loads(src_file.read_text()) if src_file.exists() else rel
        for dim, items in rel["dimensions"].items():
            by_id = {Path(it["path"]).name: it for it in src["dimensions"].get(dim, [])}
            for it in items:
                s = by_id.get(Path(it["path"]).name)
                if s is None:
                    raise SystemExit(f"{name}: {it['path']} has no counterpart in {src_file}")
                for key in ("path", "gate_path", "control_path"):
                    if it.get(key):
                        if not s.get(key) or Path(s[key]).name != Path(it[key]).name:
                            raise SystemExit(f"{name}: {key} of {it['path']} differs in the source")
                        if fmap.setdefault(it[key], s[key]) != s[key]:
                            raise SystemExit(f"{it[key]} maps to two source folders")
                        models.setdefault(it[key], set()).update(rel["models"])
    return fmap, models


def scrubber(source: Path):
    reps = [(re.compile(re.escape(str(source.resolve()))), "<workspace>"),
            (re.compile(r"/nfs/home/[^/\s\"'\\]+/(?:collaborative-intelligence|ci-release|ci-website)(?=[/\s\"'\\)\],:]|$)"),
             "<workspace>"),
            (re.compile(r"/nfs/home/[^/\s\"'\\]+"), "<home>"),
            (re.compile(r"/(?:home|Users)/[^/\s\"'\\]+(?=/)"), "<home>"),
            (re.compile(r"/tmp/pytest-of-[^/\s\"'\\]+"), "<pytest-tmp>"),
            (re.compile(r"/tmp/claude-\d+(?:/[^\s\"'\\]*)?"), "<tmp>")]

    def scrub(text: str) -> tuple[str, int]:
        n = 0
        for rx, ph in reps:
            text, k = rx.subn(ph, text)
            n += k
        return text, n
    return scrub


def archived_name(rel_path: str) -> str:
    return "/".join(p.replace(":", "_") for p in rel_path.split("/"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", required=True, help="checkout that holds the runs")
    ap.add_argument("--out", required=True, help="output directory (created; must be empty or absent)")
    ap.add_argument("--version", default="v3")
    ap.add_argument("--forbid", action="append", default=[], help="text that must not appear in the archive "
                                                                  "(e.g. a personal email); repeatable")
    ap.add_argument("--no-zip", action="store_true", help="build the folder only")
    ap.add_argument("--repo-url", default="https://github.com/zou-group/collaborative-intelligence",
                    help="the public repository, for the archive's README")
    ap.add_argument("--doi", default=None,
                    help="the archive's DOI if reserved in advance (Zenodo can), for its README (left out if not given)")
    a = ap.parse_args()
    source, out = Path(a.source).resolve(), Path(a.out).resolve()
    name = f"collab-intel-runs-{a.version}"
    tree = out / name
    if tree.exists() and any(tree.iterdir()):
        raise SystemExit(f"{tree} is not empty")
    tree.mkdir(parents=True, exist_ok=True)
    fmap, models = folder_map(ROOT, source)
    nc = {str(p.parent.relative_to(ROOT)) for p in (ROOT / "benchmark").glob("*/*/*/LICENSE")
          if "CC BY-NC-SA 4.0" in p.read_text()}
    scrub = scrubber(source)
    stats: Counter = Counter()
    renamed: dict[str, str] = {}
    gpqa_files: dict[str, bytes] = {}
    seen: dict[str, str] = {}
    per_model: Counter = Counter()
    for rel_folder in sorted(fmap):
        src_runs = source / fmap[rel_folder] / "runs"
        gpqa = rel_folder in GPQA_FOLDERS
        for m in sorted(models[rel_folder]):
            mdir = src_runs / model_slug(m)
            if not mdir.is_dir():
                continue
            for f in sorted(p for p in mdir.rglob("*") if p.is_file()):
                if not f.name.endswith(SUFFIXES):
                    stats[f"skipped {f.suffix or f.name}"] += 1
                    continue
                orig = f"{rel_folder}/runs/{f.relative_to(src_runs).as_posix()}"
                arch = archived_name(orig)
                if seen.setdefault(arch, orig) != orig:
                    raise SystemExit(f"two files map to {arch}")
                if any(WINDOWS_BAD.search(part) for part in arch.split("/")):
                    raise SystemExit(f"a name Windows cannot hold: {arch}")
                odir, adir = f"{rel_folder}/runs/{model_slug(m)}", archived_name(f"{rel_folder}/runs/{model_slug(m)}")
                if odir != adir:
                    renamed[adir] = odir
                text = f.read_text()
                if f.suffix == ".json":
                    t = json.loads(text)
                    if is_dry_run(t):
                        stats["dry runs left out"] += 1
                        continue
                text, n = scrub(text)
                stats["path placeholders"] += n
                stats["files with a path placeholder"] += bool(n)
                if f.suffix == ".json":
                    text = json_with_key(text, "canary", CANARY) or text
                    if gpqa:
                        text = json_with_key(text, "gpqa_canary", GPQA_CANARY) or text
                    json.loads(text)
                    stats["run files (.json)"] += 1
                    per_model[m] += 1
                elif f.suffix == ".md":
                    text = text_with_line(text, CANARY) or text
                    if gpqa:
                        text = text_with_line(text, GPQA_LINE) or text
                    stats["readable copies (.md)"] += 1
                else:
                    json.loads(text)
                    stats["judge sidecars (.judges)"] += 1
                stats["runs of NC-subset scenarios (files)"] += rel_folder in nc
                if gpqa:
                    gpqa_files[arch] = text.encode()
                    stats["GPQA-scenario files (encrypted)"] += 1
                    continue
                dest = tree / arch
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text)
    if gpqa_files:
        write_encrypted_zip(tree / "gpqa_runs.zip", gpqa_files, GPQA_PASSWORD,
                            comment=f"{CANARY}\n{GPQA_CANARY}\nRuns of the GPQA-based scenarios. As GPQA asks, please do "
                                    "not post them in plain text.")
    (tree / "path_map.tsv").write_text("archived_folder\toriginal_folder\n"
                                       + "".join(f"{k}\t{v}\n" for k, v in sorted(renamed.items())))
    folders = sorted(fmap)
    (tree / "README.md").write_text(readme(name, stats, per_model, renamed, sorted(nc), folders, a.repo_url, a.doi))
    # leak scan over everything written (the GPQA runs in memory)
    forbid = [x for x in a.forbid if x] + ["/nfs/home/", "/tmp/claude"]
    user = getpass.getuser()
    user_rx = re.compile(rf"\b{re.escape(user)}\b") if len(user) >= 4 else None
    leaks = []

    def scan(label: str, text: str) -> None:
        for x in forbid:
            if x in text:
                leaks.append(f"{label}: {x!r}")
        if user_rx and user_rx.search(text):
            leaks.append(f"{label}: the user name")
        if SECRETS.search(text):
            leaks.append(f"{label}: something that looks like an API key")

    for p in sorted(tree.rglob("*")):
        if p.is_file() and p.suffix != ".zip":
            scan(p.relative_to(tree).as_posix(), p.read_text(errors="replace"))
    for k, v in gpqa_files.items():
        scan(f"gpqa_runs.zip:{k}", v.decode(errors="replace"))
    if leaks:
        for x in leaks[:50]:
            print("LEAK", x)
        raise SystemExit(f"{len(leaks)} leaks: nothing to publish until they are fixed")
    files = sorted(p for p in tree.rglob("*") if p.is_file())
    manifest = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(tree).as_posix()}\n" for p in files)
    (tree / "MANIFEST.sha256").write_text(manifest)
    summary = {"name": name, "folders": len(folders), "files": len(files) + 1, **dict(stats),
               "renamed_model_folders": len(renamed), "runs_per_model": dict(sorted(per_model.items()))}
    (out / f"{name}.summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    if not a.no_zip:
        zpath = out / f"{name}.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for p in sorted(tree.rglob("*")):
                if p.is_file():
                    zi = zipfile.ZipInfo(f"{name}/{p.relative_to(tree).as_posix()}", date_time=(1980, 1, 1, 0, 0, 0))
                    zi.external_attr = 0o100644 << 16
                    zi.compress_type = zipfile.ZIP_STORED if p.suffix == ".zip" else zipfile.ZIP_DEFLATED
                    z.writestr(zi, p.read_bytes())
        digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
        (out / f"{name}.zip.sha256").write_text(f"{digest}  {zpath.name}\n")
        summary.update(zip=str(zpath), zip_bytes=zpath.stat().st_size, zip_sha256=digest)
    print(json.dumps(summary, indent=1))
    print("Next, when the archive is published (Zenodo or Hugging Face): set ARCHIVE_URL and ARCHIVE_SHA256 in "
          "scripts/fetch_runs.py and say where it is in docs/running.md, results/README.md (scripts/leaderboard.py), "
          "results/transcripts/README.md and docs/datacard.md, which now say it is not published yet.")
    return 0


def readme(name: str, stats: Counter, per_model: Counter, renamed: dict, nc: list[str], folders: list[str],
           repo_url: str, doi: str | None) -> str:
    models = "\n".join(f"| `{m}` | {n} |" for m, n in sorted(per_model.items()))
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    ncl = "\n".join(f"- `{x}`" for x in nc)
    return f"""# {name}: the run transcripts of the Collaborative Intelligence benchmark

The runs behind the published results of the Collaborative Intelligence benchmark (release {name.rsplit('-', 1)[-1]}):
every run of the scored sets' models on the {len(folders)} scenario folders of the scored sets as released (the 160
released scored scenarios, their alone-gate and baseline folders, and the 15 released scenarios of the safety stress
set). The benchmark's held-out scenarios (80 scored, 8 stress) and their runs are not in this archive; their scores
enter the results through the repository's `results/heldout_aggregates.json`.

- Source code, scenarios and results: `{repo_url}` (package version {version}, as in its `pyproject.toml`).
{f"- Archive DOI: `{doi}`.{chr(10)}" if doi else ""}
## Use

From a clone of the repository, `uv run python scripts/fetch_runs.py` downloads this archive, checks its sha256 and the
sha256 of every file against `MANIFEST.sha256`, and puts every run where it was made
(`benchmark/scenarios/<dimension>/<id>/runs/<model>/`), with the original folder names (`path_map.tsv`).
`uv run python scripts/score.py --rescore` then reproduces the scores without model calls (the repository's
`results/README.md`, "Reproduce"): the released scenarios' scores from these runs, and the scores over all scenarios
with the held-out aggregates.

## Contents

| | |
|---|---|
| Run transcripts (`<version>_seed<k>.json`: every message, tool call, judge label and the final state) | {stats['run files (.json)']} |
| Readable copies (`.md`) | {stats['readable copies (.md)']} |
| Judge labels asked for by a rescore (`.json.judges`, merged into the run by `scripts/score.py`) | {stats['judge sidecars (.judges)']} |
| Files of the 3 GPQA-based scenarios, in `gpqa_runs.zip` (see GPQA) | {stats['GPQA-scenario files (encrypted)']} |
| Dry runs left out | {stats['dry runs left out']} |

Runs per evaluated model:

| Model | Run files |
|---|--:|
{models}

`continued_from/` folders hold the original of a run that was continued under the corrected stop rules; the run at the
original path is the continuation, which the results score.

## Changes from the runs as recorded

- Paths of the build machine are replaced by placeholders: `<workspace>` (the repository), `<home>` (the rest of the
  home directory), `<pytest-tmp>` (pytest's temporary directory) and `<tmp>` ({stats['path placeholders']}
  replacements in {stats['files with a path placeholder']} files, mostly in pytest tracebacks from the coding sandbox).
  Rescoring the runs gives the same verdicts with or without them.
- Each run `.json` has a first key `canary` and each `.md` a line under its title with the benchmark's canary string
  (below). The `.judges` sidecars are unchanged apart from the paths.
- Model folder names have ':' written as '_' (Windows cannot hold ':'): {len(renamed)} folders, each listed in
  `path_map.tsv` with its original name. `scripts/fetch_runs.py` restores the original names, which
  `scripts/score.py` expects.

## Licences

- The project's text in these runs (the scenario text, the scripted teammate's lines, the judges' labels, the scores)
  is licensed under CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/), except the runs of the {len(nc)} released
  scenarios of the non-commercial subset, which are CC BY-NC-SA 4.0 (https://creativecommons.org/licenses/by-nc-sa/4.0/):
{ncl}
- Third-party material that the scenarios quote or adapt keeps its own licence (the repository's
  `THIRD_PARTY_NOTICES.md`).
- **Model outputs.** The runs contain outputs of the evaluated, partner and judge models (OpenAI, Anthropic, models
  served by Together AI, and open-weight models). They are released for evaluation research and remain subject to each
  provider's terms (OpenAI, Anthropic, Together AI) and to the open-weight models' licences. They are not offered for
  training models in breach of those terms, and the data licence does not relicense them.

## GPQA

The runs of the 3 scenarios built on GPQA (Rein et al. 2023, CC BY 4.0) contain GPQA questions. GPQA's authors ask that
they not be posted in plain text, so these runs are in `gpqa_runs.zip`, encrypted with the password of GPQA's own
dataset.zip (`{GPQA_PASSWORD}`), and carry GPQA's canary string `{GPQA_CANARY}`. Please do not post them in plain text.

## Canary

Do not train on this archive. Every run carries the canary string:

    {CANARY}
"""


if __name__ == "__main__":
    sys.exit(main())
