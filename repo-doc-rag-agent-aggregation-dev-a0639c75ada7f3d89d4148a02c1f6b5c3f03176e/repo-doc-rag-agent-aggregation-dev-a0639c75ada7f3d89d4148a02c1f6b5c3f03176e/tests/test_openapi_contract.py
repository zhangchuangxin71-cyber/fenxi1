from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "mineru.openapi.yaml",
    "ingestion.openapi.yaml",
    "retrieval.openapi.yaml",
    "knowledge-chat.openapi.yaml",
    "report-agent.openapi.yaml",
}


def test_all_service_openapi_documents_are_archived() -> None:
    actual = {path.name for path in (ROOT / "openapi").glob("*.openapi.yaml")}
    assert actual == EXPECTED


def test_openapi_documents_have_importable_core_structure() -> None:
    for name in EXPECTED:
        document = yaml.safe_load((ROOT / "openapi" / name).read_text(encoding="utf-8"))
        assert str(document["openapi"]).startswith("3.")
        assert document["info"]["title"]
        assert document["paths"]


def test_openapi_documents_do_not_embed_secrets() -> None:
    for name in EXPECTED:
        text = (ROOT / "openapi" / name).read_text(encoding="utf-8")
        assert "rrs_" not in text
