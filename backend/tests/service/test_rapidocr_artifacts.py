from pathlib import Path

from app.services.extraction_service import _rapidocr_artifacts


def test_rapidocr_artifacts_support_flat_downloader_layout(tmp_path: Path) -> None:
    (tmp_path / "PP-OCRv6_det_small.onnx").touch()
    (tmp_path / "ch_ppocr_mobile_v2.0_cls_mobile.onnx").touch()
    (tmp_path / "PP-OCRv6_rec_small.onnx").touch()
    (tmp_path / "ppocr_keys_v1.txt").touch()

    det, cls, rec, keys = _rapidocr_artifacts(tmp_path)

    assert det.name == "PP-OCRv6_det_small.onnx"
    assert cls.name == "ch_ppocr_mobile_v2.0_cls_mobile.onnx"
    assert rec.name == "PP-OCRv6_rec_small.onnx"
    assert keys is not None and keys.name == "ppocr_keys_v1.txt"


def test_rapidocr_artifacts_support_nested_layout(tmp_path: Path) -> None:
    det_dir = tmp_path / "onnx" / "det"
    cls_dir = tmp_path / "onnx" / "cls"
    rec_dir = tmp_path / "onnx" / "rec"
    det_dir.mkdir(parents=True)
    cls_dir.mkdir(parents=True)
    rec_dir.mkdir(parents=True)
    (det_dir / "det.onnx").touch()
    (cls_dir / "cls.onnx").touch()
    (rec_dir / "rec.onnx").touch()

    det, cls, rec, keys = _rapidocr_artifacts(tmp_path)

    assert det == det_dir / "det.onnx"
    assert cls == cls_dir / "cls.onnx"
    assert rec == rec_dir / "rec.onnx"
    assert keys is None
