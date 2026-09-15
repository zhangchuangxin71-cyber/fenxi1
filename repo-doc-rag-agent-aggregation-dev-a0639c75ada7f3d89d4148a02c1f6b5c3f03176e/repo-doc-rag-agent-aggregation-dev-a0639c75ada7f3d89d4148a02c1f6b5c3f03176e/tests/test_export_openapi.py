from __future__ import annotations

import argparse
import json
import sys
from io import BytesIO

import pytest
import yaml


def test_parse_service_url_accepts_known_service() -> None:
    from scripts.export_openapi import parse_service_url

    assert parse_service_url("mineru=http://127.0.0.1:18000/") == (
        "mineru",
        "http://127.0.0.1:18000",
    )


def test_parse_service_url_rejects_unknown_service() -> None:
    from scripts.export_openapi import parse_service_url

    with pytest.raises(argparse.ArgumentTypeError):
        parse_service_url("unknown=http://127.0.0.1:1")


def test_fetch_schema_validates_openapi_shape(monkeypatch) -> None:
    from scripts.export_openapi import fetch_schema

    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: Response(json.dumps({"openapi": "3.1.0", "paths": {"/health": {}}}).encode()),
    )
    assert fetch_schema("http://service", 3)["openapi"] == "3.1.0"

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: Response(json.dumps({"openapi": "3.1.0", "paths": {}}).encode()),
    )
    with pytest.raises(ValueError):
        fetch_schema("http://service", 3)


def test_main_archives_every_service_as_yaml(monkeypatch, tmp_path) -> None:
    from scripts import export_openapi

    monkeypatch.setattr(
        export_openapi,
        "fetch_schema",
        lambda base_url, timeout: {
            "openapi": "3.1.0",
            "info": {"title": base_url},
            "paths": {"/health": {"get": {}}},
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["export_openapi.py", "--output-dir", str(tmp_path)],
    )

    assert export_openapi.main() == 0
    assert {path.name for path in tmp_path.glob("*.openapi.yaml")} == {
        f"{name}.openapi.yaml" for name in export_openapi.DEFAULT_URLS
    }
    for path in tmp_path.glob("*.openapi.yaml"):
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["paths"]
