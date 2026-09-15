#!/usr/bin/env bash
set -euo pipefail

API_URL="${API_URL:-http://127.0.0.1:8000}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${OUT_DIR:-/tmp/mineru_smoke_api_formats}"
HTML_URL="${HTML_URL:-https://www.sino-life.com/}"
SKIP_HTML_URL="${SKIP_HTML_URL:-false}"

mkdir -p "$OUT_DIR"

echo "Checking MinerU API health: $API_URL/health"
curl --noproxy '*' -fsS "$API_URL/health" > "$OUT_DIR/health.json"
echo
cat "$OUT_DIR/health.json"
echo

run_case() {
    local name="$1"
    local file_path="$2"
    local output_path="$OUT_DIR/${name}.zip"

    echo "Parsing $name: $file_path"
    curl --noproxy '*' -fsS -X POST "$API_URL/file_parse" \
        -F "files=@${file_path}" \
        -F "backend=pipeline" \
        -F "parse_method=auto" \
        -F "return_md=true" \
        -F "return_middle_json=true" \
        -F "return_content_list=true" \
        -F "return_images=true" \
        -F "response_format_zip=true" \
        -F "return_original_file=true" \
        -o "$output_path"
    echo "Saved: $output_path"
}

run_html_url_case() {
    local name="$1"
    local url="$2"
    local output_path="$OUT_DIR/${name}.zip"

    echo "Parsing $name: $url"
    curl --noproxy '*' -fsS -X POST "$API_URL/file_parse" \
        -F "html_urls=${url}" \
        -F "backend=pipeline" \
        -F "parse_method=auto" \
        -F "return_md=true" \
        -F "return_middle_json=true" \
        -F "return_model_output=true" \
        -F "return_content_list=true" \
        -F "return_images=false" \
        -F "response_format_zip=true" \
        -F "return_original_file=true" \
        -o "$output_path"
    echo "Saved: $output_path"
}

run_case "pdf" "$ROOT_DIR/demo/pdfs/demo1.pdf"
run_case "word_docx" "$ROOT_DIR/demo/office_docs/docx_01.docx"
run_case "ppt_pptx" "$ROOT_DIR/demo/office_docs/pptx_01.pptx"
run_case "excel_xlsx" "$ROOT_DIR/demo/office_docs/xlsx_01.xlsx"
run_case "csv" "$ROOT_DIR/demo/office_docs/csv_01.csv"
run_case "markdown" "$ROOT_DIR/demo/text_docs/markdown_01.md"
run_case "txt" "$ROOT_DIR/demo/text_docs/txt_01.txt"
run_case "image_png" "$ROOT_DIR/demo/images/scan_01.png"
if [[ "${SKIP_HTML_URL}" != "true" ]]; then
    run_html_url_case "html_url" "$HTML_URL"
fi

echo
echo "Smoke outputs:"
find "$OUT_DIR" -maxdepth 1 -type f -printf "%f\n" | sort
