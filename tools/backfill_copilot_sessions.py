#!/usr/bin/env python3
"""
Backfill Copilot session data into claude-usage-analytics database.

Reads ~/.copilot/session-state/*/events.jsonl and imports per-day token/cost
metrics into ~/.claude/analytics.db so the VS Code extension shows combined
usage across Claude Code CLI, Copilot CLI, Copilot extension, and Forge CLI.
"""

import argparse
import datetime
import glob
import json
import os
import shutil
import sqlite3
import sys

# ---------------------------------------------------------------------------
# Pricing (API-equivalent, per 1M tokens)
# Matches the rates already used by the extension (verified against existing data)
# ---------------------------------------------------------------------------
MODEL_PRICING = {
    # Opus variants
    "claude-opus-4.6": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4.5": {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write": 18.75},
    "claude-opus-4": {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write": 18.75},
    "claude-opus": {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write": 18.75},
    # Sonnet variants
    "claude-sonnet-4.6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4.5": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    # Haiku variants
    "claude-haiku-4.5": {"input": 0.80, "output": 4.00, "cache_read": 0.08, "cache_write": 1.00},
    "claude-haiku-4": {"input": 0.80, "output": 4.00, "cache_read": 0.08, "cache_write": 1.00},
    "claude-haiku": {"input": 0.80, "output": 4.00, "cache_read": 0.08, "cache_write": 1.00},
    # Fallback (Sonnet pricing)
    "_default": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
}

MACHINE_ID = "copilot"


def get_pricing(model_name: str) -> dict:
    """Return pricing for a model, falling back to prefix matching then default."""
    name = model_name.lower()
    if name in MODEL_PRICING:
        return MODEL_PRICING[name]
    for key in MODEL_PRICING:
        if key != "_default" and name.startswith(key):
            return MODEL_PRICING[key]
    return MODEL_PRICING["_default"]


def calc_cost(model: str, usage: dict) -> float:
    p = get_pricing(model)
    return (
        usage.get("inputTokens", 0) * p["input"] / 1_000_000
        + usage.get("outputTokens", 0) * p["output"] / 1_000_000
        + usage.get("cacheReadTokens", 0) * p["cache_read"] / 1_000_000
        + usage.get("cacheWriteTokens", 0) * p["cache_write"] / 1_000_000
    )


def load_sessions(session_dir: str) -> list[dict]:
    """Parse all session.shutdown events and return a list of session dicts."""
    sessions = []
    pattern = os.path.join(session_dir, "*/events.jsonl")
    files = sorted(glob.glob(pattern))

    for f in files:
        try:
            with open(f) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if event.get("type") != "session.shutdown":
                        continue

                    data = event.get("data", {})
                    ts = data.get("sessionStartTime", 0)
                    if not ts:
                        continue

                    metrics = data.get("modelMetrics", {})
                    if not metrics:
                        continue

                    date_str = datetime.datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d")

                    # Use the parent directory name as a stable session ID
                    session_id = os.path.basename(os.path.dirname(f))
                    sessions.append(
                        {
                            "session_id": session_id,
                            "date": date_str,
                            "premium_requests": data.get("totalPremiumRequests", 0),
                            "model_metrics": metrics,
                        }
                    )
                    break  # one shutdown per file
        except OSError:
            pass

    return sessions


def aggregate_by_date(sessions: list[dict]) -> dict:
    """Group sessions by date, summing tokens/cost/requests per model."""
    by_date: dict[str, dict] = {}

    for s in sessions:
        date = s["date"]
        if date not in by_date:
            by_date[date] = {
                "sessions": 0,
                "requests": 0,
                "cost": 0.0,
                "tokens": 0,
                "models": {},
            }

        day = by_date[date]
        day["sessions"] += 1
        day["requests"] += s["premium_requests"]

        for model, mdata in s["model_metrics"].items():
            usage = mdata.get("usage", {})
            req_count = mdata.get("requests", {}).get("count", 0)
            model_cost = calc_cost(model, usage)

            day["cost"] += model_cost
            day["tokens"] += (
                usage.get("inputTokens", 0)
                + usage.get("outputTokens", 0)
                + usage.get("cacheReadTokens", 0)
                + usage.get("cacheWriteTokens", 0)
            )

            if model not in day["models"]:
                day["models"][model] = {
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                }
            m = day["models"][model]
            m["requests"] += req_count
            m["input_tokens"] += usage.get("inputTokens", 0)
            m["output_tokens"] += usage.get("outputTokens", 0)
            m["cache_read_tokens"] += usage.get("cacheReadTokens", 0)
            m["cache_write_tokens"] += usage.get("cacheWriteTokens", 0)

    return by_date


def get_imported_session_ids(conn: sqlite3.Connection) -> set:
    """Return the set of Copilot session IDs already imported (stored in metadata)."""
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM metadata WHERE key = 'copilot_imported_sessions'")
    row = cursor.fetchone()
    if not row or not row[0]:
        return set()
    return set(json.loads(row[0]))


def save_imported_session_ids(conn: sqlite3.Connection, ids: set) -> None:
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES ('copilot_imported_sessions', ?)",
        (json.dumps(sorted(ids)),),
    )


