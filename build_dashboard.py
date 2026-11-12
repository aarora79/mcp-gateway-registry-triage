"""Build a self-contained HTML triage dashboard for PRs and issues.

Reads the gh exports (fetch_data.sh) and the pr-triage / issue-triage
reports in ./data, and writes an HTML page with four tabs: open PRs, open
issues, closed PRs, closed issues. It also writes a print version with every
section expanded, which is the input for the PDF.

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
import math
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
OPEN_RELEASES_EXPANDED: int = 3
# Review accounts that are automation, not people.
BOT_REVIEWERS: set[str] = {"github-advanced-security", "copilot-pull-request-reviewer", "Copilot"}
REVIEW_STATE_TEXT: dict[str, str] = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes requested",
    "COMMENTED": "commented",
    "DISMISSED": "dismissed",
    "REQUESTED": "requested",
}
OUTPUT_NAME: str = "mcp-gateway-registry-triage-dashboard"
MAX_SECTION_COLUMNS: int = 4
MAX_CARD_COLUMNS: int = 3
ITEMS_PER_COLUMN: int = 8
CARD_BASIS_PX: int = 300
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


def _item(
    kind: str,
    number: int,
    title: str,
    icon: str,
    extras: str = "",
    note: str = "",
    source: dict | None = None,
) -> str:
    """Render one row inside a card: icon, pill, linked title, chips, people, note.

    source is the gh record for the issue or PR, used for author and reviewers.
    """
    search_text = _esc(f"#{number} {title} {_people_logins(source)}".lower())
    note_html = f'<div class="note">{_esc(note)}</div>' if note else ""
    title_link = f'<a class="title-link" href="{_esc(_url(kind, number))}" target="_blank" rel="noopener">{_esc(title)}</a>'
    return (
        f'<li class="item" data-search="{search_text}"><span class="icon {icon}"></span>'
        f'<div>{_pill(kind, number)} {title_link} {extras} {_people_html(source)}{note_html}</div></li>'
    )


def _card(
    title: str,
    owner: str,
    status: str,
    status_css: str,
    items: list[str],
    card_css: str = "",
    meta: str = "",
    search_text: str = "",
    title_url: str = "",
) -> str:
    """Render a card with a header, status label and a list of items.

    search_text makes the whole card match the filter box, used when a card
    is one PR rather than a list of them.
    """
    meta_html = f'<div class="meta">{meta}</div>' if meta else ""
    search_attr = f' data-search="{_esc(search_text.lower())}"' if search_text else ""
    title_html = _esc(title)
    if title_url:
        title_html = f'<a class="title-link" href="{_esc(title_url)}" target="_blank" rel="noopener">{title_html}</a>'
    columns = _card_columns(len(items))
    style = f' style="flex:{columns} 1 {columns * CARD_BASIS_PX}px;max-width:{columns * 760}px"'
    # data-cols lets _section size itself from the cards it holds.
    return (
        f'<div class="card {card_css}" data-cols="{columns}"{style}{search_attr}>'
        f'<div class="card-head"><div>'
        f'<div class="card-title">{title_html}</div><div class="owner">{owner}</div></div>'
        f'<div class="status {status_css}">{_esc(status)}</div></div>{meta_html}'
        f'<ul style="column-count:{columns}">{"".join(items)}</ul></div>'
    )


def _card_columns(
    item_count: int,
) -> int:
    """How many item columns a card needs: one per ITEMS_PER_COLUMN items."""
    return max(1, min(MAX_CARD_COLUMNS, math.ceil(item_count / ITEMS_PER_COLUMN)))


def _section(
    title: str,
    subtitle: str,
    cards: list[str],
    collapsible: bool = False,
    expanded: bool = True,
) -> str:
    """Render a dashed section holding a grid of cards.

    The section's starting width and share of spare row space both follow
    its card count (capped), so small sections share a row and fill it.
    """
    body = f'<div class="grid">{"".join(cards)}</div>'
    card_columns = sum(int(n) for n in re.findall(r'data-cols="(\d+)"', "".join(cards)))
    columns = max(1, min(card_columns, MAX_SECTION_COLUMNS))
    style = f' style="flex:{columns} 1 {columns * CARD_BASIS_PX + 40}px"'
    if not collapsible:
        return (
            f'<section class="section"{style}><h2>{_esc(title)}</h2>'
            f'<div class="sub">{subtitle}</div>{body}</section>'
        )
    open_attr = " open" if expanded else ""
    return (
        f'<details class="section"{style}{open_attr}><summary><h2>{_esc(title)}</h2>'
        f'<div class="sub">{subtitle}</div></summary>{body}</details>'
    )


def _pack(
    sections: list[str],
) -> str:
    """Put sections in one wrapping container so small ones share a row."""
    return f'<div class="sections">{"".join(sections)}</div>'


def _stats(
    tiles: list[tuple[str, int | str]],
) -> str:
    """Render the row of stat tiles at the top of a tab."""
    parts = [f'<div class="tile"><div class="num">{_esc(v)}</div><div>{_esc(k)}</div></div>' for k, v in tiles]
    return f'<div class="tiles">{"".join(parts)}</div>'


# ---------- Open PRs ----------

ROUTE_ORDER: list[tuple[str, str, str, str]] = [
    # (route or state, heading, card css, explanation)
    ("green-is-enough", "Green is enough", "green", "merge when CI is green"),
    ("tests-are-enough", "Tests are enough", "green", "merge when CI is green and a test exercises the change"),
    ("ai-review-is-enough", "AI review is enough", "blue", "CI, a test, and an AI review that finds nothing"),
    ("human-required", "Human required", "yellow", "a person reads it, whatever the machines say"),
    ("human-plus-author", "Human plus author", "orange", "a person reads it line by line with the author"),
    ("pending-author-rework", "Waiting on the author", "dashed", "changes requested and no push since"),
    ("ci-failing", "CI failing", "red", "checks must pass before review"),
    ("ci-pending", "CI pending", "dashed", "checks still running"),
    ("draft", "Draft", "dashed", "author is still working"),
]


def _open_pr_card(
    pr: dict,
    triaged: dict,
    gh_pr: dict,
    css: str,
    issues_by_number: dict[int, dict],
) -> str:
    """Render one open PR as a card, in the style of a work-item card."""
    items = []
    if triaged.get("consequence_from"):
        cons = triaged.get("consequence", 0)
        items.append(_bullet(f"Consequence {cons:.2f}, driven by {triaged['consequence_from']}", "dot"))
    if triaged.get("downgrade"):
        items.append(_bullet(f"Would drop a tier with: {triaged['downgrade']}", "ring"))
    if triaged.get("reason"):
        items.append(_bullet(triaged["reason"], "ring"))
    for ref in gh_pr.get("closingIssuesReferences", []):
        issue = issues_by_number.get(ref["number"], {})
        items.append(_item("issue", ref["number"], f"closes: {issue.get('title', '')}", "ring", source=issue))
    size = f"{gh_pr.get('changedFiles', '?')} files, +{gh_pr.get('additions', 0)}/-{gh_pr.get('deletions', 0)}"
    area = _classify_area(gh_pr["title"], _label_names(gh_pr))
    meta = f"{_pill('pr', pr['number'])} {_chip(area, 'area')} {_chip(size, 'size')}"
    if _is_security(gh_pr):
        meta += " " + _chip("security", "sec")
    owner = _people_html(gh_pr)
    search_text = f"#{pr['number']} {gh_pr['title']} {_people_logins(gh_pr)}"
    return _card(gh_pr["title"], owner, "", "", items, css, meta, search_text, _url("pr", pr["number"]))


def _bullet(
    text: str,
    icon: str,
) -> str:
    """Render a plain bullet row with no issue pill."""
    return f'<li class="item"><span class="icon {icon}"></span><div>{_esc(text)}</div></li>'


def _build_open_prs(
    triage: dict,
    gh_by_number: dict[int, dict],
    issues_by_number: dict[int, dict],
) -> str:
    """Render the open PR tab, one section per route or pre-review state."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for pr in triage["pull_requests"]:
        groups[pr["route"]].append(pr)
    for pr in triage["not_reviewable"]:
        groups[pr["state"]].append(pr)

    html_parts = [_start_here(triage, gh_by_number)]
    for key, heading, css, explanation in ROUTE_ORDER:
        members = groups.get(key, [])
        if not members:
            continue
        cards = [_open_pr_card(pr, pr, gh_by_number.get(pr["number"], pr), css, issues_by_number) for pr in members]
        html_parts.append(_section(f"{heading} ({len(members)})", _esc(explanation), cards))

    reviewable = len(triage["pull_requests"])
    waiting = len(triage["not_reviewable"])
    tiles = [("open PRs", reviewable + waiting), ("routed", reviewable), ("not reviewable yet", waiting),
             ("need a person", len(groups["human-required"]) + len(groups["human-plus-author"]))]
    note = (f'<p class="explain">Routes from <code>pr-triage</code> run {_esc(triage["triaged_at"][:10])} '
            f'with {_esc(triage["model"])}. A route names the evidence a merge would need, not a verdict.</p>')
    return _stats(tiles) + note + _pack(html_parts)


