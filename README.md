# MCP Gateway Registry triage dashboard

A daily snapshot of the open and closed issues and pull requests in [agentic-community/mcp-gateway-registry](https://github.com/agentic-community/mcp-gateway-registry).

Live page: <https://aarora79.github.io/mcp-gateway-registry-triage/>

A GitHub Actions workflow rebuilds it every day at 12:00 UTC. The page shows when it was generated in the header.

## What the page shows

The page has four tabs. Each one is a board like a GitHub project: fixed-width columns, with one card per issue or pull request.

**Open PRs** has a column for each thing a pull request can need before it merges: a green CI run, a test, a reviewer, or a reviewer with the author walking them through it. Drafts, pull requests failing CI, and pull requests waiting on their author get their own columns. A highlighted "Start here" column on the left lists the three to clear first.

**Open issues** has a column for each time horizon: today, this week, this month, and when time permits. A grey line on each card gives the reasons for its placement. Chips mark the area of the codebase, an issue that already has an open pull request, an outside reporter who is waiting for a reply, and a placement close enough to a cut-off that a rerun may move it.

**Closed PRs** and **Closed issues** have a column for each release, newest first. Scroll sideways for older releases.

Every number, title and `@handle` links to GitHub. The filter box matches issue numbers, titles and handles, and the dropdown on each tab narrows the board to one area. The page opens in dark mode, and the button in the header switches to light. The browser remembers the choice.

## Where the groupings come from

Two command-line tools from [aarora79/jev-samples](https://github.com/aarora79/jev-samples) supply the judgments:

- [`pr-triage`](https://github.com/aarora79/jev-samples/tree/main/samples/pr-triage) picks the route for each open pull request.
- [`issue-triage`](https://github.com/aarora79/jev-samples/tree/main/samples/issue-triage) picks the time horizon for each open issue.

Both ask a model questions about each item. Their answers are advice, and nothing in this repository labels, assigns, comments on or closes anything. TypeSafe, which runs the model, reports 67.8% accuracy on its own benchmark (September 2026). Scores move a little between runs, so an item near a cut-off can change groups from one day to the next.

`build_dashboard.py` assigns areas itself. It reads the conventional-commit scope first (`fix(helm): ...` goes to Kubernetes and Helm), then labels, then keywords in the title. About 6% of items match no rule and land in "Other".

A closed item goes under the first release published after it closed.

## Run it locally

You need [uv](https://docs.astral.sh/uv/), the [GitHub CLI](https://cli.github.com/) logged in, both triage tools on your `PATH`, and a TypeSafe API key.

```bash
# Install the triage tools
curl -fsSL https://raw.githubusercontent.com/aarora79/jev-samples/main/samples/pr-triage/go/install.sh | sh
curl -fsSL https://raw.githubusercontent.com/aarora79/jev-samples/main/samples/issue-triage/go/install.sh | sh

# Fetch the data and run both triage tools (writes ./data)
export TYPESAFE_API_KEY=YOUR_TYPESAFE_API_KEY
export TRIAGE_REPO=agentic-community/mcp-gateway-registry
./fetch_data.sh

# Build the page
uv run python build_dashboard.py --data-dir data --out site/index.html
```

Open `site/index.html` in a browser. The page has no outside dependencies, so it works offline.

To point it at another repository, change `TRIAGE_REPO`.

A full run costs about 1.3 cents in TypeSafe calls and makes roughly 150 GitHub API calls.

## How the daily build works

[`.github/workflows/daily-dashboard.yml`](.github/workflows/daily-dashboard.yml) runs on a schedule and from the "Run workflow" button on the Actions tab. It:

1. downloads both triage tools at fixed versions and checks each against a SHA-256 hash in the workflow file;
2. runs `fetch_data.sh` with the workflow's own token, which can read any public repository;
3. builds the HTML;
4. publishes the `site/` folder to GitHub Pages.

The workflow pins every action to a commit SHA. To upgrade a triage tool, change its version in the workflow and paste the `linux_amd64` line from that release's `SHA256SUMS`.

### Setup for a fork

1. Add a repository secret named `TYPESAFE_API_KEY`.
2. In Settings, Pages, set the source to "GitHub Actions".
3. Run the workflow once from the Actions tab.

GitHub turns off scheduled workflows in a public repository after 60 days with no commits. If the page stops updating, check the Actions tab and re-enable the workflow.

## Files

| File | Purpose |
| --- | --- |
| `fetch_data.sh` | Pulls issues, pull requests, reviews and releases with `gh`, then runs both triage tools |
| `build_dashboard.py` | Reads `data/` and writes `site/index.html` |
| `.github/workflows/daily-dashboard.yml` | Daily build and Pages deploy |
