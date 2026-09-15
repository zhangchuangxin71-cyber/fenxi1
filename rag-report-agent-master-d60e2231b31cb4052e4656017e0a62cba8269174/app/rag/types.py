from pydantic import BaseModel


class ProvidedChunkInput(BaseModel):
    chunk_id: str
    document_id: str
    document_name: str
    path: str
    content: str
