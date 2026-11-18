import importlib.metadata

import m97


def test_version_matches_metadata():
    assert m97.__version__ == importlib.metadata.version("m97")
