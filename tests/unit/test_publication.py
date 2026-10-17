"""Publication scanner handles exact content without printing credentials."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "check_publication", Path(__file__).resolve().parents[2] / "scripts/check_publication.py"
)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


@pytest.mark.parametrize(
    "name",
    [
        "runtime/broker.json",
        "artifacts/frame.png",
        "device.local.toml",
        ".env",
        "third_party/upstream/source.py",
        "release.zip",
    ],
)
def test_private_and_generated_files_are_rejected(name):
    assert scanner.forbidden_path(name)


@pytest.mark.parametrize(
    "name",
    [
        "config.example.toml",
        "web/icon.png",
        "LICENSE",
        "examples/contracts/agent-session-grant.json",
    ],
)
def test_public_source_and_synthetic_contracts_are_allowed(name):
    assert not scanner.forbidden_path(name)


def test_reports_rules_and_lines_without_matched_values():
    credential = "ghp_" + "Ab9" * 15
    text = '\napi_token = "' + credential + '"\n'
    findings = scanner.audit_text(text)
    assert (2, "github-token") in findings
    assert all(credential not in str(item) for item in findings)


def test_personal_path_and_placeholder_distinction():
    private_path = "C:" + "/Users/" + "private-person/work"
    assert (1, "private-windows-path") in scanner.audit_text(private_path)
    assert not scanner.audit_text('"session_token": "' + "A" * 43 + '"')
    assert not scanner.audit_text('"command": "C:/REPLACE_WITH_INSTALL_DIRECTORY/app.exe"')
