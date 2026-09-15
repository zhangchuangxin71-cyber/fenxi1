import os
import uuid
import asyncio
import concurrent.futures

import PyPDF2

from .page_index import page_index
from .page_index_md import md_to_tree
from .retrieve import get_document, get_document_structure, get_page_content
from .utils import ConfigLoader, remove_fields, setup_llm_env
from .postgres_store import PostgresWorkspaceStore


def _normalize_retrieve_model(model: str) -> str:
    """Preserve supported Agents SDK prefixes and route other provider paths via LiteLLM."""
    passthrough_prefixes = ("litellm/", "openai/")
    if not model or "/" not in model:
        return model
    if model.startswith(passthrough_prefixes):
        return model
    return f"litellm/{model}"


class PageIndexClient:
    """
    A client for indexing and retrieving document content.
    Flow: index() -> get_document() / get_document_structure() / get_page_content()
    """
    def __init__(
        self,
        api_key: str = None,
        model: str = None,
        retrieve_model: str = None,
        storage_backend: str | None = None,
        postgres_dsn: str | None = None,
        postgres_auto_init_tables: bool = True,
        user_id: str | None = None,
        session_id: str | None = None,
    ):
        if api_key:
            os.environ["ARK_API_KEY"] = api_key
        setup_llm_env()
        overrides = {}
        if model:
            overrides["model"] = model
        if retrieve_model:
            overrides["retrieve_model"] = retrieve_model
        opt = ConfigLoader().load(overrides or None)
        self.model = opt.model
        self.retrieve_model = _normalize_retrieve_model(opt.retrieve_model or self.model)
        backend = (storage_backend or "postgres").strip().lower()
        dsn = postgres_dsn or os.getenv("POSTGRES_DSN") or os.getenv("DA_POSTGRES_DSN")
        self.storage_backend = backend
        self._store = None
        if backend == "postgres":
            self._store = PostgresWorkspaceStore(
                dsn=dsn,
                auto_init_tables=postgres_auto_init_tables,
                user_id=user_id,
                session_id=session_id,
            )
        elif backend == "memory":
            self._store = None
        else:
            raise ValueError(f"Unsupported storage backend: {backend}. Use 'postgres' or 'memory'.")
        self.documents = {}
        self._load_store_meta()

    def index(self, file_path: str, mode: str = "auto") -> str:
        """Index a document. Returns a document_id."""
        file_path = os.path.abspath(os.path.expanduser(file_path))
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        doc_id = str(uuid.uuid4())
        ext = os.path.splitext(file_path)[1].lower()

        is_pdf = ext == '.pdf'
        is_md = ext in ['.md', '.markdown']

        if mode == "pdf" or (mode == "auto" and is_pdf):
            print(f"Indexing PDF: {file_path}")
            result = page_index(
                doc=file_path,
                model=self.model,
                if_add_node_summary='yes',
                if_add_node_text='yes',
                if_add_node_id='yes',
                if_add_doc_description='yes'
            )
            # Extract per-page text so queries don't need the original PDF
            pages = []
            with open(file_path, 'rb') as f:
                pdf_reader = PyPDF2.PdfReader(f)
                for i, page in enumerate(pdf_reader.pages, 1):
                    pages.append({'page': i, 'content': page.extract_text() or ''})

            self.documents[doc_id] = {
                'id': doc_id,
                'type': 'pdf',
                'path': file_path,
                'doc_name': result.get('doc_name', ''),
                'doc_description': result.get('doc_description', ''),
                'page_count': len(pages),
                'structure': result['structure'],
                'pages': pages,
            }

        elif mode == "md" or (mode == "auto" and is_md):
            print(f"Indexing Markdown: {file_path}")
            coro = md_to_tree(
                md_path=file_path,
                if_thinning=False,
                if_add_node_summary='yes',
                summary_token_threshold=800,
                model=self.model,
                if_add_doc_description='yes',
                if_add_node_text='yes',
                if_add_node_id='yes'
            )
            try:
                asyncio.get_running_loop()
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    result = pool.submit(asyncio.run, coro).result()
            except RuntimeError:
                result = asyncio.run(coro)
            self.documents[doc_id] = {
                'id': doc_id,
                'type': 'md',
                'path': file_path,
                'doc_name': result.get('doc_name', ''),
                'doc_description': result.get('doc_description', ''),
                'line_count': result.get('line_count', 0),
                'structure': result['structure'],
            }
        else:
            raise ValueError(f"Unsupported file format for: {file_path}")

        print(f"Indexing complete. Document ID: {doc_id}")
        if self._store:
            self._save_doc(doc_id)
        return doc_id

    def _save_doc(self, doc_id: str):
        if not self._store:
            return
        doc = self.documents[doc_id].copy()
        # Strip text from structure nodes, redundant with pages (PDF only).
        if doc.get('structure') and doc.get('type') == 'pdf':
            doc['structure'] = remove_fields(doc['structure'], fields=['text'])
        self._store.save_doc(doc_id, doc)
        # Drop heavy fields; will lazy-load on demand
        self.documents[doc_id].pop('structure', None)
        self.documents[doc_id].pop('pages', None)

    def _load_store_meta(self):
        if not self._store:
            return
        meta = self._store.load_meta()
        for doc_id, entry in meta.items():
            self.documents[doc_id] = dict(entry, id=doc_id)

    def _ensure_doc_loaded(self, doc_id: str):
        """Load full document JSON on demand (structure, pages, etc.)."""
        if not self._store:
            return
        doc = self.documents.get(doc_id)
        if not doc or doc.get('structure') is not None:
            return
        full = self._store.load_full_doc(doc_id)
        if not full:
            return
        doc['structure'] = full.get('structure', [])
        if full.get('pages'):
            doc['pages'] = full['pages']
        # Fill lightweight metadata fields that may be absent in initial meta load.
        for key in ("file_size", "file_mtime", "file_head_md5", "file_md5", "content_hash", "index_mode"):
            if key in full and doc.get(key) in (None, ""):
                doc[key] = full.get(key)

    def get_document(self, doc_id: str) -> str:
        """Return document metadata JSON."""
        return get_document(self.documents, doc_id)

    def get_document_structure(self, doc_id: str) -> str:
        """Return document tree structure JSON (without text fields)."""
        if self._store:
            self._ensure_doc_loaded(doc_id)
        return get_document_structure(self.documents, doc_id)

    def get_page_content(self, doc_id: str, pages: str) -> str:
        """Return page content for the given pages string (e.g. '5-7', '3,8', '12')."""
        if self._store:
            self._ensure_doc_loaded(doc_id)
        return get_page_content(self.documents, doc_id, pages)
