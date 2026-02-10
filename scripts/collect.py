"""Source collection: monitors all configured sources and outputs collected_items.json."""

import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests
import trafilatura

from config import (
    COLLECTED_ITEMS_PATH,
    CONTENT_EXTRACT_TIMEOUT,
    CONTENT_MAX_LENGTH,
    GITHUB_API_BASE,
    REPO_NAME,
    REPO_OWNER,
    TOPIC_MIN_STARS,
    get_db,
    github_headers,
    load_sources,
    logger,
)


def url_hash(url: str) -> str:
    """SHA-256 hash of a URL for deduplication."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def is_seen(db, u_hash: str) -> bool:
    """Check if a URL hash already exists in seen_items."""
    row = db.execute("SELECT 1 FROM seen_items WHERE url_hash = ?", (u_hash,)).fetchone()
    return row is not None


def record_seen(db, item: dict) -> None:
    """Record an item in seen_items for dedup."""
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT OR IGNORE INTO seen_items (url_hash, url, title, source_type, first_seen) VALUES (?, ?, ?, ?, ?)",
        (item["id"], item["url"], item["title"], item["source_type"], now),
    )


# ---------------------------------------------------------------------------
# Source collectors
# ---------------------------------------------------------------------------

def collect_github_releases(sources: list, db) -> list:
    """Collect new releases from configured GitHub repos."""
    items = []
    headers = github_headers()
    for src in sources:
        owner, repo, category = src["owner"], src["repo"], src["category"]
        try:
            resp = requests.get(
                f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases",
                headers=headers,
                params={"per_page": 10},
                timeout=15,
            )
            resp.raise_for_status()
            for release in resp.json():
                u = release.get("html_url", "")
                h = url_hash(u)
                if is_seen(db, h):
                    continue
                items.append({
                    "id": h,
                    "source_type": "github_release",
                    "category": category,
                    "title": f"{owner}/{repo} {release.get('tag_name', '')}",
                    "url": u,
                    "content": release.get("body", "") or "",
                    "published": release.get("published_at", ""),
                    "metadata": {"repo": f"{owner}/{repo}", "tag": release.get("tag_name", "")},
                })
        except Exception as e:
            logger.error("GitHub releases failed for %s/%s: %s", owner, repo, e)
    return items


def collect_npm_packages(sources: list, db) -> list:
    """Collect npm package version changes."""
    items = []
    for src in sources:
        name, category = src["name"], src["category"]
        try:
            resp = requests.get(
                f"https://registry.npmjs.org/{name}",
                timeout=15,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            latest = data.get("dist-tags", {}).get("latest", "")
            if not latest:
                continue
            u = f"https://www.npmjs.com/package/{name}/v/{latest}"
            h = url_hash(u)
            if is_seen(db, h):
                continue
            version_info = data.get("versions", {}).get(latest, {})
            description = version_info.get("description", data.get("description", ""))
            items.append({
                "id": h,
                "source_type": "npm_package",
                "category": category,
                "title": f"{name}@{latest}",
                "url": u,
                "content": description or "",
                "published": data.get("time", {}).get(latest, ""),
                "metadata": {"package": name, "version": latest},
            })
        except Exception as e:
            logger.error("npm registry failed for %s: %s", name, e)
    return items


def collect_rss_feeds(sources: list, db) -> list:
    """Collect new entries from RSS/Atom feeds."""
    items = []
    for src in sources:
        feed_url, category = src["url"], src["category"]
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:20]:
                u = entry.get("link", "")
                if not u:
                    continue
                h = url_hash(u)
                if is_seen(db, h):
                    continue
                published = ""
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = time.strftime("%Y-%m-%dT%H:%M:%SZ", entry.published_parsed)
                content = entry.get("summary", "") or entry.get("description", "")
                items.append({
                    "id": h,
                    "source_type": "rss",
                    "category": category,
                    "title": entry.get("title", u),
                    "url": u,
                    "content": content[:CONTENT_MAX_LENGTH] if content else "",
                    "published": published,
                    "metadata": {"feed_url": feed_url},
                })
        except Exception as e:
            logger.error("RSS feed failed for %s: %s", feed_url, e)
    return items


def collect_github_topics(sources: list, db) -> list:
    """Discover new repos by GitHub topic search."""
    items = []
    headers = github_headers()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    for src in sources:
        topic, category = src["topic"], src["category"]
        try:
            resp = requests.get(
                f"{GITHUB_API_BASE}/search/repositories",
                headers=headers,
                params={
                    "q": f"topic:{topic} created:>{week_ago}",
                    "sort": "stars",
                    "order": "desc",
                    "per_page": 10,
                },
                timeout=15,
            )
            resp.raise_for_status()
            for repo in resp.json().get("items", []):
                if repo.get("stargazers_count", 0) < TOPIC_MIN_STARS:
                    continue
                u = repo.get("html_url", "")
                h = url_hash(u)
                if is_seen(db, h):
                    continue
                items.append({
                    "id": h,
                    "source_type": "github_topic",
                    "category": category,
                    "title": f"{repo['full_name']} — {repo.get('description', '')[:100]}",
                    "url": u,
                    "content": repo.get("description", ""),
                    "published": repo.get("created_at", ""),
                    "metadata": {
                        "repo": repo["full_name"],
                        "stars": repo.get("stargazers_count", 0),
                        "topic": topic,
                    },
                })
        except Exception as e:
            logger.error("GitHub topic search failed for %s: %s", topic, e)
    return items


def collect_community_repos(sources: list, db) -> list:
    """Check for new commits on curated community repos."""
    items = []
    headers = github_headers()
    # Use last week as default lookback
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for src in sources:
        owner, repo, category = src["owner"], src["repo"], src["category"]
        try:
            resp = requests.get(
                f"{GITHUB_API_BASE}/repos/{owner}/{repo}/commits",
                headers=headers,
                params={"since": since, "per_page": 10},
                timeout=15,
            )
            resp.raise_for_status()
            for commit in resp.json():
                msg = commit.get("commit", {}).get("message", "")
                # Skip merge commits and formatting-only changes
                lower_msg = msg.lower()
                if any(skip in lower_msg for skip in ["merge pull", "merge branch", "formatting", "typo fix"]):
                    continue
                u = commit.get("html_url", "")
                h = url_hash(u)
                if is_seen(db, h):
                    continue
                items.append({
                    "id": h,
                    "source_type": "community_commit",
                    "category": category,
                    "title": f"{owner}/{repo}: {msg[:120]}",
                    "url": u,
                    "content": msg,
                    "published": commit.get("commit", {}).get("committer", {}).get("date", ""),
                    "metadata": {"repo": f"{owner}/{repo}"},
                })
        except Exception as e:
            logger.error("Community repo commits failed for %s/%s: %s", owner, repo, e)
    return items


def collect_reddit(sources: list, db) -> list:
    """Collect top weekly posts from configured subreddits."""
    items = []
    # Reddit blocks generic/short user-agents and datacenter IPs aggressively.
    # Use a browser-like UA and old.reddit.com which is more tolerant.
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; claude-code-digest/1.0; +https://github.com/dannyockilson/claude-code-digest)",
        "Accept": "application/json",
    }
    for src in sources:
        subreddit = src["subreddit"]
        category = src["category"]
        min_score = src.get("min_score", 0)
        try:
            resp = requests.get(
                f"https://old.reddit.com/r/{subreddit}/top.json",
                headers=headers,
                params={"t": "week", "limit": 25, "raw_json": 1},
                timeout=15,
            )
            resp.raise_for_status()
            for post_data in resp.json().get("data", {}).get("children", []):
                post = post_data.get("data", {})
                score = post.get("score", 0)
                if score < min_score:
                    continue
                permalink = post.get("permalink", "")
                u = f"https://reddit.com{permalink}" if permalink else post.get("url", "")
                h = url_hash(u)
                if is_seen(db, h):
                    continue
                items.append({
                    "id": h,
                    "source_type": "reddit",
                    "category": category,
                    "title": post.get("title", ""),
                    "url": u,
                    "content": post.get("selftext", "")[:CONTENT_MAX_LENGTH] or "",
                    "published": datetime.fromtimestamp(
                        post.get("created_utc", 0), tz=timezone.utc
                    ).isoformat() if post.get("created_utc") else "",
                    "metadata": {
                        "subreddit": subreddit,
                        "score": score,
                        "num_comments": post.get("num_comments", 0),
                        "author": post.get("author", ""),
                    },
                })
        except Exception as e:
            logger.error("Reddit failed for r/%s: %s", subreddit, e)
    return items


def collect_docs_changes(sources: list, db) -> list:
    """Detect content changes in monitored docs pages."""
    items = []
    now = datetime.now(timezone.utc).isoformat()
    for src in sources:
        page_url = src["url"]
        label = src["label"]
        category = src["category"]
        try:
            downloaded = trafilatura.fetch_url(page_url)
            if not downloaded:
                # Fallback to raw requests
                resp = requests.get(page_url, timeout=CONTENT_EXTRACT_TIMEOUT)
                resp.raise_for_status()
                text = resp.text
            else:
                text = trafilatura.extract(downloaded) or downloaded

            if not text or len(text) < 50:
                logger.warning("Docs page returned minimal content: %s (%d chars)", page_url, len(text) if text else 0)
                continue

            new_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

            # Check previous hash
            row = db.execute("SELECT content_hash FROM docs_hashes WHERE url = ?", (page_url,)).fetchone()
            old_hash = row[0] if row else None

            if old_hash is None:
                # First run — store hash but don't flag as changed
                db.execute(
                    "INSERT INTO docs_hashes (url, content_hash, last_checked) VALUES (?, ?, ?)",
                    (page_url, new_hash, now),
                )
                db.commit()
                logger.info("Docs first snapshot: %s (len=%d)", label, len(text))
                continue

            if new_hash == old_hash:
                # No change
                db.execute("UPDATE docs_hashes SET last_checked = ? WHERE url = ?", (now, page_url))
                db.commit()
                continue

            # Content changed
            h = url_hash(page_url + new_hash)
            items.append({
                "id": h,
                "source_type": "docs_change",
                "category": category,
                "title": f"{label} — content changed",
                "url": page_url,
                "content": None,
                "published": now,
                "metadata": {"label": label, "old_hash": old_hash, "new_hash": new_hash},
            })
            db.execute(
                "UPDATE docs_hashes SET content_hash = ?, last_checked = ?, last_changed = ? WHERE url = ?",
                (new_hash, now, now, page_url),
            )
            db.commit()
            logger.info("Docs change detected: %s", label)

        except Exception as e:
            logger.error("Docs monitoring failed for %s: %s", page_url, e)
    return items


def collect_star_snapshots(sources_cfg: dict, db) -> None:
    """Record current star counts for tracked repos."""
    headers = github_headers()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Build list of repos to track: all github_releases + star_velocity entries
    repos = []
    for src in sources_cfg.get("github_releases", []):
        repos.append(f"{src['owner']}/{src['repo']}")
    for src in sources_cfg.get("star_velocity", []):
        if isinstance(src, dict) and "owner" in src:
            repos.append(f"{src['owner']}/{src['repo']}")

    for repo_full in set(repos):
        try:
            resp = requests.get(
                f"{GITHUB_API_BASE}/repos/{repo_full}",
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            stars = resp.json().get("stargazers_count", 0)
            db.execute(
                "INSERT OR REPLACE INTO star_snapshots (repo, snapshot_date, star_count) VALUES (?, ?, ?)",
                (repo_full, today, stars),
            )
        except Exception as e:
            logger.error("Star snapshot failed for %s: %s", repo_full, e)

    db.commit()


def collect_inbox_issues(db) -> list:
    """Collect open GitHub issues labelled 'inbox'."""
    items = []
    headers = github_headers()
    try:
        resp = requests.get(
            f"{GITHUB_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/issues",
            headers=headers,
            params={"labels": "inbox", "state": "open", "per_page": 50},
            timeout=15,
        )
        resp.raise_for_status()
        for issue in resp.json():
            title = issue.get("title", "")
            body = issue.get("body", "") or ""
            # Use issue URL as canonical for dedup
            u = title if title.startswith("http") else issue.get("html_url", "")
            h = url_hash(u)
            if is_seen(db, h):
                continue
            items.append({
                "id": h,
                "source_type": "inbox",
                "category": "Research",
                "title": title,
                "url": u,
                "content": None,
                "published": issue.get("created_at", ""),
                "metadata": {
                    "issue_number": issue.get("number"),
                    "user_notes": body,
                },
            })
    except Exception as e:
        logger.error("Inbox issues collection failed: %s", e)
    return items


def extract_content_for_items(items: list) -> list:
    """For items without content, try to extract from URL using trafilatura."""
    for item in items:
        if item["content"] or not item["url"].startswith("http"):
            continue
        try:
            downloaded = trafilatura.fetch_url(item["url"])
            if downloaded:
                text = trafilatura.extract(downloaded)
                if text:
                    item["content"] = text[:CONTENT_MAX_LENGTH]
                    if len(text) > CONTENT_MAX_LENGTH:
                        item["content"] += "\n\n[Content truncated]"
        except Exception as e:
            logger.warning("Content extraction failed for %s: %s", item["url"], e)
    return items


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    sources_cfg = load_sources()
    db = get_db()
    all_items = []
    source_errors = 0
    total_sources = 7  # number of collector types

    collectors = [
        ("GitHub releases", lambda: collect_github_releases(sources_cfg.get("github_releases", []), db)),
        ("npm packages", lambda: collect_npm_packages(sources_cfg.get("npm_packages", []), db)),
        ("RSS feeds", lambda: collect_rss_feeds(sources_cfg.get("rss_feeds", []), db)),
        ("GitHub topics", lambda: collect_github_topics(sources_cfg.get("github_topics", []), db)),
        ("Community repos", lambda: collect_community_repos(sources_cfg.get("community_repos", []), db)),
        ("Reddit", lambda: collect_reddit(sources_cfg.get("reddit", []), db)),
        ("Docs changes", lambda: collect_docs_changes(sources_cfg.get("docs_monitoring", []), db)),
        ("Inbox issues", lambda: collect_inbox_issues(db)),
    ]
    total_sources = len(collectors)

    for name, collector in collectors:
        try:
            result = collector()
            logger.info("Collected %d items from %s", len(result), name)
            all_items.extend(result)
        except Exception as e:
            logger.error("Collector '%s' failed entirely: %s", name, e)
            source_errors += 1

    # Star snapshots (doesn't produce items, just records data)
    try:
        collect_star_snapshots(sources_cfg, db)
        logger.info("Star snapshots recorded")
    except Exception as e:
        logger.error("Star snapshots failed: %s", e)

    if source_errors >= total_sources:
        logger.error("ALL source collectors failed. Aborting.")
        return 1

    # Deduplicate items by ID (overlapping topic searches can surface the same repo)
    seen_ids = set()
    deduped = []
    for item in all_items:
        if item["id"] not in seen_ids:
            seen_ids.add(item["id"])
            deduped.append(item)
        else:
            logger.debug("Deduped collected item: %s", item["title"])
    if len(deduped) < len(all_items):
        logger.info("Deduped %d duplicate items from collection", len(all_items) - len(deduped))
    all_items = deduped

    # Extract content for items that only have URLs
    all_items = extract_content_for_items(all_items)

    # Record all items as seen
    for item in all_items:
        record_seen(db, item)
    db.commit()

    # Write output
    COLLECTED_ITEMS_PATH.write_text(
        json.dumps(all_items, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Wrote %d collected items to %s", len(all_items), COLLECTED_ITEMS_PATH)

    if source_errors > 0:
        logger.warning("%d/%d source collectors had errors", source_errors, total_sources)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
