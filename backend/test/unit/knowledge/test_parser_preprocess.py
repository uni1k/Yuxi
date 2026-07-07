from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuxi.knowledge.parser import preprocess

pytestmark = pytest.mark.unit


def _write_macro_enabled_docx(path: Path) -> None:
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/>'
        '</Types>'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("word/document.xml", "<w:document/>\n")


def _make_fake_libreoffice_run(expected_format: str, output_suffix: str):
    def fake_run(command, **kwargs):
        assert command[:4] == ["/usr/bin/soffice", "--headless", "--convert-to", expected_format]
        source_path = Path(command[-1])
        output_path = Path(command[command.index("--outdir") + 1]) / f"{source_path.stem}.{output_suffix}"
        output_path.write_bytes(b"converted")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_run


def test_normalize_file_for_parsing_converts_doc_with_libreoffice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path = tmp_path / "legacy.doc"
    source_path.write_bytes(b"fake doc")

    def fake_which(command: str):
        return "/usr/bin/soffice" if command == "soffice" else None

    monkeypatch.setattr(preprocess.shutil, "which", fake_which)
    monkeypatch.setattr(preprocess.subprocess, "run", _make_fake_libreoffice_run("docx", "docx"))

    with preprocess.normalize_file_for_parsing(source_path) as normalized_path:
        assert normalized_path.suffix == ".docx"


def test_normalize_file_for_parsing_converts_docm_with_libreoffice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path = tmp_path / "macro.docm"
    source_path.write_bytes(b"fake docm")

    def fake_which(command: str):
        return "/usr/bin/soffice" if command == "soffice" else None

    monkeypatch.setattr(preprocess.shutil, "which", fake_which)
    monkeypatch.setattr(preprocess.subprocess, "run", _make_fake_libreoffice_run("docx", "docx"))

    with preprocess.normalize_file_for_parsing(source_path) as normalized_path:
        assert normalized_path.suffix == ".docx"


def test_normalize_file_for_parsing_detects_macro_enabled_docx_disguised_as_docx(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path = tmp_path / "disguised.docx"
    _write_macro_enabled_docx(source_path)

    def fake_which(command: str):
        return "/usr/bin/soffice" if command == "soffice" else None

    monkeypatch.setattr(preprocess.shutil, "which", fake_which)
    monkeypatch.setattr(preprocess.subprocess, "run", _make_fake_libreoffice_run("docx", "docx"))

    with preprocess.normalize_file_for_parsing(source_path) as normalized_path:
        assert normalized_path.suffix == ".docx"


def test_normalize_file_for_parsing_converts_xls_with_libreoffice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path = tmp_path / "legacy.xls"
    source_path.write_bytes(b"fake xls")

    def fake_which(command: str):
        return "/usr/bin/soffice" if command == "soffice" else None

    monkeypatch.setattr(preprocess.shutil, "which", fake_which)
    monkeypatch.setattr(preprocess.subprocess, "run", _make_fake_libreoffice_run("xlsx", "xlsx"))

    with preprocess.normalize_file_for_parsing(source_path) as normalized_path:
        assert normalized_path.suffix == ".xlsx"


def test_normalize_file_for_parsing_converts_et_with_libreoffice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path = tmp_path / "wps_table.et"
    source_path.write_bytes(b"fake et")

    def fake_which(command: str):
        return "/usr/bin/soffice" if command == "soffice" else None

    monkeypatch.setattr(preprocess.shutil, "which", fake_which)
    monkeypatch.setattr(preprocess.subprocess, "run", _make_fake_libreoffice_run("xlsx", "xlsx"))

    with preprocess.normalize_file_for_parsing(source_path) as normalized_path:
        assert normalized_path.suffix == ".xlsx"


def test_export_ofd_to_images_uses_configured_exporter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path = tmp_path / "invoice.ofd"
    source_path.write_bytes(b"fake ofd")
    monkeypatch.setenv("YUXI_OFD_TO_IMAGE_COMMAND", "ofd2images")

    def fake_run(command, **kwargs):
        assert command == ["ofd2images", str(source_path), str(Path(command[-1]))]
        output_dir = Path(command[-1])
        (output_dir / "page_2.png").write_bytes(b"page2")
        (output_dir / "page_1.png").write_bytes(b"page1")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(preprocess.subprocess, "run", fake_run)

    with preprocess.export_ofd_to_images(source_path) as image_paths:
        assert [path.name for path in image_paths] == ["page_1.png", "page_2.png"]


def test_export_ofd_to_images_uses_bundled_ofdrw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path = tmp_path / "invoice.ofd"
    source_path.write_bytes(b"fake ofd")
    monkeypatch.delenv("YUXI_OFD_TO_IMAGE_COMMAND", raising=False)

    def fake_which(command: str):
        if command == "yuxi-ofdrw-ofd2images":
            return "/usr/local/bin/yuxi-ofdrw-ofd2images"
        return None

    def fake_run(command, **kwargs):
        assert command == ["/usr/local/bin/yuxi-ofdrw-ofd2images", str(source_path), str(Path(command[-1]))]
        output_dir = Path(command[-1])
        (output_dir / "page_1.png").write_bytes(b"page1")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(preprocess.shutil, "which", fake_which)
    monkeypatch.setattr(preprocess.subprocess, "run", fake_run)

    with preprocess.export_ofd_to_images(source_path) as image_paths:
        assert [path.name for path in image_paths] == ["page_1.png"]


def test_export_ofd_to_images_raises_when_exporter_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path = tmp_path / "invoice.ofd"
    source_path.write_bytes(b"fake ofd")
    monkeypatch.delenv("YUXI_OFD_TO_IMAGE_COMMAND", raising=False)
    monkeypatch.setattr(preprocess.shutil, "which", lambda command: None)

    with pytest.raises(preprocess.DocumentPreprocessError, match="ofdrw"):
        with preprocess.export_ofd_to_images(source_path):
            pass
