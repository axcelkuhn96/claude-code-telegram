"""Fixtures genéricas. Testes nunca tocam ~/.claude*, ~/.horizon ou ~/.config reais."""

import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("LIVE") == "1":
        return
    skip_live = pytest.mark.skip(reason="teste live: rode com LIVE=1")
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(skip_live)
