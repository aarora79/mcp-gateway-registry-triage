"""Build a self-contained HTML triage dashboard for PRs and issues.

Reads the gh exports (fetch_data.sh) and the pr-triage / issue-triage
reports in ./data, and writes an HTML page with four tabs: open PRs, open
issues, closed PRs, closed issues.

The repository comes from the TRIAGE_REPO environment variable, the same one
fetch_data.sh reads.

Example:
    export TRIAGE_REPO=agentic-community/mcp-gateway-registry
    ./fetch_data.sh
    uv run python build_dashboard.py --data-dir data --out site/index.html
"""

import argparse
import html
import json
import logging
import os
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,  # Set the log level to INFO
    # Define log message format
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_REPO: str = "agentic-community/mcp-gateway-registry"
REPO: str = os.environ.get("TRIAGE_REPO", DEFAULT_REPO)
GITHUB_BASE: str = f"https://github.com/{REPO}"
# pr-triage and issue-triage name their reports after the repository.
REPO_SLUG: str = REPO.replace("/", "-")
PR_TRIAGE_FILE: str = f"{REPO_SLUG}-open-all-triage.json"
ISSUE_TRIAGE_FILE: str = f"triage-{REPO_SLUG}-open.md"
# Review accounts that are automation, not people.
BOT_REVIEWERS: set[str] = {"github-advanced-security", "copilot-pull-request-reviewer", "Copilot"}
REVIEW_STATE_TEXT: dict[str, str] = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes requested",
    "COMMENTED": "commented",
    "DISMISSED": "dismissed",
    "REQUESTED": "requested",
}
OUTPUT_FILE: str = "site/index.html"
# The repository that hosts this dashboard, linked from the header.
# GitHub Actions sets GITHUB_REPOSITORY, so a fork links to itself.
DASHBOARD_REPO: str = os.environ.get("GITHUB_REPOSITORY", "aarora79/mcp-gateway-registry-triage")
# GitHub's mark, inline so the page still loads nothing from outside.
GITHUB_ICON_SVG: str = (
    '<svg viewBox="0 0 16 16" width="18" height="18" aria-hidden="true"><path fill="currentColor" '
    'd="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49'
    '-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82'
    '.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15'
    '-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 '
    '2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54'
    '.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>'
)
# Every board column, and so every card, is this wide.
COLUMN_WIDTH_PX: int = 340
# Items opened after the last triage run land here until the next 7:00 build.
NEW_COLUMN_TITLE: str = "New since last triage"
NEW_COLUMN_NOTE: str = "opened after the last triage run; sorted at the next 7:00 ET build"
HORIZONS: list[str] = ["Today", "This week", "This month", "When time permits"]

# Area rules, checked in order. First match wins.
# Each rule: (area name, labels that select it, title regex that selects it)
AREA_RULES: list[tuple[str, set[str], str]] = [
    ("Dependencies", {"dependencies", "python:uv"}, r"\bbump\b|dependabot"),
    ("Release and versioning", {"release"}, r"release[- ]notes|image tags|^chore\(release\)"),
    ("Tests and CI", {"github_actions"}, r"^(test|ci)[(:]|\btests?\b|pre-commit|workflow|\bci\b|coverage"),
    ("Kubernetes and Helm", {"helm"}, r"helm|chart|k8s|kubernetes|eks"),
    ("AWS infrastructure", set(), r"terraform|\becs\b|\bcdk\b|infra|aurora|documentdb|cloudfront|\balb\b"),
    ("Egress credentials", {"credentials-management"}, r"egress|vault|openbao|3lo|oauth_user|credential"),
    ("Auth and identity", {"authentication", "oauth", "entra-id"},
     r"auth|oauth|oidc|keycloak|cognito|okta|auth0|entra|idp|jwt|token|login|scope|session|cookie|csrf|logto|pingfed"),
    ("A2A agents", {"a2a"}, r"\ba2a\b|agent card|\bagents?\b"),
    ("Search", {"search", "semantic-search"}, r"search|embedding|vector|faiss"),
    ("Federation and integrations", {"federation", "agentcore", "integrations"},
     r"federat|agentcore|peer registry|anthropic|integration"),
    ("Docker and local deploy", {"docker", "deployment"}, r"docker|compose|podman|build_and_run|deploy|install"),
    ("Gateway and MCP proxy", {"nginx", "mcp-spec", "health-checks"},
     r"nginx|proxy|mcpgw|gateway|virtual server|tools/|mcp server|\bmcp\b|ssrf|health"),
    ("Frontend and UI", {"javascript"}, r"frontend|\bui\b|react|dashboard|console|page|button|branding"),
    ("Observability", set(), r"metric|telemetry|otel|grafana|prometheus|log|audit"),
    ("API and CLI", {"api", "cli", "openapi"}, r"\bapi\b|\bcli\b|openapi|registry_management|endpoint"),
    ("Docs", {"documentation"}, r"^docs|readme|documentation|\bdocs?\b"),
    ("Skills and custom entities", set(), r"skill|custom[- ]entit|server\.json|cimd"),
    ("Security scanning and hardening", {"security"}, r"security|scan|cve|advisor|unsafe|block|redirect|leak"),
    ("Registry core and storage", set(),
     r"registry|server|mongo|discovery|dedup|secret|rate[- ]limit|registration|ingress|provider|validate"),
    ("Tooling and setup", {"automation", "dx"}, r"usage-report|macos|setup|ruff|lambda|build|script|lint"),
]
OTHER_AREA: str = "Other"


