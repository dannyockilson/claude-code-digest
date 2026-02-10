"""Publishing: generate digest page, update index, close inbox issues, update state."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from config import (
    CURATED_DIGEST_PATH,
    DIGESTS_DIR,
    DOCS_DIR,
    GITHUB_API_BASE,
    REPO_NAME,
    REPO_OWNER,
    get_db,
    github_headers,
    logger,
)

PAGES_BASE_URL = f"https://{REPO_OWNER}.github.io/claude-code-digest"


def generate_digest_page(digest: dict) -> Path:
    """Write the digest markdown file with Jekyll front matter."""
    date = digest["date"]
    dt = datetime.strptime(date, "%Y-%m-%d")
    title = f"Claude Code Digest — {dt.strftime('%d %b %Y')}"

    front_matter = f"""---
layout: default
title: "{title}"
date: {date}
items: {digest['items_included']}
---

"""
    content = front_matter + digest["digest_markdown"]

    DIGESTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DIGESTS_DIR / f"{date}.md"
    out_path.write_text(content, encoding="utf-8")
    logger.info("Generated digest page: %s", out_path)
    return out_path


def update_index(digest_date: str) -> None:
    """Rewrite docs/index.md with latest digest and archive list."""
    dt = datetime.strptime(digest_date, "%Y-%m-%d")

    # Collect all existing digest files
    digest_files = sorted(DIGESTS_DIR.glob("*.md"), reverse=True)
    archive_lines = []
    for f in digest_files:
        d = f.stem  # YYYY-MM-DD
        try:
            fdt = datetime.strptime(d, "%Y-%m-%d")
            label = fdt.strftime("%d %b %Y")
        except ValueError:
            label = d
        archive_lines.append(f"- [{label}](digests/{d})")

    index_content = f"""---
layout: default
title: Home
---

# Claude Code Intelligence Digest

Automated weekly digest of Claude Code ecosystem updates, community tools, and research.

## Latest Digest

**[{dt.strftime('%d %b %Y')}](digests/{digest_date})** — View the latest digest.

## Archive

{chr(10).join(archive_lines) if archive_lines else '_No digests yet._'}
"""

    index_path = DOCS_DIR / "index.md"
    index_path.write_text(index_content, encoding="utf-8")
    logger.info("Updated index.md with %d archive entries", len(archive_lines))


def close_inbox_issues(items: list, digest_date: str) -> None:
    """Close processed inbox issues with a link to the digest."""
    headers = github_headers()
    digest_url = f"{PAGES_BASE_URL}/digests/{digest_date}"

    for item in items:
        if item.get("source_type") != "inbox":
            continue
        issue_number = item.get("metadata", {}).get("issue_number")
        if not issue_number:
            continue

        try:
            # Add comment
            requests.post(
                f"{GITHUB_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}/comments",
                headers=headers,
                json={"body": f"✅ Processed in digest: [{digest_date}]({digest_url})"},
                timeout=15,
            )
            # Close issue
            requests.patch(
                f"{GITHUB_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}",
                headers=headers,
                json={"state": "closed"},
                timeout=15,
            )
            logger.info("Closed inbox issue #%d", issue_number)
        except Exception as e:
            logger.error("Failed to close issue #%d: %s", issue_number, e)


def update_state_db(digest: dict) -> None:
    """Update state.db with digest run info and mark items processed."""
    db = get_db()
    date = digest["date"]

    # Record the digest run
    db.execute(
        "INSERT OR REPLACE INTO digest_runs (run_date, items_collected, items_included, items_filtered, status) VALUES (?, ?, ?, ?, ?)",
        (date, digest["item_count"], digest["items_included"], digest["items_filtered"], "completed"),
    )

    # Mark included items with last_digest date
    for item in digest.get("items", []):
        db.execute(
            "UPDATE seen_items SET last_digest = ? WHERE url_hash = ?",
            (date, item["id"]),
        )

    db.commit()
    logger.info("Updated state.db for digest %s", date)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if not CURATED_DIGEST_PATH.exists():
        logger.error("No curated_digest.json found. Run curate.py first.")
        return 1

    digest = json.loads(CURATED_DIGEST_PATH.read_text(encoding="utf-8"))
    logger.info("Publishing digest for %s (%d items)", digest["date"], digest["items_included"])

    # Generate digest page
    generate_digest_page(digest)

    # Update index
    update_index(digest["date"])

    # Close inbox issues
    close_inbox_issues(digest.get("items", []), digest["date"])

    # Update state database
    update_state_db(digest)

    # Clean up temp files
    CURATED_DIGEST_PATH.unlink(missing_ok=True)
    from config import COLLECTED_ITEMS_PATH
    COLLECTED_ITEMS_PATH.unlink(missing_ok=True)
    logger.info("Cleaned up temp files")

    logger.info("Publishing complete for %s", digest["date"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
