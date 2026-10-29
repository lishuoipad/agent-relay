"""Read-only verification of existing nested {path, sha256} delivery records.

Checks file identity/content only. No installation, acceptance, recursive file
discovery, following referenced manifests, model calls or business testing.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from supervision import read_json


def fingerprint(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def local_file(value):
    p = Path(value)
    resolved = p.resolve()
    # Evidence verification must not start a remote file transfer.
    name = str(resolved)
    if name.startswith("\\\\?\\") and not name.lower().startswith("\\\\?\\unc\\"):
        name = name[4:]
    if not p.is_absolute() or name.startswith(("\\\\", "//")):
        raise ValueError("absolute local file path required")
    return p


def hash_file(path, remaining_bytes):
    p = local_file(path)
    before = p.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("regular file required")
    if before.st_size > remaining_bytes:
        raise ValueError("total read budget exceeded")
    h = hashlib.sha256()
    size = 0
    with p.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        # Windows stat/fstat can expose different ctime semantics. Compare
        # identity/size/mtime across APIs, then ctime within each API only.
        if fingerprint(before)[:-1] != fingerprint(opened)[:-1]:
            raise ValueError("file changed before read")
        while chunk := stream.read(min(1024 * 1024, remaining_bytes - size + 1)):
            size += len(chunk)
            if size > remaining_bytes:
                raise ValueError("total read budget exceeded during read")
            h.update(chunk)
        after = os.fstat(stream.fileno())
    if fingerprint(opened) != fingerprint(after) or fingerprint(before) != fingerprint(p.stat()):
        raise ValueError("file changed during read")
    return h.hexdigest(), size


def verify(manifest, expected_sha256, max_bytes=128 * 1024 * 1024, max_refs=1024):
    """Only the supplied JSON is traversed; referenced files are hashed once."""
    report = {"schemaVersion": 1, "manifest": str(manifest), "expectedSha256": expected_sha256,
              "scope": "nested path+sha256 refs; content identity only, not acceptance",
              "ok": False, "refOccurrences": 0, "checkedFiles": 0, "completedReadBytes": 0,
              "files": [], "errors": []}
    try:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            raise ValueError("expected manifest SHA256 required")
        if max_bytes <= 0 or max_refs <= 0:
            raise ValueError("positive read budgets required")
        p = local_file(manifest)
        source, actual = read_json(p)
        report["manifestSha256"] = actual
        if actual != expected_sha256.lower():
            raise ValueError("manifest hash mismatch; no references read")
        refs = {}

        def visit(value, depth=0):
            if depth > 32:
                raise ValueError("manifest nesting limit exceeded")
            if isinstance(value, dict):
                if "sha256" in value:
                    path, sha = value.get("path"), value["sha256"]
                    if not isinstance(path, str) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
                        raise ValueError("malformed path+sha256 reference")
                    report["refOccurrences"] += 1
                    if report["refOccurrences"] > max_refs:
                        raise ValueError("reference limit exceeded")
                    normalized = str(local_file(path).resolve())
                    key = os.path.normcase(normalized)
                    previous = refs.get(key)
                    if previous and previous[1] != sha.lower():
                        raise ValueError("conflicting hashes for reference: " + normalized)
                    refs[key] = normalized, sha.lower()
                for child in value.values():
                    visit(child, depth + 1)
            elif isinstance(value, list):
                for child in value:
                    visit(child, depth + 1)

        visit(source)
        if not refs:
            raise ValueError("no path+sha256 references found")
        for path, expected in refs.values():
            row = {"path": path, "expectedSha256": expected, "ok": False}
            try:
                actual, count = hash_file(path, max_bytes - report["completedReadBytes"])
                row.update(actualSha256=actual, bytes=count, ok=actual == expected)
                report["completedReadBytes"] += count
                report["checkedFiles"] += 1
                if actual != expected:
                    row["error"] = "content hash mismatch"
            except (OSError, ValueError) as exc:
                row["error"] = str(exc)
                # A failed read may have consumed an unknown part of the budget.
                # Stop rather than resetting its budget and continuing.
                report["files"].append(row)
                break
            report["files"].append(row)
        _, final_sha = read_json(p)
        if final_sha != report["manifestSha256"]:
            raise ValueError("manifest changed during verification")
        report["ok"] = len(report["files"]) == len(refs) and all(row["ok"] for row in report["files"])
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        report["errors"].append(str(exc))
    return report


def summary(report):
    issues = report["errors"] + [row for row in report["files"] if not row["ok"]]
    return {**{k: v for k, v in report.items() if k not in {"files", "errors"}},
            "issueCount": len(issues), "issues": issues[:10], "truncated": len(issues) > 10}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--report", required=True, help="new report path in the existing task output")
    parser.add_argument("--max-bytes", type=int, default=128 * 1024 * 1024)
    parser.add_argument("--max-refs", type=int, default=1024)
    args = parser.parse_args()
    output = local_file(args.report)
    # Reserve a new output before reading; never overwrite delivery/evidence.
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        report = verify(args.manifest, args.expected_sha256, args.max_bytes, args.max_refs)
        raw = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        stream.write(raw)
    print(json.dumps({**summary(report), "report": str(output),
                      "reportSha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
