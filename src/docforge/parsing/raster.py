"""Pages as images: rendering, skew, and moving a box between a page and its rotation.

Used on the scanned path (render, straighten, read with OCR, map boxes back to the page as
it was uploaded) and by the generator of scanned test documents.
"""

import io
import math
import time

import numpy as np
import pypdfium2 as pdfium
from PIL import Image

from docforge.parsing.base import DocumentTooLarge, ParseError
from docforge.parsing.pdf import PDFIUM_LOCK

Box = tuple[float, float, float, float]  # x0, y0, x1, y1 in PDF points, origin bottom-left

_EPOCH = time.gmtime(0)  # fixed dates, so the same images always give the same PDF bytes
_MAX_PAGE_PIXELS = 60_000_000  # an A0 page at 200 dpi; a larger page is not rendered at all
_SKEW_SEARCH = ((0.5, 5.0), (0.1, 0.5))  # (step, span) in degrees: coarse, then fine
_SKEW_THUMBNAIL = 1000  # pixels on the long side; enough to see text lines, cheap to rotate


def render_pages(
    pdf: bytes, dpi: int, first: int = 1, last: int | None = None
) -> list[Image.Image]:
    """Pages `first`..`last` (1-based, inclusive; all by default) as greyscale images.

    Raises `ParseError` if the PDF cannot be rendered.
    """
    with PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(pdf)
            try:
                images = []
                for index in range(first - 1, min(last or len(document), len(document))):
                    page = document[index]
                    width, height = page.get_size()
                    # Checked from the declared size, before any memory is taken for pixels.
                    if width * height * (dpi / 72) ** 2 > _MAX_PAGE_PIXELS:
                        raise DocumentTooLarge(
                            f"page {index + 1} is too large to render "
                            f"({width:.0f} x {height:.0f} points)"
                        )
                    images.append(page.render(scale=dpi / 72).to_pil().convert("L"))
                return images
            finally:
                document.close()
        except DocumentTooLarge:
            raise
        except Exception as error:  # a malformed file can fail in more ways than PdfiumError
            raise ParseError("the PDF could not be rendered") from error


def images_to_pdf(pages: list[Image.Image], dpi: int, quality: int = 75) -> bytes:
    """One image per page, at `dpi`, so the page size in points is that of the source."""
    out = io.BytesIO()
    pages[0].save(
        out,
        format="PDF",
        resolution=dpi,
        save_all=True,
        append_images=pages[1:],
        quality=quality,
        creationDate=_EPOCH,
        modDate=_EPOCH,
    )
    return out.getvalue()


def estimate_skew(image: Image.Image) -> float:
    """Degrees to rotate `image` (counter-clockwise) so its text lines run level.

    Text lines make the row-by-row ink profile sharp when they are level and smeared when
    they are not, so the angle that gives the sharpest profile is the correction.
    """
    small = image.convert("L")
    small.thumbnail((_SKEW_THUMBNAIL, _SKEW_THUMBNAIL))
    ink = Image.fromarray(((np.asarray(small) < 128) * 255).astype(np.uint8))
    if ink.getbbox() is None:
        return 0.0

    def sharpness(angle: float) -> float:
        turned = ink.rotate(angle, resample=Image.Resampling.BILINEAR, fillcolor=0)
        rows = np.asarray(turned, dtype=np.float32).sum(axis=1)
        return float(((rows[1:] - rows[:-1]) ** 2).sum())

    best, best_score = 0.0, sharpness(0.0)
    for step, span in _SKEW_SEARCH:
        centre = best
        for angle in np.arange(centre - span, centre + span + step / 2, step):
            score = sharpness(float(angle))
            if score > best_score:
                best, best_score = float(angle), score
    return round(best, 2)


def turn_box(box: Box, angle: float, page_width: float, page_height: float) -> Box:
    """`box` moved to where its centre lands when the page is turned, keeping its size.

    For small angles this is closer to the text than `rotate_box`, whose upright box around
    a turned line of text is several lines tall.
    """
    if angle == 0.0:
        return box
    x0, y0, x1, y1 = box
    centre_x, centre_y = (x0 + x1) / 2, (y0 + y1) / 2
    moved_x, moved_y, _, _ = rotate_box(
        (centre_x, centre_y, centre_x, centre_y), angle, page_width, page_height
    )
    half_width, half_height = (x1 - x0) / 2, (y1 - y0) / 2
    return moved_x - half_width, moved_y - half_height, moved_x + half_width, moved_y + half_height


def rotate_box(box: Box, angle: float, page_width: float, page_height: float) -> Box:
    """Where `box` lands when the page image is turned `angle` degrees counter-clockwise
    about its centre: the upright box around the turned one. With `-angle` it goes back.
    """
    if angle == 0.0:
        return box
    x0, y0, x1, y1 = box
    centre_x, centre_y = page_width / 2, page_height / 2
    cos, sin = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    corners = [
        (
            centre_x + (x - centre_x) * cos - (y - centre_y) * sin,
            centre_y + (x - centre_x) * sin + (y - centre_y) * cos,
        )
        for x in (x0, x1)
        for y in (y0, y1)
    ]
    xs, ys = [x for x, _ in corners], [y for _, y in corners]
    return min(xs), min(ys), max(xs), max(ys)
