"""Extras helpers for vfy's built-in HTML reporter."""

from __future__ import annotations

import json as json_module
from pathlib import Path
from typing import TypedDict


class Extra(TypedDict):
    kind: str
    name: str
    content: str


class _Extras:
    @staticmethod
    def text(content: object, name: str = "Text") -> Extra:
        return {"kind": "text", "name": name, "content": str(content)}

    @staticmethod
    def json(content: object, name: str = "JSON") -> Extra:
        return {
            "kind": "json",
            "name": name,
            "content": json_module.dumps(content, indent=2, sort_keys=True),
        }

    @staticmethod
    def url(content: str, name: str = "URL") -> Extra:
        return {"kind": "url", "name": name, "content": content}

    @staticmethod
    def html(content: str, name: str = "HTML") -> Extra:
        return {"kind": "html", "name": name, "content": content}

    @staticmethod
    def image(content: str | Path, name: str = "Image", mime_type: str = "image/png") -> Extra:
        return {"kind": mime_type, "name": name, "content": str(content)}

    @staticmethod
    def png(content: str | Path, name: str = "Image") -> Extra:
        return _Extras.image(content, name, "image/png")

    @staticmethod
    def jpg(content: str | Path, name: str = "Image") -> Extra:
        return _Extras.image(content, name, "image/jpeg")

    @staticmethod
    def svg(content: str | Path, name: str = "Image") -> Extra:
        return _Extras.image(content, name, "image/svg+xml")


extras = _Extras()

__all__ = ["Extra", "extras"]
