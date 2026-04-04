---
layout: default
title: "Special Edition: The Claude Code Source Leak — A Deep Dive"
date: 2026-04-04
special_edition: true
---

# Claude Code Weekly Intelligence Digest
## Special Edition: The Claude Code Source Leak

*Published 4 Apr 2026 — standalone deep dive. Normal weekly digest resumes Sunday 6 Apr.*

---

On March 31, 2026, Anthropic accidentally shipped the full TypeScript source of Claude Code inside a public npm package. What followed was four days of forensic analysis, record-breaking GitHub activity, a clumsy DMCA campaign, a malware wave, and more genuine insight into how a modern AI coding agent is built than anyone expected. This special edition covers everything worth knowing.

---

## Contents
{: .no_toc }

* TOC
{:toc}

---

## How It Happened

The leak was a packaging mistake, not an intrusion. Claude Code v2.1.88 was published to npm with a **59.7 MB JavaScript source map** (`.map` file) inadvertently bundled alongside the compiled binary. Source maps exist to translate minified production code back to original source lines—useful for debugging, catastrophic when shipped publicly.

The root cause: Claude Code uses [Bun](https://bun.sh/) as its runtime, which generates source maps by default. The team's `.npmignore` didn't exclude the `.map` file. The map referenced unobfuscated TypeScript sources sitting in Anthropic's public Cloudflare R2 storage bucket—meaning anyone who noticed the reference could download a complete snapshot of the codebase.

Security researcher Chaofan Shou spotted it first and posted on X. That post reached **28.8 million views**. Within hours the package was pulled from npm, but not before mirror repositories spread across GitHub.

This was actually Anthropic's **third disclosure event in six weeks**: an early source map exposure in February 2025 was patched within hours; a CMS misconfiguration on March 26 leaked internal marketing drafts including details of the unreleased Capybara/Mythos model; and then the npm incident on March 31.

**Anthropic's official statement:** *"This was a release packaging issue caused by human error, not a security breach. No sensitive customer data or credentials were involved or exposed. We're rolling out measures to prevent this from happening again."*

---

## What Leaked

The exposed archive contained approximately **1,900 TypeScript files and 512,000 lines of code** across a fully structured project:

- `utils/` — ~180k lines (~35% of total); core orchestration and helpers
- `components/` — React-based terminal renderer
- `services/` — LLM API client, streaming, caching, session management
- `tools/` — 40 concrete tool modules across 184 files (~50.8k lines)
- `commands/` — slash command implementations
- `coordinator/`, `voice/`, `memdir/`, `buddy/` — feature-flagged unreleased systems

What it did **not** contain: model weights, training data, training pipelines, API keys, customer data, or anything touching the underlying AI infrastructure. The leak is a sophisticated CLI wrapper, not the intelligence behind it.

---

## The Architecture, Exposed

### Prompt Caching & the SYSTEM_PROMPT_DYNAMIC_BOUNDARY

The most practically instructive finding for developers building on the API: Claude Code splits every system prompt into a **stable section** and a **dynamic section** using a `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` marker. The stable section is cached and reused across sessions (prompt cache hit). The dynamic section is session-specific context injected fresh each time.

Engineers annotate cache-breaking content with `DANGEROUS_uncachedSystemPromptSection` so anyone touching those sections knows they're paying full token cost. This is a production pattern worth stealing directly.

### Memory: A Three-Layer Index

Rather than dumping all context into every request, memory operates as a tiered index:

1. **Index layer** (always loaded): ~150-character pointers to topic files
2. **Topic files** (loaded on demand): actual knowledge content
3. **Transcripts** (grep-only): never loaded into context, only searched

A nightly `autoDream` process runs in a forked sub-agent with limited tool access, consolidating memories, removing contradictions, and deduplicating entries—without touching the main agent's context window. The 3-layer approach is a clean solution to the "agent memory at scale" problem that most third-party frameworks handle poorly.

### Compaction Mechanics

Claude Code stores the **full conversation history** in JSONL files locally, but filters what actually gets sent to the API. Messages tagged `isVisibleInTranscriptOnly` are retained for user-facing display (e.g. requesting a summary of old content) but never forwarded to the model. `Microcompaction`—disabled by default—clears old tool output content after 1 hour when Anthropic's prompt cache has expired, replacing it with `[Old tool result content cleared]` while preserving the tool invocation itself.

### Anti-Distillation

Two mechanisms exist specifically to make Claude Code's API traffic useless for training a competing model:

- **Fake tools**: An `anti_distillation: ['fake_tools']` flag instructs the server to silently inject decoy tool definitions into the system prompt. Traffic recordings include fake, never-callable tools—poisoning any dataset built from captured requests.
- **CONNECTOR_TEXT**: A server-side mechanism that buffers assistant text between tool calls, summarizes it, and returns the summary with a **cryptographic signature** rather than the raw output. API-level recorders see only summaries, never full assistant turns.

---

## The Hidden Features

### KAIROS: Always-On Background Agent

Behind feature flags `PROACTIVE` and `KAIROS` sits an unreleased autonomous mode that represents a significant architectural departure. KAIROS:

- Runs 24/7 without waiting for user prompts
- Receives periodic **heartbeat queries**: *"anything worth doing right now?"*
- Has exclusive access to tools unavailable in normal mode: push notifications, file delivery, GitHub PR subscriptions
- Maintains an **append-only observation log**—it cannot rewrite or delete history
- Consolidates memory nightly via autoDream

Internal notes in the source suggest an April–May 2026 rollout window, though nothing has been confirmed.

### ULTRAPLAN

Offloads complex multi-step planning to Claude Opus for up to 30 minutes, then surfaces a browser-based approval interface before executing. Essentially a human-in-the-loop planning mode for high-stakes or long-horizon tasks.

### Coordinator Mode

Multi-agent architecture using **mailbox-based task routing** between parallel worker agents and a centralized reconciliation step. Differs from the current sub-agent dispatch pattern—workers run concurrently and post results to a shared mailbox rather than returning inline.

### BUDDY: The Gacha Companion

The most unexpected find. `buddy/companion.ts` implements a collectible virtual companion system:

- **18 species**: duck, dragon, axolotl, capybara, mushroom, ghost, and others
- **5 rarity tiers**: 60% common / 25% uncommon / 10% rare / 4% epic / 1% legendary (with a 1% shiny variant on any tier)
- **5 stats per buddy**: DEBUGGING, PATIENCE, CHAOS, WISDOM, SNARK
- Species assignment is **deterministic from a hash of the user's ID**—the same user always gets the same buddy
- Claude generates the personality and name on first activation
- Buddies appear in a speech-bubble UI alongside the input box

Internal notes flag an April 1–7 teaser window and a May 2026 full launch. Whether the timing of the leak and April 1 proximity was coincidental is left as an exercise to the reader.

---

## What the Code Quality Reveals

Three findings from the reverse-engineering community warrant attention from engineers building their own agents:

**Zero test coverage.** Across 64,464+ lines of production code, researchers found no test files. Given the product's maturity and user base, this is striking. It likely reflects Claude Code's origins as an internal prototype and the pace of iteration, but it's a notable data point.

**print.ts.** A single file, 5,594 lines, containing a function that is **3,167 lines long** with 486 branch points and 12 levels of nesting. The terminal renderer is monolithic by design—the React-in-terminal abstraction collapsed into a single render function over time. Worth knowing before using this codebase as an architectural reference.

**Silent model downgrade.** Three consecutive 529 (overloaded) errors automatically trigger a switch from Opus to Sonnet—without any user notification. If your production workflows depend on Opus-level reasoning, this is a configuration to audit.

**Dual HTTP clients.** Both Axios and `fetch` are present, suggesting the codebase accumulated dependencies from multiple authors without a consolidation pass. 74 npm dependencies total.

---

## The Ecosystem Response

### claw-code: GitHub's Fastest-Growing Repository

Within 24 hours of the leak, `instructkr/claw-code` (since transferred to `ultraworkers/claw-code`) emerged as a **clean-room Rust rewrite** of Claude Code's architecture. It reached **100,000 stars in under one day**—the fastest any repository has hit that milestone in GitHub's history. The project has its own Discord and website at claw-code.codes.

Separately, a Python rewrite targeting API parity also appeared and is trending toward 100K stars on its own trajectory.

### The Mirror and Forks

The primary mirror `leaked-claude-code/leaked-claude-code` accumulated **41,500+ forks** before GitHub actioned takedown requests. Multiple analysis repositories also appeared:

- **[Yuyz0112/claude-code-reverse](https://github.com/Yuyz0112/claude-code-reverse)** — visualization tool for Claude Code's LLM interactions, predating the leak
- **ccleaks.com** — archived and documented the exposed code
- Statistical deep-dives from Dr. Randal S. Olson and Engineers Codex

### Hacker News

Seven distinct HN threads in four days, covering: the initial discovery ([47584540](https://news.ycombinator.com/item?id=47584540)), the Frustration Regex / Undercover Mode / fake tools analysis ([47586778](https://news.ycombinator.com/item?id=47586778)), the visual architecture guide ([47597085](https://news.ycombinator.com/item?id=47597085)), the general reading thread ([47594555](https://news.ycombinator.com/item?id=47594555)), and the meta-coverage thread ([47609294](https://news.ycombinator.com/item?id=47609294)).

### Anthropic's DMCA Campaign

On April 1, Anthropic issued mass copyright takedown requests targeting **thousands of GitHub repositories**. By April 3, the company had retracted most of them, characterizing the sweep as "an accident." TechRadar's headline: *"The irony is rich."* The code continues to circulate.

---

## The Security Tail

The leak created an attack surface that was exploited almost immediately:

**Supply chain attack (critical — rotate credentials now):** Users who installed or updated Claude Code via npm in the window **March 31, 00:21–03:29 UTC** may have received a trojanized HTTP client containing a cross-platform remote access trojan. Downgrade immediately if you were in that window and rotate all credentials that were live in that environment.

**Malware campaigns:** BleepingComputer documented fake GitHub repositories deploying **Vidar Stealer** and **GhostSocks** malware through trojanized "Claude Code leak" packages. Five typosquatting npm packages mimicking internal dependency names were also flagged.

**Targeted fuzzing risk:** Zscaler ThreatLabz noted that the four-stage context management pipeline—now fully documented—gives attackers a precise map for crafting prompt injection payloads targeting the compaction and memory systems specifically.

---

## What It Actually Tells Us

The leak is less dramatic than the coverage suggests. The code is a TypeScript CLI that wraps the Claude API—it implements clever engineering around prompt caching, memory tiering, and context management, but it doesn't expose how the underlying model works, how it was trained, or what makes it better than alternatives. The intelligence is still behind Anthropic's API wall.

What the leak *does* confirm:

1. **Production AI agents at scale look messier than the blog posts suggest.** Zero test coverage, a 3,167-line function, dual HTTP clients—real systems accumulate technical debt regardless of who builds them.
2. **Prompt engineering at this level is infrastructure work.** `SYSTEM_PROMPT_DYNAMIC_BOUNDARY`, `DANGEROUS_uncachedSystemPromptSection`, the three-layer memory index—these are careful, deliberate cache optimization patterns that most developers haven't needed to build yet but will.
3. **Anti-distillation is a real concern for commercial AI products.** The fake tools approach is novel and worth watching for arms-race escalation.
4. **The roadmap is now public.** KAIROS, ULTRAPLAN, Coordinator Mode, voice, browser control, BUDDY—whether Anthropic ships them on schedule or not, competitors and the community are now building toward the same destinations.

The Undercover Mode debate (should AI commit messages suppress AI attribution?) surfaced genuine disagreement with no clean resolution. EU commenters raised Article 50 concerns. The discussion is worth reading; the [HN thread](https://news.ycombinator.com/item?id=47586778) is the best starting point.

---

## Further Reading

- **[The Claude Code Source Leak: fake tools, frustration regexes, undercover mode](https://alex000kim.com/posts/2026-03-31-claude-code-source-leak/)** — Alex Kim, the sharpest single-article technical summary
- **[Diving into Claude Code's Source Code Leak](https://read.engineerscodex.com/p/diving-into-claude-codes-source-code)** — Engineers Codex deep dive
- **[The Claude Code leak in four charts](https://www.randalolson.com/2026/04/02/claude-code-leak-four-charts/)** — Dr. Randal S. Olson, statistical breakdown
- **[We Reverse-Engineered 12 Versions of Claude Code. Then It Leaked.](https://dev.to/kolkov/we-reverse-engineered-12-versions-of-claude-code-then-it-leaked-its-own-source-code-pij)** — pre-leak analysis that predicted several findings
- **[Anthropic accidentally exposes Claude Code source code](https://www.theregister.com/2026/03/31/anthropic_claude_code_source_code/)** — The Register's news coverage
- **[Claude Code leak used to push infostealer malware](https://www.bleepingcomputer.com/news/security/claude-code-leak-used-to-push-infostealer-malware-on-github/)** — BleepingComputer on the malware campaigns
- **[claw-code: fastest-growing GitHub repo](https://cybernews.com/tech/claude-code-leak-spawns-fastest-github-repo/)** — Cybernews on the community rewrite
- **[Anthropic took down thousands of GitHub repos](https://techcrunch.com/2026/04/01/anthropic-took-down-thousands-of-github-repos-trying-to-yank-its-leaked-source-code-a-move-the-company-says-was-an-accident/)** — TechCrunch on the DMCA campaign
