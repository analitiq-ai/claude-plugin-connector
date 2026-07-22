#!/usr/bin/env python3
"""Validate an authored Analitiq document against the published contract.

This is a thin **adapter**. It holds no validation logic of its own — it
dispatches to the published `analitiq-validator` + `analitiq-contract-models`
packages (the same offline, model-driven contract the Analitiq services validate
against) and normalizes every backend into one Diagnostics envelope:

    {"passed": bool, "findings": [{"validator", "severity", "path", "message"}]}

The published package exposes one single-document entry point plus one bundle
entry point. This adapter routes each entity as follows:

  * ``database_endpoint`` -> ``analitiq.validator.validate_document`` — the model
    plus the derived-``endpoint_id`` gate and column checks (the same code the
    ``analitiq-validate`` CLI runs).
  * ``connection`` / ``stream`` / ``pipeline`` -> the matching ``*Input`` Pydantic
    model's ``.model_validate`` (the source of truth the published JSON Schemas are
    rendered from). ``validate_document`` would reach the same models, but it
    selects them by *document shape*: its detectors key off ``connector_id``,
    ``destinations`` and ``connections`` respectively. An authored document that
    omits its discriminating key — precisely the broken input this adapter exists
    to diagnose — would match no detector and collapse into a single generic
    "unrecognized artifact" finding. Routing by the caller-supplied ``--entity``,
    which is already known here, guarantees the right model runs and yields
    per-field findings instead.
  * ``type_map_read`` / ``type_map_write`` -> ``analitiq.validator.validate_document``
    over the connection-scoped type-map rule array, after an adapter filename gate:
    the engine loads ``connections/<slug>/definition/type-map-{read,write}.json`` by
    exactly those names (and the published validator derives rule direction from
    them, defaulting an unknown name to read), so a misnamed file gets the rename
    finding alone rather than findings that could be graded in the wrong direction.
    The published ``type-map-write-coverage`` warning is filtered out here: it
    presumes a connector's full-vocabulary write map, which a gap-only connection
    map deliberately is not (see ``_type_map_findings``).
  * ``pipeline`` with ``--bundle-root`` -> additionally
    ``analitiq.validator.validate_pipeline_bundle`` over the on-disk bundle, for the
    cross-document referential integrity no single document can verify. A draft
    bundle passes ``require_runnable=False`` (a not-yet-runnable draft is not an
    authoring error); an ``active`` pipeline is held to full runnability.

The adapter adds two checks of its own, each only because the published contract
structurally cannot make it:

  * ``connector-endpoint-ref`` — the published bundle validator receives
    connector *identity* only (slugs), never connector endpoint *contents*, so it
    leaves ``scope='connector'`` endpoint refs unresolved by design
    (``analitiq.validator.pipelines``: "out of scope for this check"). This plugin,
    unlike the service, has the downloaded connector endpoint files on disk, so it
    verifies each ``scope='connector'`` stream ref against the connector's on-disk
    endpoint set and emits a **warning** (with an alignment suggestion) when the
    referenced endpoint is absent. It never errors — connectors are trusted
    registry artifacts pinned by ``connector_version`` at runtime — and it never
    edits the connector; the orchestrator aligns the stream's ref instead.
  * ``connection-type-map`` — the published bundle validator receives assembled
    documents, never a connection's directory, so it cannot see the type-map
    files the engine loads beside ``connection.json``. The bundle pass therefore
    validates each connection's present ``type-map-{read,write}.json`` in full
    (via the published validator) and rejects the dead pre-split ``type-map.json``
    filename with a migration finding, mirroring the published connector-side
    check at connection scope.

Validation is offline — no schema is fetched. Usage::

    python3 plugins/analitiq-pipeline-builder/scripts/validate.py --entity pipeline --document path/to/pipeline.json --bundle-root .

Exit status is ``0`` iff ``passed`` (no error-severity finding), ``1`` on any
error finding or an unreadable document, ``2`` on a CLI usage error.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from _analitiq import ensure_deps_or_reexec

ENTITIES = ("pipeline", "stream", "connection", "database_endpoint",
            "type_map_read", "type_map_write")

# The engine loads connection-scoped type maps by these exact filenames under
# connections/<slug>/definition/ — a differently-named file is silently ignored
# at runtime, so the adapter gates the name like the endpoint filename gate does.
_TYPE_MAP_FILENAMES = {"type_map_read": "type-map-read.json",
                       "type_map_write": "type-map-write.json"}
# The pre-split filename: the engine never reads it, at either scope. The
# published validator rejects it beside a connector; the adapter mirrors that
# for connections, where the published bundle validator cannot see files.
_LEGACY_TYPE_MAP_FILENAME = "type-map.json"


# ---------------------------------------------------------------------------
# Finding + Diagnostics shape
# ---------------------------------------------------------------------------

def _finding(validator: str, severity: str, path: str, message: str) -> dict:
    return {"validator": validator, "severity": severity, "path": path, "message": message}


def _diagnostics(findings: list[dict]) -> dict:
    passed = all(f.get("severity") != "error" for f in findings)
    return {"passed": passed, "findings": findings}


# ---------------------------------------------------------------------------
# Per-entity validation (importable + unit-testable; analitiq imported lazily so
# importing this module never requires the validator to be installed)
# ---------------------------------------------------------------------------

def _model_findings(entity: str, doc) -> list[dict]:
    """Validate a single connection/stream/pipeline document against its published
    contract model, mapping each Pydantic error to a finding (the same mapping the
    validator itself uses internally)."""
    if entity == "connection":
        from analitiq.contracts.connection import ConnectionInput as Model
    elif entity == "stream":
        from analitiq.contracts.stream import StreamInput as Model
    elif entity == "pipeline":
        from analitiq.contracts.pipelines.config import PipelineInput as Model
    else:  # pragma: no cover - guarded by the entity choices
        raise ValueError(f"no contract model for entity {entity!r}")
    from pydantic import ValidationError
    try:
        Model.model_validate(doc)
        return []
    except ValidationError as exc:
        return [
            _finding("contract-model", "error",
                     "/" + "/".join(str(p) for p in err["loc"]), err["msg"])
            for err in exc.errors()
        ]


def _endpoint_findings(doc, document_path: Path) -> list[dict]:
    from analitiq.validator import validate_document
    return validate_document(doc, doc_path=document_path.resolve())


def _type_map_findings(entity: str, doc, document_path: Path) -> list[dict]:
    """Validate a connection-scoped type-map file. The filename gate runs first
    and alone on a mismatch: the published validator derives rule direction from
    the filename, so validating a misnamed file's content could grade it in the
    wrong direction (an unknown filename defaults to read) and bury the one
    actionable finding (rename it) in noise. A non-list document is likewise
    gated here — the published dispatch detects by *shape*, so a stray dict
    under a type-map filename would be graded as some other artifact (a
    connection document would even pass clean) instead of failing as the
    non-array the engine's loader will choke on."""
    expected = _TYPE_MAP_FILENAMES[entity]
    if document_path.name != expected:
        return [_finding(
            "connection-type-map", "error", "",
            f"file is named {document_path.name!r} but entity {entity!r} requires "
            f"{expected!r} — the engine loads each direction only from its exact "
            f"filename (connections/<slug>/definition/{expected}).")]
    if not isinstance(doc, list):
        return [_finding(
            "connection-type-map", "error", "",
            f"{expected} must be a top-level JSON array of rules, got "
            f"{type(doc).__name__}.")]
    from analitiq.validator import validate_document
    # Resolve the parent but keep the authored basename: the published validator
    # derives direction from `doc_path.name`, and a full resolve() would follow a
    # symlinked map to a differently-named target and silently re-grade it.
    findings = validate_document(doc, doc_path=document_path.parent.resolve() / document_path.name)
    if entity == "type_map_write":
        # The published write-vocabulary coverage warning presumes a CONNECTOR
        # write map, which must cover the full canonical vocabulary. A connection
        # map is gap-only by rule (spec-type-map-gaps.md) — the warning would fire
        # on every authored connection write map forever, and its remedy ("add
        # rules") is exactly the shadowing the gap-only rule forbids. Filtering it
        # is the same adapter-adapts-published-behavior move as require_runnable.
        findings = [f for f in findings if f.get("validator") != "type-map-write-coverage"]
    return findings


