from __future__ import annotations


class DuplicateDocumentError(ValueError):
    """Raised when the same document already exists in a user/kb scope."""

    def __init__(self, existing_doc_name: str | None = None) -> None:
        self.existing_doc_name = str(existing_doc_name or "").strip()
        if self.existing_doc_name:
            message = f"您的知识库中已存在相同的文档{self.existing_doc_name}，请勿重复上传"
        else:
            message = "您的知识库中已存在相同的文档，请勿重复上传"
        super().__init__(message)
