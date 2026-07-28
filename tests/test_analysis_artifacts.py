import json
from pathlib import Path

import pytest

from app.analysis.artifacts import AnalysisArtifactStore
from app.config import Settings
from app.errors import ErrorCode, PatentServiceError


def test_artifact_store_reads_valid_file_and_discards_it(tmp_path: Path):
    store = AnalysisArtifactStore(
        Settings(analysis_artifact_dir=str(tmp_path / "artifacts"))
    )
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-prepared")

    artifact = store.create_from_path(
        source,
        filename="EP1234567A1.pdf",
        mime_type="application/pdf",
    )

    assert store.read_bytes(artifact) == b"%PDF-prepared"
    assert artifact.sha256
    store.discard(artifact.artifact_id)
    with pytest.raises(PatentServiceError) as excinfo:
        store.read_bytes(artifact)
    assert excinfo.value.code is ErrorCode.ANALYSIS_ARTIFACT_UNAVAILABLE


def test_artifact_cleanup_removes_expired_directory(tmp_path: Path):
    store = AnalysisArtifactStore(
        Settings(analysis_artifact_dir=str(tmp_path / "artifacts"))
    )
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-prepared")
    artifact = store.create_from_path(
        source,
        filename="WO2026000001A1.pdf",
        mime_type="application/pdf",
    )
    metadata_path = store.root / artifact.artifact_id / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["expires_at_epoch"] = 0
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    assert store.cleanup_expired() == 1
    assert not (store.root / artifact.artifact_id).exists()