def _load_json(
    path: Path,
) -> object:
    """Read one JSON file."""
    logger.info(f"Reading {path}")
    with path.open() as handle:
        return json.load(handle)


def _esc(
    text: object,
) -> str:
    """Escape any value for HTML text or attribute use."""
    return html.escape(str(text), quote=True)


def _commit_scope(
    title: str,
) -> str:
    """Return the conventional-commit scope, so "fix(helm): x" gives "helm"."""
    match = re.match(r"^\w+\(([^)]+)\)!?:", title)
    return match.group(1) if match else ""


def _classify_area(
    title: str,
    labels: list[str],
) -> str:
    """Pick the first area whose labels or title pattern match."""
    lowered = title.lower()
    scope = _commit_scope(lowered)
    if scope:
        for area, _, pattern in AREA_RULES:
            if re.search(pattern, scope):
                return area
    for area, area_labels, pattern in AREA_RULES:
        if area_labels.intersection(labels):
            return area
        if re.search(pattern, lowered):
            return area
    return OTHER_AREA


def _label_names(
    item: dict,
) -> list[str]:
    """Return the label names on a gh item."""
    return [label["name"] for label in item.get("labels", [])]


def _is_security(
    item: dict,
) -> bool:
    """True when the item carries a security label or security scope."""
    if "security" in _label_names(item):
        return True
    return bool(re.search(r"\(security\)|ssrf|cve|harden", item["title"].lower()))


def _url(
    kind: str,
    number: int,
) -> str:
    """Build a github.com link for an issue or pull request."""
    path = "pull" if kind == "pr" else "issues"
    return f"{GITHUB_BASE}/{path}/{number}"


def _parse_time(
    value: str | None,
) -> datetime | None:
    """Parse a GitHub ISO timestamp."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _release_windows(
    releases: list[dict],
) -> list[tuple[str, datetime]]:
    """Return (tag, published time) sorted oldest first, prereleases dropped."""
    windows = []
    for release in releases:
        if release.get("isPrerelease"):
            continue
        windows.append((release["tagName"], _parse_time(release["publishedAt"])))
    windows.sort(key=lambda pair: pair[1])
    return windows


def _release_for(
    closed_at: datetime | None,
    windows: list[tuple[str, datetime]],
) -> str:
    """Name the first release published at or after the close time."""
    if closed_at is None:
        return "Unknown"
    for tag, published in windows:
        if closed_at <= published:
            return tag
    return "Unreleased"


def _parse_issue_triage(
    path: Path,
) -> dict[int, dict]:
    """Read horizon, kind and reasons per issue from the issue-triage markdown."""
    rows: dict[int, dict] = {}
    horizon = None
    for line in path.read_text().splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            horizon = heading if heading in HORIZONS else None
            continue
        if horizon is None or not line.startswith("| [#"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        number = int(re.search(r"#(\d+)", cells[0]).group(1))
        rows[number] = {"horizon": horizon, "kind": cells[2], "why": cells[4]}
    logger.info(f"Parsed {len(rows)} issue-triage rows from {path.name}")
    return rows


def _open_pr_links(
    prs: list[dict],
) -> dict[int, list[int]]:
    """Map issue number to the open PRs that say they close it."""
    links: dict[int, list[int]] = defaultdict(list)
    for pr in prs:
        if pr["state"] != "OPEN":
            continue
        for ref in pr.get("closingIssuesReferences", []):
            links[ref["number"]].append(pr["number"])
    return links


def _pill(
    kind: str,
    number: int,
) -> str:
    """Render the blue #number pill as a link."""
    return f'<a class="pill" href="{_esc(_url(kind, number))}" target="_blank" rel="noopener">#{number}</a>'


def _profile_url(
    login: str,
) -> str:
    """Link a login to its GitHub profile. gh reports apps as "app/name"."""
    if login.startswith("app/"):
        return f"https://github.com/apps/{login[4:]}"
    return f"https://github.com/{login}"


def _handle(
    login: str,
) -> str:
    """Render @login as a link to the profile, showing apps by their bare name."""
    shown = login[4:] if login.startswith("app/") else login
    return f'<a class="handle" href="{_esc(_profile_url(login))}" target="_blank" rel="noopener">@{_esc(shown)}</a>'


def _people_html(
    source: dict | None,
) -> str:
    """Render "by @author, reviewers @x (approved)" for an issue or PR."""
    if not source or not source.get("author"):
        return ""
    text = f"by {_handle(source['author']['login'])}"
    reviewers = source.get("reviewers", [])
    if reviewers:
        parts = [f"{_handle(login)} ({_esc(REVIEW_STATE_TEXT.get(state, state.lower()))})" for login, state in reviewers]
        text += ", reviewers " + ", ".join(parts)
    return f'<span class="people">{text}</span>'


def _people_logins(
    source: dict | None,
) -> str:
    """Plain logins for the filter box, so typing a handle finds the item."""
    if not source or not source.get("author"):
        return ""
    logins = [source["author"]["login"]] + [login for login, _ in source.get("reviewers", [])]
    return " ".join(logins)


def _chip(
    text: str,
    css: str,
    tooltip: str = "",
) -> str:
    """Render a small inline tag."""
    title = f' title="{_esc(tooltip)}"' if tooltip else ""
    return f'<span class="chip {css}"{title}>{_esc(text)}</span>'


