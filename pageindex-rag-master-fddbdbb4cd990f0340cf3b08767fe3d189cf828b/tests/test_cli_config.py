import argparse

from core.cli_config import apply_env_overrides, build_argument_parser


def test_parser_defaults_to_postgres_backend():
    parser = build_argument_parser(
        default_config_path="app/document_assistant_config.yaml",
        default_data_dir="app/data",
        default_simple_index_page_threshold=80,
        default_long_pdf_hard_threshold=180,
        default_simple_index_chunk_pages=16,
        default_hybrid_index_chunk_pages=12,
        default_complexity_sample_pages=12,
        default_index_concurrency=4,
        default_pdf_io_concurrency=2,
        default_bm25_prefilter_top_k=8,
        default_chunk_overlap_ratio=0.15,
        default_log_level="INFO",
        default_log_format="json",
    )

    args = parser.parse_args([])

    assert args.storage_backend == "postgres"
    assert args.data_dir == "app/data"


def test_parser_accepts_prune_storage_flag():
    parser = build_argument_parser(
        default_config_path="app/document_assistant_config.yaml",
        default_data_dir="app/data",
        default_simple_index_page_threshold=80,
        default_long_pdf_hard_threshold=180,
        default_simple_index_chunk_pages=16,
        default_hybrid_index_chunk_pages=12,
        default_complexity_sample_pages=12,
        default_index_concurrency=4,
        default_pdf_io_concurrency=2,
        default_bm25_prefilter_top_k=8,
        default_chunk_overlap_ratio=0.15,
        default_log_level="INFO",
        default_log_format="json",
    )

    args = parser.parse_args(["--prune-storage"])

    assert args.prune_storage is True


def test_apply_env_overrides_uses_ark_model_as_default(monkeypatch):
    args = argparse.Namespace(
        model=None,
        retrieve_model=None,
        data_dir="app/data",
        index_file=None,
        storage_backend="postgres",
        postgres_dsn=None,
        log_level="INFO",
        log_format="json",
        bm25_prefilter_top_k=8,
        index_concurrency=4,
        simple_index_page_threshold=80,
        long_pdf_hard_threshold=180,
        simple_index_chunk_pages=16,
        hybrid_index_chunk_pages=12,
        complexity_sample_pages=12,
        chunk_overlap_ratio=0.15,
        pdf_io_concurrency=2,
        session_id=None,
        summary_enabled=True,
        table_parse_mode="off",
        node_max_tokens=512,
        max_tree_depth=2,
        enable_ocr=False,
        ocr_min_chars=20,
        ocr_lang="chi_sim+eng",
        summary_concurrency=0,
        index_timeout_seconds=0,
    )

    monkeypatch.setenv("ARK_MODEL", "doubao-test-model")
    monkeypatch.delenv("DA_MODEL", raising=False)
    monkeypatch.delenv("DA_RETRIEVE_MODEL", raising=False)
    monkeypatch.delenv("ARK_ENDPOINT_ID", raising=False)

    apply_env_overrides(args, cli_provided=set())

    assert args.model == "doubao-test-model"
    assert args.retrieve_model == "doubao-test-model"


def test_apply_env_overrides_uses_ark_endpoint_id_when_model_missing(monkeypatch):
    args = argparse.Namespace(
        model=None,
        retrieve_model=None,
        data_dir="app/data",
        index_file=None,
        storage_backend="postgres",
        postgres_dsn=None,
        log_level="INFO",
        log_format="json",
        bm25_prefilter_top_k=8,
        index_concurrency=4,
        simple_index_page_threshold=80,
        long_pdf_hard_threshold=180,
        simple_index_chunk_pages=16,
        hybrid_index_chunk_pages=12,
        complexity_sample_pages=12,
        chunk_overlap_ratio=0.15,
        pdf_io_concurrency=2,
        session_id=None,
        summary_enabled=True,
        table_parse_mode="off",
        node_max_tokens=512,
        max_tree_depth=2,
        enable_ocr=False,
        ocr_min_chars=20,
        ocr_lang="chi_sim+eng",
        summary_concurrency=0,
        index_timeout_seconds=0,
    )

    monkeypatch.delenv("DA_MODEL", raising=False)
    monkeypatch.delenv("DA_RETRIEVE_MODEL", raising=False)
    monkeypatch.delenv("ARK_MODEL", raising=False)
    monkeypatch.setenv("ARK_ENDPOINT_ID", "doubao-endpoint-model")

    apply_env_overrides(args, cli_provided=set())

    assert args.model == "doubao-endpoint-model"
    assert args.retrieve_model == "doubao-endpoint-model"
