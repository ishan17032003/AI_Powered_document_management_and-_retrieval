import os
import sys
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("model_download")

# Force settings for the download environment
os.environ["DOCVAULT_DATABASE_URL"] = "sqlite:////tmp/dummy.db"
os.environ["DOCVAULT_SKIP_MODEL_PREFETCH"] = "false"
os.environ["DOCVAULT_LLM_PROVIDER"] = "none"

try:
    from huggingface_hub import snapshot_download
except ImportError:
    log.error("huggingface_hub is not installed! Make sure you are using --all-extras.")
    sys.exit(1)

def download_all():
    models = [
        "BAAI/bge-m3",
        "BAAI/bge-reranker-v2-m3",
        "google/siglip2-base-patch16-224",
    ]
    
    for model_id in models:
        log.info(f"Downloading {model_id}...")
        try:
            snapshot_download(repo_id=model_id, local_files_only=False)
            log.info(f"Successfully downloaded {model_id}")
        except Exception as e:
            log.error(f"Failed to download {model_id}: {e}")
            sys.exit(1)

    log.info("Downloading Docling artifacts...")
    try:
        from app.services.extraction_service import get_docling_converter
        # Calling get_docling_converter() forces Docling and RapidOCR to download their models
        # into settings.storage_dir / "docvault-docling-artifacts"
        get_docling_converter()
        log.info("Successfully downloaded Docling models")
    except Exception as e:
        log.error(f"Failed to download Docling models: {e}")
        sys.exit(1)

if __name__ == "__main__":
    download_all()
