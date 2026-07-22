"""Unit tests for analitiq.contracts.value_expression.resolve_value_expression.

Covers the warning logged when a dict value has none of the recognised keys
(template, function, literal, ref) — issue #607 — and confirms that
recognised forms and non-dict passthrough values do not produce spurious warnings.

Also covers base64_encode / url_encode dropping (returning None) on
unresolved input instead of encoding an empty string — issue #627,
lookup warning on unresolved input / key miss — issue #633, and
resolve_template_deep dispatching expression-form nodes in bodies —
issue #634.
"""
from __future__ import annotations

import base64

import pytest
from analitiq.contracts.value_expression import (
    OAUTH_TOKEN_PAYLOAD_KEY,
    apply_operation_content_type,
    build_resolution_context,
    resolve_body_template,
    resolve_operation_url,
    resolve_template_deep,
    resolve_template_string,
    resolve_transport_base_url,
    resolve_value_expression,
)



class TestResolveValueExpressionUnrecognisedForm:
    def test_returns_none_for_unrecognised_dict(self):
        result = resolve_value_expression({"unknown_key": "value"}, {})
        assert result is None

    def test_logs_warning_for_unrecognised_dict(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            resolve_value_expression({"no_recognised_key": 42}, {})
        assert any("unrecognised object form" in r.message for r in caplog.records)

    def test_warning_includes_keys(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            resolve_value_expression({"foo": 1, "bar": 2}, {})
        msgs = [r.getMessage() for r in caplog.records if "unrecognised object form" in r.message]
        assert msgs, "expected at least one warning"
        assert "foo" in msgs[0] and "bar" in msgs[0]

    def test_no_warning_for_recognised_template(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            resolve_value_expression({"template": "hello"}, {})
        assert not any("unrecognised object form" in r.message for r in caplog.records)

    def test_no_warning_for_recognised_literal(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            resolve_value_expression({"literal": "value"}, {})
        assert not any("unrecognised object form" in r.message for r in caplog.records)

    def test_no_warning_for_recognised_ref(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            resolve_value_expression({"ref": "secrets.key"}, {"secrets": {"key": "v"}})
        assert not any("unrecognised object form" in r.message for r in caplog.records)

    def test_no_warning_for_recognised_ref_when_path_unresolvable(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            result = resolve_value_expression({"ref": "missing.path"}, {})
        assert result is None
        assert not any("unrecognised object form" in r.message for r in caplog.records)

    def test_no_warning_for_recognised_function(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            resolve_value_expression({"function": "base64_encode", "input": "hello"}, {})
        assert not any("unrecognised object form" in r.message for r in caplog.records)

    def test_empty_dict_logs_warning(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            result = resolve_value_expression({}, {})
        assert result is None
        assert any("unrecognised object form" in r.message for r in caplog.records)


class TestEncodeFunctionsUnresolvedInput:
    """base64_encode / url_encode must return None (drop the field) when their
    input is absent or unresolvable, instead of silently encoding "" — #627.
    Mirrors the basic_auth behaviour pinned in
    test_connections_auth_request_headers.py.
    """

    def test_base64_encode_resolves_input(self):
        result = resolve_value_expression(
            {"function": "base64_encode", "input": {"ref": "secrets.key"}},
            {"secrets": {"key": "hello"}},
        )
        assert result == base64.b64encode(b"hello").decode()

    def test_base64_encode_returns_none_when_input_absent(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            result = resolve_value_expression({"function": "base64_encode"}, {})
        assert result is None
        assert any("base64_encode dropped" in r.message for r in caplog.records)

    def test_base64_encode_returns_none_when_ref_unresolvable(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            result = resolve_value_expression(
                {"function": "base64_encode", "input": {"ref": "secrets.missing"}}, {}
            )
        assert result is None
        assert any("base64_encode dropped" in r.message for r in caplog.records)

    def test_url_encode_resolves_input(self):
        result = resolve_value_expression(
            {"function": "url_encode", "input": {"literal": "a b/c"}}, {}
        )
        assert result == "a%20b%2Fc"

    def test_url_encode_respects_safe(self):
        result = resolve_value_expression(
            {"function": "url_encode", "input": {"literal": "a b/c"}, "safe": "/"}, {}
        )
        assert result == "a%20b/c"

    def test_url_encode_returns_none_when_input_absent(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            result = resolve_value_expression({"function": "url_encode"}, {})
        assert result is None
        assert any("url_encode dropped" in r.message for r in caplog.records)

    def test_url_encode_returns_none_when_ref_unresolvable(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            result = resolve_value_expression(
                {"function": "url_encode", "input": {"ref": "secrets.missing"}}, {}
            )
        assert result is None
        assert any("url_encode dropped" in r.message for r in caplog.records)

    def test_base64_encode_returns_none_when_template_input_unresolved(self, caplog):
        # An unresolved `${...}` placeholder substitutes "" in template forms,
        # which must read as unresolved here — not get empty-encoded.
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            bare = resolve_value_expression(
                {"function": "base64_encode", "input": "${secrets.missing}"}, {}
            )
            wrapped = resolve_value_expression(
                {"function": "base64_encode", "input": {"template": "${secrets.missing}"}},
                {},
            )
        assert bare is None
        assert wrapped is None
        assert any("base64_encode dropped" in r.message for r in caplog.records)

    def test_url_encode_returns_none_when_template_input_unresolved(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            result = resolve_value_expression(
                {"function": "url_encode", "input": {"template": "${secrets.missing}"}},
                {},
            )
        assert result is None
        assert any("url_encode dropped" in r.message for r in caplog.records)

    def test_basic_auth_returns_none_when_template_input_unresolved(self, caplog):
        # Same hole applied to basic_auth: a username template resolving to ""
        # must drop the header, not send `Basic base64(":pass")`.
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            result = resolve_value_expression(
                {
                    "function": "basic_auth",
                    "input": {
                        "username": "${secrets.missing}",
                        "password": {"literal": "pass"},
                    },
                },
                {},
            )
        assert result is None
        assert any("basic_auth dropped" in r.message for r in caplog.records)

    def test_resolved_template_input_is_encoded(self):
        result = resolve_value_expression(
            {"function": "base64_encode", "input": "${secrets.key}"},
            {"secrets": {"key": "hello"}},
        )
        assert result == base64.b64encode(b"hello").decode()

    def test_returns_none_when_template_input_partially_unresolved(self, caplog):
        # A template mixing static text with a failed placeholder resolves to
        # a non-empty partial string ("Bearer ") — still garbage; any failed
        # placeholder must mark the whole input unresolved.
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            b64 = resolve_value_expression(
                {"function": "base64_encode", "input": "Bearer ${secrets.missing}"},
                {},
            )
            url = resolve_value_expression(
                {"function": "url_encode", "input": {"template": "key=${missing}"}},
                {},
            )
        assert b64 is None
        assert url is None
        assert any("base64_encode dropped" in r.message for r in caplog.records)
        assert any("url_encode dropped" in r.message for r in caplog.records)

    def test_resolved_mixed_template_input_is_encoded(self):
        result = resolve_value_expression(
            {"function": "url_encode", "input": "Bearer ${secrets.key}"},
            {"secrets": {"key": "hello"}},
        )
        assert result == "Bearer%20hello"

    def test_explicit_empty_literal_is_encoded_not_dropped(self, caplog):
        # The drop guard is `is None`, not falsiness: an explicitly provided
        # empty-string literal is a resolved value and must still encode.
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            b64 = resolve_value_expression(
                {"function": "base64_encode", "input": {"literal": ""}}, {}
            )
            url = resolve_value_expression(
                {"function": "url_encode", "input": {"literal": ""}}, {}
            )
        assert b64 == ""
        assert url == ""
        assert not any("dropped" in r.message for r in caplog.records)

    def test_explicit_empty_plain_string_is_encoded_not_dropped(self, caplog):
        # A plain JSON string "" with no ${...} placeholders is an explicitly
        # authored value, not an unresolved template — it must still encode.
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            b64 = resolve_value_expression(
                {"function": "base64_encode", "input": ""}, {}
            )
            url = resolve_value_expression({"function": "url_encode", "input": ""}, {})
        assert b64 == ""
        assert url == ""
        assert not any("dropped" in r.message for r in caplog.records)


class TestLookupFunction:
    """lookup must warn + return None on unresolved input and on a key miss,
    instead of silently returning None for both — #633.
    """

    CONTEXT = {"connection": {"parameters": {"environment": "sandbox"}}}
    SPEC = {
        "function": "lookup",
        "input": {"ref": "connection.parameters.environment"},
        "map": {"sandbox": "https://sandbox.example.com", "production": "https://api.example.com"},
    }

    def test_resolves_mapped_value(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            result = resolve_value_expression(self.SPEC, self.CONTEXT)
        assert result == "https://sandbox.example.com"
        assert not any("lookup dropped" in r.message for r in caplog.records)

    def test_returns_none_when_input_absent(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            result = resolve_value_expression(
                {"function": "lookup", "map": {"a": "b"}}, {}
            )
        assert result is None
        assert any("lookup dropped — unresolved input" in r.getMessage() for r in caplog.records)

    def test_returns_none_when_ref_unresolvable(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            result = resolve_value_expression(
                {"function": "lookup", "input": {"ref": "missing.path"}, "map": {"a": "b"}},
                {},
            )
        assert result is None
        assert any("lookup dropped — unresolved input" in r.getMessage() for r in caplog.records)

    def test_unresolved_template_input_warns_as_unresolved_not_key_miss(self, caplog):
        # A template input with a failed placeholder is an unresolved input
        # (#627 semantics), not a lookup of the partial string against the map.
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            result = resolve_value_expression(
                {"function": "lookup", "input": "${secrets.missing}", "map": {"a": "b"}},
                {},
            )
        assert result is None
        assert any("lookup dropped — unresolved input" in r.getMessage() for r in caplog.records)
        assert not any("not in map" in r.getMessage() for r in caplog.records)

    def test_returns_none_when_key_not_in_map(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            result = resolve_value_expression(
                self.SPEC, {"connection": {"parameters": {"environment": "staging"}}}
            )
        assert result is None
        msgs = [r.getMessage() for r in caplog.records if "lookup dropped" in r.message]
        assert msgs and "not in map" in msgs[0] and "staging" in msgs[0]

    def test_returns_none_when_map_absent(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            result = resolve_value_expression(
                {"function": "lookup", "input": {"literal": "a"}}, {}
            )
        assert result is None
        assert any("not in map" in r.getMessage() for r in caplog.records)

    def test_returns_none_when_input_resolves_to_non_scalar(self, caplog):
        # A ref resolving to a dict/list is a connector authoring error (map
        # keys are scalars) — it must drop with a warning, not raise TypeError
        # on the unhashable membership test.
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            for bad in ({"nested": "dict"}, ["a", "list"]):
                result = resolve_value_expression(
                    {"function": "lookup", "input": {"ref": "connection.parameters"}, "map": {"a": "b"}},
                    {"connection": {"parameters": bad}},
                )
                assert result is None
        msgs = [r.getMessage() for r in caplog.records if "non-scalar input" in r.message]
        assert len(msgs) == 2 and "dict" in msgs[0] and "list" in msgs[1]

    def test_falsy_map_values_pass_through(self, caplog):
        # The key-miss guard is membership, not truthiness: explicit falsy map
        # values ("", 0, False) are resolved values and must not be dropped.
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            spec = {"function": "lookup", "input": {"literal": "k"}, "map": {"k": ""}}
            assert resolve_value_expression(spec, {}) == ""
            spec["map"] = {"k": 0}
            assert resolve_value_expression(spec, {}) == 0
            spec["map"] = {"k": False}
            assert resolve_value_expression(spec, {}) is False
        assert not any("lookup dropped" in r.message for r in caplog.records)


class TestResolveTemplateDeepExpressionNodes:
    """resolve_template_deep must dispatch expression-form dicts (template /
    function / literal / ref) through resolve_value_expression instead of
    passing them through as raw structural JSON — #634.
    """

    def test_function_node_in_body_is_resolved(self):
        body = {
            "grant_type": "client_credentials",
            "client_secret": {"function": "base64_encode", "input": {"ref": "secrets.client_secret"}},
        }
        result = resolve_template_deep(body, {"secrets": {"client_secret": "hush"}})
        assert result == {
            "grant_type": "client_credentials",
            "client_secret": base64.b64encode(b"hush").decode(),
        }

    def test_ref_node_preserves_native_type(self):
        body = {"profile_id": {"ref": "connection.selections.profile_id"}}
        result = resolve_template_deep(
            body, {"connection": {"selections": {"profile_id": 123456}}}
        )
        assert result == {"profile_id": 123456}

    def test_literal_node_passes_value_through(self):
        # `literal` is also the escape hatch for a structural object whose
        # field names collide with the reserved expression keys: it passes
        # the wrapped value through verbatim.
        body = {"payload": {"literal": {"template": "welcome-email"}}}
        assert resolve_template_deep(body, {}) == {"payload": {"template": "welcome-email"}}

    def test_template_node_is_resolved(self):
        body = {"audience": {"template": "https://${connection.parameters.region}.example.com"}}
        result = resolve_template_deep(
            body, {"connection": {"parameters": {"region": "eu"}}}
        )
        assert result == {"audience": "https://eu.example.com"}

    def test_unresolved_function_node_drops_field_with_warning(self, caplog):
        body = {
            "grant_type": "client_credentials",
            "client_secret": {"function": "base64_encode", "input": {"ref": "secrets.missing"}},
        }
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            result = resolve_template_deep(body, {})
        assert result == {"grant_type": "client_credentials"}
        assert any(
            "dropping field 'client_secret'" in r.getMessage() for r in caplog.records
        )

    def test_unresolved_ref_node_drops_field_with_warning(self, caplog):
        body = {"keep": "static", "drop": {"ref": "missing.path"}}
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            result = resolve_template_deep(body, {})
        assert result == {"keep": "static"}
        assert any("dropping field 'drop'" in r.getMessage() for r in caplog.records)

    def test_unresolved_expression_in_list_drops_item_with_warning(self, caplog):
        body = {"scopes": [{"ref": "missing.path"}, {"literal": "read"}, "write"]}
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            result = resolve_template_deep(body, {})
        assert result == {"scopes": ["read", "write"]}
        assert any("dropping list item" in r.getMessage() for r in caplog.records)

    def test_structural_dict_recurses_and_null_is_preserved(self, caplog):
        # Plain structural JSON keeps current semantics: nested dicts recurse,
        # and an authored JSON null is a value, not an unresolved expression.
        body = {"meta": {"nested": "${secrets.key}", "explicit_null": None}}
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            result = resolve_template_deep(body, {"secrets": {"key": "v"}})
        assert result == {"meta": {"nested": "v", "explicit_null": None}}
        assert not any("dropping" in r.message for r in caplog.records)

    def test_expression_node_nested_in_list_of_objects(self):
        body = {"items": [{"name": "a", "secret": {"ref": "secrets.key"}}]}
        result = resolve_template_deep(body, {"secrets": {"key": "v"}})
        assert result == {"items": [{"name": "a", "secret": "v"}]}

    def test_top_level_expression_node_is_resolved(self):
        assert resolve_template_deep({"ref": "secrets.key"}, {"secrets": {"key": "v"}}) == "v"

    def test_top_level_unresolved_expression_returns_none_with_warning(self, caplog):
        # A missed top-level `ref` has no inner warning and no enclosing field
        # to report a drop for — without this breadcrumb the whole payload
        # nulls silently.
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            assert resolve_template_deep({"ref": "missing.path"}, {}) is None
        assert any(
            "top-level expression resolved to None" in r.message for r in caplog.records
        )

    def test_literal_null_field_is_dropped(self, caplog):
        # Pinned limitation: this layer cannot distinguish "unresolved" from
        # "expression that resolved to null", so `{"literal": null}` drops its
        # field — unlike a structural null, which is preserved.
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            result = resolve_template_deep({"keep": None, "drop": {"literal": None}}, {})
        assert result == {"keep": None}
        assert any("dropping field 'drop'" in r.getMessage() for r in caplog.records)

    def test_plain_placeholder_strings_still_resolve(self):
        body = {"client_id": "${secrets.client_id}"}
        result = resolve_template_deep(body, {"secrets": {"client_id": "abc"}})
        assert result == {"client_id": "abc"}

    def test_non_string_scalars_unchanged(self):
        body = {"count": 5, "active": True, "ratio": 1.5}
        assert resolve_template_deep(body, {}) == body


class TestResolveValueExpressionPassthrough:
    def test_integer_is_returned_unchanged(self):
        assert resolve_value_expression(42, {}) == 42

    def test_none_is_returned_unchanged(self):
        assert resolve_value_expression(None, {}) is None

    def test_bool_is_returned_unchanged(self):
        assert resolve_value_expression(True, {}) is True

    def test_list_is_returned_unchanged(self):
        val = ["a", "b"]
        assert resolve_value_expression(val, {}) is val

    def test_no_warning_for_passthrough_values(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            resolve_value_expression(42, {})
            resolve_value_expression(None, {})
            resolve_value_expression(True, {})
        assert not any("unrecognised object form" in r.message for r in caplog.records)


class TestBuildResolutionContext:
    """Scoped-context assembly from a connection record + secret blob (#643).

    The opaque OAuth token payload (reserved blob entry) is the `auth`
    scope: every field at `auth.<field>`, the whole payload nested at
    `auth.token_response.<field>`, and the reserved key never leaks into
    the `secrets` scope. Platform-provisioned contract inputs project into
    the scope their declared storage names.
    """

    _CONNECTION = {
        "parameters": {"region": "eu"},
        "selections": {"tenantId": "tnt-1"},
        "discovered": {"org_name": "Acme"},
    }

    def test_payload_fields_resolve_as_auth_scope(self):
        ctx = build_resolution_context(
            connection=self._CONNECTION,
            secrets={OAUTH_TOKEN_PAYLOAD_KEY: {
                "access_token": "AT", "refresh_token": "RT",
                "api_domain": "https://accounts.zoho.eu",
            }},
        )
        assert resolve_template_string("Bearer ${auth.access_token}", ctx) == "Bearer AT"
        assert resolve_template_string("${auth.refresh_token}", ctx) == "RT"
        # A field nobody declared anywhere is still usable — no taxonomy.
        assert resolve_template_string("${auth.api_domain}", ctx) == "https://accounts.zoho.eu"
        # The whole payload also nests under auth.token_response.
        assert resolve_template_string(
            "${auth.token_response.api_domain}", ctx
        ) == "https://accounts.zoho.eu"

    def test_reserved_key_never_leaks_into_secrets_scope(self):
        ctx = build_resolution_context(
            connection={},
            secrets={
                OAUTH_TOKEN_PAYLOAD_KEY: {"access_token": "AT"},
                "client_secret": "csec",
            },
        )
        assert ctx["secrets"] == {"client_secret": "csec"}
        assert OAUTH_TOKEN_PAYLOAD_KEY not in ctx["secrets"]

    def test_connection_scopes_copied(self):
        ctx = build_resolution_context(connection=self._CONNECTION, secrets={})
        assert ctx["connection"]["parameters"] == {"region": "eu"}
        assert ctx["connection"]["selections"] == {"tenantId": "tnt-1"}
        assert ctx["connection"]["discovered"] == {"org_name": "Acme"}
        assert ctx["auth"] == {"token_response": {}}

    def test_platform_inputs_project_to_declared_storage(self):
        """Xero shape: client_id declares storage connection.parameters,
        client_secret declares storage secrets — both provisioned in the
        platform secret, neither stored on the connection."""
        connector = {
            "connection_contract": {
                "inputs": {
                    "client_id": {"source": "platform", "storage": "connection.parameters"},
                    "client_secret": {"source": "platform", "storage": "secrets"},
                    "shop": {"source": "user", "storage": "connection.parameters"},
                },
            },
        }
        ctx = build_resolution_context(
            connection={}, secrets={},
            connector=connector,
            platform_secrets={"client_id": "cid", "client_secret": "csec", "shop": "nope"},
        )
        assert ctx["connection"]["parameters"]["client_id"] == "cid"
        assert ctx["secrets"]["client_secret"] == "csec"
        # Only source=platform inputs project — a user input present in the
        # platform secret by accident must not be picked up.
        assert "shop" not in ctx["connection"]["parameters"]
        assert "shop" not in ctx["secrets"]

    def test_connection_stored_values_win_over_projection(self):
        connector = {
            "connection_contract": {
                "inputs": {
                    "client_id": {"source": "platform", "storage": "connection.parameters"},
                },
            },
        }
        ctx = build_resolution_context(
            connection={"parameters": {"client_id": "stored"}},
            secrets={},
            connector=connector,
            platform_secrets={"client_id": "platform"},
        )
        assert ctx["connection"]["parameters"]["client_id"] == "stored"

    def test_runtime_scope_passthrough(self):
        ctx = build_resolution_context(
            connection={}, secrets={}, runtime={"oauth": {"code": "C"}},
        )
        assert resolve_template_string("${runtime.oauth.code}", ctx) == "C"

    def test_no_runtime_scope_when_omitted(self):
        ctx = build_resolution_context(connection={}, secrets={})
        assert "runtime" not in ctx


class TestApplyOperationContentType:
    """Operation-level `content_type` shortcut > inherited header > form default."""

    def test_operation_shortcut_overrides_inherited_case_insensitively(self):
        headers = {"content-type": "text/plain", "Accept": "application/json"}
        ct = apply_operation_content_type(headers, {"content_type": "application/json"})
        assert ct == "application/json"
        assert headers["Content-Type"] == "application/json"
        assert "content-type" not in headers

    def test_inherited_header_wins_when_no_shortcut(self):
        headers = {"Content-Type": "application/json"}
        assert apply_operation_content_type(headers, {}) == "application/json"

    def test_form_default_when_nothing_sets_it(self):
        headers = {}
        ct = apply_operation_content_type(headers, {})
        assert ct == "application/x-www-form-urlencoded"
        assert headers["Content-Type"] == "application/x-www-form-urlencoded"


class TestResolveBodyTemplate:
    def test_invalid_json_template_raises_value_error(self):
        with pytest.raises(ValueError, match="invalid JSON body template"):
            resolve_body_template(
                "{not json", content_type="application/json",
                context={}, label="test-op",
            )

    def test_form_template_splits_before_resolving(self):
        ctx = {"secrets": {"token": "a&b=c"}}
        payload = resolve_body_template(
            "grant_type=refresh_token&refresh_token=${secrets.token}",
            content_type="application/x-www-form-urlencoded",
            context=ctx, label="test-op",
        )
        assert payload == {"grant_type": "refresh_token", "refresh_token": "a&b=c"}

    def test_form_field_with_unresolved_placeholder_is_dropped(self):
        # A form field whose value still carries an unresolved ${...} is omitted
        # from the payload (with a LOG.warning) rather than emitted as `k=`.
        ctx = {"secrets": {"token": "rt"}}
        payload = resolve_body_template(
            "grant_type=refresh_token&refresh_token=${secrets.missing}",
            content_type="application/x-www-form-urlencoded",
            context=ctx, label="test-op",
        )
        assert payload == {"grant_type": "refresh_token"}
        assert "refresh_token" not in payload

    def test_form_field_present_when_placeholder_resolves(self):
        # The same field is kept (resolved) once its placeholder is resolvable.
        ctx = {"secrets": {"refresh_token": "rt-value"}}
        payload = resolve_body_template(
            "grant_type=refresh_token&refresh_token=${secrets.refresh_token}",
            content_type="application/x-www-form-urlencoded",
            context=ctx, label="test-op",
        )
        assert payload == {"grant_type": "refresh_token", "refresh_token": "rt-value"}

    def test_form_field_drop_warns(self, caplog):
        with caplog.at_level("WARNING", logger="analitiq.contracts.value_expression"):
            resolve_body_template(
                "refresh_token=${secrets.missing}",
                content_type="application/x-www-form-urlencoded",
                context={}, label="test-op",
            )
        assert any(
            "dropping field 'refresh_token'" in r.getMessage() for r in caplog.records
        )


class TestResolveOperationUrl:
    """resolve_operation_url raises when the resolved URL still carries an
    unresolved ${...} placeholder (an unconstructable request URL), and returns
    the substituted URL string when fully resolvable. Operation `url` wins over
    `path` joined onto the transport `base_url`.
    """

    def test_explicit_url_resolves(self):
        url = resolve_operation_url(
            {"url": "https://${connection.parameters.region}.example.com/v1/users"},
            {"connection": {"parameters": {"region": "eu"}}},
            transport=None,
        )
        assert url == "https://eu.example.com/v1/users"

    def test_path_joined_onto_transport_base_url(self):
        url = resolve_operation_url(
            {"path": "/users/${connection.selections.id}"},
            {"connection": {"selections": {"id": "u-1"}}},
            transport={"base_url": "https://api.example.com/"},
        )
        assert url == "https://api.example.com/users/u-1"

    def test_unresolved_placeholder_in_url_raises(self):
        with pytest.raises(ValueError, match="unresolved placeholder"):
            resolve_operation_url(
                {"url": "https://api.example.com/users/${connection.selections.missing}"},
                {},
                transport=None,
            )

    def test_unresolved_placeholder_in_path_raises(self):
        with pytest.raises(ValueError, match="unresolved placeholder"):
            resolve_operation_url(
                {"path": "/users/${connection.selections.missing}"},
                {},
                transport={"base_url": "https://api.example.com"},
            )

    def test_object_template_base_url_joined_and_resolved(self):
        # #1006: a value-expression `{template}` base_url (per-tenant host) is
        # joined with the path and resolved exactly like a bare-string template.
        url = resolve_operation_url(
            {"path": "/widgets"},
            {"connection": {"discovered": {"api_domain": "acme"}}},
            transport={"base_url": {
                "template": "https://${connection.discovered.api_domain}.example.com/api/v1"
            }},
        )
        assert url == "https://acme.example.com/api/v1/widgets"

    def test_object_ref_base_url_resolved_against_context(self):
        url = resolve_operation_url(
            {"path": "/widgets"},
            {"connection": {"parameters": {"host": "https://tenant.example.com"}}},
            transport={"base_url": {"ref": "connection.parameters.host"}},
        )
        assert url == "https://tenant.example.com/widgets"

    def test_object_template_base_url_unresolved_placeholder_raises(self):
        # The `{template}` form inherits the same fail-loud on an unresolved
        # placeholder as a bare-string base_url — no malformed URL is emitted.
        with pytest.raises(ValueError, match="unresolved placeholder"):
            resolve_operation_url(
                {"path": "/widgets"},
                {},
                transport={"base_url": {
                    "template": "https://${connection.discovered.api_domain}.example.com"
                }},
            )


class TestResolveTransportBaseUrl:
    """`resolve_transport_base_url` normalizes a transport's `base_url` — literal
    or value-expression object — to the string shape a URL builder joins with
    the operation path (#1006)."""

    def test_literal_string_unchanged(self):
        assert resolve_transport_base_url({"base_url": "https://api.wise.com"}) == "https://api.wise.com"

    def test_bare_template_string_unchanged(self):
        # A literal string bearing ${...} is returned verbatim for the caller's
        # own downstream resolution.
        s = "https://${connection.parameters.region}.api.example.com"
        assert resolve_transport_base_url({"base_url": s}) == s

    def test_absent_base_url_is_empty(self):
        assert resolve_transport_base_url({}) == ""
        assert resolve_transport_base_url(None) == ""

    def test_object_template_returns_raw_template_string(self):
        # `{template}` flattens to its raw string (context-free) so every caller
        # resolves it through its own ${...} pipeline, identically to a
        # bare-string base_url.
        tmpl = "https://${connection.discovered.api_domain}.example.com"
        assert resolve_transport_base_url({"base_url": {"template": tmpl}}) == tmpl

    def test_object_ref_resolves_with_context(self):
        base = resolve_transport_base_url(
            {"base_url": {"ref": "connection.parameters.host"}},
            {"connection": {"parameters": {"host": "https://tenant.example.com"}}},
        )
        assert base == "https://tenant.example.com"

    def test_object_ref_without_context_raises(self):
        with pytest.raises(ValueError, match="requires a resolution context"):
            resolve_transport_base_url({"base_url": {"ref": "connection.parameters.host"}})

    def test_object_literal_resolves_without_context(self):
        # A literal is context-free — it must resolve even when no context is
        # passed (the oauth-start path), not raise the context-required error.
        assert resolve_transport_base_url(
            {"base_url": {"literal": "https://login.example.com"}}
        ) == "https://login.example.com"

    @pytest.mark.parametrize("bad", [{"literal": ""}, {"literal": {"host": "x"}}, {"literal": 123}])
    def test_object_literal_non_string_raises(self, bad):
        with pytest.raises(ValueError, match="literal must be a non-empty string"):
            resolve_transport_base_url({"base_url": bad})

    def test_object_ref_resolving_to_non_string_raises(self):
        with pytest.raises(ValueError, match="did not resolve to a URL string"):
            resolve_transport_base_url(
                {"base_url": {"ref": "connection.parameters.host"}},
                {"connection": {"parameters": {"host": {"nested": "obj"}}}},
            )

    @pytest.mark.parametrize("bad_template", [123, {"nested": "obj"}, ["x"], None])
    def test_non_string_template_raises_clean_error(self, bad_template):
        # A `{template}` whose value isn't a string is malformed; the helper must
        # fail loud with a clear message, not a raw TypeError from the regex sub.
        with pytest.raises(ValueError, match="non-string template"):
            resolve_transport_base_url(
                {"base_url": {"template": bad_template}},
                {"connection": {"parameters": {}}},
            )
