"""Every shipped example must validate against the pinned contract.

The `examples/` trees are what creator agents read as authoring archetypes, so a
stale example is worse than no example — it teaches a shape the validator
rejects. (All three API examples were silently invalid before this guard: a
`response.records` ref over a non-array node, a lowercase `exact` read-map
native that can never match an uppercased probe, and a templated `base_url` the
contract types as a plain string.)

Examples are laid out for readability (`<name>/<name>.example.json` beside its
type maps and `endpoints/`), not as an on-disk connector, so each is staged into
a `definition/` directory first — the layout the cross-file coverage checks walk.

Same environment contract as the other drift guards: skipped when the pinned
packages are absent, hard-failed in CI via `DRIFT_REQUIRE_CONTRACT_MODELS=1`.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

try:
    import analitiq.contracts  # noqa: F401
    from analitiq.validator import validate_document  # noqa: F401
except ImportError:  # pragma: no cover - environment guard
    if os.environ.get("DRIFT_REQUIRE_CONTRACT_MODELS") == "1":
        raise
    pytest.skip(
        "analitiq-validator / analitiq-contract-models not installed — run "
        '`pip install --pre "analitiq-validator==1.0.0rc10" '
        '"analitiq-contract-models==1.0.0rc10"` to run the drift guards',
        allow_module_level=True,
    )

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO_ROOT / "src" / "skills"
CONNECTOR_SCHEMA = "https://schemas.analitiq.ai/connector/latest.json"
ENDPOINT_SCHEMA = "https://schemas.analitiq.ai/api-endpoint/latest.json"


def _example_dirs() -> list[Path]:
    return sorted(
        d
        for d in SKILLS_ROOT.glob("connector-spec-*/examples/*")
        if d.is_dir() and any(d.glob("*.example.json"))
    )


def _stage(example_dir: Path, dest_root: Path) -> Path:
    """Copy an example into the `definition/` layout the sibling walks expect."""
    definition = dest_root / "definition"
    definition.mkdir(parents=True)

    body = next(example_dir.glob("*.example.json"))
    shutil.copy(body, definition / "connector.json")
    for name in ("type-map-read.json", "type-map-write.json"):
        src = example_dir / name
        if src.exists():
            shutil.copy(src, definition / name)
    endpoints = example_dir / "endpoints"
    if endpoints.is_dir():
        shutil.copytree(endpoints, definition / "endpoints")
    return definition / "connector.json"


def _errors(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["severity"] == "error"]


def test_examples_exist() -> None:
    """Guard the glob itself — a renamed layout must not silently pass nothing."""
    assert _example_dirs(), f"no examples discovered under {SKILLS_ROOT}"


@pytest.mark.parametrize("example_dir", _example_dirs(), ids=lambda d: d.name)
def test_example_connector_validates(example_dir: Path, tmp_path: Path) -> None:
    doc_path = _stage(example_dir, tmp_path)
    document = json.loads(doc_path.read_text(encoding="utf-8"))

    findings = validate_document(
        document, doc_path=doc_path.resolve(), schema_url=CONNECTOR_SCHEMA
    )
    errors = _errors(findings)
    assert not errors, "\n".join(
        f"{f['validator']} {f['path']}: {f['message']}" for f in errors
    )


@pytest.mark.parametrize("example_dir", _example_dirs(), ids=lambda d: d.name)
def test_example_endpoints_validate(example_dir: Path, tmp_path: Path) -> None:
    """Endpoints must also hold up standalone.

    Validating from the connector walks siblings, but `endpoint-filename` and
    `endpoint-id-locator` are most direct here — and an endpoint is authored and
    validated on its own during the fan-out.
    """
    definition = _stage(example_dir, tmp_path).parent
    endpoint_files = sorted((definition / "endpoints").glob("*.json"))
    if not endpoint_files:
        pytest.skip(f"{example_dir.name} ships no endpoints")

    for endpoint_path in endpoint_files:
        document = json.loads(endpoint_path.read_text(encoding="utf-8"))
        findings = validate_document(
            document, doc_path=endpoint_path.resolve(), schema_url=ENDPOINT_SCHEMA
        )
        errors = _errors(findings)
        assert not errors, f"{endpoint_path.name}\n" + "\n".join(
            f"{f['validator']} {f['path']}: {f['message']}" for f in errors
        )
