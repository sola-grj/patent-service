import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.errors import ErrorCode, PatentServiceError

_MIME_TYPES = {
    "pdf": {"application/pdf"},
    "doc": {"application/msword", "application/octet-stream"},
    "docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
        "application/x-zip-compressed",
        "application/octet-stream",
    },
}
_OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")


@dataclass(slots=True)
class StoredUpload:
    path: Path
    filename: str
    file_type: str
    content_type: str


def validate_upload(
    path: Path,
    *,
    filename: str,
    content_type: str,
    settings: Settings,
) -> StoredUpload:
    extension = Path(filename).suffix.lower().lstrip(".")
    if extension not in _MIME_TYPES:
        raise PatentServiceError(
            code=ErrorCode.UNSUPPORTED_FILE_TYPE,
            status_code=415,
            message="Only PDF, DOC and DOCX files are supported.",
            details={"filename": filename},
        )
    normalized_mime = content_type.split(";", 1)[0].strip().lower()
    if normalized_mime not in _MIME_TYPES[extension]:
        raise PatentServiceError(
            code=ErrorCode.FILE_SIGNATURE_MISMATCH,
            status_code=422,
            message="The upload MIME type does not match its extension.",
            details={"filename": filename, "content_type": normalized_mime},
        )
    with path.open("rb") as stream:
        header = stream.read(8)
    valid = False
    if extension == "pdf":
        valid = header.startswith(b"%PDF-")
    elif extension == "doc":
        valid = header == _OLE_SIGNATURE
    else:
        valid = _is_docx(path, settings, filename=filename)
    if not valid:
        raise PatentServiceError(
            code=ErrorCode.FILE_SIGNATURE_MISMATCH,
            status_code=422,
            message="The upload content does not match its extension.",
            details={"filename": filename},
        )
    return StoredUpload(
        path=path,
        filename=filename,
        file_type=extension,
        content_type=normalized_mime,
    )


def convert_doc_to_docx(path: Path, settings: Settings) -> Path:
    command = _libreoffice_command(settings)
    if not command:
        raise PatentServiceError(
            code=ErrorCode.DOCUMENT_CONVERSION_UNAVAILABLE,
            status_code=503,
            message="LibreOffice is required to process legacy DOC files.",
            details={"filename": path.name},
        )
    output_dir = path.parent / f"{path.stem}-converted"
    profile_dir = Path(tempfile.mkdtemp(prefix="patent-lo-profile-"))
    output_dir.mkdir(parents=True, exist_ok=True)
    _configure_libreoffice_profile(profile_dir)
    profile_uri = profile_dir.resolve().as_uri()
    try:
        completed = subprocess.run(
            [
                command,
                "--headless",
                "--invisible",
                "--nologo",
                "--nodefault",
                "--norestore",
                "--nolockcheck",
                f"-env:UserInstallation={profile_uri}",
                "--convert-to",
                "docx",
                "--outdir",
                str(output_dir),
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=settings.analysis_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PatentServiceError(
            code=ErrorCode.DOCUMENT_CONVERSION_FAILED,
            status_code=422,
            message="Legacy DOC conversion timed out.",
            details={"filename": path.name},
        ) from exc
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
    candidates = list(output_dir.glob("*.docx"))
    if completed.returncode != 0 or not candidates:
        raise PatentServiceError(
            code=ErrorCode.DOCUMENT_CONVERSION_FAILED,
            status_code=422,
            message="Legacy DOC conversion failed.",
            details={
                "filename": path.name,
                "returncode": completed.returncode,
                "stderr": completed.stderr[-1000:],
            },
        )
    return candidates[0]


def _is_docx(path: Path, settings: Settings, *, filename: str) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > settings.analysis_max_docx_entries:
                raise _archive_limit_error(filename, "too many archive entries")
            if sum(info.file_size for info in infos) > settings.analysis_max_docx_uncompressed_bytes:
                raise _archive_limit_error(filename, "expanded archive is too large")
            if any(
                info.file_size > 10 * 1024 * 1024
                and info.file_size > max(info.compress_size, 1) * 1000
                for info in infos
            ):
                raise _archive_limit_error(filename, "suspicious compression ratio")
            names = {info.filename.replace("\\", "/") for info in infos}
            return "[Content_Types].xml" in names and "word/document.xml" in names
    except zipfile.BadZipFile:
        return False


def _archive_limit_error(filename: str, reason: str) -> PatentServiceError:
    return PatentServiceError(
        code=ErrorCode.UPLOAD_TOO_LARGE,
        status_code=422,
        message="The DOCX archive exceeds a configured safety limit.",
        details={"filename": filename, "reason": reason},
    )


def _configure_libreoffice_profile(profile_dir: Path) -> None:
    user_dir = profile_dir / "user"
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "registrymodifications.xcu").write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<oor:items xmlns:oor=\"http://openoffice.org/2001/registry\">"
        "<item oor:path=\"/org.openoffice.Office.Common/Security/Scripting\">"
        "<prop oor:name=\"MacroSecurityLevel\" oor:op=\"fuse\"><value>3</value></prop>"
        "</item></oor:items>",
        encoding="utf-8",
    )


def _libreoffice_command(settings: Settings) -> str | None:
    if settings.libreoffice_command:
        return settings.libreoffice_command
    detected = shutil.which("soffice") or shutil.which("libreoffice")
    if detected:
        return detected
    windows = Path(r"C:\Program Files\LibreOffice\program\soffice.com")
    return str(windows) if windows.is_file() else None