def _start_here(
    triage: dict,
    gh_by_number: dict[int, dict],
) -> str:
    """Render the three PRs pr-triage says to clear first."""
    items = []
    for pick in triage.get("start_here", []):
        number = pick["number"]
        title = gh_by_number.get(number, {}).get("title", pick.get("title", ""))
        source = gh_by_number.get(number)
        items.append(_item("pr", number, title, "dot", _chip(pick.get("route", ""), "area"), source=source))
    card = _card("Start here", "Cheapest evidence first, smallest diff inside that", "today", "", items, "yellow")
    return _section("Start here", "What to clear this morning", [card])


# ---------- Open issues ----------


def _issue_row(
    issue: dict,
    triage_row: dict,
    pr_links: list[int],
) -> str:
    """Render one open issue with PR-in-flight and reporter-waiting markers."""
    extras = []
    why = triage_row.get("why", "")
    pr_links = sorted(set(pr_links) | {int(n) for n in re.findall(r"#(\d+) already open", why)})
    for pr_number in pr_links:
        extras.append(f'<a class="chip pr" href="{_esc(_url("pr", pr_number))}" target="_blank" rel="noopener">PR #{pr_number} open</a>')
    if "outside reporter" in why:
        extras.append(_chip("reporter waiting", "wait", why))
    if "bucket edge" in why:
        extras.append(_chip("on an edge", "edge", why))
    if _is_security(issue):
        extras.append(_chip("security", "sec"))
    icon = "half" if pr_links else "ring"
    return _item("issue", issue["number"], issue["title"], icon, " ".join(extras), why, issue)


