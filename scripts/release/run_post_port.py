"""Run the release steps that must follow the port of results, scenarios, run copies and harness code from the
development repository, in order (no model calls). Each step is idempotent, so the whole sequence can be rerun.

    uv run python scripts/release/run_post_port.py                   # all steps, then the check
    uv run python scripts/release/run_post_port.py --keep-gpqa-plaintext --raw-root DIR

Order (each step's script says what it does):
  1. neutralise_notes.py              "project lead" -> "the authors" in notes.md and the llm.py comment
  2. dename_fda.py [--raw-root]       FDA records cited by NIDPOE ids; local raw texts renamed
  3. annotate_impossiblebench.py      citation and licence lines in the r_ib_promptB__* source.md
  4. pack_gpqa.py prepare             GPQA canary in the 3 GPQA folders, no "do not publish" wording
  5. add_canary.py                    the benchmark canary in every scenario file, transcript and results file
  6. scripts/build_index.py           index.json with the new citations and the nc / encrypted flags
  7. scripts/leaderboard.py           results/README.md (runs archive, licences, canary) and the leaderboards
  8. make_readme_figures.py           the README's figures (docs/assets/readme/) from the results files
  9. pack_gpqa.py pack                benchmark/gpqa_scenarios.zip; the plain GPQA files deleted and ignored
 10. check_release.py                 every step done, and no placeholder left for the authors
Then commit by explicit path (pack_gpqa.py prints the git rm/add for the GPQA folders), run pytest and ruff, and build
the run archive with build_runs_archive.py --source <the repository with the runs> --out <dir>; publishing the archive is the
authors' step (until then the docs say it is not published yet).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
from collab_intel.release import GPQA_FOLDERS, gpqa_packed  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--raw-root", default=None, help="where data/raw lives, for dename_fda.py (default: this checkout)")
    ap.add_argument("--keep-gpqa-plaintext", action="store_true", help="pack the GPQA archive but keep the plain files")
    ap.add_argument("--ignore-leaks", action="store_true", help="passed to pack_gpqa.py pack")
    a = ap.parse_args()
    steps = [
        [HERE / "neutralise_notes.py"],
        [HERE / "dename_fda.py"] + (["--raw-root", a.raw_root] if a.raw_root else []),
        [HERE / "annotate_impossiblebench.py"],
        [HERE / "pack_gpqa.py", "prepare"],
        [HERE / "add_canary.py"],
        [ROOT / "scripts/build_index.py"],
        [ROOT / "scripts/leaderboard.py"],
        [HERE / "make_readme_figures.py"],
        [HERE / "pack_gpqa.py", "pack"] + (["--keep-plaintext"] if a.keep_gpqa_plaintext else [])
        + (["--ignore-leaks"] if a.ignore_leaks else []),
        [HERE / "check_release.py"],
    ]
    for i, step in enumerate(steps, 1):
        name = " ".join([Path(step[0]).name] + [str(x) for x in step[1:]])
        print(f"\n== {i}. {name}", flush=True)
        if Path(step[0]).name == "pack_gpqa.py" and step[1] == "prepare" and gpqa_packed(ROOT) == set(GPQA_FOLDERS):
            print("   the GPQA folders are already packed: skipped")
            continue
        if Path(step[0]).name == "leaderboard.py":
            r = subprocess.run([sys.executable, *map(str, step)], cwd=ROOT, stdout=subprocess.DEVNULL)
        else:
            r = subprocess.run([sys.executable, *map(str, step)], cwd=ROOT)
        if r.returncode != 0:
            last = i == len(steps)
            print(f"step {i} ({name}) exited {r.returncode}" + ("" if last else "; stopping"), file=sys.stderr)
            return r.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
