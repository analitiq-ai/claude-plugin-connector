"""Guard: the PUBLISHED validator pin must accept the drivers the prose teaches.

The plugin prose and the runtime validator pin release on independent trains
(release-please for the plugins, hand-pushed tags for the packages). Nothing
else mechanically ties them together: every in-repo drift test grades the
in-repo contract SOURCE, while `connector-schema-validator` self-installs the
PUBLISHED `VALIDATOR_PIN` wheel. Issue #71 is the failure this permits — prose
instructing authors to write `redshift+redshift_connector` while the pinned
wheel still carried the old async-only pattern that rejects it.

This script closes that gap. It installs the pin into an ISOLATED venv (never
the current environment — an installed wheel is a regular package that shadows
the in-repo namespace source; see root CLAUDE.md "The contract") and validates
the canonical `dialect+driver` values against the wheel's own
`SqlAlchemyTransport`.

Single sources, referenced not copied:
  - the pin:     `VALIDATOR_PIN` in plugins/analitiq-pipeline-builder/scripts/_analitiq.py
  - the canon:   the "## Driver examples" table in
                 plugins/analitiq-connector-builder/skills/connector-spec-db/spec-dsn-bindings.md
  - shipped:     `[project].version` in packages/validator/pyproject.toml

Strictness — a contradiction is only sometimes a defect. The marketplace's
plugin sources are unpinned relative paths, so installs and updates ship main
HEAD, not release tags: main carrying the contradiction IS user exposure, not
merely a precursor to it.
  - pin == shipped (steady state): FAIL. No release is in flight to excuse it.
  - pin != shipped, ordinary pull_request: WARN, exit 0. The pin is
    deliberately behind while the new version publishes — root CLAUDE.md
    documents this sequencing, and failing here would make every
    contract-widening PR red with no way to unblock itself.
  - VALIDATOR_PIN_GUARD_STRICT=1: FAIL regardless of window. The tests
    workflow sets it on pushes to main (main is what users install — the
    window is then a visible red on main until the pin catch-up lands, by
    design) and on `release-please--*` branches (a Release PR merge stamps a
    plugin version onto the contradiction). Any other non-empty value is a
    GuardError — a typo must not silently downgrade to non-strict.

Exit codes: 0 verdict-ok (or window warning), 1 strict contradiction, 2
GuardError. EVERY infrastructure failure — unreadable sources, venv/pip
failure, probe crash, unparseable probe output — is a GuardError: a guard
that cannot run must never read as green, and must not be mistaken for a
verdict either. Inside the probe, only pydantic's ValidationError counts as
"rejected"; any other exception crashes the probe and surfaces as exit 2, so
a defective wheel cannot launder itself into a rejection list.

Live-settings caveat (same class root CLAUDE.md notes for the environments):
no branch protection currently *requires* this check, and workflow runs on
bot-opened Release PRs start `action_required` until a human approves them —
enforcement on the Release PR is convention plus this red X, not a merge
block, unless a ruleset requiring the check is added on main.

Wiring is pinned by tests/connector_builder/test_validator_pin_guard.py.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PIN_SOURCE = (
    REPO_ROOT / "plugins" / "analitiq-pipeline-builder" / "scripts" / "_analitiq.py"
)
CANON_SOURCE = (
    REPO_ROOT
    / "plugins"
    / "analitiq-connector-builder"
    / "skills"
    / "connector-spec-db"
    / "spec-dsn-bindings.md"
)
SHIPPED_SOURCE = REPO_ROOT / "packages" / "validator" / "pyproject.toml"

# The wheel's generated `analitiq/contracts/__init__.py` force-sets
# os.environ["DOMAIN"] (packages/contract-models/scripts/build.py), so the
# probe needs no environment of its own. Only ValidationError counts as a
# rejection — anything else (a defective wheel, an unbuilt model) must crash
# the probe so it surfaces as a GuardError, never as a verdict.
_PROBE = """\
import json, sys
from pydantic import ValidationError
from analitiq.contracts.connector import SqlAlchemyTransport

rejected = []
for value in sys.argv[1:]:
    try:
        SqlAlchemyTransport.model_validate(
            {"transport_type": "sqlalchemy", "driver": value}
        )
    except ValidationError:
        rejected.append(value)