def _card(
    kind: str,
    number: int,
    title: str,
    source: dict | None,
    icon: str = "ring",
    area: str = "",
    chips: str = "",
    details: list[str] | None = None,
    note: str = "",
) -> str:
    """Render one issue or PR as a board card.

    Every card has the same width; the column sets it. source is the gh record,
    used for the author and reviewers line. details is a list of bullet rows
    (open PRs only). area feeds the area filter dropdown.
    """
    search_text = _esc(f"#{number} {title} {_people_logins(source)}".lower())
    title_link = f'<a class="title-link" href="{_esc(_url(kind, number))}" target="_blank" rel="noopener">{_esc(title)}</a>'
    area_chip = _chip(area, "area") if area else ""
    details_html = f'<ul>{"".join(details)}</ul>' if details else ""
    note_html = f'<div class="note">{_esc(note)}</div>' if note else ""
    people = _people_html(source)
    people_html = f'<div class="people-row">{people}</div>' if people else ""
    return (
        f'<div class="card" data-search="{search_text}" data-area="{_esc(area)}">'
        f'<div class="card-top"><span class="icon {icon}"></span>{_pill(kind, number)}</div>'
        f'<div class="card-title">{title_link}</div>'
        f'<div class="chips">{area_chip} {chips}</div>'
        f'{details_html}{people_html}{note_html}</div>'
    )


def _bullet(
    text: str,
    icon: str,
) -> str:
    """Render a plain bullet row inside a card."""
    return f'<li class="item"><span class="icon {icon}"></span><div>{_esc(text)}</div></li>'


def _column(
    title: str,
    subtitle: str,
    cards: list[str],
    css: str = "",
) -> str:
    """Render one fixed-width board column: a header, a count, and stacked cards."""
    return (
        f'<section class="column {css}"><div class="col-head">'
        f'<div class="col-title"><span class="col-dot"></span>{_esc(title)}'
        f'<span class="count">{len(cards)}</span></div>'
        f'<div class="sub">{_esc(subtitle)}</div></div>'
        f'<div class="col-body">{"".join(cards)}</div></section>'
    )


def _board(
    columns: list[str],
) -> str:
    """Put columns side by side. The board scrolls sideways when they do not fit."""
    return f'<div class="board">{"".join(columns)}</div>'


def _area_filter(
    areas: list[str],
) -> str:
    """Render the area dropdown for one tab, listing only the areas present."""
    options = "".join(f'<option value="{_esc(a)}">{_esc(a)}</option>' for a in sorted(set(areas)))
    return f'<select class="area-filter"><option value="">All areas</option>{options}</select>'


def _stats(
    tiles: list[tuple[str, int | str]],
) -> str:
    """Render the row of stat tiles at the top of a tab."""
    parts = [f'<div class="tile"><div class="num">{_esc(v)}</div><div>{_esc(k)}</div></div>' for k, v in tiles]
    return f'<div class="tiles">{"".join(parts)}</div>'


def _tab_intro(
    tiles: list[tuple[str, int | str]],
    note_html: str,
    areas: list[str],
) -> str:
    """Stat tiles, then one line holding the area filter and the explanation."""
    return f'{_stats(tiles)}<div class="toolbar">{_area_filter(areas)}<span class="explain">{note_html}</span></div>'


# ---------- Open PRs ----------

ROUTE_ORDER: list[tuple[str, str, str, str]] = [
    # (route or state, column title, column color, explanation)
    ("green-is-enough", "Green is enough", "green", "merge when CI is green"),
    ("tests-are-enough", "Tests are enough", "green", "merge when CI is green and a test exercises the change"),
    ("ai-review-is-enough", "AI review is enough", "blue", "CI, a test, and an AI review that finds nothing"),
    ("human-required", "Human required", "yellow", "a person reads it, whatever the machines say"),
    ("human-plus-author", "Human plus author", "orange", "a person reads it line by line with the author"),
    ("pending-author-rework", "Waiting on the author", "grey", "changes requested and no push since"),
    ("ci-failing", "CI failing", "red", "checks must pass before review"),
    ("ci-pending", "CI pending", "grey", "checks still running"),
    ("draft", "Draft", "grey", "author is still working"),
]


def _open_pr_card(
    triaged: dict,
    gh_pr: dict,
    issues_by_number: dict[int, dict],
) -> str:
    """Render one open PR with the triage detail that set its route."""
    details = []
    if triaged.get("consequence_from"):
        cons = triaged.get("consequence", 0)
        details.append(_bullet(f"Consequence {cons:.2f}, driven by {triaged['consequence_from']}", "dot"))
    if triaged.get("downgrade"):
        details.append(_bullet(f"Would drop a tier with: {triaged['downgrade']}", "ring"))
    if triaged.get("reason"):
        details.append(_bullet(triaged["reason"], "ring"))
    for ref in gh_pr.get("closingIssuesReferences", []):
        issue = issues_by_number.get(ref["number"], {})
        details.append(_bullet(f"closes #{ref['number']}: {issue.get('title', '')}", "ring"))
    size = f"{gh_pr.get('changedFiles', '?')} files, +{gh_pr.get('additions', 0)}/-{gh_pr.get('deletions', 0)}"
    chips = _chip(size, "size")
    if _is_security(gh_pr):
        chips += " " + _chip("security", "sec")
    area = _classify_area(gh_pr["title"], _label_names(gh_pr))
    return _card("pr", triaged["number"], gh_pr["title"], gh_pr, "dot", area, chips, details)


