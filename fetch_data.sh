#!/bin/bash
# Fetch everything the dashboard reads into ./data:
#   issues, PRs, reviews and releases from gh, then the two triage reports.
#
# Needs: gh (logged in, or GH_TOKEN set), pr-triage, issue-triage,
#        TYPESAFE_API_KEY in the environment.
#
# Usage:
#   export TRIAGE_REPO=agentic-community/mcp-gateway-registry
#   ./fetch_data.sh

# Exit on error
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
TRIAGE_REPO="${TRIAGE_REPO:-agentic-community/mcp-gateway-registry}"
DATA_DIR="$SCRIPT_DIR/data"
GH_LIMIT=5000

if [ -z "$TYPESAFE_API_KEY" ]; then
    echo "TYPESAFE_API_KEY is not set. The triage tools need it." >&2
    exit 1
fi

mkdir -p "$DATA_DIR"
cd "$SCRIPT_DIR"
echo "Fetching data for $TRIAGE_REPO into $DATA_DIR"

echo "Fetching issues..."
gh issue list -R "$TRIAGE_REPO" --state all --limit "$GH_LIMIT" \
    --json number,title,state,stateReason,labels,author,createdAt,closedAt,updatedAt,comments,assignees,milestone \
    > "$DATA_DIR/issues.json"

echo "Fetching pull requests..."
gh pr list -R "$TRIAGE_REPO" --state all --limit "$GH_LIMIT" \
    --json number,title,state,author,createdAt,mergedAt,closedAt,updatedAt,labels,additions,deletions,changedFiles,milestone,isDraft,closingIssuesReferences,headRefName \
    > "$DATA_DIR/prs.json"

echo "Fetching reviews..."
gh pr list -R "$TRIAGE_REPO" --state all --limit "$GH_LIMIT" \
    --json number,latestReviews,reviewRequests \
    > "$DATA_DIR/reviews.json"

echo "Fetching releases..."
gh release list -R "$TRIAGE_REPO" --limit 500 \
    --json tagName,publishedAt,isPrerelease \
    > "$DATA_DIR/releases.json"

echo "Running pr-triage on every open pull request..."
pr-triage "$TRIAGE_REPO" -all -out "$DATA_DIR" -quiet > "$DATA_DIR/pr-triage-stdout.md"

# issue-triage writes data/triage-<owner>-<repo>-open.md relative to the working directory.
echo "Running issue-triage on every open issue..."
issue-triage "$TRIAGE_REPO"

echo "Done. Files in $DATA_DIR:"
ls -la "$DATA_DIR"
