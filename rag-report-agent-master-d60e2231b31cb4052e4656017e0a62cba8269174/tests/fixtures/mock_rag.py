from app.rag.types import ProvidedChunkInput


def build_chunks() -> list[ProvidedChunkInput]:
    return [
        ProvidedChunkInput(
            chunk_id="ck_a",
            document_id="doc_a",
            document_name="示例文档.pdf",
            path="1",
            content="示例文档说明了知识库问答和报告生成。",
        )
    ]