def _is_open(
    number: int,
    gh_by_number: dict[int, dict],
) -> bool:
    """True when gh still reports the pull request as open."""
    return gh_by_number.get(number, {}).get("state") == "OPEN"


def _new_pr_card(
    gh_pr: dict,
) -> str:
    """Render an open PR that the last pr-triage run has not seen."""
    size = f"{gh_pr.get('changedFiles', '?')} files, +{gh_pr.get('additions', 0)}/-{gh_pr.get('deletions', 0)}"
    chips = _chip(size, "size")
    if gh_pr.get("isDraft"):
        chips += " " + _chip("draft", "edge")
    area = _classify_area(gh_pr["title"], _label_names(gh_pr))
    return _card("pr", gh_pr["number"], gh_pr["title"], gh_pr, "ring", area, chips)


def _start_here_column(
    triage: dict,
    gh_by_number: dict[int, dict],
) -> str:
    """The three PRs pr-triage says to clear first, as a highlighted column."""
    cards = []
    for pick in triage.get("start_here", []):
        if not _is_open(pick["number"], gh_by_number):
            continue
        gh_pr = gh_by_number.get(pick["number"], {"title": pick.get("title", "")})
        area = _classify_area(gh_pr["title"], _label_names(gh_pr))
        chips = _chip(pick.get("route", ""), "route")
        cards.append(_card("pr", pick["number"], gh_pr["title"], gh_pr, "dot", area, chips))
    return _column("Start here", "cheapest evidence first, smallest diff inside that", cards, "highlight")


def _build_open_prs(
    triage: dict,
    gh_by_number: dict[int, dict],
    issues_by_number: dict[int, dict],
) -> str:
    """Render the open PR tab: one column per route or pre-review state.

    The triage results can be hours older than the gh data, so PRs closed
    since then are dropped and PRs opened since then get their own column.
    """
    routed = [pr for pr in triage["pull_requests"] if _is_open(pr["number"], gh_by_number)]
    waiting_list = [pr for pr in triage["not_reviewable"] if _is_open(pr["number"], gh_by_number)]
    seen = {pr["number"] for pr in triage["pull_requests"] + triage["not_reviewable"]}
    new_prs = [pr for pr in gh_by_number.values() if pr["state"] == "OPEN" and pr["number"] not in seen]
    new_prs.sort(key=lambda pr: -pr["number"])

    groups: dict[str, list[dict]] = defaultdict(list)
    for pr in routed:
        groups[pr["route"]].append(pr)
    for pr in waiting_list:
        groups[pr["state"]].append(pr)

    columns = [_start_here_column(triage, gh_by_number)]
    areas = [_classify_area(pr["title"], _label_names(pr)) for pr in new_prs]
    if new_prs:
        columns.append(_column(NEW_COLUMN_TITLE, NEW_COLUMN_NOTE, [_new_pr_card(pr) for pr in new_prs], "blue"))
    for key, heading, css, explanation in ROUTE_ORDER:
        members = groups.get(key, [])
        if not members:
            continue
        cards = []
        for pr in members:
            gh_pr = gh_by_number.get(pr["number"], pr)
            areas.append(_classify_area(gh_pr["title"], _label_names(gh_pr)))
            cards.append(_open_pr_card(pr, gh_pr, issues_by_number))
        columns.append(_column(heading, explanation, cards, css))

    tiles = [("open PRs", len(routed) + len(waiting_list) + len(new_prs)), ("routed", len(routed)),
             ("not reviewable yet", len(waiting_list)),
             ("need a person", len(groups["human-required"]) + len(groups["human-plus-author"]))]
    if new_prs:
        tiles.append(("new since triage", len(new_prs)))
    note = (f'Routes from <code>pr-triage</code> run {_esc(triage["triaged_at"][:10])} with '
            f'{_esc(triage["model"])}. A route names the evidence a merge would need, not a verdict.')
    return _tab_intro(tiles, note, areas) + _board(columns)


# ---------- Open issues ----------

HORIZON_COLORS: dict[str, str] = {
    "Today": "red",
    "This week": "orange",
    "This month": "yellow",
    "When time permits": "grey",
}
HORIZON_NOTES: dict[str, str] = {
    "Today": "start on these now",
    "This week": "pick up in the next few days",
    "This month": "plan for this month",
    "When time permits": "no pressure, revisit later",
}


def _issue_card(
    issue: dict,
    triage_row: dict,
    pr_links: list[int],
    area: str,
) -> str:
    """Render one open issue with PR-in-flight and reporter-waiting markers."""
    chips = []
    why = triage_row.get("why", "")
    pr_links = sorted(set(pr_links) | {int(n) for n in re.findall(r"#(\d+) already open", why)})
    for pr_number in pr_links:
        chips.append(f'<a class="chip pr" href="{_esc(_url("pr", pr_number))}" target="_blank" rel="noopener">PR #{pr_number} open</a>')
    if "outside reporter" in why:
        chips.append(_chip("reporter waiting", "wait", why))
    if "bucket edge" in why:
        chips.append(_chip("on an edge", "edge", why))
    if _is_security(issue):
        chips.append(_chip("security", "sec"))
    icon = "half" if pr_links else "ring"
    return _card("issue", issue["number"], issue["title"], issue, icon, area, " ".join(chips), note=why)


