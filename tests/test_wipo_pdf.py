import io
import zipfile
from pathlib import Path

import pikepdf
from PIL import Image

from app.utils.wipo_pdf import convert_wipo_zip_to_pdf


def _tiff(color: int) -> bytes:
    output = io.BytesIO()
    Image.new("1", (100, 140), color=color).save(
        output, format="TIFF", compression="group4", dpi=(300, 300)
    )
    return output.getvalue()


def test_convert_wipo_zip_uses_pag_list_order(tmp_path: Path):
    zip_path = tmp_path / "publication.zip"
    pdf_path = tmp_path / "publication.pdf"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("000001.tif", _tiff(1))
        archive.writestr("000002.tif", _tiff(0))
        archive.writestr(
            "Pag.lst",
            "<DOC NBP=2><DP N=1 IMA=000002.tif><DP N=2 IMA=000001.tif></DOC>",
        )

    ordered_pages = convert_wipo_zip_to_pdf(zip_path, pdf_path)

    assert ordered_pages == ["000002.tif", "000001.tif"]
    assert pdf_path.read_bytes().startswith(b"%PDF-")
    with pikepdf.Pdf.open(pdf_path) as pdf:
        assert len(pdf.pages) == 2


def test_convert_wipo_zip_rejects_package_without_tiff_pages(tmp_path: Path):
    zip_path = tmp_path / "publication.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("wo-published-application.xml", "<document />")

    try:
        convert_wipo_zip_to_pdf(zip_path, tmp_path / "publication.pdf")
    except ValueError as exc:
        assert "does not contain TIFF" in str(exc)
    else:
        raise AssertionError("Expected ZIP without TIFF pages to be rejected")