def _horizon_card(
    horizon: str,
    rows: list[str],
) -> str:
    """Render one horizon card inside an area section."""
    css = {"Today": "yellow", "This week": "yellow", "This month": "", "When time permits": "dashed"}[horizon]
    return _card(horizon, f"{len(rows)} issue(s)", "", "", rows, css)


def _build_open_issues(
    issues: list[dict],
    triage_rows: dict[int, dict],
    pr_links: dict[int, list[int]],
) -> str:
    """Render the open issue tab: one section per area, one card per horizon."""
    by_area: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    counts = defaultdict(int)
    in_flight = 0
    for issue in issues:
        row = triage_rows.get(issue["number"], {"horizon": "When time permits", "why": "not in triage run"})
        area = _classify_area(issue["title"], _label_names(issue))
        by_area[area][row["horizon"]].append(_issue_row(issue, row, pr_links.get(issue["number"], [])))
        counts[row["horizon"]] += 1
        if issue["number"] in pr_links or "already open" in row["why"]:
            in_flight += 1

    # Areas with the most urgent work come first.
    def _urgency(area: str) -> tuple:
        buckets = by_area[area]
        return (-(len(buckets["Today"]) + len(buckets["This week"])), -sum(len(v) for v in buckets.values()))

    sections = []
    for area in sorted(by_area, key=_urgency):
        buckets = by_area[area]
        cards = [_horizon_card(h, buckets[h]) for h in HORIZONS if buckets[h]]
        total = sum(len(v) for v in buckets.values())
        soon = len(buckets["Today"]) + len(buckets["This week"])
        sections.append(_section(area, f"{total} open, {soon} for today or this week", cards))

    waiting = sum(1 for r in triage_rows.values() if "outside reporter" in r["why"])
    tiles = [(h.lower(), counts[h]) for h in HORIZONS] + [("PR in flight", in_flight), ("reporter waiting", waiting)]
    note = ('<p class="explain">Horizons and reasons from <code>issue-triage</code>; areas from labels and title '
            'keywords. Hover a chip, or read the grey line, for why an issue sits where it does.</p>')
    return _stats(tiles) + note + _pack(sections)


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


