"""Pipeline validation — the single `pipeline` document, and the assembled
pipeline-run bundle.

This module registers TWO kinds:

1. **`pipeline` document** — validated wholly against its contract model
   (`PipelineInput`, the source of the published `pipeline` JSON Schema):
   `TypeAdapter(...).validate_python` enforces structure and every cross-field
   rule offline. The model IS the whole single-document validity story, so it
   registers via `register_model_kind`.

2. **pipeline bundle** — cross-document referential integrity across an assembled
   run (pipeline + streams + connections + connectors + endpoints).

Per-document *shape* validity (field types, lengths, enums) is each document
kind's own job — the connector / api-endpoint / database-endpoint / type-map kinds
in `connectors`, and the pipeline / stream / connection contract models the
producers validate against. The BUNDLE validator checks the referential integrity
BETWEEN the assembled documents — the cross-document relationships no single
document can verify in isolation. It does NOT assume each document was already
contract-validated, so a missing *reference* field (a connection that names no
connector, a stream slot with no endpoint_ref, an endpoint_ref with no
connection_id / endpoint_id) is treated as an UNRESOLVED reference — a referential
failure — not skipped. A bundle that passes here resolves cleanly at load:

- every `pipeline.streams[]` ref resolves to exactly one bundled stream document;
- every bundled stream declares this pipeline as its parent;
- each referenced connection is present in the bundle exactly once;
- the pipeline does not pin two different versions of the same connection base;
- a stream's SOURCE connection equals `connections.source` and each DESTINATION
  connection is one of `connections.destinations` (role-specific wiring);
- every connection names a `connector_id` present among the bundled connectors;
- every `scope='connection'` endpoint_ref resolves to exactly one bundled
  connection-scoped endpoint document;
- the bundle names a pipeline (`pipeline_id`) — a referential requirement.

Referential integrity is separate from RUNNABILITY. `require_runnable=True` (the
default) additionally gates the pipeline on `status='active'` with at least one
referenced stream that is itself runnable — the check an executor needs. An
authoring tool validating a **draft** bundle passes `require_runnable=False` to get
the referential checks WITHOUT the active-status gate.

The bundle is an IN-MEMORY set of already-parsed documents, independent of any
on-disk layout or storage. Stream and connection refs are matched on their base
form: a `{id}_v{n}` versioned ref and the bare `{id}` it pins resolve to the same
object, so a pipeline that pins `{stream}_v2` resolves the stream document that
declares the bare id. Connector identities are matched whole (their version is a
separate field, not a `_v{n}` ref suffix).

At import this module registers its detector -> validator pairs and its finding ids
with the core dispatch registry, so `_core` never hard-codes pipeline branches — a
new referential rule is a new function, a new kind is a new module.
"""
from __future__ import annotations

import re
from typing import Any

from ._core import (
    contract_model_domain,
    finding,
    register_kind,
    register_model_kind,
    register_validator_ids,
)

# Import the single-document contract model under the shared DOMAIN guard (the
# model binds the `$schema` host at import; see `contract_model_domain`).
with contract_model_domain():
    from pydantic import TypeAdapter
    from analitiq.contracts.pipelines.config import PipelineInput

_PIPELINE_ADAPTER = TypeAdapter(PipelineInput)

register_validator_ids({
    "bundle-pipeline",
    "bundle-stream-ref",
    "bundle-connection-ref",
    "bundle-connector-ref",
    "bundle-endpoint-ref",
})


# A trailing `_v{n}` version suffix selects a revision of an id; the base id is
# the object's identity. Referential matching is on the base, so `{id}_v2` and the
# bare `{id}` resolve to the same document — matching the published versioned-id
# contract (`{id}_v{version}`) and the consumers' own version-suffix stripping.
_VERSION_SUFFIX_RE = re.compile(r"_v\d+$")


def _base_id(ref: Any) -> Any:
    """Strip a trailing `_v{n}` version suffix; pass non-strings through."""
    if not isinstance(ref, str):
        return ref
    return _VERSION_SUFFIX_RE.sub("", ref)


