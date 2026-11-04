#!/usr/bin/env python3
"""Gold smoke test: verify every gold SQL in an eval set runs and returns NON-empty rows.

Why: an empty gold row means the QUESTION is wrong (bad field / threshold / date),
not the model. Running a baseline on broken questions is self-deception.
This extends "validate the ruler first" to the ruler itself.

Usage:
    python check_gold.py                        # default: eval_set.json
    python check_gold.py eval_heldout.json

Exit code: 0 = all pass; 1 = some rows empty or errored (ids listed at the end).

Note: runs on SQLite by default (db.py USE_PG switch applies here too).
Gold SQL must be dialect-neutral so this same file validates on PG later.
"""

import json
import os
import sys

from db import connect_readonly

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "eval_set.json"
    path = name if os.path.isabs(name) else os.path.join(BASE_DIR, name)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    cases = data["cases"]
    print(f"checking {len(cases)} cases in {name}\n")

    conn = connect_readonly()
    bad = []
    for c in cases:
        cid = c["id"]
        sql = c.get("sql")
        if not sql:
            kind = c.get("type", "?")
            print(f"[{cid:>3}] {kind:<8} skip (no gold sql)  {c['q'][:36]}")
            continue
        try:
            rows = conn.execute(sql).fetchall()
            n = len(rows)
            flag = "EMPTY!" if n == 0 else "ok"
            if n == 0:
                bad.append(cid)
            print(f"[{cid:>3}] {c['type']:<8} rows={n:<5} {flag:<6} {c['q'][:36]}")
        except Exception as e:  # noqa: BLE001 - report and continue
            bad.append(cid)
            msg = str(e).replace("\n", " ")[:90]
            print(f"[{cid:>3}] {c['type']:<8} SQL ERROR: {msg} | q: {c['q'][:36]}")
    conn.close()

    print()
    if bad:
        print(f"FAIL: {len(bad)} case(s) empty or errored -> ids {bad}")
        sys.exit(1)
    print("PASS: every gold sql runs and returns non-empty rows")


if __name__ == "__main__":
    main()
