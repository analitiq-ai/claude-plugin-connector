"""Tests for the type-map gap prober (plugins/analitiq-pipeline-builder/scripts/type_map_gaps.py).

The script holds no matching logic — resolution dispatches to the pinned
`analitiq-validator`'s internal helpers (the exact semantics every runtime
reader uses); these tests exercise them against the in-repo source, which moves
in lockstep with the pin. They pin the *wiring*: probe/rule routing per
direction, map precedence (connection primary over connector fallback,
mirroring the engine's `TypeMapper.compose`), per-map model validation, gap
reporting, and the CLI envelope.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "plugins" / "analitiq-pipeline-builder"
sys.path.insert(0, str(ROOT / "scripts"))
import type_map_gaps as G  # noqa: E402

pytest.importorskip("analitiq.validator",
                    reason="requires: pip install -r requirements-dev.txt")

CONNECTOR_READ = [
    {"match": "exact", "native": "CITEXT", "canonical": "Utf8"},
    {"match": "regex", "native": "^NUMERIC\\((?<precision>[0-9]+),\\s*(?<scale>[0-9]+)\\)$",
     "canonical": "Decimal128(${precision}, ${scale})"},
]
CONNECTOR_WRITE = [
    {"match": "exact", "canonical": "Utf8", "native": "TEXT"},
    {"match": "regex", "canonical": "^Decimal(128|256)\\((?<p>\\d+),\\s*(?<s>\\d+)\\)$",
     "native": "NUMERIC(${p}, ${s})"},
]


def _map(tmp_path: Path, name: str, rules: list) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(rules))
    return p


def test_read_exact_is_normalized(tmp_path):
    # read-side probes are normalized (trim/collapse/uppercase) before matching
    result = G.resolve("read", ["citext", "  Citext  "], [_map(tmp_path, "r.json", CONNECTOR_READ)])
    assert result["resolved"] == {"citext": "Utf8", "  Citext  ": "Utf8"}
    assert result["gaps"] == []


def test_read_regex_substitutes_captures(tmp_path):
    result = G.resolve("read", ["numeric(10,2)"], [_map(tmp_path, "r.json", CONNECTOR_READ)])
    assert result["resolved"]["numeric(10,2)"] == "Decimal128(10, 2)"


def test_read_gap_reported(tmp_path):
    result = G.resolve("read", ["vector(3)", "citext"], [_map(tmp_path, "r.json", CONNECTOR_READ)])
    assert result["resolved"]["vector(3)"] is None
    assert result["gaps"] == ["vector(3)"]


def test_read_connection_map_is_primary(tmp_path):
    # maps concatenate in argument order, first match wins — the engine's compose order
    connection = _map(tmp_path, "conn.json",
                      [{"match": "exact", "native": "CITEXT", "canonical": "LargeUtf8"}])
    connector = _map(tmp_path, "base.json", CONNECTOR_READ)
    result = G.resolve("read", ["citext"], [connection, connector])
    assert result["resolved"]["citext"] == "LargeUtf8"


def test_write_matches_canonical_case_preserving(tmp_path):
    m = _map(tmp_path, "w.json", CONNECTOR_WRITE)
    result = G.resolve("write", ["Utf8", "utf8", "Decimal128(20, 4)"], [m])
    # write matchers compare the canonical as authored — no normalization
    assert result["resolved"]["Utf8"] == "TEXT"
    assert result["resolved"]["utf8"] is None
    assert result["resolved"]["Decimal128(20, 4)"] == "NUMERIC(20, 4)"
    assert result["gaps"] == ["utf8"]


def test_write_gap_reported(tmp_path):
    result = G.resolve("write", ["List<Float32>"], [_map(tmp_path, "w.json", CONNECTOR_WRITE)])
    assert result["gaps"] == ["List<Float32>"]


def test_non_array_map_rejected(tmp_path):
    bad = tmp_path / "r.json"
    bad.write_text('{"match": "exact"}')
    with pytest.raises(ValueError, match="not a JSON array"):
        G.resolve("read", ["citext"], [bad])


def test_cli_end_to_end(tmp_path, capsys):
    m = _map(tmp_path, "type-map-read.json", CONNECTOR_READ)
    probes = tmp_path / "probes.json"
    probes.write_text(json.dumps(["citext", "vector(3)"]))
    rc = G.main(["--direction", "read", "--map", str(m), "--probes-file", str(probes)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"direction": "read",
                   "resolved": {"citext": "Utf8", "vector(3)": None},
                   "gaps": ["vector(3)"]}


@pytest.mark.parametrize("probes_payload", ['{"not": "a list"}', '["ok", 1]', "[ broken"])
def test_cli_rejects_bad_probes(tmp_path, capsys, probes_payload):
    m = _map(tmp_path, "type-map-read.json", CONNECTOR_READ)
    probes = tmp_path / "probes.json"
    probes.write_text(probes_payload)
    rc = G.main(["--direction", "read", "--map", str(m), "--probes-file", str(probes)])
    assert rc == 2
    assert json.loads(capsys.readouterr().out or "null") is None  # nothing on stdout


def test_cli_missing_map_is_an_error(tmp_path, capsys):
    probes = tmp_path / "probes.json"
    probes.write_text('["citext"]')
    rc = G.main(["--direction", "read", "--map", str(tmp_path / "absent.json"),
                 "--probes-file", str(probes)])
    assert rc == 2
    assert not capsys.readouterr().out


def test_duplicate_probes_deduped(tmp_path):
    result = G.resolve("read", ["vector(3)", "vector(3)"], [_map(tmp_path, "r.json", CONNECTOR_READ)])
    assert result["gaps"] == ["vector(3)"]
    assert list(result["resolved"]) == ["vector(3)"]


def test_malformed_rule_fails_loud(tmp_path):
    # the resolver mirrors runtime semantics and SKIPS a malformed rule — which
    # would surface as a false "gap" and drive the agent to shadow the rule the
    # map intended. The prober must therefore refuse the map outright, naming it.
    bad = _map(tmp_path, "r.json", [{"match": "exact", "native": "CITEXT"}])  # no canonical
    with pytest.raises(ValueError, match="r.json"):
        G.resolve("read", ["citext"], [bad])


def test_map_direction_must_match_model(tmp_path):
    # a write regex rule's canonical is a matcher pattern — invalid as a read
    # rule's rendered Arrow type — so model validation per --direction catches a
    # write map probed as read even under a neutral filename. (The reverse is not
    # always model-detectable — rule shapes are symmetric for exact rules — which
    # is what the CLI filename gate is for.)
    with pytest.raises(ValueError, match="not a valid read type map"):
        G.resolve("read", ["citext"], [_map(tmp_path, "m.json", CONNECTOR_WRITE)])


def test_cli_rejects_direction_filename_mismatch(tmp_path, capsys):
    m = _map(tmp_path, "type-map-read.json", CONNECTOR_READ)
    probes = tmp_path / "probes.json"
    probes.write_text('["Utf8"]')
    rc = G.main(["--direction", "write", "--map", str(m), "--probes-file", str(probes)])
    assert rc == 2
    err = capsys.readouterr()
    assert not err.out
    assert "read-direction map" in err.err


def test_cli_parse_error_names_the_file(tmp_path, capsys):
    bad = tmp_path / "broken.json"
    bad.write_text("[ not json")
    probes = tmp_path / "probes.json"
    probes.write_text('["citext"]')
    rc = G.main(["--direction", "read", "--map", str(bad), "--probes-file", str(probes)])
    assert rc == 2
    assert "broken.json" in capsys.readouterr().err


def test_cli_reads_probes_from_stdin(tmp_path, capsys, monkeypatch):
    # stdin is the documented primary invocation (spec-type-map-gaps.md)
    import io
    m = _map(tmp_path, "type-map-read.json", CONNECTOR_READ)
    monkeypatch.setattr("sys.stdin", io.StringIO('["citext"]'))
    rc = G.main(["--direction", "read", "--map", str(m)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["resolved"] == {"citext": "Utf8"}
