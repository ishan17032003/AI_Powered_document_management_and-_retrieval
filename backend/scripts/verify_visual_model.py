"""Verify a staged local SigLIP2 artifact without network access or persistence."""

from __future__ import annotations

import argparse
import json
from io import BytesIO
from pathlib import Path

from PIL import Image

from app.services.visual_semantic_embeddings import Siglip2EmbeddingAdapter


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a local-only text/image embedding smoke test."
    )
    parser.add_argument("model_path", type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--sha256", required=True, dest="model_sha256")
    parser.add_argument("--dimension", type=int, default=768)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument(
        "--image",
        type=Path,
        help="optional local image; otherwise a generated red test image is used",
    )
    return parser


def _image_bytes(path: Path | None) -> bytes:
    if path is None:
        image = Image.new("RGB", (224, 224), "#b51f3a")
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    return path.read_bytes()


def main() -> int:
    args = _parser().parse_args()
    adapter = Siglip2EmbeddingAdapter(
        model_path=args.model_path,
        model_revision=args.revision,
        model_sha256=args.model_sha256,
        dimension=args.dimension,
        device=args.device,
        max_batch=2,
    )
    text = adapter.embed_text(["a photo of a lady in a red dress"])[0]
    image = adapter.embed_images([_image_bytes(args.image)])[0]
    print(
        json.dumps(
            {
                "revision": adapter.model_revision,
                "sha256": adapter.model_sha256,
                "dimension": adapter.dimension,
                "text_norm": round(sum(value * value for value in text.vector), 8),
                "image_norm": round(sum(value * value for value in image.vector), 8),
                "offline": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
