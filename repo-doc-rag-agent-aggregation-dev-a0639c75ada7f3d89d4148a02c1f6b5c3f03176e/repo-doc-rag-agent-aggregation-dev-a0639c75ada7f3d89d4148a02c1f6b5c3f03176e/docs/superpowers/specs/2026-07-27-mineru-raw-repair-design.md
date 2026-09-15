# MinerU Raw Repair Design

## Goal

When a single permanent document is submitted again and its existing database record lacks a complete `raw.raw_mineru`, re-run only MinerU parsing and patch that field in place.

## Safety Boundary

- Keep the existing content-derived `doc_id` and binding.
- Do not run indexing, summaries, document-description generation, or native-parser fallback.
- Do not update document metadata, timestamps, pages, nodes, or bindings.
- Keep normal duplicate rejection for non-MinerU backends, complete raw data, temporary documents, and batch requests.
- Validate `md_content`, `content_list`, and `middle_json` before writing.

## Flow

The submission service identifies an existing binding. For a single permanent MinerU request it asks the store whether `raw_mineru` is complete. An incomplete record is queued with an internal repair flag instead of failing as a duplicate. The runner downloads the same source file, calls the MinerU adapter directly, and uses a dedicated PostgreSQL JSONB patch method. All regular ingestion paths remain unchanged.