print(json.dumps(rejected))
"""


class GuardError(RuntimeError):
    """The guard could not run — infrastructure, not a verdict. Exits 2."""


def read_pin() -> str:
    """The full `analitiq-validator==X` requirement from `_analitiq.py`."""
    match = re.search(
        r'^VALIDATOR_PIN = "(analitiq-validator==[^"]+)"$',
        PIN_SOURCE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not match:
        raise GuardError(f"VALIDATOR_PIN not found in {PIN_SOURCE}")
    return match.group(1)


def read_pin_version() -> str:
    return read_pin().split("==", 1)[1]


def read_shipped_version() -> str:
    """What this repo ships: `[project].version` in the validator pyproject.

    Anchored to the `[project]` table — a bare `^version =` would take
    whichever table came first if one were ever added above it (same
    precaution as tests/pipeline_builder/test_contract_enforcement.py).
    """
    text = SHIPPED_SOURCE.read_text(encoding="utf-8")
    project = text.split("[project]", 1)[-1].split("\n[", 1)[0]
    match = re.search(r'^version\s*=\s*"([^"]+)"', project, re.MULTILINE)
    if not match:
        raise GuardError(f"no [project] version found in {SHIPPED_SOURCE}")
    return match.group(1)


def read_canonical_drivers() -> list[str]:
    """Every driver in the prose's "## Driver examples" table, first column.

    The prose OWNS the canon; extracting it here (instead of copying it) means
    a new canonical driver is guarded the moment it is documented. To keep
    that promise, extraction is all-or-error: EVERY body row's first cell must
    be one backticked `dialect+driver` token, and a row this parser cannot
    read raises instead of silently dropping out of coverage. The token
    charset restates the contract's driver pattern — pinned together with the
    prose sites by tests/connector_builder/test_schema_drift.py
    (test_sqlalchemy_driver_pattern_matches_schema names this file).
    """
    text = CANON_SOURCE.read_text(encoding="utf-8")
    match = re.search(
        r"^## Driver examples$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    if not match:
        raise GuardError(f'no "## Driver examples" section in {CANON_SOURCE}')
    rows = [
        line for line in match.group(1).splitlines() if line.lstrip().startswith("|")
    ]
    if len(rows) < 3:  # GFM: header, separator, then at least one body row
        raise GuardError(
            f'the "## Driver examples" table in {CANON_SOURCE} has no body rows'
        )
    drivers = []
    for row in rows[2:]:
        first_cell = row.split("|")[1].strip()
        cell_match = re.fullmatch(r"`([a-z][a-z0-9_]*\+[a-z][a-z0-9_]*)`", first_cell)
        if not cell_match:
            raise GuardError(
                f"unparseable row in the Driver examples table of {CANON_SOURCE}: "
                f"{row!r} — every body row's first cell must be a single "
                "backticked dialect+driver token; if the driver charset widened, "
                "update read_canonical_drivers() with it (see "
                "test_sqlalchemy_driver_pattern_matches_schema)"
            )
        drivers.append(cell_match.group(1))
    return drivers


def read_strict_env() -> bool:
    """The VALIDATOR_PIN_GUARD_STRICT override. Only '1' or unset/'' parse.

    Anything else raises: a typo like `true` must not silently downgrade a
    strict run to warn-only.
    """
    value = os.environ.get("VALIDATOR_PIN_GUARD_STRICT", "")
    if value not in ("", "1"):
        raise GuardError(
            f"VALIDATOR_PIN_GUARD_STRICT={value!r} not recognized — "
            "set '1' for strict or leave unset"
        )
    return value == "1"


def probe_pinned_wheel(pin: str, drivers: list[str]) -> list[str]:
    """Install `pin` into a throwaway venv; return the drivers its models reject."""
    with tempfile.TemporaryDirectory(prefix="validator-pin-guard-") as tmp:
        venv_dir = Path(tmp) / "venv"
        py = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
        steps = [
            [sys.executable, "-m", "venv", str(venv_dir)],
            # -I (isolated): ambient PYTHONPATH / user site-packages must not
            # front-run the venv. An exact `==<rc>` pin resolves pre-releases
            # without --pre (PEP 440).
            [str(py), "-I", "-m", "pip", "install", "--quiet",
             "--disable-pip-version-check", pin],
        ]
        for cmd in steps:
            # check=False: the returncode is inspected by hand so the failure
            # can be wrapped as a GuardError (exit 2, never a verdict).
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                raise GuardError(
                    f"{' '.join(cmd)} failed:\n"
                    f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                )
        result = subprocess.run(
            [str(py), "-I", "-c", _PROBE, *drivers],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise GuardError(f"probe crashed inside the venv:\n{result.stderr}")
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise GuardError(
                f"probe output is not JSON ({exc}) — raw stdout:\n{result.stdout}"
            ) from exc


def _surface_warning(text: str) -> None:
    """Print the window warning where someone will actually see it.

    A plain print in a green job is read by no one; on Actions, also emit a
    workflow annotation (shows on the PR checks UI) and a step summary.
    """
    print(text)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title=validator pin::{' '.join(text.split())}")
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as fh:
                fh.write(f"⚠️ {text}\n")


def main() -> int:
    try:
        pin = read_pin()
        pin_version = read_pin_version()
        shipped = read_shipped_version()
        drivers = read_canonical_drivers()
        strict = read_strict_env() or pin_version == shipped

        print(f"pin: {pin}  shipped: {shipped}  strict: {strict}")
        print(f"canonical drivers ({CANON_SOURCE.name}): {', '.join(drivers)}")

        rejected = probe_pinned_wheel(pin, drivers)
    except (GuardError, OSError) as exc:
        # OSError covers unreadable source files and unlaunchable
        # subprocesses — infrastructure, exactly like a GuardError.
        print(f"GUARD ERROR (not a verdict): {exc}", file=sys.stderr)
        return 2

    if not rejected:
        print("OK: the pinned release accepts every canonical driver.")
        return 0

    verdict = (
        f"the pinned {pin} REJECTS canonical driver(s) the plugin prose "
        f"teaches: {', '.join(rejected)}"
    )
    if strict:
        print(f"FAIL: {verdict}", file=sys.stderr)
        print(
            "Users install main HEAD, so this contradiction is (or is about to "
            "be) live inside one plugin flow — finish the package release and "
            "bump VALIDATOR_PIN first (see root CLAUDE.md, issue #71).",
            file=sys.stderr,
        )
        return 1
    _surface_warning(
        f"WARNING (release window, pin {pin_version} != shipped {shipped}): {verdict}\n"
        "Allowed on an ordinary PR while the new package version publishes; the "
        "pin bump follow-up must land before any plugin Release PR merges, and "
        "main stays red (strict on push) until it does."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