def is_pipeline_bundle(doc: Any) -> bool:
    """A bundle is a mapping carrying a `pipeline` document plus its `streams` and
    `connections` collections — the assembled run inputs. Structurally distinct
    from every single-document kind (connector / endpoint / type-map / pipeline)."""
    return (
        isinstance(doc, dict)
        and isinstance(doc.get("pipeline"), dict)
        and "streams" in doc
        and "connections" in doc
    )


def is_pipeline_doc(doc: Any) -> bool:
    """A single pipeline document declares its source/destination wiring under
    `connections` and, unlike a bundle, carries no nested `pipeline` document. The
    bundle detector (registered first) claims the assembled-run shape, so a
    `connections`-bearing mapping that is not a bundle is a pipeline document."""
    return isinstance(doc, dict) and "connections" in doc and "pipeline" not in doc


# ---------------------------------------------------------------------------
# Field extraction (defensive — documents are already per-document validated)
# ---------------------------------------------------------------------------

def _pipeline_connection_ids(pipeline: dict) -> set[str]:
    """The pipeline's connection references (source + destinations), base-form."""
    conns = pipeline.get("connections")
    if not isinstance(conns, dict):
        return set()
    refs = (conns.get("source"), *(conns.get("destinations") or ()))
    return {_base_id(r) for r in refs if isinstance(r, str) and r}


def _connector_ids(connectors: Any) -> set[str]:
    """The connector identities present in the bundle.

    Matched verbatim, not base-form: a connector is referenced by its whole
    identity (slug or id), with the version carried separately — unlike a
    stream/connection ref, `foo_v2` and `foo_v3` are not the same connector.
    Accepts the identity set however the assembler holds it: a mapping keyed by
    connector id, or an iterable of id strings / connector-meta dicts.
    """
    ids: set[str] = set()
    if isinstance(connectors, dict):
        return {k for k in connectors if isinstance(k, str)}
    if isinstance(connectors, (list, tuple, set)):
        for item in connectors:
            if isinstance(item, str) and item:
                ids.add(item)
            elif isinstance(item, dict):
                for key in ("connector_id", "slug", "id"):
                    value = item.get(key)
                    if isinstance(value, str) and value:
                        ids.add(value)
                        break
    return ids


def _iter_endpoint_refs(streams: Any):
    """Yield `(json_path, endpoint_ref)` for each stream's source and destinations."""
    if not isinstance(streams, list):
        return
    for i, stream in enumerate(streams):
        if not isinstance(stream, dict):
            continue
        source = stream.get("source")
        if isinstance(source, dict) and isinstance(source.get("endpoint_ref"), dict):
            yield f"/streams/{i}/source/endpoint_ref", source["endpoint_ref"]
        destinations = stream.get("destinations")
        if isinstance(destinations, list):
            for k, dest in enumerate(destinations):
                if isinstance(dest, dict) and isinstance(dest.get("endpoint_ref"), dict):
                    yield f"/streams/{i}/destinations/{k}/endpoint_ref", dest["endpoint_ref"]


# ---------------------------------------------------------------------------
# Referential checks — each a pure function of the parsed bundle
# ---------------------------------------------------------------------------

def _check_stream_endpoint_targets(streams: Any) -> list[dict]:
    """Every stream slot carries an endpoint_ref — the stream's read/write target.

    A source (or destination) with no endpoint_ref object references nothing
    resolvable at load, so it is a referential failure rather than a skipped slot.
    (A non-dict stream document is reported by `_check_stream_refs`, which cannot
    find its `stream_id`.)
    """
    findings: list[dict] = []
    for i, stream in enumerate(streams if isinstance(streams, list) else []):
        if not isinstance(stream, dict):
            continue
        source = stream.get("source")
        if not (isinstance(source, dict) and isinstance(source.get("endpoint_ref"), dict)):
            findings.append(finding(
                "bundle-endpoint-ref", "error", f"/streams/{i}/source/endpoint_ref",
                "stream source has no endpoint_ref; it references no read target.",
            ))
        destinations = stream.get("destinations")
        if isinstance(destinations, list):
            for k, dest in enumerate(destinations):
                if not (isinstance(dest, dict) and isinstance(dest.get("endpoint_ref"), dict)):
                    findings.append(finding(
                        "bundle-endpoint-ref", "error",
                        f"/streams/{i}/destinations/{k}/endpoint_ref",
                        "stream destination has no endpoint_ref; it references no write target.",
                    ))
    return findings


