#!/usr/bin/env python3
"""End-to-end smoke check against a running source + API pair.

Usage:
    python3 scripts/smoke.py --api http://127.0.0.1:8000 --source http://127.0.0.1:8001

Exercises: initial publish, query hit, version switch with a dropped notice,
source failure retaining old data, retry idempotency, and the documented
failure modes exposed by the mock source's scenarios.
"""

from __future__ import annotations

import argparse
import sys

import httpx


class Checker:
    def __init__(self, api: str, source: str) -> None:
        self.api = api.rstrip("/")
        self.source = source.rstrip("/")
        self.failures: list[str] = []

    def check(self, condition: bool, message: str) -> None:
        if condition:
            print(f"  PASS  {message}")
        else:
            print(f"  FAIL  {message}")
            self.failures.append(message)

    def switch(self, name: str) -> dict:
        response = httpx.post(f"{self.source}/scenario", json={"name": name}, timeout=10)
        response.raise_for_status()
        return response.json()

    def collect(self) -> tuple[int, dict]:
        response = httpx.post(f"{self.api}/api/v1/collect", timeout=30)
        return response.status_code, response.json()

    def query(self, stop_id: str, at: str) -> dict:
        response = httpx.get(
            f"{self.api}/api/v1/announcements",
            params={"stop_id": stop_id, "at": at},
            timeout=10,
        )
        return {"_status": response.status_code} | response.json()

    def run(self) -> int:
        print("1. publish v1")
        self.switch("v1")
        status, body = self.collect()
        self.check(status == 200 and body["status"] == "published", "v1 published")
        self.check(body.get("source_version") == "2026-11-17T06:00:00Z", "v1 version echoed")

        hit = self.query("STOP-001", "2026-11-17T09:00:00Z")
        self.check(hit["_status"] == 200, "query returns 200")
        self.check(
            [item["announcement_no"] for item in hit["announcements"]] == ["A-1001"],
            "STOP-001 at 09:00Z hits only A-1001",
        )
        self.check(hit["source_version"] == "2026-11-17T06:00:00Z", "query reports source version")

        print("2. retry same version is idempotent")
        status, body = self.collect()
        self.check(status == 200 and body["status"] == "already_current", "retry not republished")

        print("3. switch to v2: A-1001 disappears, A-1003 stays")
        self.switch("v2")
        status, body = self.collect()
        self.check(status == 200 and body["status"] == "published", "v2 published")
        self.check(
            self.query("STOP-001", "2026-11-17T09:00:00Z")["announcements"] == [],
            "A-1001 dropped with v2",
        )
        self.check(
            self.query("STOP-001", "2026-11-17T11:00:00Z")["announcements"]
            == [
                {
                    "announcement_no": "A-2001",
                    "stop_id": "STOP-001",
                    "start_utc": "2026-11-17T10:00:00Z",
                    "end_utc": "2026-11-17T14:00:00Z",
                }
            ],
            "A-2001 served from v2",
        )
        self.check(
            [item["announcement_no"] for item in self.query("STOP-002", "2026-11-17T10:00:00Z")["announcements"]]
            == ["A-1003"],
            "same-stop/other-stop notice A-1003 still served",
        )

        for scenario, label in (
            ("mixed_versions", "version flip across pages"),
            ("missing_page", "missing page"),
            ("short_pages", "page count shortfall"),
            ("duplicate_conflict", "conflicting duplicate number"),
        ):
            print(f"4.{label}")
            self.switch(scenario)
            status, body = self.collect()
            self.check(status == 502 and body["status"] == "failed", f"{label} rejected")
            current = httpx.get(f"{self.api}/api/v1/current-version", timeout=10).json()
            self.check(current["source_version"] == "2026-11-17T09:00:00Z", "old data retained")

        print("5. stale revision is rejected")
        self.switch("old_revision")
        status, body = self.collect()
        self.check(status == 409 and body["status"] == "failed", "stale revision rejected")
        self.check(
            httpx.get(f"{self.api}/api/v1/current-version", timeout=10).json()["source_version"]
            == "2026-11-17T09:00:00Z",
            "old data retained after stale revision",
        )

        print("6. restart durability is checked separately (scripts/restart_check.sh)")
        return 1 if self.failures else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--source", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    return Checker(args.api, args.source).run()


if __name__ == "__main__":
    sys.exit(main())
