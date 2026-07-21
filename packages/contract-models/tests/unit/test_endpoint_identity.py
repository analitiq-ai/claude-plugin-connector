"""Unit tests for deterministic database endpoint-id derivation (#918).

Pins the exact algorithm from the endpoint-identity contract model
(``analitiq.contracts.endpoint_identity`` — ``derive_db_endpoint_id`` /
``db_hash8`` / ``slug``). The hash suffixes below are the real SHA-256
prefixes over the verbatim ``catalog.schema.name`` payload — hard-coded so any
change to the derivation (slug rule, payload order, hash length) fails loudly
instead of silently re-minting every database endpoint id.
"""
from __future__ import annotations

import re

import pytest

from analitiq.contracts.endpoint_identity import (
    ENDPOINT_SLUG_SEPARATOR,
    db_hash8,
    derive_db_endpoint_id,
    slug,
)
from analitiq.contracts.endpoints import SLUG_RE



class TestSlug:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("public", "public"),
            ("Sales", "sales"),
            ("Order Items", "order_items"),
            ("order_items", "order_items"),  # single "_" is preserved
            ('a."b"', "a_b"),
            ("  spaced  ", "spaced"),  # leading/trailing runs trimmed
            ("a---b__c", "a_b_c"),  # each out-of-charset run collapses to one "_"
            ("***", ""),  # entirely out of charset -> empty
            ("Naïve", "na_ve"),  # unicode folded then non-ascii mapped
        ],
    )
    def test_slug(self, raw: str, expected: str) -> None:
        assert slug(raw) == expected


class TestDbHash8:
    def test_is_eight_lowercase_hex(self) -> None:
        h = db_hash8(None, "public", "orders")
        assert re.fullmatch(r"[0-9a-f]{8}", h)

    def test_fixed_three_slot_payload_disambiguates_shifts(self) -> None:
        # A three-slot payload keeps segment boundaries unambiguous: moving a
        # value between catalog/schema/name must change the hash.
        assert db_hash8("a", "b", "c") != db_hash8(None, "a", "b.c")
        assert db_hash8(None, "a", "b") != db_hash8("a", None, "b")

    def test_verbatim_case_sensitive(self) -> None:
        # The hash is over the EXACT verbatim identifier — same slug, different
        # casing must hash differently (no collision).
        assert db_hash8(None, "Sales", "Order Items") != db_hash8(
            None, "Sales", "order_items"
        )

    def test_delimiter_in_identifier_does_not_collide(self) -> None:
        # A delimiter-joined payload would be ambiguous when an identifier
        # contains the delimiter: (schema="x.", name="y") and (schema="x",
        # name=".y") must NOT hash the same. The JSON-array encoding keeps them
        # distinct (regression guard for the collision-safety contract).
        assert db_hash8(None, "x.", "y") != db_hash8(None, "x", ".y")
        # And the full derived ids (same slug `x__y`) stay distinct.
        assert derive_db_endpoint_id(None, "x.", "y") != derive_db_endpoint_id(
            None, "x", ".y"
        )


class TestDeriveDbEndpointId:
    def test_spec_worked_examples(self) -> None:
        # Real hashes over the JSON array [null, "<schema>", "<name>"] (catalog absent).
        assert (
            derive_db_endpoint_id(None, "Sales", "Order Items")
            == "sales__order_items__0e62f7e9"
        )
        assert (
            derive_db_endpoint_id(None, "Sales", "order_items")
            == "sales__order_items__ce7aee55"
        )
        assert (
            derive_db_endpoint_id(None, "public", "orders")
            == "public__orders__371c8422"
        )

    def test_same_slug_different_verbatim_no_collision(self) -> None:
        a = derive_db_endpoint_id(None, "Sales", "Order Items")
        b = derive_db_endpoint_id(None, "Sales", "order_items")
        assert a != b
        # identical legible slug, distinct hash suffix
        assert a.rsplit(ENDPOINT_SLUG_SEPARATOR, 1)[0] == "sales__order_items"
        assert b.rsplit(ENDPOINT_SLUG_SEPARATOR, 1)[0] == "sales__order_items"

    def test_catalog_appended_last_when_present(self) -> None:
        eid = derive_db_endpoint_id("mydb", "public", "orders")
        assert eid == "public__orders__mydb__bfd0c93c"

    def test_schema_omitted_when_absent(self) -> None:
        assert derive_db_endpoint_id(None, None, "orders") == "orders__e53bb11a"

    def test_empty_slug_dropped_but_hash_disambiguates(self) -> None:
        # An all-out-of-charset table slugs to "" and is dropped; the hash keeps
        # the id unique and non-empty.
        eid = derive_db_endpoint_id(None, "public", "***")
        assert eid == "public__6062447c"

    def test_all_slugs_empty_yields_bare_hash(self) -> None:
        eid = derive_db_endpoint_id(None, "***", "***")
        assert re.fullmatch(r"[0-9a-f]{8}", eid)

    def test_idempotent(self) -> None:
        first = derive_db_endpoint_id(None, "public", "orders")
        second = derive_db_endpoint_id(None, "public", "orders")
        assert first == second

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="name is required"):
            derive_db_endpoint_id(None, "public", "")

    @pytest.mark.parametrize(
        "catalog,schema,name",
        [
            (None, "public", "orders"),
            ("mydb", "public", "orders"),
            (None, None, "orders"),
            (None, "Sales", "Order Items"),
            (None, "Sales", "Naïve Café"),
            (None, "***", "***"),
            (None, "public", "***"),
        ],
    )
    def test_always_matches_published_endpoint_id_pattern(
        self, catalog: str | None, schema: str | None, name: str
    ) -> None:
        # The derived handle must satisfy the published endpoint_id regex the
        # DatabaseEndpointDoc / EndpointRef validators enforce.
        assert SLUG_RE.fullmatch(derive_db_endpoint_id(catalog, schema, name))
