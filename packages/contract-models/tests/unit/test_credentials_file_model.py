"""Public `credentials` sidecar contract tests (#951).

`CredentialsFile` is the source of the public `credentials` JSON Schema: the
flat `{name: value}` shape of a connection's local `credentials.json`. The
connection document references each value by name via `secret_refs.<name>`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from analitiq.contracts.credentials_file import CredentialsFile


REPO_ROOT = Path(__file__).resolve().parents[4]
CREDENTIALS_SCHEMA_PATH = REPO_ROOT / "schemas" / "credentials" / "latest.json"


def test_credentials_round_trip_flat_map():
    """A flat name→value map round-trips through the model unchanged."""
    sample = {
        "password": "s3cr3t",
        "api_key": "AKIAEXAMPLE",
        # A structured credential (e.g. a service-account key) is supplied as a
        # JSON-encoded string, since the engine string-coerces every value.
        "service_account": json.dumps({"type": "service_account", "project_id": "p"}),
    }
    model = CredentialsFile.model_validate(sample)
    assert model.model_dump() == sample


def test_credentials_accepts_empty_map():
    assert CredentialsFile.model_validate({}).model_dump() == {}


def test_credentials_accepts_typed_values():
    """Values may be any JSON type: a connector may declare a non-string secret,
    the write path stores it verbatim, and the engine string-coerces on read
    (#951 review). The contract must not reject such a valid file."""
    sample = {"pin": 1234, "enabled": True, "ratio": 0.5}
    assert CredentialsFile.model_validate(sample).model_dump() == sample


def test_credentials_rejects_non_object_root():
    with pytest.raises(ValidationError):
        CredentialsFile.model_validate(["not", "an", "object"])


def test_published_schema_is_flat_open_map():
    schema = json.loads(CREDENTIALS_SCHEMA_PATH.read_text())
    assert schema["type"] == "object"
    # Open map: any key, any JSON value (the engine string-coerces on read).
    assert schema["additionalProperties"] is True
    assert "properties" not in schema
