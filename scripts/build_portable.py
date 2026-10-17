"""Build a fresh Windows x64 onedir bundle and a clean portable release ZIP.

uv run --locked --extra app --extra packaging python scripts/build_portable.py
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ENTRIES = {"Superuse.exe", "SuperuseMCP.exe", "_internal"}


def release_paths(root: Path):
    """Visit files in order, rejecting links before descending outside the tree."""
    if root.is_symlink() or root.is_junction():
        raise ValueError("Release contains a link; review it before release")
    yield root
    if root.is_dir():
        for child in sorted(root.iterdir()):
            yield from release_paths(child)


def collect_licenses(destination: Path) -> None:
    """Include installed build-env licenses conservatively, not just direct deps."""
    destination.mkdir()
    pins = json.loads((ROOT / "third_party/runtime-licenses.json").read_text(encoding="utf-8"))
    fallbacks = {item["distribution"].lower(): item for item in pins["licenses"]}
    inventory = []
    for dist in sorted(
        importlib.metadata.distributions(), key=lambda d: d.metadata["Name"].lower()
    ):
        name = dist.metadata["Name"]
        if name.lower() == "superuse":
            continue
        label = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{name}-{dist.version}")
        folder = destination / label
        files = []
        for item in dist.files or []:
            path = Path(dist.locate_file(item))
            if (
                path.name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE"))
                and path.is_file()
            ):
                files.append(path)
        folder.mkdir()
        copied = []
        for index, path in enumerate(sorted(set(files)), 1):
            filename = f"{index:03d}-{path.name}"
            (folder / filename).write_bytes(path.read_bytes())
            copied.append(f"licenses/{label}/{filename}")
        if not copied:
            pin = fallbacks.get(name.lower())
            if pin is None or pin["version"] != dist.version:
                raise ValueError(f"License text missing for {name} {dist.version}")
            source = ROOT / "third_party" / pin["file"]
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != pin["sha256"]:
                raise ValueError(f"Pinned license changed for {name}")
            (folder / "LICENSE.txt").write_bytes(data)
            copied.append(f"licenses/{label}/LICENSE.txt")
        inventory.append({"name": name, "version": dist.version, "files": copied})
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise ValueError("Python runtime license is missing")
    (destination / "Python-LICENSE.txt").write_bytes(python_license.read_bytes())
    (destination.parent / "license-inventory.json").write_text(
        json.dumps({"python": platform.python_version(), "distributions": inventory}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    summary = [
        "# Third-party notices",
        "",
        "Original license texts are included under licenses/. The inventory conservatively",
        "covers installed distributions, conservatively including build/test tools.",
        "Bundled Python and dependency vendor-library notices are retained as provided.",
        "",
        "Superuse source adaptations and their original licenses are under _internal/third_party/.",
        "See LICENSE and NOTICE.md for project attribution.",
        "",
    ]
    summary.extend(
        f"- {item['name']} {item['version']}: " + ", ".join(item["files"]) for item in inventory
    )
    (destination.parent / "THIRD-PARTY-NOTICES.md").write_text(
        "\n".join(summary) + "\n", encoding="utf-8"
    )


def prepare_release(bundle: Path, stage: Path) -> None:
    """Accept only pristine PyInstaller output; never copy an installed data tree."""
    for _ in release_paths(bundle):
        pass
    if {p.name for p in bundle.iterdir()} != BUNDLE_ENTRIES:
        raise ValueError("Expected only the two executables and _internal in the fresh bundle")
    if not (bundle / "_internal").is_dir():
        raise ValueError("Missing bundle dependencies")
    if not all((bundle / name).is_file() for name in BUNDLE_ENTRIES - {"_internal"}):
        raise ValueError("Missing bundle executables")
    stage.mkdir()
    for name in sorted(BUNDLE_ENTRIES):
        source = bundle / name
        if source.is_dir():
            shutil.copytree(source, stage / name)
        else:
            shutil.copy2(source, stage / name)
    for name in ("LICENSE", "NOTICE.md", "config.example.toml"):
        shutil.copy2(ROOT / name, stage / name)
    shutil.copy2(ROOT / "packaging/PORTABLE.md", stage / "PORTABLE.md")
    shutil.copy2(ROOT / "packaging/MCP.md", stage / "MCP.md")
    for name in ("mcp-client.example.json", "mcp-client-http.example.json"):
        shutil.copy2(ROOT / "examples" / name, stage / name)
    # Empty directory is the mode selector; never ship a user's configuration,
    # runtime credentials, logs, or latch. Empty directories survive in the ZIP.
    (stage / "data").mkdir()


def write_zip(stage: Path, output: Path) -> None:
    if list((stage / "data").iterdir()):
        raise ValueError("Release data directory must be empty")
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=output.stem + "-", suffix=".tmp", dir=output.parent)
    os.close(handle)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in release_paths(stage):
                archive.write(path, path.relative_to(stage.parent).as_posix())
        os.replace(temporary, output)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
    with output.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(
        f"{digest}  {output.name}\n", encoding="ascii"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if (
        sys.platform != "win32"
        or platform.machine().lower() not in {"amd64", "x86_64"}
        or sys.maxsize <= 2**32
    ):
        parser.error("Build on Windows x64 with the locked Python environment")
    from superuse import __version__

    build_root = (ROOT / "build").resolve()
    build_root.mkdir(exist_ok=True)
    # Fresh scratch tree prevents any existing distribution's local data from
    # entering a release. The checked directory is also the only cleanup target.
    with tempfile.TemporaryDirectory(prefix="portable-", dir=build_root) as temporary:
        scratch = Path(temporary).resolve()
        if scratch.parent != build_root:
            raise ValueError("Unexpected build scratch directory")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                str(ROOT / "packaging/superuse.spec"),
                "--noconfirm",
                "--distpath",
                str(scratch / "dist"),
                "--workpath",
                str(scratch / "work"),
            ],
            cwd=ROOT,
            check=True,
        )
        stage = scratch / "Superuse"
        prepare_release(scratch / "dist/Superuse", stage)
        collect_licenses(stage / "licenses")
        output = args.output_dir.resolve() / f"Superuse-{__version__}-windows-x64-portable.zip"
        write_zip(stage, output)
    print(f"Portable ZIP: {output}")
    print(f"SHA-256: {output.with_suffix(output.suffix + '.sha256')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