def _connection_type_map_findings(conn_dir: Path) -> list[dict]:
    """Validate the connection-scoped type maps beside one connection.json —
    file-level checks the published bundle validator structurally cannot make
    (it receives assembled documents, never the connection's directory). A
    present map is validated in full via the published validator; the dead
    pre-split filename is rejected with a migration finding, mirroring the
    published connector-side check."""
    findings: list[dict] = []
    definition = conn_dir / "definition"
    site = f"connections/{conn_dir.name}/definition"
    legacy = definition / _LEGACY_TYPE_MAP_FILENAME
    if legacy.exists() or legacy.is_symlink():
        findings.append(_finding(
            "connection-type-map", "error", f"{site}/{_LEGACY_TYPE_MAP_FILENAME}",
            f"{_LEGACY_TYPE_MAP_FILENAME} is the pre-split filename; the engine never "
            "reads it. Split it into type-map-read.json (native → Arrow) and, for the "
            "write direction, type-map-write.json (Arrow → native)."))
    for entity, fname in _TYPE_MAP_FILENAMES.items():
        path = definition / fname
        if not (path.exists() or path.is_symlink()):
            continue
        if not path.is_file():
            # A directory or dangling symlink under a load-bearing name would
            # pass silently here and fail at the engine's loader — the most
            # expensive place to find out.
            findings.append(_finding(
                "connection-type-map", "error", f"{site}/{fname}",
                f"{fname} exists but is not a readable file (directory or dangling "
                "symlink); the engine's loader will fail to open it."))
            continue
        try:
            doc = _read_json(path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            findings.append(_finding("connection-type-map", "error", f"{site}/{fname}",
                                     f"Cannot read {fname}: {exc}"))
            continue
        findings.extend({**f, "path": f"{site}/{fname}{f.get('path', '')}"}
                        for f in _type_map_findings(entity, doc, path))
    return findings


def _read_bundle_member(path: Path, findings: list[dict]) -> dict | None:
    """Read one sibling bundle document. On an unreadable/invalid file or a
    non-object payload, append an error finding and return None — so a malformed
    sibling becomes a clear diagnostic instead of an uncaught traceback or a
    silently dropped document."""
    try:
        doc = _read_json(path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        findings.append(_finding("document", "error", "", f"Cannot read {path.name}: {exc}"))
        return None
    if not isinstance(doc, dict):
        findings.append(_finding("document", "error", "", f"{path.name} is not a JSON object"))
        return None
    return doc


def _assemble_bundle(pipeline_doc: dict, document_path: Path, root: Path) -> tuple[dict, list[dict]]:
    """Gather the on-disk pipeline bundle the way the engine resolves it at load:
    the pipeline plus its sibling stream documents, every connection, the
    connection-scoped endpoint documents (stamped with their owning connection's
    id, which endpoint documents do not carry themselves), and the downloaded
    connector identities. Returns the bundle plus any read-error findings for
    malformed siblings."""
    # The engine locates a connection-scoped endpoint by its filename stem, so a file
    # named other than <endpoint_id>.json won't resolve at runtime. validate_document
    # gates this for a stem-addressed file, but validate_pipeline_bundle takes a
    # filename-less dict — so run the published gate here, where the names are known.
    from analitiq.validator import endpoint_filename_findings

    findings: list[dict] = []

    streams: list[dict] = []
    for p in sorted((document_path.parent / "streams").glob("*.json")):
        doc = _read_bundle_member(p, findings)
        if doc is not None:
            streams.append(doc)

    connections: list[dict] = []
    endpoints: list[dict] = []
    for conn_json in sorted((root / "connections").glob("*/connection.json")):
        # Connection-scoped type maps are files the engine loads beside the
        # connection, invisible to the assembled-document bundle — check them here.
        findings.extend(_connection_type_map_findings(conn_json.parent))
        conn = _read_bundle_member(conn_json, findings)
        if conn is None:
            continue
        connections.append(conn)
        connection_id = conn.get("connection_id")
        for ep_json in sorted((conn_json.parent / "definition" / "endpoints").glob("*.json")):
            endpoint = _read_bundle_member(ep_json, findings)
            if endpoint is None:
                continue
            # files here are stem-addressed by construction (globbed from
            # definition/endpoints/), so the published filename gate applies directly
            findings.extend(endpoint_filename_findings(endpoint, ep_json.name))
            # Endpoint documents omit connection_id (server-managed); supply the
            # owning connection's id so the bundle's endpoint-ref check can resolve
            # connection-scoped references.
            endpoint.setdefault("connection_id", connection_id)
            endpoint.setdefault("scope", "connection")
            endpoints.append(endpoint)

    # Connectors supply identity only, and the directory slug already is that
    # identity — so a malformed connector.json is best-effort skipped (its slug
    # still counts), not a bundle error.
    connectors: set[str] = set()
    for conn_json in sorted((root / "connectors").glob("*/definition/connector.json")):
        connectors.add(conn_json.parent.parent.name)  # directory slug
        try:
            cid = _read_json(conn_json).get("connector_id")
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            continue
        if isinstance(cid, str) and cid:
            connectors.add(cid)

    bundle = {
        "pipeline": pipeline_doc,
        "streams": streams,
        "connections": connections,
        "connectors": sorted(connectors),
        "endpoints": endpoints,
    }
    return bundle, findings


def _connector_endpoint_sets(root: Path) -> dict[str, set[str]]:
    """Map each downloaded connector — by directory slug **and** its authored
    `connector_id` — to the set of endpoint ids it publishes on disk (each
    `connectors/<slug>/definition/endpoints/*.json` contributes both its filename
    stem and its `endpoint_id` field, which the connector's own filename gate keeps
    equal for well-formed registry connectors — this adapter records both to stay
    correct even if a malformed connector let them diverge).

    A connector whose `definition/endpoints/` directory is absent or empty is
    **omitted**, not recorded as an empty set: its endpoint set is *unknown* here
    (the plugin may not have downloaded endpoints for it), and an unknown set must
    not read as "no endpoints", which would warn on every ref. Callers treat a
    missing key as "cannot verify — skip"."""
    sets: dict[str, set[str]] = {}
    for ep_dir in sorted(root.glob("connectors/*/definition/endpoints")):
        if not ep_dir.is_dir():
            continue
        ids: set[str] = set()
        for ep_json in sorted(ep_dir.glob("*.json")):
            ids.add(ep_json.stem)
            try:
                eid = _read_json(ep_json).get("endpoint_id")
            except (OSError, json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                eid = None
            if isinstance(eid, str) and eid:
                ids.add(eid)
        if not ids:
            continue
        slug_dir = ep_dir.parent.parent  # connectors/<slug>
        keys = {slug_dir.name}
        try:
            cid = _read_json(slug_dir / "definition" / "connector.json").get("connector_id")
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            cid = None
        if isinstance(cid, str) and cid:
            keys.add(cid)
        for key in keys:
            sets[key] = ids
    return sets


def _check_connector_endpoint_refs(streams, connections,
                                   connector_endpoint_sets: dict[str, set[str]]) -> list[dict]:
    """Verify every `scope='connector'` stream endpoint_ref names an endpoint that
    actually exists in the referenced connector's on-disk endpoint set. Emits a
    `connector-endpoint-ref` **warning** (never an error) per unresolved ref, with a
    closest-match alignment suggestion so the orchestrator can retarget the stream to
    the connector's real endpoint name (it never edits the connector).

    Skipped silently when the endpoint set is unknown (connector not downloaded), so
    absence never produces a false positive. Reuses the published ref iterator and
    version-suffix normaliser so ref paths and connection-id matching stay identical
    to the bundle validator's own resolution."""
    import difflib
    from analitiq.validator.pipelines import _base_id, _iter_endpoint_refs

    conn_to_connector: dict = {}
    for conn in connections if isinstance(connections, list) else []:
        if not isinstance(conn, dict):
            continue
        cid, connector = conn.get("connection_id"), conn.get("connector_id")
        if isinstance(cid, str) and isinstance(connector, str):
            conn_to_connector[_base_id(cid)] = connector

    findings: list[dict] = []
    for path, ref in _iter_endpoint_refs(streams):
        if ref.get("scope") != "connector":
            continue
        cid, eid = ref.get("connection_id"), ref.get("endpoint_id")
        if not (isinstance(cid, str) and cid and isinstance(eid, str) and eid):
            continue  # missing ids are contract-model / bundle-connection-ref concerns
        connector = conn_to_connector.get(_base_id(cid))
        if connector is None:
            continue  # unresolved connection — already flagged by the connection check
        endpoint_ids = connector_endpoint_sets.get(connector)
        if not endpoint_ids:
            continue  # endpoint set unknown (connector not downloaded) — cannot verify
        if eid in endpoint_ids:
            continue
        available = sorted(endpoint_ids)
        case_match = next((e for e in available if e.lower() == eid.lower()), None)
        close = difflib.get_close_matches(eid, available, n=1, cutoff=0.6)
        suggestion = case_match or (close[0] if close else None)
        hint = f" Did you mean {suggestion!r}?" if suggestion else ""
        findings.append(_finding(
            "connector-endpoint-ref", "warning", path,
            f"endpoint_id {eid!r} is not among connector {connector!r}'s published "
            f"endpoints {available}.{hint} Align the stream's endpoint_ref to the "
            f"connector's endpoint name; the plugin never edits the connector.",
        ))
    return findings


def _bundle_findings(pipeline_doc: dict, document_path: Path, root: Path) -> list[dict]:
    from analitiq.validator import validate_pipeline_bundle
    bundle, findings = _assemble_bundle(pipeline_doc, document_path, root)
    # This plugin authors draft bundles by design: a draft pipeline is not yet
    # runnable, so its runnability verdicts are an author-time expectation, not a
    # defect. Ask the bundle validator for referential integrity only
    # (require_runnable=False) while the pipeline is a draft, and enforce runnability
    # once it is authored 'active'. Every referential finding stays blocking either way.
    require_runnable = pipeline_doc.get("status") == "active"
    findings = findings + validate_pipeline_bundle(bundle, require_runnable=require_runnable)
    # Plugin-local aid the published bundle can't make: it receives connector identity
    # only, so scope='connector' endpoint refs go unresolved. The plugin has the
    # downloaded connector endpoint files, so verify those refs here and warn (with an
    # alignment suggestion) rather than error — connectors are trusted, pinned at runtime.
    findings += _check_connector_endpoint_refs(
        bundle["streams"], bundle["connections"], _connector_endpoint_sets(root))
    return findings


def diagnostics_for(entity: str, document_path: Path, bundle_root: Path | None = None) -> dict:
    """Validate one document and return the Diagnostics envelope. Raises nothing
    for validation failures — those become findings; only a genuinely unreadable
    document short-circuits."""
    try:
        doc = _read_json(document_path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _diagnostics([_finding("document", "error", "", f"Cannot read document: {exc}")])

    if entity == "database_endpoint":
        findings = _endpoint_findings(doc, document_path)
    elif entity in _TYPE_MAP_FILENAMES:
        findings = _type_map_findings(entity, doc, document_path)
    else:
        findings = _model_findings(entity, doc)
        if entity == "pipeline" and bundle_root is not None:
            findings = findings + _bundle_findings(doc, document_path, bundle_root)
    return _diagnostics(findings)


def _read_json(path: Path):
    return json.loads(Path(path).read_text())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--entity", required=True, choices=ENTITIES,
                        help="Which published contract the document is authored against.")
    parser.add_argument("--document", required=True, help="Path to the JSON document to validate.")
    parser.add_argument("--bundle-root",
                        help="Project root for cross-document validation of a stitched pipeline "
                             "(walks connections/, connectors/, and the pipeline's streams/). "
                             "Only meaningful with --entity pipeline.")
    args = parser.parse_args(argv)

    try:
        ensure_deps_or_reexec(__file__)
    except RuntimeError as exc:
        print(json.dumps(_diagnostics([_finding("contract-model", "error", "", str(exc))]), indent=2))
        return 1

    bundle_root = Path(args.bundle_root) if args.bundle_root else None
    diagnostics = diagnostics_for(args.entity, Path(args.document), bundle_root)
    print(json.dumps(diagnostics, indent=2))
    return 0 if diagnostics["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
