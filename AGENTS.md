# AGENTS.md

Instructions for coding agents working in this repository. Read the [README](README.md) first for what the dashboard shows.

## What the repository does

A scheduled GitHub Actions workflow fetches the issues and pull requests of `agentic-community/mcp-gateway-registry`, runs two triage tools over the open ones, renders one self-contained HTML page, and publishes it to GitHub Pages at <https://aarora79.github.io/mcp-gateway-registry-triage/>.

| File | Role |
| --- | --- |
| `fetch_data.sh` | Calls `gh` for issues, pull requests, reviews and releases, then runs `pr-triage` and `issue-triage`. Writes everything to `data/`. |
| `build_dashboard.py` | Reads `data/` and writes `site/index.html`. Uses the standard library only. |
| `.github/workflows/daily-dashboard.yml` | Installs the pinned tools, runs both scripts, deploys `site/` to Pages. |

The target repository comes from the `TRIAGE_REPO` environment variable, which both scripts read. The triage tools name their reports after it, so `build_dashboard.py` derives the input file names from it too.

## Verify a change

Run these before you call a change done:

```bash
uv run python -m py_compile build_dashboard.py
bash -n fetch_data.sh
uv run --with pyyaml python -c "import yaml; yaml.safe_load(open('.github/workflows/daily-dashboard.yml'))"
```

For any change to the page, rebuild it from real data and look at it:

```bash
export TRIAGE_REPO=agentic-community/mcp-gateway-registry
export TYPESAFE_API_KEY=YOUR_TYPESAFE_API_KEY
./fetch_data.sh
uv run python build_dashboard.py --data-dir data --out site/index.html
```

`site/index.html` needs no server. Check all four tabs, both themes, and the filter box. A headless browser screenshot works when there is no display:

```bash
chrome --headless=new --window-size=2700,1300 --screenshot=/tmp/page.png "file://$PWD/site/index.html"
```

If you only changed layout or styles, rebuild from the `data/` you already have and skip the fetch. Each fetch spends about 1.3 cents of TypeSafe calls.

## Rules for this repository

**Treat every fetched string as hostile.** Issue titles, pull request titles, labels, handles and the triage tools' reason text all come from people outside the project. Pass every one through `_esc()` before it goes into the HTML, including attribute values. Build links only from the `github.com` base and a number or login, never from a URL found in the data. The page loads no outside scripts, styles or fonts. Keep it that way.

**Keep the workflow pinned.** Each action is pinned to a full commit SHA with the tag in a comment. Each triage tool is pinned to a release version and checked against its SHA-256. To upgrade a tool, change its version and paste the `linux_amd64` line from that release's `SHA256SUMS` in [aarora79/jev-samples](https://github.com/aarora79/jev-samples/releases). Never replace the download with `curl ... | sh`.

**Keep permissions minimal.** The workflow has `contents: read` at the top level. Only the deploy job gets `pages: write` and `id-token: write`. The workflow token reads the target repository, which is public, so no personal token is needed.

**Never print or log `TYPESAFE_API_KEY`.** It reaches the workflow as a repository secret and goes to the tools through the environment only. Do not put it on a command line, in a file, or in an example. Examples use `YOUR_TYPESAFE_API_KEY`.

**Do not commit generated output.** `data/` and `site/` are rebuilt on every run and are in `.gitignore`. So is `uv.lock`, because the project has no dependencies. Stage files by name rather than with `git add -A`.

**Ask before anything that publishes.** Pushing to `main`, re-running the workflow and changing repository settings all change the public page. Confirm with the maintainer first.

## How the page is put together

Each tab is a board in the style of a GitHub project: fixed-width columns side by side, one card per issue or pull request stacked inside each column. Columns scroll vertically on their own, and the board scrolls sideways when the columns do not fit.

`build_dashboard.py` has one builder per tab: `_build_open_prs` (a column per route), `_build_open_issues` (a column per time horizon), `_build_closed_prs` and `_build_closed_issues` (a column per release, newest first). Each returns stat tiles, a toolbar with the area dropdown, and a board. Every builder renders through the same three helpers, `_card`, `_column` and `_board`, so a style change in one place reaches all four tabs.

Areas come from `AREA_RULES`, an ordered list of (area, labels, title pattern). `_classify_area` checks the conventional-commit scope against the patterns first, then labels, then the title, and takes the first match. Each card carries its area as a chip and in `data-area`, which the dropdown filters on. To move items between areas, edit or reorder the rules, then rebuild and count what lands in "Other".

`COLUMN_WIDTH_PX` at the top of the file sets the width of every column and so every card. Colors live in `LIGHT_VARS` and `DARK_VARS`. Change a color in both, and check it against the card background in both themes.

## Code style

- Private functions start with `_` and sit above the public ones.
- One parameter per line, with type hints using `X | None` and built-in generics.
- Keep functions under about 50 lines. `main()` only parses arguments and calls helpers.
- Module constants go at the top of the file, not inside functions.
- Use the logging setup already in the file. Log with `logger.info`, not `print`.
- No new dependencies without a reason. The script runs on the standard library alone.
- No emojis, and no em-dashes or double hyphens standing in for one, in code, comments or docs.
