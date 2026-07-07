from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

CONVERTIBLE_FILE_EXTENSIONS: tuple[str, ...] = (".doc", ".docm", ".wps", ".xls", ".et")
LIBREOFFICE_COMMANDS: tuple[str, ...] = ("soffice", "libreoffice")
DEFAULT_CONVERT_TIMEOUT_SECONDS = 120
OFD_IMAGE_EXTENSIONS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".bmp")

# 每种异构格式允许转换成的目标格式（按优先级排列）
OFFICE_TARGET_FORMATS: dict[str, tuple[str, ...]] = {
    ".doc": ("docx", "pdf"),
    ".docm": ("docx",),
    ".wps": ("docx", "pdf"),
    ".xls": ("xlsx",),
    ".et": ("xlsx",),
}

class DocumentPreprocessError(RuntimeError):
    pass


def _is_macro_enabled_docx(source_path: Path) -> bool:
    import zipfile

    try:
        with zipfile.ZipFile(source_path, "r") as zf:
            content_types = zf.read("[Content_Types].xml").decode("utf-8", errors="ignore")
    except (zipfile.BadZipFile, KeyError, OSError):
        return False
    return "application/vnd.ms-word.document.macroEnabled.main+xml" in content_types


@contextmanager
def normalize_file_for_parsing(file_path: str | os.PathLike[str]) -> Iterator[Path]:
    source_path = Path(file_path)
    actual_ext = source_path.suffix.lower()

    if actual_ext == ".docx" and _is_macro_enabled_docx(source_path):
        actual_ext = ".docm"

    if actual_ext not in CONVERTIBLE_FILE_EXTENSIONS:
        yield source_path
        return

    with tempfile.TemporaryDirectory(prefix="yuxi-parser-preprocess-") as temp_dir:
        output_dir = Path(temp_dir)
        work_path = source_path

        if actual_ext == ".docm" and source_path.suffix.lower() != ".docm":
            work_path = output_dir / f"{source_path.stem}.docm"
            shutil.copy2(source_path, work_path)

        if actual_ext in OFFICE_TARGET_FORMATS:
            converted_path = _convert_office_document(work_path, output_dir)
        else:
            raise DocumentPreprocessError(f"不支持的预处理文件类型: {actual_ext}")

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


@contextmanager
def export_ofd_to_images(file_path: str | os.PathLike[str]) -> Iterator[list[Path]]:
    source_path = Path(file_path)
    if source_path.suffix.lower() != ".ofd":
        raise DocumentPreprocessError(f"不支持的 OFD 文件类型: {source_path.suffix.lower()}")

    exporter_command = _resolve_ofd_export_command()
    if exporter_command is None:
        raise DocumentPreprocessError(
            "解析 OFD 文件需要可用的 ofdrw 图片导出命令，"
            "可使用内置 yuxi-ofdrw-ofd2images 或通过 YUXI_OFD_TO_IMAGE_COMMAND 配置外部命令"
        )

    with tempfile.TemporaryDirectory(prefix="yuxi-ofd-images-") as temp_dir:
        output_dir = Path(temp_dir)
        completed = _run_command([*exporter_command, str(source_path), str(output_dir)])
        image_paths = _find_exported_images(output_dir)
        if completed.returncode != 0 or not image_paths:
            error = _format_command_error(completed)
            raise DocumentPreprocessError(f"OFD 导出图片失败: {error}")
        yield image_paths


def _find_libreoffice_command() -> str | None:
    for command in LIBREOFFICE_COMMANDS:
        resolved = shutil.which(command)
        if resolved:
            return resolved
    return None


def _resolve_ofd_export_command() -> list[str] | None:
    configured_command = os.getenv("YUXI_OFD_TO_IMAGE_COMMAND")
    if configured_command:
        return shlex.split(configured_command)

    bundled_command = shutil.which("yuxi-ofdrw-ofd2images")
    if bundled_command:
        return [bundled_command]

    return None


def _find_exported_images(output_dir: Path) -> list[Path]:
    return sorted(
        [
            path
            for path in output_dir.iterdir()
            if path.is_file() and path.suffix.lower() in OFD_IMAGE_EXTENSIONS
        ],
        key=lambda path: path.name,
    )


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
