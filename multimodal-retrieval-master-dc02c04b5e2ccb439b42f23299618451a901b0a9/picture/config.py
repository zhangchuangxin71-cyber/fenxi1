from pathlib import Path

PICTURE_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = PICTURE_ROOT.parent
DEFAULT_MODEL_PATH = WORKSPACE_ROOT / "video_retrieval" / "models"
DEFAULT_IMAGE_DIR = WORKSPACE_ROOT / "data" / "images"

INDEX_KIND = "picture_image"
INDEX_KIND_CAPTION = "picture_caption_bge"
MODALITY = "image"

PICTURE_BGE_MODEL_ENV = "PICTURE_BGE_MODEL"
DEFAULT_BGE_MODEL_NAME = "BAAI/bge-large-zh-v1.5"