def _build_open_issues(
    issues: list[dict],
    triage_rows: dict[int, dict],
    pr_links: dict[int, list[int]],
) -> str:
    """Render the open issue tab: one column per time horizon, in triage order."""
    # issue-triage lists each bucket best-first; keep that order inside each column.
    order = {number: index for index, number in enumerate(triage_rows)}
    by_horizon: dict[str, list[str]] = defaultdict(list)
    areas = []
    in_flight = 0
    for issue in sorted(issues, key=lambda i: order.get(i["number"], len(order))):
        row = triage_rows.get(issue["number"], {"horizon": NEW_COLUMN_TITLE, "why": ""})
        area = _classify_area(issue["title"], _label_names(issue))
        areas.append(area)
        by_horizon[row["horizon"]].append(_issue_card(issue, row, pr_links.get(issue["number"], []), area))
        if issue["number"] in pr_links or "already open" in row["why"]:
            in_flight += 1

    columns = []
    if by_horizon[NEW_COLUMN_TITLE]:
        columns.append(_column(NEW_COLUMN_TITLE, NEW_COLUMN_NOTE, by_horizon[NEW_COLUMN_TITLE], "blue"))
    columns += [_column(h, HORIZON_NOTES[h], by_horizon[h], HORIZON_COLORS[h]) for h in HORIZONS]
    waiting = sum(1 for r in triage_rows.values() if "outside reporter" in r["why"])
    tiles = [(h.lower(), len(by_horizon[h])) for h in HORIZONS]
    if by_horizon[NEW_COLUMN_TITLE]:
        tiles.append(("new since triage", len(by_horizon[NEW_COLUMN_TITLE])))
    tiles += [("PR in flight", in_flight), ("reporter waiting", waiting)]
    note = ('Horizons and reasons from <code>issue-triage</code>; areas from labels and title keywords. '
            'The grey line on each card says why it sits in its column.')
    return _tab_intro(tiles, note, areas) + _board(columns)


# ---------- Closed PRs and issues ----------


