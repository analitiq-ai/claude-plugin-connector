"""Every shipped example must validate against the pinned contract.

The `examples/` trees are what creator agents read as authoring archetypes, so a
stale example is worse than no example — it teaches a shape the validator
rejects. (All three API examples were silently invalid when this guard was
added, including a `response.records` ref over a non-array node.)

Examples are laid out for readability (`<name>/<name>.example.json` beside its
type maps and `endpoints/`), not as an on-disk connector, so each is staged into
a `definition/` directory first — the layout the cross-file coverage checks walk.

Same environment contract as the other drift guards: skipped when the pinned
packages are absent, hard-failed in CI via `DRIFT_REQUIRE_CONTRACT_MODELS=1`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from _pins import require_contract_models

require_contract_models("analitiq.contracts", "analitiq.validator")

from analitiq.validator import validate_document  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO_ROOT / "plugins" / "analitiq-connector-builder" / "skills"
CONNECTOR_SCHEMA = "https://schemas.analitiq.ai/connector/latest.json"
ENDPOINT_SCHEMA = "https://schemas.analitiq.ai/api-endpoint/latest.json"
TYPE_MAP_SCHEMAS = {
    "type-map-read.json": "https://schemas.analitiq.ai/type-map-read/latest.json",
    "type-map-write.json": "https://schemas.analitiq.ai/type-map-write/latest.json",
}


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


def test_every_example_dir_is_covered() -> None:
    """Guard the glob itself — no example may silently drop out of coverage.

    `_example_dirs()` selects on a `*.example.json` body, so a renamed body file
    would quietly remove that example from the parametrized tests below while
    still leaving examples discovered. Assert against the directory listing, not
    against emptiness.
    """
    all_dirs = {d for d in SKILLS_ROOT.glob("connector-spec-*/examples/*") if d.is_dir()}
    assert all_dirs, f"no example directories under {SKILLS_ROOT}"

    uncovered = sorted(str(d.relative_to(SKILLS_ROOT)) for d in all_dirs - set(_example_dirs()))
    assert not uncovered, (
        f"example directories with no `*.example.json` body: {uncovered} — these "
        "are skipped by every check below. Rename the body file to "
        "`<name>.example.json` or remove the directory."
    )


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
def test_example_type_maps_validate(example_dir: Path, tmp_path: Path) -> None:
    """Validate each type map as a standalone document, under its own filename.

    This is the invocation `connector-schema-validator` documents (each map
    validated against its matching read/write schema URL, direction derived from
    the filename), so it should be exercised directly rather than only through
    the connector's sibling walk. It also localizes a failure to the map instead
    of surfacing it on the connector.

    It does NOT close the database read-map gap: rule-shape errors are already
    caught by the sibling walk, and neither level probes natives on a DB
    connector, so a wrong-case `exact` native still ships silently. That gap is
    documented in `spec-type-maps.md`, not covered here.
    """
    definition = _stage(example_dir, tmp_path).parent
    present = [(definition / name, url) for name, url in TYPE_MAP_SCHEMAS.items()
               if (definition / name).exists()]
    assert present, f"{example_dir.name} ships no type map"

    for map_path, schema_url in present:
        document = json.loads(map_path.read_text(encoding="utf-8"))
        findings = validate_document(
            document, doc_path=map_path.resolve(), schema_url=schema_url
        )
        errors = _errors(findings)
        assert not errors, f"{map_path.name}\n" + "\n".join(
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
