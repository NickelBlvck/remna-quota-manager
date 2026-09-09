#!/usr/bin/env python3
"""Docker HEALTHCHECK: exit 0 if the monitor loop has run recently.

The monitor writes ``kv_settings.heartbeat_at`` at the end of every loop
iteration. Container is unhealthy if that timestamp is older than
``HEALTHCHECK_STALE_MINUTES`` (default 30) or missing.
"""
import os
import sys
from datetime import datetime, timedelta, timezone


def main() -> int:
    try:
        from database import QuotaDatabase, DEFAULT_DB_PATH
    except Exception as e:  # noqa: BLE001
        print(f"import failed: {e}")
        return 1

    if not os.path.exists(DEFAULT_DB_PATH):
        print("db not created yet")
        return 1

    stale_min = int(os.environ.get("HEALTHCHECK_STALE_MINUTES", "30"))
    try:
        beat = QuotaDatabase().get_setting("heartbeat_at")
    except Exception as e:  # noqa: BLE001
        print(f"db read failed: {e}")
        return 1

    if not beat:
        print("no heartbeat yet")
        return 1

    try:
        ts = datetime.fromisoformat(beat)
    except ValueError:
        print(f"bad heartbeat value: {beat!r}")
        return 1
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    age = datetime.now(timezone.utc) - ts
    if age > timedelta(minutes=stale_min):
        print(f"stale heartbeat: {age}")
        return 1

    print(f"ok (heartbeat age {age})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
