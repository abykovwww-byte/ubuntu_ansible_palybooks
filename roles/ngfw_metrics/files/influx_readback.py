#!/usr/bin/env python3
"""Compare saved Influx v1 JSON queries at ns precision, including tags and fields.

Queries must select the same bounded interval, GROUP BY *, and request epoch=ns.
Source files are evidence receipts; this tool never claims transport was tested.
"""
import argparse
import hashlib
import json
from pathlib import Path


def canonical_rows(response):
    def reject_partial(value):
        if isinstance(value, dict):
            if value.get("partial"):
                raise ValueError("Partial/chunked query receipt is not complete readback proof")
            for child in value.values():
                reject_partial(child)
        elif isinstance(value, list):
            for child in value:
                reject_partial(child)
    reject_partial(response)
    rows = []
    if response.get("error"):
        raise ValueError("Influx query failed")
    if "results" not in response:
        raise ValueError("Not an Influx v1 query response")
    for result in response["results"]:
        if result.get("error"):
            raise ValueError("Influx statement failed")
        for series in result.get("series", []):
            columns = series.get("columns", [])
            if "time" not in columns:
                raise ValueError("Readback requires raw points with time")
            for row in series.get("values", []):
                if len(row) != len(columns):
                    raise ValueError("Malformed row")
                data = dict(zip(columns, row))
                if not isinstance(data["time"], int):
                    raise ValueError("Query must specify epoch=ns; no lossy float timestamps")
                rows.append(json.dumps({"measurement": series["name"], "tags": series.get("tags", {}), "fields_and_time": data}, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return sorted(rows)


def compare(left, right):
    a, b = canonical_rows(left), canonical_rows(right)
    if not a or not b:
        raise ValueError("Empty readback is not delivery proof")
    return {"equal": a == b, "local_rows": len(a), "remote_rows": len(b),
            "local_sha256": hashlib.sha256("\n".join(a).encode()).hexdigest(),
            "remote_sha256": hashlib.sha256("\n".join(b).encode()).hexdigest(),
            "scope": "Only the supplied bounded raw queries; completeness of query coverage must be verified independently"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("local_json")
    parser.add_argument("remote_json")
    args = parser.parse_args()
    result = compare(json.loads(Path(args.local_json).read_text(encoding="utf-8")), json.loads(Path(args.remote_json).read_text(encoding="utf-8")))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["equal"] else 1)