def _check_pipeline_id(pipeline: dict) -> list[dict]:
    """The bundle must name a pipeline: stream parent refs and pipeline references
    resolve against `pipeline_id`. A REFERENTIAL requirement — always checked,
    independent of whether the pipeline is currently runnable."""
    pipeline_id = pipeline.get("pipeline_id")
    if not isinstance(pipeline_id, str) or not pipeline_id:
        return [finding(
            "bundle-pipeline", "error", "/pipeline/pipeline_id",
            "pipeline document has no pipeline_id; the bundle cannot be resolved to a pipeline.",
        )]
    return []


def _check_pipeline_active(pipeline: dict) -> list[dict]:
    """RUNNABILITY: only an `active` pipeline is executable. Gated by
    `require_runnable` — an authoring tool validating a draft bundle skips it."""
    status = pipeline.get("status")
    if status != "active":
        return [finding(
            "bundle-pipeline", "error", "/pipeline/status",
            f"pipeline status is {status!r}; only 'active' pipelines are runnable.",
        )]
    return []


def _check_pipeline_active_gate(pipeline: dict, streams: Any) -> list[dict]:
    """Per the pipeline contract's cross-field rules, an `active` pipeline must
    reference at least one stream AND at least one referenced stream must be
    runnable (`stream.status == 'active'`). (`draft`/`inactive` may be empty.)"""
    if pipeline.get("status") != "active":
        return []
    refs = pipeline.get("streams")
    referenced = {_base_id(r) for r in refs if isinstance(r, str) and r} if isinstance(refs, list) else set()
    if not referenced:
        return [finding(
            "bundle-pipeline", "error", "/pipeline/streams",
            "an active pipeline must reference at least one stream.",
        )]
    runnable = any(
        isinstance(s, dict)
        and s.get("status") == "active"
        and isinstance(s.get("stream_id"), str)
        and _base_id(s["stream_id"]) in referenced
        for s in (streams if isinstance(streams, list) else [])
    )
    if not runnable:
        return [finding(
            "bundle-pipeline", "error", "/streams",
            "an active pipeline requires at least one runnable stream "
            "(a referenced stream with status='active').",
        )]
    return []


def _check_stream_refs(pipeline: dict, streams: Any) -> list[dict]:
    """Every `pipeline.streams[]` ref resolves to exactly one bundled stream
    document (by declared id), with no duplicate refs and no duplicate documents."""
    findings: list[dict] = []
    streams = streams if isinstance(streams, list) else []

    declared: dict[str, int] = {}
    for i, stream in enumerate(streams):
        stream_id = stream.get("stream_id") if isinstance(stream, dict) else None
        if not isinstance(stream_id, str) or not stream_id:
            findings.append(finding(
                "bundle-stream-ref", "error", f"/streams/{i}/stream_id",
                "bundled stream document has no stream_id; no pipeline ref can resolve to it.",
            ))
            continue
        base = _base_id(stream_id)
        if base in declared:
            findings.append(finding(
                "bundle-stream-ref", "error", f"/streams/{i}/stream_id",
                f"stream id {base!r} is declared by two bundled stream documents "
                f"(indexes {declared[base]} and {i}).",
            ))
        else:
            declared[base] = i

    refs = pipeline.get("streams", [])
    if not isinstance(refs, list):
        findings.append(finding(
            "bundle-stream-ref", "error", "/pipeline/streams",
            f"pipeline.streams must be a list of stream references, got "
            f"{type(refs).__name__}; its stream refs cannot be resolved.",
        ))
        return findings
    seen: dict[str, str] = {}
    for j, ref in enumerate(refs):
        if not isinstance(ref, str) or not ref:
            continue  # ref shape is the pipeline model's job
        base = _base_id(ref)
        if base in seen:
            findings.append(finding(
                "bundle-stream-ref", "error", f"/pipeline/streams/{j}",
                f"pipeline.streams lists {ref!r} and {seen[base]!r}, which both resolve "
                f"to stream id {base!r}.",
            ))
        else:
            seen[base] = ref
        if base not in declared:
            findings.append(finding(
                "bundle-stream-ref", "error", f"/pipeline/streams/{j}",
                f"pipeline.streams references {ref!r} but no bundled stream document "
                f"declares id {base!r} (known: {sorted(declared)}).",
            ))
    return findings


