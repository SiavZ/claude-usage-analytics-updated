# Changes from Original

This is a modified fork of [claude-usage-analytics](https://github.com/AnalyticEndeavorsUser/claude-usage-analytics) (v1.1.8) by [Reid Havens / Analytic Endeavors](https://analyticendeavors.com/).

## Modifications

### Model Support (Opus 4.6 / Sonnet 4.6 / Haiku 4.5)

- **Updated `formatModelName()`** in `dataProvider.js` to correctly display "Opus 4.6" and "Sonnet 4.6" for new model IDs (falls back to legacy names for older models)
- **Added model entries** in both `modelPricing.json` files for `claude-opus-4-6` ($5/$25), `claude-sonnet-4-6` ($3/$15), and `claude-haiku-4-5-20251001` ($0.80/$4)

### Pricing Fixes

- **Fixed `MODEL_PRICING` in `dataProvider.js` and `database.js`** — added Opus 4.6 ($5/$25), Sonnet 4.6, and Haiku pricing tiers with correct cache read/write rates
- **Fixed `cacheSavings` calculation** — now computes per-model savings using actual pricing instead of hardcoded Sonnet rates

### Data Population Fixes

- **Fixed `allTime.cacheTokens`** — was hardcoded to 0 with a "Can't determine" comment; now sums `cacheReadInputTokens` + `cacheCreationInputTokens` from modelUsage
- **Added `live-today-stats.json` auto-load on startup** — the scan-today.js script writes this file, but the extension never read it back; now loads persisted live stats on init if they're from today
- **Added `backfill-results.json` auto-import** — on startup, if a backfill results file exists, automatically imports into SQLite and renames to `.imported`

### New Tool: Conversation Stats Generator

- **Added `tools/generate-conversation-stats.js`** — a Node.js script that scans all JSONL files and generates `~/.claude/conversation-stats-cache.json` with:
  - Personality analysis (politeness, frustration, curiosity scores)
  - Request type classification (debugging, features, explain, refactor, review, testing)
  - Sentiment analysis (positive, negative, urgent, confused)
  - Code statistics (code blocks, lines of code, top languages)
  - Expression tracking (questions, exclamations, thanks, celebrations, etc.)
  - Hour-of-day activity counts for peak hour / night owl / early bird scores

  The original extension only populated these fields via the Python backfill script (`backfill_claude_export.py`) which requires a Claude.ai data export. This new script extracts the same data directly from Claude Code's local JSONL files.
