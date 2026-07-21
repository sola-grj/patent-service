import io
import re
import zipfile
from pathlib import Path, PurePosixPath

import img2pdf
from PIL import Image


_PAGE_ENTRY = re.compile(rb"<DP\s+N=(\d+)\s+IMA=([^\s>]+)", re.IGNORECASE)


def convert_wipo_zip_to_pdf(zip_path: Path, pdf_path: Path) -> list[str]:
    """Create an image-only PDF from the official WIPO TIFF publication package."""
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        name_by_lower = {name.lower(): name for name in names}
        ordered_names = _ordered_tiff_pages(archive, name_by_lower)
        if not ordered_names:
            raise ValueError("WIPO ZIP does not contain TIFF publication pages")

        images: list[bytes] = []
        for name in ordered_names:
            payload = archive.read(name)
            _verify_single_page_tiff(payload, name)
            images.append(payload)

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = pdf_path.with_suffix(pdf_path.suffix + ".part")
    try:
        partial_path.write_bytes(img2pdf.convert(*images))
        partial_path.replace(pdf_path)
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise
    return ordered_names


def _ordered_tiff_pages(
    archive: zipfile.ZipFile, name_by_lower: dict[str, str]
) -> list[str]:
    pag_list_name = name_by_lower.get("pag.lst")
    ordered: list[tuple[int, str]] = []
    if pag_list_name:
        for page_number, raw_name in _PAGE_ENTRY.findall(archive.read(pag_list_name)):
            requested_name = raw_name.decode("ascii", errors="strict")
            safe_name = _safe_member_name(requested_name)
            actual_name = name_by_lower.get(safe_name.lower())
            if actual_name and actual_name.lower().endswith((".tif", ".tiff")):
                ordered.append((int(page_number), actual_name))
    if ordered:
        return [name for _, name in sorted(ordered)]

    return sorted(
        name
        for name in name_by_lower.values()
        if name.lower().endswith((".tif", ".tiff"))
        and _safe_member_name(name)
    )


def _safe_member_name(value: str) -> str:
    candidate = PurePosixPath(value.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts or len(candidate.parts) != 1:
        raise ValueError("WIPO ZIP contains an unsafe page path")
    return candidate.as_posix()


def _verify_single_page_tiff(payload: bytes, name: str) -> None:
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.format != "TIFF":
                raise ValueError(f"WIPO page is not TIFF: {name}")
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError(f"WIPO TIFF page contains multiple frames: {name}")
            image.verify()
    except (OSError, SyntaxError) as exc:
        raise ValueError(f"WIPO TIFF page is invalid: {name}") from exc
