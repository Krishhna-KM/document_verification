"""Turn an uploaded image / PDF into base64 data-URLs ready for the vision model."""
import base64
import io
import logging
from typing import List

import pymupdf
from PIL import Image, ImageOps, UnidentifiedImageError

from backend.config import settings
from backend.errors import DocumentError

log = logging.getLogger(__name__)

PDF_DPI = 200
MIN_SIDE_PX = 32                    # smaller than this cannot hold a readable document
Image.MAX_IMAGE_PIXELS = 60_000_000  # PIL raises DecompressionBombError above 2x this


def _to_data_url(img: Image.Image) -> str:
    img = ImageOps.exif_transpose(img).convert("RGB")
    if min(img.size) < MIN_SIDE_PX:
        raise DocumentError(f"The image is too small ({img.width}x{img.height}px) to contain a readable document.")
    longest = max(img.size)
    if longest > settings.IMAGE_MAX_SIDE:
        scale = settings.IMAGE_MAX_SIDE / longest
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _load_pdf(raw: bytes) -> List[str]:
    try:
        pdf = pymupdf.open(stream=raw, filetype="pdf")
    except Exception as e:
        raise DocumentError("The PDF could not be opened; it may be corrupt.", details=str(e)) from e
    try:
        if pdf.needs_pass or pdf.is_encrypted:
            raise DocumentError("Password-protected PDFs are not supported.", code="PDF_ENCRYPTED")
        if pdf.page_count == 0:
            raise DocumentError("The PDF has no pages.")
        urls = []
        for index in range(min(pdf.page_count, settings.MAX_PDF_PAGES)):
            try:
                page = pdf[index]
                # never render a huge page (e.g. an A0 poster) at full DPI
                longest_pt = max(page.rect.width, page.rect.height, 1)
                zoom = min(PDF_DPI / 72, settings.IMAGE_MAX_SIDE * 1.5 / longest_pt)
                png = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")
                urls.append(_to_data_url(Image.open(io.BytesIO(png))))
            except DocumentError:
                raise
            except Exception as e:
                raise DocumentError(f"Page {index + 1} of the PDF could not be rendered.", details=str(e)) from e
        return urls
    finally:
        pdf.close()


def load_document(raw: bytes) -> List[str]:
    """Return one data-URL per page (PDFs capped at MAX_PDF_PAGES)."""
    if not raw:
        raise DocumentError("The uploaded file is empty.")

    if raw[:5] == b"%PDF-":
        return _load_pdf(raw)

    try:
        return [_to_data_url(Image.open(io.BytesIO(raw)))]
    except DocumentError:
        raise
    except Image.DecompressionBombError as e:
        raise DocumentError("The image dimensions are too large to process.", code="IMAGE_TOO_LARGE") from e
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as e:
        raise DocumentError("Unsupported or corrupt file. Send a PDF or an image (png, jpg, tiff, bmp, webp).",
                            status=415, code="UNSUPPORTED_DOCUMENT", details=str(e)) from e
