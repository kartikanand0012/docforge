"""Page images: rendering, skew estimation, and moving boxes between a page and its rotation."""

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from docforge.parsing.pdf import has_text_layer
from docforge.parsing.raster import estimate_skew, images_to_pdf, render_pages, rotate_box

INVOICE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "pair_001" / "invoice.pdf"
).read_bytes()
WIDTH, HEIGHT, DPI = 600.0, 400.0, 144  # points, points, two pixels per point
SCALE = DPI / 72


def page_with_rectangle(box: tuple[float, float, float, float]) -> Image.Image:
    """A white page with one black rectangle at `box` (PDF points, origin bottom-left)."""
    image = Image.new("L", (int(WIDTH * SCALE), int(HEIGHT * SCALE)), 255)
    x0, y0, x1, y1 = box
    ImageDraw.Draw(image).rectangle(
        (x0 * SCALE, (HEIGHT - y1) * SCALE, x1 * SCALE, (HEIGHT - y0) * SCALE), fill=0
    )
    return image


def ink_box(image: Image.Image) -> tuple[float, float, float, float]:
    """The box around every dark pixel, in PDF points."""
    found = image.point(lambda value: 255 if value < 128 else 0).getbbox()
    assert found is not None
    left, top, right, bottom = found
    return left / SCALE, HEIGHT - bottom / SCALE, right / SCALE, HEIGHT - top / SCALE


def striped_page() -> Image.Image:
    """Rows of dashes, like lines of text."""
    image = Image.new("L", (1200, 800), 255)
    draw = ImageDraw.Draw(image)
    for row in range(60, 760, 40):
        for start in range(80, 1100, 90):
            draw.rectangle((start, row, start + 70, row + 12), fill=0)
    return image


@pytest.mark.parametrize("angle", [1.8, -2.5, 4.0])
@pytest.mark.parametrize(
    "box", [(500.0, 30.0, 580.0, 45.0), (20.0, 350.0, 120.0, 370.0), (280.0, 190.0, 320.0, 210.0)]
)
def test_a_rotated_box_encloses_where_the_ink_went(
    angle: float, box: tuple[float, float, float, float]
) -> None:
    """Checked against pixels: PIL turns the image, `rotate_box` must agree on where to."""
    turned = page_with_rectangle(box).rotate(
        angle, resample=Image.Resampling.BICUBIC, fillcolor=255
    )

    x0, y0, x1, y1 = rotate_box(box, angle, WIDTH, HEIGHT)
    ink = ink_box(turned)

    assert x0 - 1 <= ink[0] and y0 - 1 <= ink[1] and ink[2] <= x1 + 1 and ink[3] <= y1 + 1
    # and it is no looser than the rotation makes necessary
    assert (x1 - x0) - (ink[2] - ink[0]) < 3 and (y1 - y0) - (ink[3] - ink[1]) < 3


def test_rotating_by_nothing_changes_nothing() -> None:
    assert rotate_box((10.0, 20.0, 30.0, 40.0), 0.0, WIDTH, HEIGHT) == (10.0, 20.0, 30.0, 40.0)


def test_rotating_there_and_back_returns_a_box_around_the_original() -> None:
    box = (500.0, 30.0, 580.0, 45.0)

    back = rotate_box(rotate_box(box, 2.0, WIDTH, HEIGHT), -2.0, WIDTH, HEIGHT)

    assert back[0] <= box[0] and back[1] <= box[1] and back[2] >= box[2] and back[3] >= box[3]
    assert back[2] - back[0] < (box[2] - box[0]) + 8


@pytest.mark.parametrize("angle", [1.8, -3.2, 0.7])
def test_skew_is_estimated_as_the_rotation_that_would_straighten_the_page(angle: float) -> None:
    skewed = striped_page().rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=255)

    assert estimate_skew(skewed) == pytest.approx(-angle, abs=0.25)


def test_a_straight_page_has_no_skew() -> None:
    assert abs(estimate_skew(striped_page())) < 0.15


def test_a_blank_page_has_no_skew() -> None:
    assert estimate_skew(Image.new("L", (800, 600), 255)) == 0.0


def test_pages_are_rendered_at_the_requested_resolution() -> None:
    (page,) = render_pages(INVOICE, dpi=100)

    assert page.mode == "L"
    assert page.size == pytest.approx((842 * 100 / 72, 595 * 100 / 72), abs=2)


def test_images_become_a_pdf_of_the_same_page_size_with_no_text() -> None:
    pages = render_pages(INVOICE, dpi=100)

    pdf = images_to_pdf(pages, dpi=100)

    assert pdf == images_to_pdf(pages, dpi=100)  # no timestamps: the same bytes every time
    (again,) = render_pages(pdf, dpi=100)
    assert again.size == pytest.approx(pages[0].size, abs=2)
    assert not has_text_layer(pdf)


def test_a_born_digital_pdf_has_a_text_layer() -> None:
    assert has_text_layer(INVOICE)
