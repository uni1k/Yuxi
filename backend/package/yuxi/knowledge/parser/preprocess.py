from __future__ import annotations

import base64
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

CONVERTIBLE_FILE_EXTENSIONS: tuple[str, ...] = (".doc", ".docm", ".wps", ".xls", ".et", ".ofd")
LIBREOFFICE_COMMANDS: tuple[str, ...] = ("soffice", "libreoffice")
DEFAULT_CONVERT_TIMEOUT_SECONDS = 120

# 每种异构格式允许转换成的目标格式（按优先级排列）
OFFICE_TARGET_FORMATS: dict[str, tuple[str, ...]] = {
    ".doc": ("docx", "pdf"),
    ".docm": ("docx",),
    ".wps": ("docx", "pdf"),
    ".xls": ("xlsx",),
    ".et": ("xlsx",),
}

try:
    from easyofd.ofd import OFD as _EasyOFD
except ImportError:
    _EasyOFD = None


class DocumentPreprocessError(RuntimeError):
    pass


@contextmanager
def normalize_file_for_parsing(file_path: str | os.PathLike[str]) -> Iterator[Path]:
    source_path = Path(file_path)
    file_ext = source_path.suffix.lower()
    if file_ext not in CONVERTIBLE_FILE_EXTENSIONS:
        yield source_path
        return

    with tempfile.TemporaryDirectory(prefix="yuxi-parser-preprocess-") as temp_dir:
        output_dir = Path(temp_dir)
        if file_ext in OFFICE_TARGET_FORMATS:
            converted_path = _convert_office_document(source_path, output_dir)
        elif file_ext == ".ofd":
            converted_path = _convert_ofd_document(source_path, output_dir)
        else:
            raise DocumentPreprocessError(f"不支持的预处理文件类型: {file_ext}")

        yield converted_path


def _convert_office_document(source_path: Path, output_dir: Path) -> Path:
    office_command = _find_libreoffice_command()
    if office_command is None:
        raise DocumentPreprocessError("解析 Office 文件需要安装 LibreOffice 或 soffice 命令")

    file_ext = source_path.suffix.lower()
    target_formats = OFFICE_TARGET_FORMATS.get(file_ext, ("docx", "pdf"))

    last_error = ""
    for target_format in target_formats:
        converted_path, error = _run_libreoffice_convert(
            command=office_command,
            source_path=source_path,
            output_dir=output_dir,
            target_format=target_format,
        )
        if converted_path is not None:
            return converted_path
        last_error = error

    raise DocumentPreprocessError(f"无法将 {source_path.suffix.lower()} 文件转换为可解析格式: {last_error}")


def _convert_ofd_document(source_path: Path, output_dir: Path) -> Path:
    output_path = output_dir / f"{source_path.stem}.pdf"

    # 如果用户配置了外部 OFD 转换命令，优先使用
    external_command = _resolve_ofd_converter_command()
    if external_command is not None:
        completed = _run_command([*external_command, str(source_path), str(output_path)])
        if completed.returncode != 0 or not output_path.exists():
            error = _format_command_error(completed)
            raise DocumentPreprocessError(f"OFD 文件转换为 PDF 失败: {error}")
        return output_path

    # 默认使用 easyofd 进行纯 Python 转换
    if _EasyOFD is None:
        raise DocumentPreprocessError(
            "解析 OFD 文件需要安装 easyofd，或通过 YUXI_OFD_TO_PDF_COMMAND 配置外部转换命令"
        )

    try:
        ofd_b64 = base64.b64encode(source_path.read_bytes()).decode("utf-8")
        ofd = _EasyOFD()
        ofd.read(ofd_b64)
        pdf_bytes = ofd.to_pdf()
        ofd.del_data()
        output_path.write_bytes(pdf_bytes)
    except Exception as exc:
        raise DocumentPreprocessError(f"OFD 文件转换为 PDF 失败: {exc}") from exc

    if not output_path.exists():
        raise DocumentPreprocessError("OFD 文件转换为 PDF 失败: 输出文件未生成")

    return output_path


def _find_libreoffice_command() -> str | None:
    for command in LIBREOFFICE_COMMANDS:
        resolved = shutil.which(command)
        if resolved:
            return resolved
    return None


def _resolve_ofd_converter_command() -> list[str] | None:
    configured_command = os.getenv("YUXI_OFD_TO_PDF_COMMAND")
    if configured_command:
        return shlex.split(configured_command)

    resolved = shutil.which("ofd2pdf")
    if resolved:
        return [resolved]

    return None


def _run_libreoffice_convert(
    *,
    command: str,
    source_path: Path,
    output_dir: Path,
    target_format: str,
) -> tuple[Path | None, str]:
    completed = _run_command(
        [
            command,
            "--headless",
            "--convert-to",
            target_format,
            "--outdir",
            str(output_dir),
            str(source_path),
        ]
    )
    converted_path = _find_converted_file(source_path, output_dir, target_format)
    if completed.returncode == 0 and converted_path is not None:
        return converted_path, ""

    return None, _format_command_error(completed)


def _find_converted_file(source_path: Path, output_dir: Path, target_format: str) -> Path | None:
    expected_path = output_dir / f"{source_path.stem}.{target_format}"
    if expected_path.exists():
        return expected_path

    matches = sorted(output_dir.glob(f"*.{target_format}"))
    return matches[0] if matches else None


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=_convert_timeout_seconds(),
    )


def _convert_timeout_seconds() -> int:
    raw_value = os.getenv("YUXI_DOCUMENT_CONVERT_TIMEOUT_SECONDS")
    if not raw_value:
        return DEFAULT_CONVERT_TIMEOUT_SECONDS

    try:
        timeout = int(raw_value)
    except ValueError:
        return DEFAULT_CONVERT_TIMEOUT_SECONDS

    return timeout if timeout > 0 else DEFAULT_CONVERT_TIMEOUT_SECONDS


def _format_command_error(completed: subprocess.CompletedProcess[str]) -> str:
    output = (completed.stderr or completed.stdout or "").strip()
    return output or f"退出码 {completed.returncode}"
