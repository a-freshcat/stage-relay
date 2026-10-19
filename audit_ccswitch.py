#!/usr/bin/env python3
"""Optional read-only cost observation for this run's CLI session IDs only.

Never reads provider credentials or changes the live database. No price inference.
"""
import argparse
from decimal import Decimal, InvalidOperation
import json
import pathlib
import sqlite3

from relaylib.contracts import load_json
from relaylib.runner import parse_events
from relaylib.runtime import atomic_json


def audit(run_dir, database):
    run_dir = pathlib.Path(run_dir).resolve()
    sessions = set()
    for path in run_dir.glob("*-*/events.jsonl"):
        sessions.update(parse_events(path)["thread_ids"])
    if not sessions:
        raise ValueError("No CLI session IDs found")
    con = sqlite3.connect(pathlib.Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        placeholders = ",".join("?" for _ in sessions)
        columns = ["request_id", "model", "request_model", "pricing_model", "status_code", "total_cost_usd", "input_tokens", "output_tokens", "cache_read_tokens", "session_id"]
        rows = con.execute("SELECT " + ",".join(columns) + " FROM proxy_request_logs WHERE session_id IN (" + placeholders + ") ORDER BY created_at,request_id", sorted(sessions)).fetchall()
    finally:
        con.close()
    known, unknown, records = Decimal("0"), 0, []
    for row in rows:
        record = dict(zip(columns, row))
        try:
            cost = Decimal(str(record["total_cost_usd"]))
            if not cost.is_finite() or cost <= 0:
                raise InvalidOperation()
            known += cost
            record["cost_status"] = "ledger_priced"
        except (InvalidOperation, ValueError):
            unknown += 1
            record["cost_status"] = "unknown_not_free"
        records.append(record)
    result = {"matched_sessions": len(sessions), "matched_rows": len(records),
              "known_ledger_cost_usd": str(known) if records else None, "unpriced_rows": unknown,
              "total_cost_usd": None,
              "coverage": "matched_rows_only" if records else "no_matching_ledger_rows_cost_unknown",
              "scope": "matched_CLI_session_IDs_only; unrelated_or_unattributable_review_calls_not_included",
              "source": "CCSwitch read-only ledger; not an independent provider bill",
              "records": records}
    atomic_json(run_dir / "ccswitch-audit.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--database", default=str(pathlib.Path.home() / ".cc-switch/cc-switch.db"))
    args = parser.parse_args()
    try:
        result = audit(args.run_dir, args.database)
        print(json.dumps({k: v for k, v in result.items() if k != "records"}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": "Audit unavailable; no zero-cost inference was made"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