def _check_stream_parent_pipeline(pipeline: dict, streams: Any) -> list[dict]:
    """Every bundled stream declares this pipeline as its parent.

    A stream carries an immutable parent `pipeline_id`; one bundled under a
    different pipeline would run under the wrong pipeline context, so a mismatch
    is a referential defect. (A stream that omits `pipeline_id` is a per-document
    shape defect the stream model reports.)
    """
    pipeline_id = pipeline.get("pipeline_id")
    if not isinstance(pipeline_id, str) or not pipeline_id:
        return []  # a missing pipeline id is already flagged by _check_pipeline_id
    parent = _base_id(pipeline_id)
    findings: list[dict] = []
    for i, stream in enumerate(streams if isinstance(streams, list) else []):
        if not isinstance(stream, dict):
            continue
        stream_parent = stream.get("pipeline_id")
        if not isinstance(stream_parent, str) or not stream_parent:
            continue  # shape is the stream model's job
        if _base_id(stream_parent) != parent:
            findings.append(finding(
                "bundle-stream-ref", "error", f"/streams/{i}/pipeline_id",
                f"stream declares pipeline_id {stream_parent!r} but is bundled under "
                f"pipeline {parent!r}; it belongs to a different pipeline.",
            ))
    return findings


def _check_connection_version_conflicts(pipeline: dict) -> list[dict]:
    """The pipeline must not pin two different versions of one connection base.

    Connections resolve by base id, so `source={X}_v1` with `destination={X}_v2`
    would collapse to a single connection at load and cannot represent both — an
    ambiguous run bundle.
    """
    conns = pipeline.get("connections")
    if not isinstance(conns, dict):
        return []
    refs = (conns.get("source"), *(conns.get("destinations") or ()))
    by_base: dict[str, str] = {}
    findings: list[dict] = []
    for ref in refs:
        if not isinstance(ref, str) or not ref:
            continue
        base = _base_id(ref)
        prior = by_base.get(base)
        if prior is not None and prior != ref:
            findings.append(finding(
                "bundle-connection-ref", "error", "/pipeline/connections",
                f"pipeline pins two versions of connection {base!r} ({prior!r} and "
                f"{ref!r}); connections resolve by base id and cannot represent both.",
            ))
        else:
            by_base[base] = ref
    return findings


def _check_connections_present(pipeline: dict, connections: Any) -> list[dict]:
    """Every referenced connection is present in the bundle exactly once.

    Two bundled documents whose ids collapse to the same base are ambiguous — the
    run layout keys one document per base connection — so a duplicate is flagged.
    """
    connections = connections if isinstance(connections, list) else []
    findings: list[dict] = []
    present: dict[str, int] = {}
    for i, conn in enumerate(connections):
        if not (isinstance(conn, dict) and isinstance(conn.get("connection_id"), str) and conn["connection_id"]):
            continue
        base = _base_id(conn["connection_id"])
        if base in present:
            findings.append(finding(
                "bundle-connection-ref", "error", f"/connections/{i}/connection_id",
                f"connection {base!r} is declared by two bundled documents "
                f"(indexes {present[base]} and {i}); the run layout keys one per base.",
            ))
        else:
            present[base] = i
    for cid in sorted(_pipeline_connection_ids(pipeline)):
        if cid not in present:
            findings.append(finding(
                "bundle-connection-ref", "error", "/pipeline/connections",
                f"pipeline references connection {cid!r} but no connection document for "
                f"it is bundled (known: {sorted(present)}).",
            ))
    return findings


