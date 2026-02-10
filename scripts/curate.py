"""Curation: summarize, score, filter, compute star velocity, and produce editorial digest."""

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import anthropic

from config import (
    BATCH_POLL_INTERVAL,
    BATCH_POLL_TIMEOUT,
    COLLECTED_ITEMS_PATH,
    CURATED_DIGEST_PATH,
    HAIKU_MODEL,
    RELEVANCE_THRESHOLD,
    SEQUENTIAL_API_DELAY,
    SONNET_MODEL,
    get_db,
    load_prompt,
    load_sources,
    logger,
)


def parse_json_response(text: str) -> dict:
    """Parse JSON from Claude response, stripping markdown code fences if present."""
    cleaned = text.strip()
    # Strip ```json ... ``` or ``` ... ``` wrappers
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(1).strip()
    return json.loads(cleaned)


def build_summarize_message(item: dict, system_prompt: str) -> dict:
    """Build a Claude message request for summarizing a single item."""
    content_parts = [f"Title: {item['title']}", f"Source: {item['source_type']}", f"Category: {item['category']}"]
    if item.get("url"):
        content_parts.append(f"URL: {item['url']}")
    if item.get("content"):
        content_parts.append(f"\nContent:\n{item['content'][:8000]}")
    elif item.get("metadata", {}).get("user_notes"):
        content_parts.append(f"\nNotes: {item['metadata']['user_notes']}")

    if item["source_type"] == "reddit":
        meta = item.get("metadata", {})
        content_parts.append(f"\nReddit score: {meta.get('score', 'N/A')}, Comments: {meta.get('num_comments', 'N/A')}")

    return {
        "model": HAIKU_MODEL,
        "max_tokens": 1024,
        "system": system_prompt,
        "messages": [{"role": "user", "content": "\n".join(content_parts)}],
    }


def summarize_batch(client: anthropic.Anthropic, items: list, system_prompt: str) -> dict:
    """Summarize items using the Batch API. Returns dict mapping item id -> parsed result."""
    if not items:
        return {}

    # Build batch requests, deduplicating by item ID
    requests_list = []
    seen_ids = set()
    for item in items:
        if item["id"] in seen_ids:
            logger.warning("Skipping duplicate item in batch: %s", item["title"])
            continue
        seen_ids.add(item["id"])
        msg_params = build_summarize_message(item, system_prompt)
        requests_list.append({
            "custom_id": item["id"],
            "params": msg_params,
        })

    results = {}

    try:
        logger.info("Submitting batch of %d items to Claude Batch API", len(requests_list))
        batch = client.messages.batches.create(requests=requests_list)
        batch_id = batch.id
        logger.info("Batch created: %s", batch_id)

        # Poll for completion
        elapsed = 0
        while elapsed < BATCH_POLL_TIMEOUT:
            time.sleep(BATCH_POLL_INTERVAL)
            elapsed += BATCH_POLL_INTERVAL
            batch = client.messages.batches.retrieve(batch_id)
            logger.info("Batch status: %s (elapsed %ds)", batch.processing_status, elapsed)
            if batch.processing_status == "ended":
                break
        else:
            logger.warning("Batch timed out after %ds, falling back to sequential", BATCH_POLL_TIMEOUT)
            return summarize_sequential(client, items, system_prompt)

        # Retrieve results
        for result in client.messages.batches.results(batch_id):
            custom_id = result.custom_id
            if result.result.type == "succeeded":
                try:
                    text = result.result.message.content[0].text
                    parsed = parse_json_response(text)
                    results[custom_id] = parsed
                except (json.JSONDecodeError, IndexError, KeyError) as e:
                    logger.warning("Failed to parse batch result for %s: %s", custom_id, e)
            else:
                logger.warning("Batch item %s failed: %s", custom_id, result.result.type)

        logger.info("Batch completed: %d/%d results parsed", len(results), len(items))
        return results

    except Exception as e:
        logger.error("Batch API failed: %s. Falling back to sequential.", e)
        return summarize_sequential(client, items, system_prompt)


def summarize_sequential(client: anthropic.Anthropic, items: list, system_prompt: str) -> dict:
    """Fallback: summarize items one by one via Messages API."""
    results = {}
    for item in items:
        try:
            msg_params = build_summarize_message(item, system_prompt)
            response = client.messages.create(**msg_params)
            text = response.content[0].text
            parsed = parse_json_response(text)
            results[item["id"]] = parsed
        except Exception as e:
            logger.warning("Sequential summarize failed for %s: %s", item["id"], e)
        time.sleep(SEQUENTIAL_API_DELAY)
    logger.info("Sequential summarization: %d/%d succeeded", len(results), len(items))
    return results


