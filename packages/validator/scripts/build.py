#!/usr/bin/env python3
"""Build the `analitiq-validator` distribution for release.

This repo is the canonical source of the validator. The source package
`validator/src/analitiq/validator/` is already written in the public namespace
and imports the public contract package (`analitiq.contracts`), so the
distribution is that tree plus metadata — copied, not translated. What is in the
source is what ships.

The guards therefore check the SOURCE structurally, where a human types.

Responsibilities:
  1. Guard the source's imports: only stdlib + `pydantic` + the public contract
     package (`analitiq`). A private import would both leak private code and make
     the published package unimportable. (Prose hygiene — not naming Analitiq
     internals in docstrings/`description`s — is kept by author and review
     discipline, not machine-checked here.)
  2. Stage `dist/` (src layout): copy each module alongside `pyproject.toml`,
     `README`, and `LICENSE`, ready for `python -m build`.

Stdlib only (`tomllib` is 3.11+), so it runs anywhere the validator does.
"""

from __future__ import annotations

import argparse
import ast
import shutil
import sys
import tomllib
from pathlib import Path

VALIDATOR_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = VALIDATOR_DIR / "src" / "analitiq" / "validator"
PYPROJECT = VALIDATOR_DIR / "pyproject.toml"
README = VALIDATOR_DIR / "README.md"
LICENSE = VALIDATOR_DIR / "LICENSE"

# Third-party the validator SOURCE may carry. It validates via the contract
# models, imported from the public `analitiq.contracts` namespace (its
# `analitiq-contract-models` dependency) — so `pydantic` and `analitiq` — plus
# `jsonschema`, a declared dependency used to meta-validate the JSON-Schema
# documents embedded in api-endpoints (Draft 2020-12). All are public, portable
# packages; anything heavier would leak private code or make the package
# non-portable.
ALLOWED_THIRD_PARTY = {"pydantic", "analitiq", "jsonschema"}


def _top_level_imports(source: str) -> set[str]:
    """Top-level package name of every absolute import in the module."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def _source_files(src_dir: Path = SRC_DIR) -> list[Path]:
    """Every `*.py` module of the validator source package."""
    return sorted(src_dir.glob("*.py"))


def check_public_safe(src_dir: Path = SRC_DIR) -> list[str]:
    """Disallowed third-party imports across the source package (empty ⇒ safe)."""
    imports: set[str] = set()
    for src_path in _source_files(src_dir):
        imports |= _top_level_imports(src_path.read_text())
    stdlib = sys.stdlib_module_names  # 3.10+
    return sorted(
        name
        for name in imports
        if name not in stdlib and name not in ALLOWED_THIRD_PARTY
    )


def read_version() -> str:
    return tomllib.loads(PYPROJECT.read_text())["project"]["version"]


def run_checks(expect_version: str | None) -> None:
    leaks = check_public_safe()
    if leaks:
        raise SystemExit(
            "build: validator source imports disallowed non-stdlib packages "
            f"{leaks} — the published package must stay dependency-light "
            f"(stdlib + {sorted(ALLOWED_THIRD_PARTY)})."
        )
    version = read_version()
    if expect_version is not None and expect_version != version:
        raise SystemExit(
            f"build: version mismatch — pyproject.toml is {version!r} but the "
            f"release expects {expect_version!r}. Bump validator/pyproject.toml "
            "to match the validator-v* tag."
        )
    print(f"build: source checks passed (version {version}, imports public-safe).")


def stage(dist_dir: Path) -> None:
    """Copy the source package into `src/analitiq/validator/` + metadata (src
    layout, matching pyproject's `package-dir = {"" = "src"}`).

    `analitiq` gets NO `__init__.py` — it is a PEP 420 namespace shared with
    `analitiq-contract-models`.
    """
    pkg_dir = dist_dir / "src" / "analitiq" / "validator"
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    pkg_dir.mkdir(parents=True)

    for src_path in _source_files():
        shutil.copy2(src_path, pkg_dir / src_path.name)

    shutil.copy2(PYPROJECT, dist_dir / "pyproject.toml")
    shutil.copy2(README, dist_dir / "README.md")
    shutil.copy2(LICENSE, dist_dir / "LICENSE")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build/check the analitiq-validator package.")
    ap.add_argument("--check", action="store_true", help="Guard only; stages to --dist, then removes it.")
    ap.add_argument("--expect-version", help="Assert pyproject.toml version equals this (release use).")
    ap.add_argument("--dist", default=str(VALIDATOR_DIR / "dist"), help="Output dir for the staged artifact.")
    args = ap.parse_args()

    run_checks(args.expect_version)
    dist_dir = Path(args.dist)
    stage(dist_dir)
    if args.check:
        shutil.rmtree(dist_dir)
        print(f"build: checks passed (version {read_version()}).")
    else:
        print(f"build: staged dist at {dist_dir} (version {read_version()}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