def _closed_pr_cards(
    prs: list[dict],
) -> list[str]:
    """Render the area cards for one release worth of closed PRs."""
    by_area: dict[str, list[dict]] = defaultdict(list)
    for pr in prs:
        by_area[_classify_area(pr["title"], _label_names(pr))].append(pr)
    cards = []
    for area in sorted(by_area, key=lambda a: (a == "Dependencies", -len(by_area[a]))):
        rows = []
        for pr in sorted(by_area[area], key=lambda p: -p["number"]):
            merged = pr["state"] == "MERGED"
            extras = "" if merged else _chip("closed unmerged", "edge")
            rows.append(_item("pr", pr["number"], pr["title"], "check" if merged else "cross", extras, source=pr))
        merged_count = sum(1 for p in by_area[area] if p["state"] == "MERGED")
        css = "dashed" if area == "Dependencies" else "green"
        cards.append(_card(area, f"{merged_count} merged of {len(rows)}", "", "", rows, css))
    return cards


def _build_closed_prs(
    prs: list[dict],
    windows: list[tuple[str, datetime]],
) -> str:
    """Render the closed PR tab: one collapsible section per release."""
    closed = [p for p in prs if p["state"] != "OPEN"]
    grouped = _closed_by_release(closed, windows, "mergedAt")
    sections = []
    for index, tag in enumerate(t for t in _release_order(windows) if grouped.get(t)):
        members = grouped[tag]
        merged = sum(1 for p in members if p["state"] == "MERGED")
        sub = f"{merged} merged, {len(members) - merged} closed without merging"
        sections.append(_section(tag, sub, _closed_pr_cards(members), True, index < OPEN_RELEASES_EXPANDED))
    merged_total = sum(1 for p in closed if p["state"] == "MERGED")
    tiles = [("closed PRs", len(closed)), ("merged", merged_total), ("closed unmerged", len(closed) - merged_total),
             ("dependency bumps", sum(1 for p in closed if _classify_area(p["title"], _label_names(p)) == "Dependencies"))]
    note = '<p class="explain">Each PR sits under the first release published after it merged or closed.</p>'
    return _stats(tiles) + note + _pack(sections)


def _closed_issue_cards(
    issues: list[dict],
) -> list[str]:
    """Render the area cards for one release worth of closed issues."""
    by_area: dict[str, list[dict]] = defaultdict(list)
    for issue in issues:
        by_area[_classify_area(issue["title"], _label_names(issue))].append(issue)
    cards = []
    for area in sorted(by_area, key=lambda a: -len(by_area[a])):
        rows = []
        for issue in sorted(by_area[area], key=lambda i: -i["number"]):
            done = issue.get("stateReason") == "COMPLETED"
            extras = "" if done else _chip(issue.get("stateReason", "").lower().replace("_", " "), "edge")
            if _is_security(issue):
                extras += " " + _chip("security", "sec")
            rows.append(_item("issue", issue["number"], issue["title"], "check" if done else "cross", extras, source=issue))
        done_count = sum(1 for i in by_area[area] if i.get("stateReason") == "COMPLETED")
        cards.append(_card(area, f"{done_count} completed of {len(rows)}", "", "", rows, "green"))
    return cards


