#!/bin/bash
# Fetch everything the dashboard reads into ./data:
#   issues, PRs, reviews and releases from gh, then the two triage reports.
#
# Needs: gh (logged in, or GH_TOKEN set), pr-triage, issue-triage,
#        TYPESAFE_API_KEY in the environment.
#
# Usage:
#   export TRIAGE_REPO=agentic-community/mcp-gateway-registry
#   ./fetch_data.sh                 # GitHub data and both triage tools
#   ./fetch_data.sh --github-only   # GitHub data only: no triage, no TypeSafe key

# Exit on error
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
TRIAGE_REPO="${TRIAGE_REPO:-agentic-community/mcp-gateway-registry}"
DATA_DIR="$SCRIPT_DIR/data"
GH_LIMIT=5000
# GitHub's API sometimes answers a long query with a 502. Retry each call.
MAX_ATTEMPTS=3
RETRY_DELAY_SECONDS=20


# Run a command, retrying on failure. Its stdout goes to a temp file that
# replaces the output file only on success, so a failed attempt never leaves
# half-written JSON behind.
#   fetch_to <output file> <command> [args...]
fetch_to() {
    local out_file="$1"
    shift
    local attempt=1
    while true; do
        if "$@" > "$out_file.tmp"; then
            mv "$out_file.tmp" "$out_file"
            return 0
        fi
        if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
            rm -f "$out_file.tmp"
            echo "Giving up after $attempt attempts: $1 $2 $3" >&2
            return 1
        fi
        echo "Attempt $attempt of $MAX_ATTEMPTS failed: $1 $2 $3. Retrying in ${RETRY_DELAY_SECONDS}s..." >&2
        sleep "$RETRY_DELAY_SECONDS"
        attempt=$((attempt + 1))
    done
}

GITHUB_ONLY=false
if [ "$1" = "--github-only" ]; then
    GITHUB_ONLY=true
fi

if [ "$GITHUB_ONLY" = false ] && [ -z "$TYPESAFE_API_KEY" ]; then
    echo "TYPESAFE_API_KEY is not set. The triage tools need it." >&2
    exit 1
fi

mkdir -p "$DATA_DIR"
cd "$SCRIPT_DIR"
echo "Fetching data for $TRIAGE_REPO into $DATA_DIR"

echo "Fetching issues..."
fetch_to "$DATA_DIR/issues.json" \
    gh issue list -R "$TRIAGE_REPO" --state all --limit "$GH_LIMIT" \
    --json number,title,state,stateReason,labels,author,createdAt,closedAt,updatedAt,comments,assignees,milestone

# One query for every pull request with every field times out on GitHub's
# side (HTTP 502) at this repository's size. Open pull requests need the
# heavy fields (sizes, linked issues); closed ones only need what their card
# shows. Two queries, then one merged file.
echo "Fetching open pull requests..."
fetch_to "$DATA_DIR/prs-open.json" \
    gh pr list -R "$TRIAGE_REPO" --state open --limit "$GH_LIMIT" \
    --json number,title,state,author,createdAt,mergedAt,closedAt,updatedAt,labels,additions,deletions,changedFiles,milestone,isDraft,closingIssuesReferences,headRefName

echo "Fetching closed and merged pull requests..."
fetch_to "$DATA_DIR/prs-closed.json" \
    gh pr list -R "$TRIAGE_REPO" --state closed --limit "$GH_LIMIT" \
    --json number,title,state,author,createdAt,mergedAt,closedAt,labels

jq -s 'add' "$DATA_DIR/prs-open.json" "$DATA_DIR/prs-closed.json" > "$DATA_DIR/prs.json"

echo "Fetching reviews..."
fetch_to "$DATA_DIR/reviews.json" \
    gh pr list -R "$TRIAGE_REPO" --state all --limit "$GH_LIMIT" \
    --json number,latestReviews,reviewRequests

echo "Fetching releases..."
fetch_to "$DATA_DIR/releases.json" \
    gh release list -R "$TRIAGE_REPO" --limit 500 \
    --json tagName,publishedAt,isPrerelease

if [ "$GITHUB_ONLY" = true ]; then
    echo "GitHub-only run: skipping both triage tools."
    ls -la "$DATA_DIR"
    exit 0
fi

echo "Running pr-triage on every open pull request..."
fetch_to "$DATA_DIR/pr-triage-stdout.md" \
    pr-triage "$TRIAGE_REPO" -all -out "$DATA_DIR" -quiet

# issue-triage writes data/triage-<owner>-<repo>-open.md relative to the working directory.
echo "Running issue-triage on every open issue..."
fetch_to "$DATA_DIR/issue-triage-stdout.txt" \
    issue-triage "$TRIAGE_REPO"

echo "Done. Files in $DATA_DIR:"
ls -la "$DATA_DIR"