def _closed_by_release(
    items: list[dict],
    windows: list[tuple[str, datetime]],
    time_field: str,
) -> dict[str, list[dict]]:
    """Group closed items by the release that shipped after they closed."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        stamp = _parse_time(item.get(time_field) or item.get("closedAt"))
        grouped[_release_for(stamp, windows)].append(item)
    return grouped


def _release_order(
    windows: list[tuple[str, datetime]],
) -> list[str]:
    """Newest release first, with Unreleased on top."""
    return ["Unreleased"] + [tag for tag, _ in reversed(windows)] + ["Unknown"]


def _closed_pr_card(
    pr: dict,
    area: str,
) -> str:
    """Render one closed PR: green when merged, grey when closed without merging."""
    merged = pr["state"] == "MERGED"
    chips = "" if merged else _chip("closed unmerged", "edge")
    return _card("pr", pr["number"], pr["title"], pr, "check" if merged else "cross", area, chips)


def _build_closed_prs(
    prs: list[dict],
    windows: list[tuple[str, datetime]],
) -> str:
    """Render the closed PR tab: one column per release, newest first."""
    closed = [p for p in prs if p["state"] != "OPEN"]
    grouped = _closed_by_release(closed, windows, "mergedAt")
    columns = []
    areas = []
    for tag in _release_order(windows):
        members = grouped.get(tag, [])
        if not members:
            continue
        cards = []
        # Dependency bumps go to the bottom of each column; newest first otherwise.
        for pr in sorted(members, key=lambda p: (_classify_area(p["title"], _label_names(p)) == "Dependencies", -p["number"])):
            area = _classify_area(pr["title"], _label_names(pr))
            areas.append(area)
            cards.append(_closed_pr_card(pr, area))
        merged = sum(1 for p in members if p["state"] == "MERGED")
        columns.append(_column(tag, f"{merged} merged, {len(members) - merged} closed without merging", cards, "green"))
    merged_total = sum(1 for p in closed if p["state"] == "MERGED")
    deps = sum(1 for p in closed if _classify_area(p["title"], _label_names(p)) == "Dependencies")
    tiles = [("closed PRs", len(closed)), ("merged", merged_total),
             ("closed unmerged", len(closed) - merged_total), ("dependency bumps", deps)]
    note = 'Each PR sits under the first release published after it merged or closed. Scroll sideways for older releases.'
    return _tab_intro(tiles, note, areas) + _board(columns)


def _closed_issue_card(
    issue: dict,
    area: str,
) -> str:
    """Render one closed issue: green when completed, grey otherwise."""
    done = issue.get("stateReason") == "COMPLETED"
    chips = "" if done else _chip(issue.get("stateReason", "").lower().replace("_", " "), "edge")
    if _is_security(issue):
        chips += " " + _chip("security", "sec")
    return _card("issue", issue["number"], issue["title"], issue, "check" if done else "cross", area, chips)


def _build_closed_issues(
    issues: list[dict],
    windows: list[tuple[str, datetime]],
) -> str:
    """Render the closed issue tab: one column per release, newest first."""
    closed = [i for i in issues if i["state"] == "CLOSED"]
    grouped = _closed_by_release(closed, windows, "closedAt")
    columns = []
    areas = []
    for tag in _release_order(windows):
        members = grouped.get(tag, [])
        if not members:
            continue
        cards = []
        for issue in sorted(members, key=lambda i: -i["number"]):
            area = _classify_area(issue["title"], _label_names(issue))
            areas.append(area)
            cards.append(_closed_issue_card(issue, area))
        done = sum(1 for i in members if i.get("stateReason") == "COMPLETED")
        columns.append(_column(tag, f"{done} completed, {len(members) - done} not planned or duplicate", cards, "green"))
    done_total = sum(1 for i in closed if i.get("stateReason") == "COMPLETED")
    tiles = [("closed issues", len(closed)), ("completed", done_total),
             ("not planned or duplicate", len(closed) - done_total)]
    note = 'Each issue sits under the first release published after it closed. Scroll sideways for older releases.'
    return _tab_intro(tiles, note, areas) + _board(columns)


# ---------- Page ----------

# Colors live in variables so the theme switch swaps one block.
# LIGHT_VARS is also forced under @media print, so a printout is always light.
LIGHT_VARS: str = """
--bg:#fff;--fg:#1f2328;--muted:#57606a;--faint:#8c959f;--line:#e5e7eb;--dash:#c9ced6;
--section-bg:#fbfcfd;--card-bg:#fff;--btn-bg:#f6f8fa;--btn-line:#d0d7de;
--accent:#1d4ed8;--accent-fg:#fff;--link:#0969da;--owner:#4338ca;
--yellow-line:#e3b341;--yellow-bg:#fffaeb;--orange-line:#f0883e;--orange-bg:#fff4ec;
--green-line:#4ac26b;--green-bg:#f0fbf3;--blue-line:#54aeff;--blue-bg:#eef6ff;
--red-line:#f85149;--red-bg:#fff0ef;
--done:#1a7f37;--gold:#d29922;--chip-pr-fg:#9a6700;--chip-pr-bg:#fff8c5;
--wait:#cf222e;--sec:#8250df;--cross:#d0d7de;
--highlight-glow:#1d4ed826;--highlight-bg:#f5f8ff;
"""

DARK_VARS: str = """
--bg:#0d1117;--fg:#e6edf3;--muted:#9da7b3;--faint:#7d8590;--line:#30363d;--dash:#3d444d;
--section-bg:#11161d;--card-bg:#161b22;--btn-bg:#21262d;--btn-line:#3d444d;
--accent:#2f6feb;--accent-fg:#fff;--link:#58a6ff;--owner:#a5b4fc;
--yellow-line:#9e6a03;--yellow-bg:#272115;--orange-line:#bd561d;--orange-bg:#2a1b12;
--green-line:#2ea043;--green-bg:#12261a;--blue-line:#1f6feb;--blue-bg:#121d2f;
--red-line:#da3633;--red-bg:#2b1517;
--done:#3fb950;--gold:#d29922;--chip-pr-fg:#e3b341;--chip-pr-bg:#2e2510;
--wait:#ff7b72;--sec:#bc8cff;--cross:#484f58;
--highlight-glow:#2f6feb40;--highlight-bg:#111a2b;
"""

CSS: str = """
:root{""" + LIGHT_VARS + """color-scheme:light}
:root[data-theme=dark]{""" + DARK_VARS + """color-scheme:dark}
body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:0;background:var(--bg);color:var(--fg);font-size:14px}
header{padding:14px 24px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:center;flex-wrap:wrap}
header h1{font-size:20px;margin:0}
#source{margin-left:auto;display:inline-flex;align-items:center;gap:6px;color:var(--fg);text-decoration:none;font-weight:600}
#source:hover{color:var(--link)}
.tabs button,#theme,.area-filter{border:1px solid var(--btn-line);background:var(--btn-bg);color:var(--fg);padding:6px 14px;border-radius:6px;cursor:pointer;font-size:14px;margin-right:6px}
.tabs button.active{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
#search{padding:6px 10px;border:1px solid var(--btn-line);border-radius:6px;width:280px;background:var(--card-bg);color:var(--fg)}
main{padding:14px 24px}
.tab{display:none}.tab.active{display:block}
.tiles{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.tile{border:1px solid var(--line);border-radius:8px;padding:8px 14px;min-width:110px;color:var(--muted);background:var(--card-bg)}
.tile .num{font-size:22px;font-weight:600;color:var(--fg)}
.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.explain{color:var(--muted)}
/* Board: fixed-width columns side by side, scrolling sideways when they do not fit. */
.board{display:flex;gap:12px;overflow-x:auto;align-items:flex-start;padding-bottom:10px}
.column{flex:0 0 """ + str(COLUMN_WIDTH_PX) + """px;width:""" + str(COLUMN_WIDTH_PX) + """px;box-sizing:border-box;display:flex;flex-direction:column;
  max-height:calc(100vh - 230px);border:1px solid var(--line);border-radius:10px;background:var(--section-bg)}
.column.highlight{border:2.5px solid var(--accent);box-shadow:0 0 0 4px var(--highlight-glow);background:var(--highlight-bg)}
.col-head{padding:10px 12px 6px;border-bottom:1px solid var(--line)}
.col-title{font-weight:600;font-size:15px;display:flex;align-items:center;gap:8px}
.col-dot{width:12px;height:12px;border-radius:50%;border:2px solid var(--faint);flex:none}
.column.green .col-dot{border-color:var(--green-line)}
.column.yellow .col-dot{border-color:var(--yellow-line)}
.column.orange .col-dot{border-color:var(--orange-line)}
.column.red .col-dot{border-color:var(--red-line)}
.column.blue .col-dot{border-color:var(--blue-line)}
.column.highlight .col-dot{border-color:var(--accent);background:var(--accent)}
.count{background:var(--btn-bg);border:1px solid var(--btn-line);color:var(--muted);border-radius:10px;padding:0 8px;font-size:12px;font-weight:500}
.sub{color:var(--muted);font-size:12px;margin-top:2px}
.col-body{overflow-y:auto;padding:8px;display:flex;flex-direction:column;gap:8px}
.card{border:1px solid var(--line);border-radius:8px;padding:8px 10px;background:var(--card-bg)}
.card:hover{border-color:var(--btn-line)}
.card-top{display:flex;align-items:center;gap:6px}
.card-title{font-size:14px;line-height:1.35;margin:4px 0}
.chips{line-height:1.8}
ul{list-style:none;padding:0;margin:4px 0}
.item{display:flex;gap:8px;margin:3px 0;line-height:1.35;font-size:12px;color:var(--muted)}
.icon{flex:none;width:12px;height:12px;border-radius:50%;border:1.5px solid var(--faint)}
.item .icon{width:9px;height:9px;margin-top:3px}
.icon.dot{background:var(--accent);border-color:var(--accent)}
.icon.half{background:linear-gradient(90deg,var(--gold) 50%,var(--card-bg) 50%);border-color:var(--gold)}
.icon.check{background:var(--done);border-color:var(--done)}
.icon.cross{background:var(--cross);border-color:var(--faint)}
.pill{background:var(--accent);color:var(--accent-fg);border-radius:10px;padding:0 7px;font-size:12px;font-weight:600;text-decoration:none}
.chip{display:inline-block;border-radius:10px;padding:0 6px;font-size:11px;margin-right:2px;border:1px solid var(--btn-line);color:var(--muted);text-decoration:none;line-height:1.5}
.chip.pr{border-color:var(--gold);color:var(--chip-pr-fg);background:var(--chip-pr-bg)}
.chip.wait{border-color:var(--wait);color:var(--wait)}
.chip.sec{border-color:var(--sec);color:var(--sec)}
.chip.edge{color:var(--faint)}
.chip.area{color:var(--link);border-color:var(--blue-line)}
.chip.route{color:var(--owner);border-color:var(--owner)}
.note{color:var(--faint);font-size:12px;margin-top:2px}
.title-link{color:inherit;text-decoration:none}
.title-link:hover{color:var(--link);text-decoration:underline}
.people-row{margin-top:2px}
.people{font-size:11px;color:var(--muted)}
.handle{color:var(--owner);font-weight:600;text-decoration:none}
.handle:hover{text-decoration:underline}
.hidden{display:none !important}
@media print{
  :root,:root[data-theme=dark]{""" + LIGHT_VARS + """color-scheme:light}
  *{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .tabs,#search,#theme,#source,.area-filter{display:none}
  .board{flex-wrap:wrap;overflow:visible}
  .column{max-height:none}
  .col-body{overflow:visible}
  .card{break-inside:avoid}
}
"""

# Runs in <head> before the body paints, so there is no white flash.
# A saved choice from the theme button wins; otherwise the page opens dark.
DEFAULT_THEME: str = "dark"
THEME_INIT_JS: str = """
(function(){
  var saved=null;
  try{saved=localStorage.getItem('triage-theme');}catch(e){}
  document.documentElement.dataset.theme=saved||'""" + DEFAULT_THEME + """';
})();
"""

JS: str = """
const tabs=document.querySelectorAll('.tabs button');
tabs.forEach(b=>b.addEventListener('click',()=>{
  tabs.forEach(x=>x.classList.remove('active'));b.classList.add('active');
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t.id===b.dataset.tab));
  applyFilters();
}));
const box=document.getElementById('search');
// Hide cards that miss the text filter or the area dropdown, update each
// column's count, and hide columns left empty while a filter is on.
function applyFilters(){
  const q=box.value.trim().toLowerCase();
  const tab=document.querySelector('.tab.active');
  const select=tab.querySelector('.area-filter');
  const area=select?select.value:'';
  const filtering=q!==''||area!=='';
  tab.querySelectorAll('.card').forEach(c=>{
    const textOk=q===''||c.dataset.search.includes(q);
    const areaOk=area===''||c.dataset.area===area;
    c.classList.toggle('hidden',!(textOk&&areaOk));
  });
  tab.querySelectorAll('.column').forEach(col=>{
    const shown=col.querySelectorAll('.card:not(.hidden)').length;
    col.querySelector('.count').textContent=shown;
    col.classList.toggle('hidden',filtering&&shown===0);
  });
}
box.addEventListener('input',applyFilters);
document.querySelectorAll('.area-filter').forEach(s=>s.addEventListener('change',applyFilters));
const themeBtn=document.getElementById('theme');
function showTheme(){themeBtn.textContent=document.documentElement.dataset.theme==='dark'?'Light mode':'Dark mode';}
themeBtn.addEventListener('click',()=>{
  const next=document.documentElement.dataset.theme==='dark'?'light':'dark';
  document.documentElement.dataset.theme=next;
  try{localStorage.setItem('triage-theme',next);}catch(e){}
  showTheme();
});
showTheme();
// "generated 2 hours ago (2026-11-12 22:58 UTC)", refreshed every minute.
const stamp=document.getElementById('generated');
function relativeTime(then){
  const minutes=Math.floor((Date.now()-then.getTime())/60000);
  if(minutes<1)return 'just now';
  if(minutes<60)return minutes===1?'1 minute ago':minutes+' minutes ago';
  const hours=Math.floor(minutes/60);
  if(hours<24)return hours===1?'1 hour ago':hours+' hours ago';
  const days=Math.floor(hours/24);
  if(days===1)return 'yesterday';
  return days+' days ago';
}
function showStamp(){
  const then=new Date(stamp.getAttribute('datetime'));
  if(isNaN(then))return;
  stamp.textContent=relativeTime(then)+' ('+stamp.dataset.utc+')';
}
showStamp();
setInterval(showStamp,60000);
"""


def _page(
    tabs: list[tuple[str, str, str]],
    generated: datetime,
) -> str:
    """Assemble the full HTML page from (id, label, body) tabs.

    generated is the build time. The page shows it in UTC, and the browser
    puts "2 hours ago" style text in front of it (see RELATIVE_TIME_JS).
    """
    generated_text = generated.strftime("%Y-%m-%d %H:%M UTC")
    generated_iso = generated.isoformat(timespec="seconds")
    buttons = "".join(
        f'<button data-tab="{tid}" class="{"active" if i == 0 else ""}">{_esc(label)}</button>'
        for i, (tid, label, _) in enumerate(tabs)
    )
    bodies = "".join(
        f'<div class="tab{" active" if i == 0 else ""}" id="{tid}">{body}</div>'
        for i, (tid, label, body) in enumerate(tabs)
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>Triage dashboard</title>'
        f'<style>{CSS}</style><script>{THEME_INIT_JS}</script></head><body><header><h1><a class="title-link" href="{_esc(GITHUB_BASE)}" target="_blank" rel="noopener">{_esc(REPO)}</a></h1>'
        f'<div class="tabs">{buttons}</div><input id="search" placeholder="Filter by number, title or handle">'
        f'<button id="theme" type="button">Light mode</button>'
        f'<span class="note">generated <time id="generated" datetime="{_esc(generated_iso)}" '
        f'data-utc="{_esc(generated_text)}">{_esc(generated_text)}</time></span>'
        f'<a id="source" href="https://github.com/{_esc(DASHBOARD_REPO)}" target="_blank" rel="noopener" '
        f'title="Source for this dashboard: {_esc(DASHBOARD_REPO)}">{GITHUB_ICON_SVG}Source</a></header>'
        f'<main>{bodies}</main><script>{JS}</script></body></html>'
    )


def _attach_reviewers(
    prs: list[dict],
    reviews: list[dict],
) -> None:
    """Add pr["reviewers"] = [(login, state)]: latest reviews, then pending requests."""
    by_number = {r["number"]: r for r in reviews}
    for pr in prs:
        record = by_number.get(pr["number"], {})
        reviewers: list[tuple[str, str]] = []
        seen = {pr["author"]["login"]} | BOT_REVIEWERS
        for review in record.get("latestReviews", []):
            login = review["author"]["login"]
            if login not in seen:
                reviewers.append((login, review["state"]))
                seen.add(login)
        for request in record.get("reviewRequests", []):
            login = request.get("login") or request.get("name") or ""
            if login and login not in seen:
                reviewers.append((login, "REQUESTED"))
                seen.add(login)
        pr["reviewers"] = reviewers


def _parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Build an HTML triage dashboard for PRs and issues",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example:\n    uv run python build_dashboard.py --data-dir data",
    )
    parser.add_argument("--data-dir", default="data", help="Folder with gh exports and triage reports")
    parser.add_argument("--out", default=OUTPUT_FILE, help="Output HTML file")
    return parser.parse_args()


def main() -> None:
    """Load the inputs, render each tab and write the page."""
    args = _parse_args()
    data = Path(args.data_dir)
    issues = _load_json(data / "issues.json")
    prs = _load_json(data / "prs.json")
    _attach_reviewers(prs, _load_json(data / "reviews.json"))
    windows = _release_windows(_load_json(data / "releases.json"))
    pr_triage = _load_json(data / PR_TRIAGE_FILE)
    issue_rows = _parse_issue_triage(data / ISSUE_TRIAGE_FILE)

    gh_by_number = {pr["number"]: pr for pr in prs}
    open_issues = [i for i in issues if i["state"] == "OPEN"]
    issues_by_number = {i["number"]: i for i in issues}
    tabs = [
        ("open-prs", f"Open PRs ({sum(1 for p in prs if p['state'] == 'OPEN')})", _build_open_prs(pr_triage, gh_by_number, issues_by_number)),
        ("open-issues", f"Open issues ({len(open_issues)})", _build_open_issues(open_issues, issue_rows, _open_pr_links(prs))),
        ("closed-prs", f"Closed PRs ({sum(1 for p in prs if p['state'] != 'OPEN')})", _build_closed_prs(prs, windows)),
        ("closed-issues", f"Closed issues ({len(issues) - len(open_issues)})", _build_closed_issues(issues, windows)),
    ]
    generated = datetime.now(UTC)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(_page(tabs, generated))
    logger.info(f"Wrote {args.out} for {REPO}")


if __name__ == "__main__":
    main()
