# Changelog

## Unreleased

### Added

- Added `INGEST_PARSER_BACKEND` to switch the document parser backend between the native parser and MinerU.
- Added MinerU HTTP client and adapter. MinerU results are mapped back to the existing PageIndex payload shape and persisted into the existing `documents`, `document_bindings`, `doc_nodes`, and `doc_pages` tables without schema changes.
- Added native parser fallback for MinerU failures when `MINERU_FALLBACK_TO_NATIVE=true`.
- Added MinerU request controls: client-side concurrency, timeout, retry count, retry backoff, async task polling interval, backend, parse method, and language.
- Added weak-heading postprocessing for overlong MinerU leaf nodes in `md`, `markdown`, `txt`, `html`, and `htm` style inputs.
- Added parser logs for MinerU start, success, failure, native fallback, source path, extension, error stage, and exception details.
- Added tests covering MinerU adapter conversion, fallback logging, empty heading blocks, weak-heading splitting, original `doc_type` persistence, `doc_nodes.text` persistence, and slimmed `documents.raw` payloads.

### Changed

- Temporary duplicate uploads are now idempotent within the same `user_id/kb_id` scope: the
  service returns the existing `doc_id` without creating another binding or rerunning parsing.
- `session_id` remains task/upload metadata and an explicit cleanup selector, but no longer
  participates in document visibility or duplicate decisions.
- HTML/HTM files can still be normalized before parsing, but persisted `documents.doc_type` now keeps the original file extension instead of the temporary converted extension.
- `doc_nodes.text` is now persisted from the parsed PageIndex node body. The storage layer still removes duplicated `structure[].text` from `documents.raw` to avoid storing full node text twice.
- PDF parsing is now expected to prefer MinerU when configured, with native parsing retained as a safety fallback.
- Batch ingestion can run multiple document parses concurrently through `INGEST_INDEX_CONCURRENCY`; MinerU calls are additionally bounded by `MINERU_CLIENT_CONCURRENCY` and the MinerU API service's own `MINERU_API_MAX_CONCURRENT_REQUESTS`.
- MinerU adapter now preserves md_content, content_list, and middle_json under documents.raw.raw_mineru. Normalized page text and node text continue to live in doc_pages and doc_nodes instead of being duplicated in raw.
- MinerU model_output and extracted image binaries remain disabled and are not persisted.
- Production example settings now use bounded parser/LLM concurrency and timeouts; Docker Compose binds the unauthenticated ingestion API to `127.0.0.1` by default.

### Fixed

- Force-disable Ark deep thinking for document summarization and vision OCR; remove the misleading `ARK_REASONING_EFFORT` setting to avoid hidden reasoning latency and avoidable timeouts.

- Fixed a MinerU adapter crash when MinerU returned a heading-like block with no usable text.
- Fixed stale metadata behavior where converted inputs could store the temporary parsed type rather than the original document type.
- Reduced false weak-heading splits by ignoring Markdown headings inside fenced code blocks and by merging very short split results.
- Fixed `raw_mineru` being dropped by the final validation/legacy-normalization stage after the MinerU adapter had produced it.

- Duplicate permanent uploads within the same `user_id/kb_id` scope fail fast with HTTP 409 and the message `您的知识库中已存在相同的文档{文档名}，请勿重复上传`; temporary duplicates return the existing document successfully.
- Duplicate detection now happens before queueing as well as during persistence, so the frontend can surface the conflict immediately instead of waiting for a background failure.

### Known Limitations

- Image blocks are currently persisted as markers such as `[图片]` plus any caption/footnote text returned by the parser. The service does not yet generate general image semantic descriptions.
- Native Ark vision parsing focuses on OCR, layout text, and table extraction. It does not provide full figure/photo understanding.
- MinerU can still fail on some PDFs; native parser fallback is required for production robustness.
- `documents.raw` intentionally excludes duplicated `pages` and nested `structure[].text`; use `doc_pages.content` and `doc_nodes.text` for full text inspection.

## Earlier Changes

- Improved document parsing order for PPTX text boxes.
- Fixed TXT/XLSX alignment issues between node page metadata and stored page content.
- Changed document IDs from path-dependent IDs to content-hash-derived stable IDs.
- Added document deduplication for repeated uploads of the same file.
- Added temporary document fields; temporary document visibility is scoped by user and knowledge base.
- Added temporary document cleanup endpoint.
- Changed parse duration estimation from fixed values to file-type/page-count-based estimates.
