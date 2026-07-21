"""Guards on the publishable artifact: dependency-light, versioned, and authored
in the public `analitiq.validator` namespace with no private codename.

The source IS the artifact — it is copied, not rendered — so the interesting
assertions are on the SOURCE. `test_staged_artifact_matches_source` pins that
property: if staging ever starts transforming again, it fails.
"""
import importlib.util
import re
from pathlib import Path

BUILD = Path(__file__).resolve().parents[1] / "scripts" / "build.py"

# PEP 440: plain X.Y.Z, optionally with a pre-release (aN/bN/rcN) or .devN suffix
# — the validator-v* tag is cut from it, and pre-releases are used while iterating.
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+|\.dev\d+)?$")


def _build_module():
    spec = importlib.util.spec_from_file_location("validator_build", BUILD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_is_public_safe():
    leaks = _build_module().check_public_safe()
    assert leaks == [], (
        f"validator source imports disallowed packages {leaks}; the published "
        "package may use only stdlib + pydantic + the contract-models subset."
    )


def test_version_is_release_or_prerelease():
    version = _build_module().read_version()
    assert _VERSION_RE.match(version), (
        f"pyproject version {version!r} must be X.Y.Z with an optional pre-release "
        "suffix (rcN/.devN); the validator-v* tag is cut from it."
    )


def test_source_imports_the_public_contract():
    """The validator binds to the contract via its PUBLIC import path — the same
    one an installed consumer uses, which is why nothing needs rewriting."""
    build = _build_module()
    text = "\n".join(p.read_text() for p in build._source_files())
    assert "from analitiq.contracts." in text, (
        "the validator must import the contract models from the public namespace"
    )
    assert "alq" not in text, "the private codename must not appear in the source"


def test_staged_artifact_matches_source(tmp_path):
    """Staging COPIES; it does not transform.

    This is the property the whole split buys: what a maintainer reads in
    `validator/src/` is byte-for-byte what a consumer installs. If a render step
    ever creeps back in, this fails.
    """
    build = _build_module()
    dist = tmp_path / "dist"
    build.stage(dist)

    pkg = dist / "src" / "analitiq" / "validator"
    assert (pkg / "__init__.py").is_file(), "package must expose analitiq/validator/__init__.py"
    assert (pkg / "_core.py").is_file(), "the _core module must be staged"
    assert (pkg / "connectors.py").is_file(), "the connectors module must be staged"
    assert not (dist / "src" / "analitiq" / "__init__.py").exists(), (
        "analitiq/ must stay a PEP 420 namespace (no __init__.py) so it can be "
        "shared with analitiq-contract-models"
    )
    for src_path in build._source_files():
        assert (pkg / src_path.name).read_text() == src_path.read_text(), (
            f"{src_path.name} was transformed during staging — the published "
            "package must be the source verbatim"
        )
