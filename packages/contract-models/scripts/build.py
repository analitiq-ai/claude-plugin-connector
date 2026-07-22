#!/usr/bin/env python3
"""Build the `analitiq-contract-models` distribution.

The authored contract models are written directly in the public namespace at
`contract-models/src/analitiq/contracts/`. That tree IS the published package —
not a rendering of a private one. There is no namespace rewrite and no prose
scrub: what is in the source is what ships.

Consequently the guards here check the SOURCE, where a human types, rather than
an artifact after a translation.

Responsibilities:
  1. Guard the source structurally: it may import nothing beyond stdlib +
     `pydantic` + the package itself — the architectural rule that public code may
     not depend on private code, enforced rather than documented. (Prose hygiene —
     not naming Analitiq internals in docstrings/`description`s — is kept by author
     and review discipline, not machine-checked here.)
  2. Stage `dist/`: copy the source verbatim, plus the ONE file that cannot live
     in the source tree — `analitiq/contracts/__init__.py`, which pins `DOMAIN`
     to the public contract host. See `_INIT_BODY` for why it must be generated.
  3. Guard the artifact: `analitiq/` stays a PEP 420 namespace, and the staged
     package imports boto3-free with `DOMAIN` pinned and no private module
     reachable.

Stdlib only (`tomllib` is 3.11+).
"""

from __future__ import annotations

import argparse
import ast
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = PKG_DIR.parent
SRC = PKG_DIR / "src"
PKG_SRC = SRC / "analitiq" / "contracts"
PYPROJECT = PKG_DIR / "pyproject.toml"
README = PKG_DIR / "README.md"

# The private runtime's codename, named here only for the import-graph leak check
# in `import_guard` (a private module reaching the public package surfaces as
# `alq` in sys.modules). Public-facing PROSE cleanliness is not machine-checked —
# it is kept by author and review discipline; the structural guards below (import
# scope, boto3-free, namespace, generated __init__) are what fail loud in CI.
PRIVATE_CODENAME = "alq"

# Third-party the public package may import. It is pure Pydantic contract
# models; anything else would either make it non-portable or drag private code
# into a published artifact.
ALLOWED_THIRD_PARTY = {"pydantic", "analitiq"}

_INIT_BANNER = (
    "# GENERATED — do not edit. Written by contract-models/scripts/build.py at\n"
    "# release. This is the one file of the package that is NOT source: in-repo\n"
    "# the same modules are imported by infrastructure that needs the ambient,\n"
    "# per-environment DOMAIN to win, so the pin below cannot live in the tree.\n"
)
_INIT_BODY = (
    "import os\n\n"
    "# This public package validates PUBLIC connector/connection/stream/pipeline\n"
    "# documents, whose contract host is ALWAYS analitiq.ai. Force it (not\n"
    "# setdefault) — an ambient DOMAIN=analitiq.dev (common in dev shells) would\n"
    "# otherwise build the `$schema` Literals for the wrong host and reject valid\n"
    "# public docs. Runs before analitiq.contracts.shared.common imports.\n"
    'os.environ["DOMAIN"] = "analitiq.ai"\n'
)


def read_version() -> str:
    return tomllib.loads(PYPROJECT.read_text())["project"]["version"]


def _is_staged(path: Path) -> bool:
    """Whether `stage()` would copy this path (mirrors its ignore patterns)."""
    return path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"


def _source_files() -> list[Path]:
    """Every Python module of the package — what the AST guards parse."""
    return sorted(p for p in PKG_SRC.rglob("*.py") if _is_staged(p))


def dependency_guard() -> None:
    """Public code may not depend on private code, or on anything heavy.

    The architectural rule of the split, enforced. Previously the equivalent
    check ran against a rendered copy and had to special-case which private
    submodules were tolerable; now a private import is simply absent from the
    allowlist, and the package's own namespace is the only first-party name.
    """
    stdlib = sys.stdlib_module_names
    offenders: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue  # relative imports stay inside the package by definition
            for name in names:
                top = name.split(".")[0]
                if top in stdlib or top in ALLOWED_THIRD_PARTY:
                    continue
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {name}")
    if offenders:
        raise SystemExit(
            "build: the public contract source imports outside its allowed set "
            f"(stdlib + {sorted(ALLOWED_THIRD_PARTY)}). Public code cannot depend "
            "on private code:\n  " + "\n  ".join(offenders)
        )


