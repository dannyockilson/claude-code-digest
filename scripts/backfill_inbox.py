"""Backfill inbox issues into existing digest pages.

Usage:
    GITHUB_TOKEN=... ANTHROPIC_API_KEY=... python scripts/backfill_inbox.py

Fetches all open inbox-labelled GitHub issues, determines which published digest
each belongs to based on creation date, summarises with Claude, injects them into
the appropriate digest markdown, closes the issues, and updates state.db.
"""

import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import requests
import trafilatura

from config import (
    CONTENT_MAX_LENGTH,
    DIGESTS_DIR,
    GITHUB_API_BASE,
    HAIKU_MODEL,
    REPO_NAME,
    REPO_OWNER,
    SEQUENTIAL_API_DELAY,
    get_db,
    github_headers,
    load_prompt,
    logger,
)

PAGES_BASE_URL = f"https://{REPO_OWNER}.github.io/claude-code-digest"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def parse_inbox_body(body: str) -> tuple[str, str]:
    """Extract (resource_url, notes) from a structured inbox issue body.

    The inbox issue template produces bodies in the format:

        ### URL
        https://example.com

        ### Notes
        Some notes here

        ### Type
        URL / Article
    """
    resource_url = ""
    notes = body or ""

    url_match = re.search(r"### URL\s*\n+([^\n#]+)", notes)
    if url_match:
        candidate = url_match.group(1).strip()
        if candidate.startswith("http"):
            resource_url = candidate

    notes_match = re.search(r"### Notes\s*\n+(.*?)(?=\n### |\Z)", notes, re.DOTALL)
    if notes_match:
        notes = notes_match.group(1).strip()

    return resource_url, notes


def find_target_digest(issue_date: str, digest_dates: list) -> str:
    """Return the digest date closest on-or-after the issue creation date.

    Falls back to the latest digest if the issue is newer than all digests.
    """
    try:
        issue_dt = datetime.fromisoformat(issue_date.replace("Z", "+00:00")).date()
    except ValueError:
        return sorted(digest_dates)[-1]

    for d in sorted(digest_dates):
        if datetime.strptime(d, "%Y-%m-%d").date() >= issue_dt:
            return d

    return sorted(digest_dates)[-1]


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def fetch_inbox_issues() -> list:
    """Fetch all open issues labelled 'inbox' (handles pagination)."""
    headers = github_headers()
    issues = []
    page = 1
    while True:
        resp = requests.get(
            f"{GITHUB_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/issues",
            headers=headers,
            params={"labels": "inbox", "state": "open", "per_page": 100, "page": page},
            timeout=15,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        issues.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    logger.info("Fetched %d open inbox issues", len(issues))
    return issues


def close_issue(issue_number: int, digest_date: str) -> None:
    """Post a comment and close a processed inbox issue."""
    headers = github_headers()
    digest_url = f"{PAGES_BASE_URL}/digests/{digest_date}"
    try:
        requests.post(
            f"{GITHUB_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}/comments",
            headers=headers,
            json={"body": f"✅ Processed in digest: [{digest_date}]({digest_url})"},
            timeout=15,
        )
        requests.patch(
            f"{GITHUB_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}",
            headers=headers,
            json={"state": "closed"},
            timeout=15,
        )
        logger.info("Closed inbox issue #%d", issue_number)
    except Exception as e:
        logger.error("Failed to close issue #%d: %s", issue_number, e)


# ---------------------------------------------------------------------------
# Content & summarisation
# ---------------------------------------------------------------------------

def extract_content(items: list) -> list:
    """Fetch and extract content for items that have a resource URL."""
    for item in items:
        resource_url = item["metadata"].get("resource_url")
        if not resource_url or not resource_url.startswith("http"):
            continue
        try:
            downloaded = trafilatura.fetch_url(resource_url)
            if downloaded:
                text = trafilatura.extract(downloaded)
                if text:
                    item["content"] = text[:CONTENT_MAX_LENGTH]
                    if len(text) > CONTENT_MAX_LENGTH:
                        item["content"] += "\n\n[Content truncated]"
        except Exception as e:
            logger.warning("Content extraction failed for %s: %s", resource_url, e)
    return items


def summarize_item(client: anthropic.Anthropic, item: dict, system_prompt: str) -> dict:
    """Call Claude Haiku to summarise and score one item."""
    resource_url = item["metadata"].get("resource_url") or item["url"]
    content_parts = [
        f"Title: {item['title']}",
        f"Source: inbox (user-submitted)",
        f"Category: {item['category']}",
    ]
    if resource_url:
        content_parts.append(f"URL: {resource_url}")
    if item.get("content"):
        content_parts.append(f"\nContent:\n{item['content'][:8000]}")
    elif item["metadata"].get("user_notes"):
        content_parts.append(f"\nNotes: {item['metadata']['user_notes']}")

    try:
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": "\n".join(content_parts)}],
        )
        text = resp.content[0].text.strip()
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
        return json.loads(text)
    except Exception as e:
        logger.warning("Summarisation failed for '%s': %s", item["title"], e)
        return {
            "summary": f"- User-submitted inbox item",
            "relevance_score": 7,
            "relevance_reason": "User-submitted item — assigned default score",
            "category_suggestion": "Research Notes",
        }


