"""Check design artifacts without importing application extras or opening hardware."""

import hashlib
import json
import re
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path):
    def reject_constant(value: str):
        raise ValueError(f"Non-finite JSON constant in {path}: {value}")

    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)


def check_contracts() -> int:
    schema = read_json(ROOT / "schemas/contracts.schema.json")
    Draft202012Validator.check_schema(schema)
    validators = {
        name: Draft202012Validator(
            {
                "$schema": schema["$schema"],
                "$ref": f"#/$defs/{name}",
                "$defs": schema["$defs"],
            },
            format_checker=FormatChecker(),
        )
        for name in schema["$defs"]
    }
    manifest = read_json(ROOT / "examples/contracts/manifest.json")
    names = set()
    for case in manifest:
        if case["name"] in names:
            raise ValueError(f"Duplicate case: {case['name']}")
        names.add(case["name"])
        value = read_json(ROOT / "examples/contracts" / case["file"])
        errors = list(validators[case["definition"]].iter_errors(value))
        if case["valid"] and errors:
            raise ValueError(f"Expected valid {case['name']}: {errors[0].message}")
        if not case["valid"] and not errors:
            raise ValueError(f"Expected rejection: {case['name']}")
    config = tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))
    validators["Config"].validate(config)
    control, video = config["control"], config["video"]
    if control["renew_interval_ms"] * 2 > control["lease_ttl_ms"]:
        raise ValueError("Renew interval must be at most half the configured lease TTL")
    if video["freshness_ms"] >= video["stall_timeout_ms"]:
        raise ValueError("Freshness threshold must precede stall timeout")
    return len(manifest)


def check_links() -> int:
    checked = 0
    paths = [*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md")]
    for path in paths:
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            target = target.strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            linked = (path.parent / unquote(parsed.path)).resolve()
            if not linked.is_relative_to(ROOT):
                raise ValueError(f"External filesystem link in {path}: {target}")
            if not linked.exists():
                raise FileNotFoundError(f"Broken link in {path}: {target}")
            checked += 1
    return checked


def check_sources() -> int:
    lock = read_json(ROOT / "third_party/upstream-lock.json")
    for source in lock["sources"]:
        if not re.fullmatch(r"[0-9a-f]{40}", source["commit"]):
            raise ValueError(f"Invalid upstream commit: {source['repo']}")
        path = ROOT / "third_party" / source["license_file"]
        data = path.read_bytes()
        actual = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        if actual != source["license_git_blob"]:
            raise ValueError(f"Upstream license changed: {path}")
    extra = read_json(ROOT / "third_party/runtime-licenses.json")["licenses"]
    for license in extra:
        path = ROOT / "third_party" / license["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != license["sha256"]:
            raise ValueError(f"Runtime dependency license changed: {path}")
    return len(lock["sources"]) + len(extra)


def check_vectors() -> int:
    vectors = read_json(ROOT / "examples/protocol-vectors.json")
    for vector in vectors:
        data = bytes.fromhex(vector["hex"])
        if data[:3] != b"\x57\xab\x00":
            raise ValueError(f"Invalid HID header: {vector['name']}")
        if data[4] != len(data) - 6 or data[-1] != sum(data[:-1]) & 255:
            raise ValueError(f"Invalid HID length/checksum: {vector['name']}")
        if data[3] not in (2, 4, 5):
            raise ValueError(f"Unexpected HID command: {vector['name']}")
    return len(vectors)


def main() -> None:
    # Parse every authored JSON file, including non-contract reference fixtures.
    json_paths = [*(ROOT / "schemas").rglob("*.json"), *(ROOT / "examples").rglob("*.json")]
    json_paths += list((ROOT / "docs").rglob("*.json"))
    for path in json_paths:
        read_json(path)
    tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    cases = check_contracts()
    links = check_links()
    sources = check_sources()
    vectors = check_vectors()
    print(f"OK: {cases} contract cases, config, {links} local links, {sources} licenses.")
    print(f"OK: {vectors} HID vectors; no network, hardware, or application services opened.")


if __name__ == "__main__":
    main()