def src_init_guard() -> None:
    """`analitiq/contracts/__init__.py` must NOT exist in the source tree.

    It is generated at stage time and pins DOMAIN to the public host. If it were
    committed, every in-repo importer of these models would be pinned to
    analitiq.ai too and would build `$schema` Literals for the wrong host on any
    non-production environment.
    """
    stray = PKG_SRC / "__init__.py"
    if stray.exists():
        raise SystemExit(
            f"build: {stray.relative_to(REPO_ROOT)} must not be committed — it is "
            "generated at release to pin DOMAIN, and in-repo importers need the "
            "ambient per-environment DOMAIN to win."
        )


def namespace_guard(dist_dir: Path) -> None:
    """`analitiq/` must stay a PEP 420 namespace (no __init__) so the validator
    package can contribute `analitiq.validator` to the same namespace."""
    stray = dist_dir / "analitiq" / "__init__.py"
    if stray.exists():
        raise SystemExit(
            "build: analitiq/__init__.py must not exist — `analitiq` is a PEP 420 "
            "namespace shared with analitiq-validator."
        )


def stage(dist_dir: Path) -> None:
    """Copy the source verbatim + metadata; generate only the DOMAIN-pinning
    `__init__.py`. `pyproject.toml` lists the packages relative to the dist root,
    so the tree is mirrored as-is."""
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    dist_dir.mkdir(parents=True)

    shutil.copytree(
        SRC / "analitiq",
        dist_dir / "analitiq",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (dist_dir / "analitiq" / "contracts" / "__init__.py").write_text(
        _INIT_BANNER + _INIT_BODY
    )
    shutil.copy2(PYPROJECT, dist_dir / "pyproject.toml")
    shutil.copy2(README, dist_dir / "README.md")
    shutil.copy2(PKG_DIR / "LICENSE", dist_dir / "LICENSE")


def import_guard(dist_dir: Path) -> None:
    """Import the staged package with DOMAIN unset — proving the generated
    `__init__` pins it — and fail on a boto3 or private-module leak."""
    probe = (
        "import sys, os; "
        f"sys.path.insert(0, {str(dist_dir)!r}); "
        "import analitiq.contracts.connector, analitiq.contracts.endpoints, "
        "analitiq.contracts.type_map, analitiq.contracts.endpoint_identity, "
        "analitiq.contracts.connection, analitiq.contracts.stream, "
        "analitiq.contracts.pipelines.config; "
        "sys.exit('boto3 leaked into contract-models') if 'boto3' in sys.modules else None; "
        "sys.exit('package did not pin DOMAIN') if os.environ.get('DOMAIN') != 'analitiq.ai' else None; "
        f"sys.exit('private module leaked into the public import graph') if {PRIVATE_CODENAME!r} in sys.modules else None; "
        "from analitiq.contracts.connector import Connector; "
        "from analitiq.contracts.endpoints import ApiEndpointDoc, DatabaseEndpointDoc, DatabaseObject; "
        "from analitiq.contracts.endpoint_identity import derive_db_endpoint_id; "
        "from analitiq.contracts.type_map import TypeMapReadDoc, TypeMapWriteDoc; "
        "from analitiq.contracts.connection import ConnectionInput; "
        "from analitiq.contracts.stream import StreamInput, ConnectionEndpointRef, validate_endpoint_ref; "
        "from analitiq.contracts.pipelines.config import PipelineInput; "
        "print('import-guard ok')"
    )
    env = {k: v for k, v in os.environ.items() if k != "DOMAIN"}
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env,
        check=False,  # the guard inspects returncode itself to raise a clearer error
    )
    if result.returncode != 0:
        raise SystemExit(
            "build: staged contract-models failed the import guard "
            "(missing model, or an AWS/private import leaked in):\n"
            + result.stderr.strip()
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Build/check analitiq-contract-models.")
    ap.add_argument("--check", action="store_true", help="Guard only; stages to --dist, import-checks it, then removes it.")
    ap.add_argument("--expect-version", help="Assert pyproject.toml version equals this (release use).")
    ap.add_argument("--dist", default=str(PKG_DIR / "dist"), help="Output dir for the staged package.")
    args = ap.parse_args()

    version = read_version()
    if args.expect_version is not None and args.expect_version != version:
        raise SystemExit(
            f"build: version mismatch — pyproject.toml is {version!r} but the "
            f"release expects {args.expect_version!r}."
        )

    # Source invariants first: they name the file a human must fix.
    dependency_guard()
    src_init_guard()

    dist_dir = Path(args.dist)
    stage(dist_dir)
    namespace_guard(dist_dir)
    import_guard(dist_dir)

    if args.check:
        shutil.rmtree(dist_dir)
        print(f"build: checks passed (version {version}, analitiq.contracts imports boto3-free).")
    else:
        print(f"build: staged dist at {dist_dir} (version {version}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
