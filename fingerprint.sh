#!/bin/bash
# Print one SHA-256 hash of what the page shows about open issues and pull
# requests: which are open, their titles and labels, and for pull requests
# their draft flag and review decision. Comments and CI status are left out
# on purpose: a comment changes nothing the watcher can redraw, and CI state
# only reaches the page through pr-triage at the 7:00 build.
#
# Needs: gh (logged in, or GH_TOKEN set).
#
# Usage:
#   export TRIAGE_REPO=agentic-community/mcp-gateway-registry
#   ./fingerprint.sh

# Exit on error
set -e

TRIAGE_REPO="${TRIAGE_REPO:-agentic-community/mcp-gateway-registry}"
GH_LIMIT=5000

issues=$(gh issue list -R "$TRIAGE_REPO" --state open --limit "$GH_LIMIT" \
    --json number,title,labels \
    --jq '[.[] | {n: .number, t: .title, l: ([.labels[].name] | sort)}] | sort_by(.n)')

prs=$(gh pr list -R "$TRIAGE_REPO" --state open --limit "$GH_LIMIT" \
    --json number,title,labels,isDraft,reviewDecision \
    --jq '[.[] | {n: .number, t: .title, l: ([.labels[].name] | sort), d: .isDraft, r: .reviewDecision}] | sort_by(.n)')

printf 'issues %s\nprs %s\n' "$issues" "$prs" | sha256sum | cut -d' ' -f1
