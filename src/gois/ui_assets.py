"""Loader for embedded UI assets (HTML/CSS/JS templates) shipped as package data."""

from importlib import resources


def load_asset(name: str) -> str:
    return (resources.files(__package__) / "assets" / name).read_text(encoding="utf-8")
