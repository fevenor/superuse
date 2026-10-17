"""Offline publication hygiene checks; findings never include matched secrets.

Default: tracked and non-ignored working files. --staged: exact Git index blobs.
This is a conservative heuristic, not a guarantee that every secret is found.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
from collections import Counter
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRS = {
    "data",
    ".git",
    ".ssh",
    ".aws",
    ".codex",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "artifacts",
    "runtime",
    "logs",
    "captures",
    "diagnostics",
    "private",
    ".private",
    "build",
    "dist",
}
PRIVATE_SUFFIXES = {
    ".log",
    ".pem",
    ".key",
    ".pfx",
    ".p12",
    ".jks",
    ".dmp",
    ".exe",
    ".dll",
    ".zip",
    ".7z",
    ".jpg",
    ".jpeg",
    ".mp4",
    ".pyc",
}
RULES = {
    "private-key": re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "service-api-key": re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{30,}"),
    "private-windows-path": re.compile(r"\b[A-Za-z]:[\\/]+(?:Users|project)[\\/]", re.I),
    "url-credentials": re.compile(r"https?://[^\s/@:]+:[^\s/@]+@", re.I),
}
CREDENTIAL = re.compile(
    r"""["']?(?:[a-z_]*_)?(?:token|secret|password|bootstrap|api[_-]?key)["']?"""
    r"""\s*[:=]\s*["']([A-Za-z0-9_+/=-]{24,})["']""",
    re.I,
)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")


def forbidden_path(name: str) -> bool:
    path = PurePosixPath(name)
    lowered = path.as_posix().lower()
    if set(part.lower() for part in path.parts) & PRIVATE_DIRS:
        return True
    if lowered.startswith("third_party/upstream/"):
        return True
    if path.suffix.lower() in PRIVATE_SUFFIXES:
        return True
    if path.suffix.lower() in {".png", ".ico"} and lowered not in {"web/icon.png", "web/icon.ico"}:
        return True
    leaf = path.name.lower()
    return (
        (leaf.startswith(".env") and leaf != ".env.example")
        or ".local." in leaf
        or leaf
        in {"broker.json", "control.json", "config.toml", "credentials.json", "secrets.json"}
        or leaf.startswith(("id_rsa", "id_ed25519"))
    )


def entropy(value: str) -> float:
    return -sum((n / len(value)) * math.log2(n / len(value)) for n in Counter(value).values())


def audit_text(text: str) -> list[tuple[int, str]]:
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        for rule, pattern in RULES.items():
            if pattern.search(line):
                findings.append((number, rule))
        for match in CREDENTIAL.finditer(line):
            value = match[1]
            placeholder = value.lower().startswith(
                ("test-", "test_", "example-", "example_", "dummy-", "replace_")
            )
            if not placeholder and entropy(value) >= 3.5:
                findings.append((number, "credential-literal"))
        for match in EMAIL.finditer(line):
            domain = match[1].lower()
            if domain not in {
                "example.com",
                "example.org",
                "example.invalid",
                "users.noreply.github.com",
            } and not domain.endswith(".example"):
                findings.append((number, "review-email"))
    return findings


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def index_files():
    for entry in git("ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, oid, stage = metadata.decode("ascii").split()
        name = raw_name.decode("utf-8")
        if stage != "0" or mode not in {"100644", "100755"}:
            raise ValueError(f"Unsupported index entry: {name}")
        yield name, git("cat-file", "blob", oid)


def working_files():
    names = git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    for name in sorted(set(names.decode("utf-8").split("\0")) - {""}):
        path = ROOT / name
        if path.is_symlink():
            raise ValueError(f"Review symlink before publishing: {name}")
        if path.is_file():
            yield name, path.read_bytes()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--staged", action="store_true", help="read exact Git index, not working files"
    )
    args = parser.parse_args()
    # Attribution email addresses in exact, hash-pinned upstream licenses are
    # intentional public provenance. All other sensitive-data rules still apply.
    license_manifest = ROOT / "third_party/runtime-licenses.json"
    manifest_data = (
        git("show", ":third_party/runtime-licenses.json")
        if args.staged
        else license_manifest.read_bytes()
    )
    pins = json.loads(manifest_data)["licenses"]
    attribution = {"third_party/" + pin["file"]: pin["sha256"] for pin in pins}
    findings = []
    count = 0
    for name, data in index_files() if args.staged else working_files():
        count += 1
        if forbidden_path(name):
            findings.append((name, 0, "private-or-generated-path"))
            continue
        if name.lower() in {"web/icon.png", "web/icon.ico"}:
            continue
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            findings.append((name, 0, "review-binary-file"))
            continue
        pinned_license = attribution.get(name) == hashlib.sha256(data).hexdigest()
        findings.extend(
            (name, line, rule)
            for line, rule in audit_text(text)
            if not (pinned_license and rule == "review-email")
        )
    if not count:
        raise ValueError("No files selected; stage the publication tree first")
    for name, line, rule in findings:
        print(f"{name}:{line}: {rule}")
    print(
        f"Publication check: {count} files, {len(findings)} findings "
        "(heuristic; review still required)."
    )
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
