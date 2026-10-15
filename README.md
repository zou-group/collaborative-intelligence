# Project website

A static site (plain HTML, CSS and JavaScript, no build step, no backend). It renders only from `data/*.json`.

```bash
python site/build_data.py          # rebuild data/*.json from the repository's result files; re-checks every quote
cd site && python -m http.server   # then open http://localhost:8000/
```

- `build_data.py` reads `results/`, `docs/analysis/`, `pilots/*/README.md`, `benchmark/index.json`, `docs/licence_audit.md`
  and `docs/datacard.md`. Every quote in `data/examples.json` is verified verbatim against its transcript before it is
  written, with the method of `docs/slides/beamer/make_v2_figs.py`; persona names are replaced by roles afterwards. The
  script stops on the first mismatch.
- The allergen example comes from a pilot run (`results/transcripts/pilots/...`). If that transcript is not in this
  checkout, the script looks in a sibling `collaborative-intelligence/results/transcripts` or in `--extra-transcripts`,
  and skips the example if it finds none.
- Examples: `build_data.py` turns each quoted run into an event stream (chat messages, environment events, the
  model's tool calls) with names swapped for roles. The page shows each run as a card with a Pass or Fail stamp, a static
  workspace panel (the file tree, with the files that matter marked opened, edited or never opened) and the verified
  excerpt; Play reveals the excerpt step by step; Full transcript expands the whole run.
- The public/private split: `build_data.py` reads from `benchmark/split.json` only the counts of released and held-out
  scenarios and each model's score on each part (into `data/leaderboard.json`). That file names the private scenarios,
  so nothing else from it may reach the site: before a deploy, run
  `uv run python scripts/make_split.py --exposure-scan <this folder>` in the research repository; it must exit 0.
- `assets/logos/SOURCES.md` lists where each company logo came from and under which licence.
- Placeholders to fill before release: the Paper, Code and Dataset buttons (`data-placeholder` in `index.html`), the
  BibTeX `howpublished` and `url` fields, the team list and the contact link.