# ---------------------------------------------------------------------------
# Digest injection
# ---------------------------------------------------------------------------

def inject_into_digest(digest_path: Path, items: list, digest_date: str) -> None:
    """Append (or extend) an '## 📥 Inbox Additions' section in a digest file."""
    if not items:
        return

    content = digest_path.read_text(encoding="utf-8")
    section_header = "## 📥 Inbox Additions"

    item_blocks = []
    for item in items:
        resource_url = item["metadata"].get("resource_url") or item["url"]
        title_link = f"[{item['title']}]({resource_url})" if resource_url else item["title"]

        submitted = ""
        if item.get("published"):
            try:
                dt = datetime.fromisoformat(item["published"].replace("Z", "+00:00"))
                submitted = dt.strftime("%d %b %Y")
            except ValueError:
                pass

        block = [f"### {title_link}"]
        if submitted:
            block.append(f"*Submitted: {submitted}*\n")
        if item.get("summary"):
            block.append(item["summary"])
        block.append("")
        item_blocks.append("\n".join(block))

    new_entries = "\n---\n\n".join(item_blocks)

    if section_header in content:
        # Section already exists — append new entries
        content = content.rstrip() + "\n\n---\n\n" + new_entries + "\n"
    else:
        content = content.rstrip() + f"\n\n{section_header}\n\n" + new_entries + "\n"

    digest_path.write_text(content, encoding="utf-8")
    logger.info("Injected %d item(s) into %s", len(items), digest_path.name)


# ---------------------------------------------------------------------------
# State DB
# ---------------------------------------------------------------------------

def update_state_db(db, items: list, digest_date: str) -> None:
    """Insert items into seen_items and link them to a digest date."""
    now = datetime.now(timezone.utc).isoformat()
    for item in items:
        db.execute(
            """INSERT OR IGNORE INTO seen_items
               (url_hash, url, title, source_type, first_seen, last_digest)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (item["id"], item["url"], item["title"], item["source_type"], now, digest_date),
        )
        db.execute(
            "UPDATE seen_items SET last_digest = ? WHERE url_hash = ?",
            (digest_date, item["id"]),
        )
    db.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    digest_files = sorted(DIGESTS_DIR.glob("*.md"))
    if not digest_files:
        logger.error("No existing digest files found in %s", DIGESTS_DIR)
        return 1
    digest_dates = [f.stem for f in digest_files]
    logger.info("Found %d existing digests: %s", len(digest_dates), digest_dates)

    try:
        issues = fetch_inbox_issues()
    except Exception as e:
        logger.error("Failed to fetch inbox issues: %s", e)
        return 1

    if not issues:
        logger.info("No open inbox issues — nothing to backfill.")
        return 0

    # Build normalised item dicts
    items = []
    for issue in issues:
        title = issue.get("title", "")
        body = issue.get("body", "") or ""
        resource_url, notes = parse_inbox_body(body)

        # If body had no URL, check whether the title itself is a URL
        if not resource_url and title.startswith("http"):
            resource_url = title

        canonical = resource_url if resource_url else issue.get("html_url", "")
        h = url_hash(canonical)

        items.append({
            "id": h,
            "source_type": "inbox",
            "category": "Research Notes",
            "title": title,
            "url": canonical,
            "content": None,
            "published": issue.get("created_at", ""),
            "metadata": {
                "issue_number": issue.get("number"),
                "user_notes": notes,
                "resource_url": resource_url,
            },
        })

    logger.info("Built %d items from issues", len(items))

    # Extract content from resource URLs
    items = extract_content(items)

    # Summarise with Claude Haiku
    client = anthropic.Anthropic()
    summarize_prompt = load_prompt("summarize")
    for item in items:
        result = summarize_item(client, item, summarize_prompt)
        item["summary"] = result.get("summary", "")
        item["relevance_score"] = result.get("relevance_score", 7)
        item["category_suggestion"] = result.get("category_suggestion", "Research Notes")
        time.sleep(SEQUENTIAL_API_DELAY)

    # Group by target digest
    groups: dict[str, list] = {}
    for item in items:
        target = find_target_digest(item["published"], digest_dates)
        groups.setdefault(target, []).append(item)
        logger.info(
            "Issue #%s ('%s') → digest %s",
            item["metadata"]["issue_number"],
            item["title"][:60],
            target,
        )

    # Inject, record, and close
    db = get_db()
    for digest_date, digest_items in sorted(groups.items()):
        digest_path = DIGESTS_DIR / f"{digest_date}.md"
        inject_into_digest(digest_path, digest_items, digest_date)
        update_state_db(db, digest_items, digest_date)
        for item in digest_items:
            issue_number = item["metadata"].get("issue_number")
            if issue_number:
                close_issue(issue_number, digest_date)

    logger.info("Backfill complete — processed %d issue(s).", len(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
