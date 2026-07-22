"""Import safety: importing `analitiq.validator` must not mutate the caller's env.

Every kind binds `DOMAIN=analitiq.ai` while it imports the contract models (their
`$schema` host `Literal` reads `DOMAIN` at import) via the shared
`contract_model_domain()` guard. That override is scoped to each import window and
the caller's ambient `DOMAIN` is restored — so an in-process consumer that reads
`DOMAIN` at runtime is never silently repointed by importing the validator, even
though the package now imports four model-bearing kinds in sequence. This is
verified in a subprocess because the in-process package is already imported (under
the test's own `DOMAIN`).
"""
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_SRC_ROOT = _REPO_ROOT / "contract-models" / "src"
SRC_ROOT = _REPO_ROOT / "validator" / "src"
# Both public source trees contribute to the `analitiq` namespace. No private
# layer is on the path: the validator depends only on the public contract, so the
# subprocess sees exactly what an installed consumer would.
_PYTHONPATH = os.pathsep.join([str(SRC_ROOT), str(CONTRACTS_SRC_ROOT)])

# Import the full package (exercises every kind's model import + DOMAIN override)
# and report the ambient DOMAIN afterwards.
_PROBE = (
    "import os, analitiq.validator; "
    "print(os.environ.get('DOMAIN', '<unset>'))"
)


def _domain_after_import(ambient: str | None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "DOMAIN"}
    env["PYTHONPATH"] = _PYTHONPATH
    if ambient is not None:
        env["DOMAIN"] = ambient
    r = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True, text=True, env=env, check=False,
    )
    assert r.returncode == 0, f"import failed: {r.stderr}"
    return r.stdout.strip()


def test_import_preserves_ambient_domain():
    # A dev-shell DOMAIN survives importing the validator (no leak to analitiq.ai).
    assert _domain_after_import("analitiq.dev") == "analitiq.dev"


def test_import_leaves_unset_domain_unset():
    # An unset DOMAIN is restored to unset, not left forced to analitiq.ai.
    assert _domain_after_import(None) == "<unset>"
