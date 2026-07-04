from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from yuxi.knowledge.parser import preprocess

pytestmark = pytest.mark.unit


def test_normalize_file_for_parsing_converts_doc_with_libreoffice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path = tmp_path / "legacy.doc"
    source_path.write_bytes(b"fake doc")

    def fake_which(command: str):
        return "/usr/bin/soffice" if command == "soffice" else None

    def fake_run(command, **kwargs):
        assert command[:4] == ["/usr/bin/soffice", "--headless", "--convert-to", "docx"]
        output_path = Path(command[command.index("--outdir") + 1]) / "legacy.docx"
        output_path.write_bytes(b"converted docx")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(preprocess.shutil, "which", fake_which)
    monkeypatch.setattr(preprocess.subprocess, "run", fake_run)

    with preprocess.normalize_file_for_parsing(source_path) as normalized_path:
        assert normalized_path.suffix == ".docx"
        assert normalized_path.read_bytes() == b"converted docx"


def test_normalize_file_for_parsing_converts_ofd_with_configured_converter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path = tmp_path / "invoice.ofd"
    source_path.write_bytes(b"fake ofd")
    monkeypatch.setenv("YUXI_OFD_TO_PDF_COMMAND", "ofd2pdf")

    def fake_run(command, **kwargs):
        assert command == ["ofd2pdf", str(source_path), str(Path(command[-1]))]
        Path(command[-1]).write_bytes(b"converted pdf")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(preprocess.subprocess, "run", fake_run)

    with preprocess.normalize_file_for_parsing(source_path) as normalized_path:
        assert normalized_path.suffix == ".pdf"
        assert normalized_path.read_bytes() == b"converted pdf"


def test_normalize_file_for_parsing_converts_ofd_with_easyofd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path = tmp_path / "invoice.ofd"
    source_path.write_bytes(b"fake ofd")
    monkeypatch.delenv("YUXI_OFD_TO_PDF_COMMAND", raising=False)
    monkeypatch.setattr(preprocess.shutil, "which", lambda command: None)

    class FakeOFD:
        def read(self, _ofd_b64: str) -> None:
            pass

        def to_pdf(self) -> bytes:
            return b"converted pdf"

        def del_data(self) -> None:
            pass

    monkeypatch.setattr(preprocess, "_EasyOFD", FakeOFD)

    with preprocess.normalize_file_for_parsing(source_path) as normalized_path:
        assert normalized_path.suffix == ".pdf"
        assert normalized_path.read_bytes() == b"converted pdf"


def test_normalize_file_for_parsing_raises_when_ofd_converter_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path = tmp_path / "invoice.ofd"
    source_path.write_bytes(b"fake ofd")
    monkeypatch.delenv("YUXI_OFD_TO_PDF_COMMAND", raising=False)
    monkeypatch.setattr(preprocess.shutil, "which", lambda command: None)
    monkeypatch.setattr(preprocess, "_EasyOFD", None)

    with pytest.raises(preprocess.DocumentPreprocessError, match="OFD"):
        with preprocess.normalize_file_for_parsing(source_path):
            pass
