#!/usr/bin/env python3
"""Sync visitor logs from the Render deployment to a local JSONL file.

The live site writes visitors to data/visitors.jsonl on Render's EPHEMERAL
filesystem (lost on every redeploy). /api/visitors exposes the latest 200
records, so polling frequently enough means zero data loss in practice.

Usage:
    python _sync_visitors.py            # one pull
    python _sync_visitors.py --test     # same, but does not write the file

Requires CHATBI_ACCESS_CODE in .env (the same value set on Render).

Design notes:
- Dedup by exact record tuple (ts, ip, ua, event, q) against the local file.
- Filters keep-alive bot noise (UptimeRobot pings /api/meta every 5 min,
  each ping logs an "open" event -> ~288 junk records/day).
- Console output is ASCII-only (Windows GBK console).
"""
import json
import os
import sys
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
LOCAL_LOG = os.path.join(BASE_DIR, "data", "visitors_local.jsonl")
SITE = "https://chatbi-opy6.onrender.com/api/visitors"


def load_env_key(key: str) -> str:
    if key in os.environ:
        return os.environ[key]
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ""


def record_key(rec: dict) -> tuple:
    return (rec.get("ts", ""), rec.get("ip", ""), rec.get("ua", ""),
            rec.get("event", ""), rec.get("q", ""))


def is_bot(rec: dict) -> bool:
    return "uptime" in (rec.get("ua") or "").lower()


def main() -> int:
    test_mode = "--test" in sys.argv
    code = load_env_key("CHATBI_ACCESS_CODE")
    if not code:
        print("ERROR: CHATBI_ACCESS_CODE not found (add it to .env)")
        return 1

    req = urllib.request.Request(SITE, headers={"X-Access-Code": code})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print("ERROR: fetch failed:", e)
        return 1

    recent = payload.get("recent") or []
    bots = sum(1 for r in recent if is_bot(r))
    human = [r for r in recent if not is_bot(r)]

    if test_mode:
        print("[TEST] site reachable, %d records (bot-filtered: %d -> %d kept)"
              % (len(recent), bots, len(human)))
        return 0

    os.makedirs(os.path.dirname(LOCAL_LOG), exist_ok=True)
    existing = set()
    if os.path.exists(LOCAL_LOG):
        with open(LOCAL_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        existing.add(record_key(json.loads(line)))
                    except Exception:
                        continue

    new_lines = []
    for rec in human:  # recent is newest-first; iterate any order, dedup by key
        k = record_key(rec)
        if k not in existing:
            new_lines.append(json.dumps(rec, ensure_ascii=False))

    if new_lines:
        with open(LOCAL_LOG, "a", encoding="utf-8") as f:
            f.write("\n".join(new_lines) + "\n")

    print("OK: fetched %d records (%d bot-filtered), %d new appended, total local=%d"
          % (len(recent), bots, len(new_lines),
              len(existing) + len(new_lines)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
