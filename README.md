# Claude Code Intelligence Digest

**[https://dannyockilson.github.io/claude-code-digest](https://dannyockilson.github.io/claude-code-digest)**

Automated weekly digest of Claude Code ecosystem updates, community tools, and research items. Runs entirely on GitHub Actions + Claude API.

## How It Works

1. **Collect** — Monitors GitHub releases, npm packages, RSS feeds, Reddit, docs pages, and a GitHub Issues inbox
2. **Curate** — Summarizes and scores items with Claude Haiku (Batch API), synthesizes editorial with Claude Sonnet
3. **Publish** — Generates a markdown digest, deploys to GitHub Pages, closes processed inbox issues

## Setup

1. Clone/fork this repo
2. Add `ANTHROPIC_API_KEY` to **Settings → Secrets → Actions**
3. Enable **GitHub Pages** (source: `docs/` folder on `main` branch)
4. Edit `data/sources.yml` to customise monitored sources
5. Manually trigger the workflow via the **Actions** tab to verify
6. Optionally add the shell alias below for quick inbox capture

## Quick Inbox Add

```bash
# Add to shell profile
alias inbox='gh issue create --repo dannyockilson/claude-code-digest --label inbox --title'

# Usage:
inbox "https://github.com/cool/tool"
inbox "Research: Claude Code agent teams pattern"

# With notes:
gh issue create --repo dannyockilson/claude-code-digest --label inbox \
  --title "https://example.com/post" \
  --body "Interesting approach to multi-agent workflows"
```

## Manual Trigger

```bash
# Full run
gh workflow run digest.yml

# Dry run (collect + curate, no publish)
gh workflow run digest.yml -f dry_run=true
```

## Architecture

```
collect.py → collected_items.json → curate.py → curated_digest.json → publish.py → docs/digests/YYYY-MM-DD.md
```

All state is stored in `data/state.db` (SQLite, committed to repo). No external services beyond GitHub API and Claude API.

## Cost

Target < $15/year. Haiku Batch API (50% discount) handles summarization; one Sonnet call per week for editorial synthesis.

## License

MIT