def import_to_db(db_path: str, by_date: dict, sessions: list[dict], dry_run: bool = False) -> dict:
    """Merge Copilot data into analytics.db. Returns import stats."""
    stats = {"inserted": 0, "merged": 0, "skipped": 0, "dates": []}

    if dry_run:
        print("\n[DRY RUN — no changes written]")
        for date in sorted(by_date.keys()):
            d = by_date[date]
            print(
                f"  {date}: sessions={d['sessions']}, requests={d['requests']}, "
                f"cost=${d['cost']:.2f}, tokens={d['tokens']:,}"
            )
        return stats

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Ensure the copilot-specific tables exist (in case of older DB without them).
    # The extension creates these via createSchema, but the DB may predate the patch.
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS copilot_additions (
            date TEXT PRIMARY KEY,
            cost REAL DEFAULT 0,
            messages INTEGER DEFAULT 0,
            tokens INTEGER DEFAULT 0,
            sessions INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS copilot_model_additions (
            date TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            PRIMARY KEY (date, model)
        );
    """)

    # Check which sessions were already imported to prevent double-counting on re-runs.
    # Session IDs are the directory names from ~/.copilot/session-state/<id>/
    imported = get_imported_session_ids(conn)
    new_session_ids = set()

    # Filter sessions to only those not yet imported
    pending_sessions = []
    for s in sessions:
        sid = s['session_id']
        if sid in imported:
            stats["skipped"] += 1
        else:
            pending_sessions.append(s)
            new_session_ids.add(sid)

    if not pending_sessions:
        conn.close()
        return stats

    # Re-aggregate only the pending sessions
    pending_by_date = aggregate_by_date(pending_sessions)

    for date in sorted(pending_by_date.keys()):
        d = pending_by_date[date]

        # Write to copilot_additions — the extension's INSERT OR REPLACE on daily_snapshots
        # never touches this table, so the data survives all future extension refreshes.
        cursor.execute("SELECT 1 FROM copilot_additions WHERE date = ?", (date,))
        if cursor.fetchone():
            cursor.execute(
                "UPDATE copilot_additions SET "
                "  cost = cost + ?, messages = messages + ?, "
                "  tokens = tokens + ?, sessions = sessions + ? "
                "WHERE date = ?",
                (d["cost"], d["requests"], d["tokens"], d["sessions"], date),
            )
            stats["merged"] += 1
        else:
            cursor.execute(
                "INSERT INTO copilot_additions (date, cost, messages, tokens, sessions) VALUES (?, ?, ?, ?, ?)",
                (date, d["cost"], d["requests"], d["tokens"], d["sessions"]),
            )
            stats["inserted"] += 1

        stats["dates"].append(date)

        for model, m in d["models"].items():
            cursor.execute(
                "SELECT 1 FROM copilot_model_additions WHERE date = ? AND model = ?",
                (date, model),
            )
            if cursor.fetchone():
                cursor.execute(
                    "UPDATE copilot_model_additions SET "
                    "  input_tokens = input_tokens + ?, output_tokens = output_tokens + ?, "
                    "  cache_read_tokens = cache_read_tokens + ?, cache_write_tokens = cache_write_tokens + ? "
                    "WHERE date = ? AND model = ?",
                    (
                        m["input_tokens"],
                        m["output_tokens"],
                        m["cache_read_tokens"],
                        m["cache_write_tokens"],
                        date,
                        model,
                    ),
                )
            else:
                cursor.execute(
                    "INSERT INTO copilot_model_additions "
                    "  (date, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        date,
                        model,
                        m["input_tokens"],
                        m["output_tokens"],
                        m["cache_read_tokens"],
                        m["cache_write_tokens"],
                    ),
                )

    # Record imported session IDs so re-runs skip them
    save_imported_session_ids(conn, imported | new_session_ids)

    conn.commit()

    # Write sidecar JSON so the VS Code extension can load copilot rows at init
    # (the extension's saveDatabase() overwrites copilot_additions otherwise)
    sidecar_path = os.path.join(os.path.dirname(db_path), "copilot-additions.json")
    try:
        cursor2 = conn.cursor()
        cursor2.execute("SELECT date, cost, messages, tokens, sessions FROM copilot_additions ORDER BY date")
        rows = [
            {"date": r[0], "cost": r[1], "messages": r[2], "tokens": r[3], "sessions": r[4]} for r in cursor2.fetchall()
        ]
        cursor2.execute(
            "SELECT date, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens FROM copilot_model_additions ORDER BY date, model"
        )
        model_rows = [
            {
                "date": r[0],
                "model": r[1],
                "input_tokens": r[2],
                "output_tokens": r[3],
                "cache_read_tokens": r[4],
                "cache_write_tokens": r[5],
            }
            for r in cursor2.fetchall()
        ]
        with open(sidecar_path, "w") as f:
            json.dump({"rows": rows, "modelRows": model_rows}, f)
    except Exception:
        pass  # non-fatal

    conn.close()
    return stats


def print_summary(by_date: dict, import_stats: dict) -> None:
    total_sessions = sum(d["sessions"] for d in by_date.values())
    total_requests = sum(d["requests"] for d in by_date.values())
    total_cost = sum(d["cost"] for d in by_date.values())
    total_tokens = sum(d["tokens"] for d in by_date.values())

    # Per-model totals
    model_totals: dict[str, dict] = {}
    for d in by_date.values():
        for model, m in d["models"].items():
            if model not in model_totals:
                model_totals[model] = {"requests": 0, "input": 0, "output": 0, "cache_read": 0}
            model_totals[model]["requests"] += m["requests"]
            model_totals[model]["input"] += m["input_tokens"]
            model_totals[model]["output"] += m["output_tokens"]
            model_totals[model]["cache_read"] += m["cache_read_tokens"]

    print("\n" + "=" * 60)
    print("COPILOT BACKFILL SUMMARY")
    print("=" * 60)
    print(f"  Sessions processed:      {total_sessions:,}")
    print(f"  Active days:             {len(by_date)}")
    print(f"  Total premium requests:  {total_requests:,}")
    print(f"  Total tokens:            {total_tokens:,}")
    print(f"  Estimated cost:          ${total_cost:.2f}")
    print()
    print("  Per-model breakdown:")
    for model in sorted(model_totals.keys()):
        m = model_totals[model]
        print(f"    {model}:")
        print(f"      Requests: {m['requests']:,}  |  Input: {m['input']:,}  |  Output: {m['output']:,}")

    if import_stats["dates"]:
        print()
        print("  Database changes:")
        print(f"    Inserted (new dates):  {import_stats['inserted']}")
        print(f"    Merged (updated):      {import_stats['merged']}")
        print()
        print("  Dates imported:")
        for date in sorted(import_stats["dates"]):
            d = by_date[date]
            cost = d["cost"]
            tokens = d["tokens"]
            print(f"    {date}  sessions={d['sessions']}  cost=${cost:.2f}  tokens={tokens:,}")

    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Backfill Copilot session data into claude-usage-analytics database.")
    parser.add_argument(
        "--session-dir",
        default=os.path.expanduser("~/.copilot/session-state"),
        help="Copilot session-state directory (default: ~/.copilot/session-state)",
    )
    parser.add_argument(
        "--db",
        default=os.path.expanduser("~/.claude/analytics.db"),
        help="Path to analytics.db (default: ~/.claude/analytics.db)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Parse and summarise without writing to database")
    parser.add_argument("--no-backup", action="store_true", help="Skip database backup (not recommended)")
    args = parser.parse_args()

    # Validate inputs
    if not os.path.isdir(args.session_dir):
        print(f"Error: session-state directory not found: {args.session_dir}", file=sys.stderr)
        sys.exit(1)

    if not args.dry_run and not os.path.exists(args.db):
        print(f"Error: analytics.db not found at {args.db}", file=sys.stderr)
        print("Open VS Code with the Claude Usage Analytics extension to create it first.")
        sys.exit(1)

    print("Copilot Backfill Tool")
    print("=" * 60)
    print(f"Session dir: {args.session_dir}")
    print(f"Database:    {args.db}")

    # Load sessions
    print("\nLoading Copilot sessions...", end=" ", flush=True)
    sessions = load_sessions(args.session_dir)
    print(f"{len(sessions)} sessions found")

    if not sessions:
        print("No sessions with token data found. Nothing to import.")
        sys.exit(0)

    # Aggregate by date
    by_date = aggregate_by_date(sessions)
    print(f"Spanning {len(by_date)} unique days")

    # Backup
    if not args.dry_run and not args.no_backup:
        backup_path = args.db + ".copilot-backup"
        shutil.copy2(args.db, backup_path)
        print(f"Backup created: {backup_path}")

    # Import
    import_stats = import_to_db(args.db, by_date, sessions, dry_run=args.dry_run)

    # Summary
    print_summary(by_date, import_stats)

    if not args.dry_run:
        print("\nDone! Refresh the Claude Usage Analytics extension to see updated stats.")
        print("Keyboard shortcut: Ctrl+Alt+R (or Cmd+Alt+R on macOS)")


if __name__ == "__main__":
    main()
