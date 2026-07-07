"""Guards on consuming the prebuilt `analitiq-connector-validator` release.

The plugin no longer ships the validator's source; it installs the pinned
release and the `connector-schema-validator` agent invokes its
`analitiq-validate-connector` **console script** (not the module). The other
suites only `import` the module, so they can't catch a packaging defect in a
future pin bump — a broken `[project.scripts]`, `py-modules`, or `package-dir`
would break every agent run with no other signal. These tests cover that
runtime surface (mirroring the retired `validator-package.yml` smoke job) and
pin-string drift across the consumers that must agree.

Offline: `--semantic-only` needs no schema host.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The exact pinned spec the plugin consumes. Every consumer below must install
# / invoke this same release, or CI validates against a different validator
# version than the agent actually runs.
PIN = (
    "analitiq-connector-validator @ "
    "git+https://github.com/analitiq-ai/claude-plugin-connector.git"
    "@validator-v0.1.0#subdirectory=validator"
)


def _validate(connector: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "analitiq-validate-connector",
            "--document",
            str(FIXTURES / connector / "connector.json"),
            "--semantic-only",
        ],
        capture_output=True,
        text=True,
    )


def test_entry_point_passes_clean_connector() -> None:
    proc = _validate("valid_api_connector")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_entry_point_fails_uncovered_connector() -> None:
    # An endpoint declares a native its read map doesn't cover — the entry
    # point must surface a type-map-coverage error and exit non-zero.
    proc = _validate("api_endpoints_uncovered")
    assert proc.returncode != 0, proc.stdout + proc.stderr


def test_pin_is_consistent_across_consumers() -> None:
    for rel in (
        ".github/workflows/tests.yml",
        "agents/connector-schema-validator.md",
        "README.md",
    ):
        text = (REPO_ROOT / rel).read_text()
        assert PIN in text, f"{rel} does not pin the exact prebuilt validator spec"
