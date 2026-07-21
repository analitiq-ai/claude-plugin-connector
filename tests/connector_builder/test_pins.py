"""The suite's own pin metadata must not rot."""
from __future__ import annotations

from _pins import assert_pinned_version_matches_pyproject


def test_pinned_version_matches_the_shipped_package():
    assert_pinned_version_matches_pyproject()
