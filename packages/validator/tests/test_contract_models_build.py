"""Structural guards on the analitiq-contract-models build.

The package is AUTHORED in the public namespace and ships verbatim, so the build
guards run against the source. These pin the STRUCTURAL invariants that
`build.py --check` otherwise only smoke-covers (it proves the guards PASS on the
clean source, never that they still FIRE): the public/private import boundary
actually raises on an injected private import, and staging copies the source
verbatim plus the one generated DOMAIN-pinning `__init__`. Prose hygiene is kept
by author and review discipline, not machine-checked.
"""
import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUILD = _REPO_ROOT / "contract-models" / "scripts" / "build.py"


def _build_module():
    spec = importlib.util.spec_from_file_location("contract_models_build", _BUILD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_code_does_not_import_private_code():
    """The architectural rule of the split: `analitiq.contracts` may import only
    stdlib + pydantic + itself."""
    _build_module().dependency_guard()  # must not raise


def test_domain_pin_is_not_committed():
    """`analitiq/contracts/__init__.py` pins DOMAIN to the public host and is
    generated at release. Committed, it would pin every in-repo importer to
    analitiq.ai and build `$schema` Literals for the wrong host off-prod."""
    _build_module().src_init_guard()  # must not raise


def test_staged_package_is_the_source_verbatim(tmp_path):
    """Staging COPIES; it does not transform.

    This is the property the whole split buys, and contract-models is the package
    that HAD the render — so it's where a transform could creep back. The one
    exception is `__init__.py`, which is generated to pin DOMAIN and must not
    exist in the source.
    """
    b = _build_module()
    dist = tmp_path / "dist"
    b.stage(dist)
    staged_pkg = dist / "analitiq" / "contracts"

    generated = staged_pkg / "__init__.py"
    assert generated.is_file(), "the DOMAIN-pinning __init__ must be generated"
    assert 'os.environ["DOMAIN"] = "analitiq.ai"' in generated.read_text()
    assert not (b.PKG_SRC / "__init__.py").exists(), "…and must not be committed"

    for src_path in b._source_files():
        rel = src_path.relative_to(b.PKG_SRC)
        assert (staged_pkg / rel).read_text() == src_path.read_text(), (
            f"{rel} was transformed during staging — the published package must "
            "be the source verbatim"
        )


def test_dependency_guard_rejects_a_private_import(tmp_path, monkeypatch):
    """Public code may not import private code — the architectural rule of the
    split. Pin that the guard actually fires, not just that it passes today."""
    b = _build_module()
    leaky = tmp_path / "connection.py"
    leaky.write_text("from alq.dynamodb import get_store\n")
    monkeypatch.setattr(b, "_source_files", lambda: [leaky])
    monkeypatch.setattr(b, "REPO_ROOT", tmp_path)
    with pytest.raises(SystemExit, match="cannot depend"):
        b.dependency_guard()