def _build_closed_issues(
    issues: list[dict],
    windows: list[tuple[str, datetime]],
) -> str:
    """Render the closed issue tab: one collapsible section per release."""
    closed = [i for i in issues if i["state"] == "CLOSED"]
    grouped = _closed_by_release(closed, windows, "closedAt")
    sections = []
    for index, tag in enumerate(t for t in _release_order(windows) if grouped.get(t)):
        members = grouped[tag]
        done = sum(1 for i in members if i.get("stateReason") == "COMPLETED")
        sub = f"{done} completed, {len(members) - done} not planned or duplicate"
        sections.append(_section(tag, sub, _closed_issue_cards(members), True, index < OPEN_RELEASES_EXPANDED))
    done_total = sum(1 for i in closed if i.get("stateReason") == "COMPLETED")
    tiles = [("closed issues", len(closed)), ("completed", done_total), ("not planned or duplicate", len(closed) - done_total)]
    note = '<p class="explain">Each issue sits under the first release published after it closed.</p>'
    return _stats(tiles) + note + _pack(sections)


# ---------- Page ----------

# Colors live in variables so the theme switch swaps one block.
# LIGHT_VARS is also forced under @media print, so the PDF is always light.
LIGHT_VARS: str = """
--bg:#fff;--fg:#1f2328;--muted:#57606a;--faint:#8c959f;--line:#e5e7eb;--dash:#c9ced6;
--section-bg:#fbfcfd;--card-bg:#fff;--btn-bg:#f6f8fa;--btn-line:#d0d7de;
--accent:#1d4ed8;--accent-fg:#fff;--link:#0969da;--owner:#4338ca;
--yellow-line:#e3b341;--yellow-bg:#fffaeb;--orange-line:#f0883e;--orange-bg:#fff4ec;
--green-line:#4ac26b;--green-bg:#f0fbf3;--blue-line:#54aeff;--blue-bg:#eef6ff;
--red-line:#f85149;--red-bg:#fff0ef;
--done:#1a7f37;--gold:#d29922;--chip-pr-fg:#9a6700;--chip-pr-bg:#fff8c5;
--wait:#cf222e;--sec:#8250df;--cross:#d0d7de;
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
"""