def _check_stream_connection_roles(pipeline: dict, streams: Any) -> list[dict]:
    """Per the pipeline contract's cross-field rules, a stream's SOURCE connection
    must equal `pipeline.connections.source`, and each DESTINATION connection must
    be one of `pipeline.connections.destinations` (compared base-form). Wiring a
    read/write side to the wrong role is a referential defect even though the
    connection is present in the pipeline."""
    conns = pipeline.get("connections")
    conns = conns if isinstance(conns, dict) else {}
    src = conns.get("source")
    source_base = _base_id(src) if isinstance(src, str) and src else None
    dest_bases = {_base_id(d) for d in (conns.get("destinations") or []) if isinstance(d, str) and d}

    findings: list[dict] = []

    def _connection_id(slot: Any) -> str | None:
        ref = slot.get("endpoint_ref") if isinstance(slot, dict) else None
        cid = ref.get("connection_id") if isinstance(ref, dict) else None
        return cid if isinstance(cid, str) and cid else None

    for i, stream in enumerate(streams if isinstance(streams, list) else []):
        if not isinstance(stream, dict):
            continue
        source = stream.get("source")
        if isinstance(source, dict) and isinstance(source.get("endpoint_ref"), dict):
            cid = _connection_id(source)
            path = f"/streams/{i}/source/endpoint_ref/connection_id"
            if cid is None:
                findings.append(finding("bundle-connection-ref", "error", path,
                                        "source endpoint_ref names no connection_id."))
            elif source_base is None or _base_id(cid) != source_base:
                findings.append(finding(
                    "bundle-connection-ref", "error", path,
                    f"stream source connection {cid!r} must match the pipeline's "
                    f"connections.source ({src!r}).",
                ))
        for k, dest in enumerate(stream.get("destinations") or []):
            if not (isinstance(dest, dict) and isinstance(dest.get("endpoint_ref"), dict)):
                continue
            cid = _connection_id(dest)
            path = f"/streams/{i}/destinations/{k}/endpoint_ref/connection_id"
            if cid is None:
                findings.append(finding("bundle-connection-ref", "error", path,
                                        "destination endpoint_ref names no connection_id."))
            elif _base_id(cid) not in dest_bases:
                findings.append(finding(
                    "bundle-connection-ref", "error", path,
                    f"stream destination connection {cid!r} is not one of the pipeline's "
                    f"connections.destinations ({sorted(dest_bases)}).",
                ))
    return findings


def _check_connection_connector_refs(connections: Any, connectors: Any) -> list[dict]:
    """Every connection's `connector_id` is present among the bundled connectors."""
    connections = connections if isinstance(connections, list) else []
    present = _connector_ids(connectors)
    findings: list[dict] = []
    for i, conn in enumerate(connections):
        if not isinstance(conn, dict):
            continue
        connector_id = conn.get("connector_id")
        if not isinstance(connector_id, str) or not connector_id:
            findings.append(finding(
                "bundle-connector-ref", "error", f"/connections/{i}/connector_id",
                f"connection {conn.get('connection_id')!r} names no connector_id; "
                "its connector cannot be resolved.",
            ))
            continue
        if connector_id not in present:
            findings.append(finding(
                "bundle-connector-ref", "error", f"/connections/{i}/connector_id",
                f"connection {conn.get('connection_id')!r} references connector "
                f"{connector_id!r} but it is not among the bundled connectors "
                f"({sorted(present)}).",
            ))
    return findings


def _check_connection_scoped_endpoints(streams: Any, endpoints: Any) -> list[dict]:
    """Every `scope='connection'` endpoint_ref resolves to a bundled endpoint
    document keyed by `(connection_id, endpoint_id)`.

    `scope='connector'` refs are resolved from the connector's own endpoint set,
    not from the bundle's connection-scoped endpoint documents, so they are out of
    scope for this check.
    """
    findings: list[dict] = []
    present: set[tuple] = set()
    for j, endpoint in enumerate(endpoints if isinstance(endpoints, list) else []):
        if not isinstance(endpoint, dict):
            continue
        # Only a connection-scoped endpoint document satisfies a connection-scoped
        # ref; a connector-scoped entry does not. A missing scope defaults to
        # 'connection' (the only kind a bundle materializes) so minimal
        # `{connection_id, endpoint_id}` entries still count.
        if endpoint.get("scope", "connection") != "connection":
            continue
        cid, eid = endpoint.get("connection_id"), endpoint.get("endpoint_id")
        if not (isinstance(cid, str) and isinstance(eid, str)):
            continue
        key = (_base_id(cid), eid)
        if key in present:
            findings.append(finding(
                "bundle-endpoint-ref", "error", f"/endpoints/{j}",
                f"two bundled endpoint documents share (connection {key[0]!r}, endpoint "
                f"{key[1]!r}); the run layout keys one document per that pair.",
            ))
        else:
            present.add(key)

    for path, ref in _iter_endpoint_refs(streams):
        if ref.get("scope") != "connection":
            continue
        cid, eid = ref.get("connection_id"), ref.get("endpoint_id")
        if not isinstance(cid, str) or not cid:
            continue  # a missing connection_id is already flagged by the connection check
        if not isinstance(eid, str) or not eid:
            findings.append(finding(
                "bundle-endpoint-ref", "error", path,
                "connection-scoped endpoint_ref names no endpoint_id; its endpoint "
                "cannot be resolved.",
            ))
            continue
        if (_base_id(cid), eid) not in present:
            findings.append(finding(
                "bundle-endpoint-ref", "error", path,
                f"connection-scoped endpoint_ref (connection {_base_id(cid)!r}, endpoint "
                f"{eid!r}) has no matching bundled endpoint document.",
            ))
    return findings


