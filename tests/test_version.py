"""The version string in the package matches the installed distribution's."""

import importlib.metadata

import auditry


def test_version_matches_distribution_metadata():
    assert auditry.__version__ == importlib.metadata.version("auditry")