CSS: str = """
:root{""" + LIGHT_VARS + """color-scheme:light}
:root[data-theme=dark]{""" + DARK_VARS + """color-scheme:dark}
body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:0;background:var(--bg);color:var(--fg);font-size:14px}
header{padding:16px 24px;border-bottom:1px solid var(--line);display:flex;gap:24px;align-items:center;flex-wrap:wrap}
header h1{font-size:20px;margin:0}
#pdf{color:var(--link);font-weight:600;text-decoration:none}
.tabs button,#theme{border:1px solid var(--btn-line);background:var(--btn-bg);color:var(--fg);padding:6px 14px;border-radius:6px;cursor:pointer;font-size:14px;margin-right:6px}
.tabs button.active{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
#search{padding:6px 10px;border:1px solid var(--btn-line);border-radius:6px;width:280px;background:var(--card-bg);color:var(--fg)}
main{padding:16px 24px}
.tab{display:none}.tab.active{display:block}
.tiles{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.tile{border:1px solid var(--line);border-radius:8px;padding:8px 14px;min-width:110px;color:var(--muted);background:var(--card-bg)}
.tile .num{font-size:22px;font-weight:600;color:var(--fg)}
.explain{color:var(--muted);margin:4px 0 16px}
.sections{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-start}
.section{box-sizing:border-box;max-width:100%;border:1.5px dashed var(--dash);border-radius:12px;padding:10px 12px;background:var(--section-bg)}
.section h2{font-size:20px;margin:0;display:inline}
summary{cursor:pointer;list-style:none}summary::-webkit-details-marker{display:none}
details.section>summary h2::before{content:"\\25B8  ";font-size:14px;color:var(--muted)}
details.section[open]>summary h2::before{content:"\\25BE  "}
.sub{color:var(--muted);margin:2px 0 8px}
.grid{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-start}
.card{box-sizing:border-box;border:1.5px dashed var(--dash);border-radius:8px;padding:8px 10px;background:var(--card-bg)}
.card.yellow{border:1.5px solid var(--yellow-line);background:var(--yellow-bg)}
.card.orange{border:1.5px solid var(--orange-line);background:var(--orange-bg)}
.card.green{border:1.5px solid var(--green-line);background:var(--green-bg)}
.card.blue{border:1.5px solid var(--blue-line);background:var(--blue-bg)}
.card.red{border:1.5px solid var(--red-line);background:var(--red-bg)}
.card-head{display:flex;justify-content:space-between;gap:8px}
.card-title{font-weight:600;font-size:15px}
.owner{color:var(--owner);font-weight:600;font-size:12px;margin-top:2px}
.status{color:var(--muted);font-size:12px;white-space:nowrap}
.meta{margin:6px 0}
ul{list-style:none;padding:0;margin:6px 0 0;column-gap:16px}
.item{break-inside:avoid;display:flex;gap:8px;margin:5px 0;line-height:1.35}
.icon{flex:none;width:12px;height:12px;border-radius:50%;margin-top:3px;border:1.5px solid var(--faint)}
.icon.dot{background:var(--accent);border-color:var(--accent)}
.icon.half{background:linear-gradient(90deg,var(--gold) 50%,var(--card-bg) 50%);border-color:var(--gold)}
.icon.check{background:var(--done);border-color:var(--done)}
.icon.cross{background:var(--cross);border-color:var(--faint)}
.pill{background:var(--accent);color:var(--accent-fg);border-radius:10px;padding:0 7px;font-size:12px;font-weight:600;text-decoration:none}
.chip{display:inline-block;border-radius:10px;padding:0 6px;font-size:11px;margin-left:2px;border:1px solid var(--btn-line);color:var(--muted);text-decoration:none}
.chip.pr{border-color:var(--gold);color:var(--chip-pr-fg);background:var(--chip-pr-bg)}
.chip.wait{border-color:var(--wait);color:var(--wait)}
.chip.sec{border-color:var(--sec);color:var(--sec)}
.chip.edge{color:var(--faint)}
.chip.area{color:var(--link);border-color:var(--blue-line)}
.note{color:var(--faint);font-size:12px}
.title-link{color:inherit;text-decoration:none}
.title-link:hover{color:var(--link);text-decoration:underline}
.people{font-size:11px;color:var(--muted);margin-left:2px}
.handle{color:var(--owner);font-weight:600;text-decoration:none}
.handle:hover{text-decoration:underline}
.hidden{display:none !important}
.print-title{display:none}
@page{size:17in 11in;margin:0.35in}
@media print{
  :root,:root[data-theme=dark]{""" + LIGHT_VARS + """color-scheme:light}
  *{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .tabs,#search,#theme,#pdf{display:none}
  .tab{display:block !important}
  .tab+.tab{break-before:page}
  .print-title{display:block;font-size:22px;margin:0 0 8px}
  .section,.card,.section>summary{break-inside:avoid}
}
"""

# Runs in <head> before the body paints, so a dark-mode reader sees no white flash.
# Saved choice wins; otherwise follow the operating system setting.
THEME_INIT_JS: str = """
(function(){
  var saved=null;
  try{saved=localStorage.getItem('triage-theme');}catch(e){}
  var dark=window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme=saved||(dark?'dark':'light');
})();
"""