# ---------------------------------------------------------------------------
# Aggregate + registration
# ---------------------------------------------------------------------------

def validate_pipeline_bundle(bundle: Any, *, require_runnable: bool = True) -> list[dict]:
    """Validate referential integrity across an assembled pipeline bundle.

    `bundle` is a mapping of the already-parsed documents:
    `{pipeline, streams, connections, connectors, endpoints}`. `connectors` and
    `endpoints` supply only identity — the connector ids present, and the
    connection-scoped endpoint documents (`connection_id` + `endpoint_id`). Returns
    a list of findings (empty == referentially sound); every referential defect is
    error severity.

    `require_runnable` (default True) additionally gates RUNNABILITY: the pipeline
    must be `status='active'` with at least one referenced stream that is itself
    runnable — the check an executor needs. An authoring tool validating a **draft**
    bundle passes `require_runnable=False` to get the referential checks WITHOUT the
    active-status gate (a draft is expected not to be active, not a defect); the
    always-checked referential rules — including that the bundle names a
    `pipeline_id` — still apply.
    """
    if not isinstance(bundle, dict):
        return [finding(
            "bundle-pipeline", "error", "/",
            "pipeline bundle must be a mapping of pipeline/streams/connections/"
            "connectors/endpoints.",
        )]
    pipeline = bundle.get("pipeline")
    if not isinstance(pipeline, dict):
        return [finding(
            "bundle-pipeline", "error", "/pipeline",
            "pipeline bundle is missing its 'pipeline' document.",
        )]
    streams = bundle.get("streams")
    connections = bundle.get("connections")
    findings: list[dict] = []
    findings += _check_pipeline_id(pipeline)
    if require_runnable:
        findings += _check_pipeline_active(pipeline)
        findings += _check_pipeline_active_gate(pipeline, streams)
    findings += _check_stream_refs(pipeline, streams)
    findings += _check_stream_parent_pipeline(pipeline, streams)
    findings += _check_stream_endpoint_targets(streams)
    findings += _check_connection_version_conflicts(pipeline)
    findings += _check_connections_present(pipeline, connections)
    findings += _check_stream_connection_roles(pipeline, streams)
    findings += _check_connection_connector_refs(connections, bundle.get("connectors"))
    findings += _check_connection_scoped_endpoints(streams, bundle.get("endpoints"))
    return findings


def _validate_pipeline_bundle(doc: Any, doc_path: Any = None,  # skipcq: PYL-W0613
                              schema_url: Any = None) -> list[dict]:  # skipcq: PYL-W0613 — uniform registered-validator signature; a bundle has no filesystem anchor
    """Kind entry point: dispatch a bundle document to the referential validator.

    A bundle carries no filesystem anchor, so `doc_path`/`schema_url` (the
    registry's per-kind signature) are unused here.
    """
    return validate_pipeline_bundle(doc)


# The bundle is registered BEFORE the single-pipeline document so the bundle
# detector claims an assembled-run mapping first; `is_pipeline_doc` then only sees
# a `connections`-bearing mapping with no nested `pipeline` document.
register_kind(is_pipeline_bundle, _validate_pipeline_bundle)
register_model_kind(is_pipeline_doc, _PIPELINE_ADAPTER)