def compute_star_velocity(db, sources_cfg: dict) -> list:
    """Compute star velocity and return synthetic trending items."""
    threshold = 500
    sv_config = sources_cfg.get("star_velocity", [])
    for entry in sv_config:
        if isinstance(entry, dict) and "trending_threshold" in entry:
            threshold = entry["trending_threshold"]
            break
    if isinstance(sv_config, list):
        for entry in sv_config:
            if not isinstance(entry, dict):
                continue
            if "trending_threshold" in entry:
                threshold = entry["trending_threshold"]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    rows = db.execute("""
        SELECT s1.repo, s1.star_count as current_stars,
               COALESCE(s2.star_count, 0) as prev_stars
        FROM star_snapshots s1
        LEFT JOIN star_snapshots s2 ON s1.repo = s2.repo AND s2.snapshot_date = ?
        WHERE s1.snapshot_date = ?
    """, (week_ago, today)).fetchall()

    trending = []
    for repo, current, prev in rows:
        if prev == 0:
            continue  # No previous snapshot to compare
        delta = current - prev
        if delta >= threshold:
            pct = round((delta / prev) * 100, 1) if prev > 0 else 0
            trending.append({
                "id": f"trending-{repo}-{today}",
                "source_type": "star_velocity",
                "category": "Trending",
                "title": f"{repo} — +{delta:,} stars this week ({pct}% increase)",
                "url": f"https://github.com/{repo}",
                "content": f"Currently at {current:,} stars. Gained {delta:,} stars in the past week.",
                "published": today,
                "metadata": {
                    "repo": repo,
                    "current_stars": current,
                    "previous_stars": prev,
                    "delta": delta,
                    "pct_increase": pct,
                },
                # Pre-fill curation data so these skip LLM scoring
                "summary": f"- {repo} gained {delta:,} stars this week ({pct}% increase)\n- Currently at {current:,} total stars",
                "relevance_score": 8,
                "relevance_reason": "Significant star velocity indicates growing community interest",
                "category_suggestion": "Trending",
            })
            logger.info("Trending: %s +%d stars", repo, delta)

    return trending


def editorial_synthesis(client: anthropic.Anthropic, items: list) -> str:
    """Generate the editorial digest markdown using Sonnet."""
    editorial_prompt = load_prompt("editorial")

    # Build item summaries for the editorial
    item_texts = []
    for item in items:
        parts = [f"### {item['title']}"]
        parts.append(f"- URL: {item['url']}")
        parts.append(f"- Category: {item.get('category_suggestion', item.get('category', 'Uncategorized'))}")
        parts.append(f"- Relevance: {item.get('relevance_score', 'N/A')}/10")
        if item.get("summary"):
            parts.append(f"- Summary:\n{item['summary']}")
        if item.get("source_type") == "reddit":
            meta = item.get("metadata", {})
            parts.append(f"- Reddit: {meta.get('score', 0)} upvotes, {meta.get('num_comments', 0)} comments")
        item_texts.append("\n".join(parts))

    user_content = f"Here are {len(items)} curated items for this week's digest:\n\n" + "\n\n---\n\n".join(item_texts)

    try:
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=4096,
            system=editorial_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        return response.content[0].text
    except Exception as e:
        logger.error("Editorial synthesis failed: %s", e)
        # Fallback: produce a basic digest from summaries
        lines = [f"# Claude Code Weekly Digest — {datetime.now(timezone.utc).strftime('%d %b %Y')}\n"]
        lines.append("*Editorial synthesis unavailable — showing raw summaries.*\n")
        for item in items:
            lines.append(f"## [{item['title']}]({item['url']})")
            if item.get("summary"):
                lines.append(item["summary"])
            lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    # Load collected items
    if not COLLECTED_ITEMS_PATH.exists():
        logger.error("No collected_items.json found. Run collect.py first.")
        return 1

    items = json.loads(COLLECTED_ITEMS_PATH.read_text(encoding="utf-8"))
    logger.info("Loaded %d collected items", len(items))

    if not items:
        logger.warning("No items collected. Generating empty digest.")
        output = {
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "item_count": 0,
            "items_filtered": 0,
            "items_included": 0,
            "digest_markdown": f"# Claude Code Weekly Digest — {datetime.now(timezone.utc).strftime('%d %b %Y')}\n\nQuiet week — no new items to report.",
            "items": [],
        }
        CURATED_DIGEST_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
        return 0

    client = anthropic.Anthropic()
    summarize_prompt = load_prompt("summarize")

    # Step 1: Summarize & score with Haiku via Batch API
    logger.info("Step 1: Summarizing %d items with %s", len(items), HAIKU_MODEL)
    results = summarize_batch(client, items, summarize_prompt)

    # Merge results back into items
    for item in items:
        if item["id"] in results:
            r = results[item["id"]]
            item["summary"] = r.get("summary", "")
            item["relevance_score"] = r.get("relevance_score", 0)
            item["relevance_reason"] = r.get("relevance_reason", "")
            item["category_suggestion"] = r.get("category_suggestion", item["category"])
        else:
            # Items that failed summarization get a default low score
            item["relevance_score"] = 0
            item["summary"] = ""

    # Step 2: Filter by relevance
    included = [i for i in items if i.get("relevance_score", 0) >= RELEVANCE_THRESHOLD]
    filtered = [i for i in items if i.get("relevance_score", 0) < RELEVANCE_THRESHOLD]
    logger.info("Step 2: %d items pass threshold (>=%d), %d filtered out", len(included), RELEVANCE_THRESHOLD, len(filtered))

    for item in filtered:
        logger.debug("Filtered: %s (score=%s, reason=%s)", item["title"], item.get("relevance_score"), item.get("relevance_reason"))

    # Step 2.5: Star velocity
    db = get_db()
    sources_cfg = load_sources()
    trending = compute_star_velocity(db, sources_cfg)
    if trending:
        included.extend(trending)
        logger.info("Step 2.5: Added %d trending items", len(trending))

    # Step 3: Editorial synthesis with Sonnet
    logger.info("Step 3: Editorial synthesis with %s (%d items)", SONNET_MODEL, len(included))
    digest_markdown = editorial_synthesis(client, included)

    # Build output
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output = {
        "date": today,
        "item_count": len(items),
        "items_filtered": len(filtered),
        "items_included": len(included),
        "digest_markdown": digest_markdown,
        "items": included,
    }

    CURATED_DIGEST_PATH.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Wrote curated digest: %d items included, %d filtered", len(included), len(filtered))
    return 0


if __name__ == "__main__":
    sys.exit(main())