JS: str = """
const tabs=document.querySelectorAll('.tabs button');
tabs.forEach(b=>b.addEventListener('click',()=>{
  tabs.forEach(x=>x.classList.remove('active'));b.classList.add('active');
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t.id===b.dataset.tab));
  applySearch();
}));
const box=document.getElementById('search');
function applySearch(){
  const q=box.value.trim().toLowerCase();
  const tab=document.querySelector('.tab.active');
  tab.querySelectorAll('[data-search]').forEach(el=>el.classList.toggle('hidden',q!==''&&!el.dataset.search.includes(q)));
  tab.querySelectorAll('.card').forEach(c=>{
    const rows=c.querySelectorAll('li[data-search]');
    if(rows.length===0)return;
    const any=[...rows].some(r=>!r.classList.contains('hidden'));
    c.classList.toggle('hidden',q!==''&&!any);
  });
  tab.querySelectorAll('.section').forEach(s=>{
    const any=[...s.querySelectorAll('.card')].some(c=>!c.classList.contains('hidden'));
    s.classList.toggle('hidden',q!==''&&!any);
    if(q!==''&&any&&s.tagName==='DETAILS')s.open=true;
  });
}
box.addEventListener('input',applySearch);
const themeBtn=document.getElementById('theme');
function showTheme(){themeBtn.textContent=document.documentElement.dataset.theme==='dark'?'Light mode':'Dark mode';}
themeBtn.addEventListener('click',()=>{
  const next=document.documentElement.dataset.theme==='dark'?'light':'dark';
  document.documentElement.dataset.theme=next;
  try{localStorage.setItem('triage-theme',next);}catch(e){}
  showTheme();
});
showTheme();
"""


def _page(
    tabs: list[tuple[str, str, str]],
    generated: str,
    print_mode: bool = False,
    pdf_link: str = "",
) -> str:
    """Assemble the full HTML page from (id, label, body) tabs.

    print_mode expands every collapsed release, because a printer cannot
    open a <details> element. The print CSS then shows all four tabs.
    pdf_link, when set, adds a header link to the PDF beside the page.
    """
    buttons = "".join(
        f'<button data-tab="{tid}" class="{"active" if i == 0 else ""}">{_esc(label)}</button>'
        for i, (tid, label, _) in enumerate(tabs)
    )
    bodies = "".join(
        f'<div class="tab{" active" if i == 0 else ""}" id="{tid}">'
        f'<h1 class="print-title">{_esc(label)}</h1>{body}</div>'
        for i, (tid, label, body) in enumerate(tabs)
    )
    if print_mode:
        bodies = bodies.replace("<details ", "<details open ")
    pdf_html = ""
    if pdf_link:
        pdf_html = f'<a id="pdf" href="{_esc(pdf_link)}">PDF</a>'
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>Triage dashboard</title>'
        f'<style>{CSS}</style><script>{THEME_INIT_JS}</script></head><body><header><h1><a class="title-link" href="{_esc(GITHUB_BASE)}" target="_blank" rel="noopener">{_esc(REPO)}</a></h1>'
        f'<div class="tabs">{buttons}</div><input id="search" placeholder="Filter by number, title or handle">'
        f'<button id="theme" type="button">Dark mode</button>{pdf_html}'
        f'<span class="note">generated {_esc(generated)}</span></header>'
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
    parser.add_argument("--out", default=f"{OUTPUT_NAME}.html", help="Output HTML file")
    parser.add_argument("--pdf-link", default="", help="Relative link to the PDF, shown in the header")
    parser.add_argument(
        "--print-out",
        default=f"{OUTPUT_NAME}.print.html",
        help="HTML with every section expanded, the input for the PDF",
    )
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
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.print_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(_page(tabs, generated, pdf_link=args.pdf_link))
    Path(args.print_out).write_text(_page(tabs, generated, print_mode=True))
    logger.info(f"Wrote {args.out} and {args.print_out} for {REPO}")


if __name__ == "__main__":
    main()
