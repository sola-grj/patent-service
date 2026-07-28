import hashlib
import json
import re
import shutil
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import PatentAnalysisArtifact

_ARTIFACT_ID = re.compile(r"^[0-9a-f]{32}$")


class AnalysisArtifactStore:
    """Short-lived server-side files prepared during patent analysis."""

    def __init__(self, settings: Settings) -> None:
        self._root = Path(
            settings.analysis_artifact_dir
            or Path(tempfile.gettempdir())
            / "patent-service"
            / "analysis-artifacts"
        ).resolve()
        self._ttl_seconds = max(settings.analysis_artifact_ttl_seconds, 60)

    @property
    def root(self) -> Path:
        return self._root

    def create_from_path(
        self,
        source_path: Path,
        *,
        filename: str,
        mime_type: str,
    ) -> PatentAnalysisArtifact:
        if not source_path.is_file():
            raise _artifact_unavailable("The prepared patent file is unavailable.")
        self.cleanup_expired()
        artifact_id = uuid.uuid4().hex
        safe_filename = _safe_filename(filename) or "patent-document.bin"
        artifact_dir = self._root / artifact_id
        staging_dir = self._root / f".{artifact_id}.{uuid.uuid4().hex}.tmp"
        target = staging_dir / safe_filename
        expires_at_epoch = int(time.time()) + self._ttl_seconds
        try:
            staging_dir.mkdir(parents=True, exist_ok=False)
            shutil.copyfile(source_path, target)
            sha256 = _sha256(target)
            byte_size = target.stat().st_size
            metadata = {
                "artifact_id": artifact_id,
                "filename": safe_filename,
                "mime_type": mime_type or "application/octet-stream",
                "byte_size": byte_size,
                "sha256": sha256,
                "expires_at_epoch": expires_at_epoch,
            }
            (staging_dir / "metadata.json").write_text(
                json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
            staging_dir.replace(artifact_dir)
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            shutil.rmtree(artifact_dir, ignore_errors=True)
            raise
        return PatentAnalysisArtifact(
            artifact_id=artifact_id,
            filename=safe_filename,
            mime_type=metadata["mime_type"],
            byte_size=byte_size,
            sha256=sha256,
            expires_at=datetime.fromtimestamp(
                expires_at_epoch, tz=UTC
            ).isoformat().replace("+00:00", "Z"),
        )

    def read_bytes(self, artifact: PatentAnalysisArtifact) -> bytes:
        path, metadata = self._resolve(artifact)
        content = path.read_bytes()
        if len(content) != artifact.byte_size:
            raise _artifact_unavailable("The prepared patent file size is invalid.")
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise _artifact_unavailable("The prepared patent file checksum is invalid.")
        if metadata.get("sha256") != artifact.sha256:
            raise _artifact_unavailable("The prepared patent file metadata is invalid.")
        return content

    def discard(self, artifact_id: str) -> None:
        if not _ARTIFACT_ID.fullmatch(artifact_id):
            return
        shutil.rmtree(self._root / artifact_id, ignore_errors=True)

    def cleanup_expired(self) -> int:
        if not self._root.is_dir():
            return 0
        now = int(time.time())
        removed = 0
        for directory in self._root.iterdir():
            if not directory.is_dir() or not _ARTIFACT_ID.fullmatch(directory.name):
                continue
            metadata_path = directory / "metadata.json"
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                expired = int(metadata.get("expires_at_epoch") or 0) <= now
            except (OSError, ValueError, TypeError):
                expired = True
            if expired:
                shutil.rmtree(directory, ignore_errors=True)
                removed += 1
        return removed

    def _resolve(
        self, artifact: PatentAnalysisArtifact
    ) -> tuple[Path, dict[str, object]]:
        if not _ARTIFACT_ID.fullmatch(artifact.artifact_id):
            raise _artifact_unavailable("The prepared patent file identifier is invalid.")
        directory = (self._root / artifact.artifact_id).resolve()
        if directory.parent != self._root:
            raise _artifact_unavailable("The prepared patent file identifier is invalid.")
        metadata_path = directory / "metadata.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise _artifact_unavailable(
                "The prepared patent file is unavailable or expired."
            ) from exc
        if int(metadata.get("expires_at_epoch") or 0) <= int(time.time()):
            self.discard(artifact.artifact_id)
            raise _artifact_unavailable("The prepared patent file has expired.")
        filename = _safe_filename(str(metadata.get("filename") or ""))
        if not filename or filename != artifact.filename:
            raise _artifact_unavailable("The prepared patent file metadata is invalid.")
        path = (directory / filename).resolve()
        if path.parent != directory or not path.is_file():
            raise _artifact_unavailable("The prepared patent file is unavailable.")
        return path, metadata


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", Path(value).name).strip("-")


def _artifact_unavailable(message: str) -> PatentServiceError:
    return PatentServiceError(
        code=ErrorCode.ANALYSIS_ARTIFACT_UNAVAILABLE,
        status_code=410,
        message=message,
        source="service",
    )
