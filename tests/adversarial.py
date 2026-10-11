"""Hostile and broken files for the robustness suite, made when needed, never committed or
downloaded.

Each case names what DocForge should do with it - refuse it at upload (413, 415, 422), fail
it with a reason, or process it - as a set: a parser may survive what another refuses, and
both are safe. What is never acceptable (a crash, a 500, a hang, a lost worker) is checked
by the suite, not listed here. Password-protected PDFs need `pypdf` (a dev dependency).
"""

import io
import zipfile
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial

from PIL import Image

MB = 1024 * 1024
Outcome = str  # "413" | "415" | "422" | "failed" | "processed"
_EPOCH = (1980, 1, 1, 0, 0, 0)  # zip entry times, fixed so the bytes are too


@dataclass(frozen=True)
class Case:
    name: str
    filename: str
    doc_type: str
    build: Callable[[], bytes]
    expect: frozenset[Outcome]
    why: str


# --- PDFs, written object by object ---------------------------------------------------------


def _pdf(objects: list[bytes], *, root: int = 1, trailer: bytes = b"") -> bytes:
    """A PDF of `objects` (numbered from 1) with a correct cross-reference table."""
    out = io.BytesIO()
    out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root %d 0 R %s>>\n" % (len(objects) + 1, root, trailer))
    out.write(b"startxref\n%d\n%%%%EOF\n" % xref)
    return out.getvalue()


_FONT = b"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> "
_PAGE = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] " + _FONT + b"/Contents 4 0 R >>"
_TEXT = b"BT /F1 24 Tf 72 700 Td (Invoice INV-0001 Total 1,180.00) Tj ET"


def _stream(data: bytes, extra: bytes = b"") -> bytes:
    return b"<< /Length %d %s>>\nstream\n" % (len(data), extra) + data + b"\nendstream"


def one_page_pdf(*extra: bytes) -> bytes:
    """A plain one-page PDF; `extra` objects follow it (numbered from 5)."""
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            _PAGE,
            _stream(_TEXT),
            *extra,
        ]
    )


def _deflated(chunks: Iterator[bytes], *, raw: bool = False) -> bytes:
    packer = zlib.compressobj(9, zlib.DEFLATED, -15 if raw else 15)
    return b"".join(packer.compress(chunk) for chunk in chunks) + packer.flush()


def _spaces(megabytes: int) -> Iterator[bytes]:
    block = b" " * MB
    for _ in range(megabytes):
        yield block


def flate_bomb() -> bytes:
    """A page whose content stream inflates to 256 MB of spaces (valid, and empty)."""
    packed = _deflated(_spaces(256))
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            _PAGE,
            _stream(packed, b"/Filter /FlateDecode "),
        ]
    )


def nested_flate_bomb() -> bytes:
    """The same, compressed twice: small on disk, and small after the first inflation."""
    inner = _deflated(_spaces(256))
    packed = _deflated(iter([inner]))
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            _PAGE,
            _stream(packed, b"/Filter [/FlateDecode /FlateDecode] "),
        ]
    )


def xref_stream_bomb() -> bytes:
    """A cross-reference stream that claims ten million entries and inflates to 64 MB."""
    packed = _deflated(iter([b"\0" * MB] * 64))
    body = one_page_pdf()
    xref_object = (
        b"5 0 obj\n"
        + _stream(
            packed, b"/Type /XRef /Size 10000000 /W [1 4 2] /Root 1 0 R /Filter /FlateDecode "
        )
        + b"\nendobj\n"
    )
    cut = body.index(b"xref\n")
    return body[:cut] + xref_object + b"startxref\n%d\n%%%%EOF\n" % cut


def pages_pdf(count: int) -> bytes:
    """`count` pages sharing one content stream."""
    first = 5
    kids = b" ".join(b"%d 0 R" % (first + i) for i in range(count))
    pages = [
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] " + _FONT + b"/Contents 3 0 R >>"
        for _ in range(count)
    ]
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % count,
            _stream(_TEXT),
            b"<< >>",
            *pages,
        ]
    )


def count_lies() -> bytes:
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1000000 >>",
            _PAGE,
            _stream(_TEXT),
        ]
    )


def deep_page_tree(depth: int = 10_000) -> bytes:
    """Page-tree nodes nested `depth` deep, one page at the bottom."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    for level in range(depth):
        number = 2 + level
        parent = b"/Parent %d 0 R " % (number - 1) if level else b""
        objects.append(b"<< /Type /Pages %s/Kids [%d 0 R] /Count 1 >>" % (parent, number + 1))
    page = 2 + depth
    objects.append(
        b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] %s/Contents %d 0 R >>"
        % (page - 1, _FONT, page + 1)
    )
    objects.append(_stream(_TEXT))
    return _pdf(objects)


def broken_xref() -> bytes:
    """Every cross-reference offset wrong by 7 bytes: a reader must repair or refuse."""
    data = one_page_pdf()
    head, _, tail = data.partition(b"xref\n")
    lines = tail.split(b"\n")
    fixed = [
        b"%010d 00000 n " % (int(line[:10]) + 7) if line.endswith(b" n ") else line
        for line in lines
    ]
    return head + b"xref\n" + b"\n".join(fixed)


def circular_prev() -> bytes:
    """A trailer whose /Prev points back at its own cross-reference table."""
    data = one_page_pdf()
    xref = data.index(b"xref\n")
    return data.replace(b"/Root 1 0 R ", b"/Root 1 0 R /Prev %d " % xref)


def truncated_pdf() -> bytes:
    data = one_page_pdf()
    return data[: len(data) * 2 // 3]


def active_content() -> bytes:
    """JavaScript on open, a launch action and an attached file: nothing may run or leak."""
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R /OpenAction 5 0 R /Names << /EmbeddedFiles "
            b"<< /Names [(payload.exe) 7 0 R] >> >> >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] " + _FONT + b"/Contents 4 0 R "
            b"/Annots [6 0 R] >>",
            _stream(_TEXT),
            b"<< /S /JavaScript /JS (app.alert('hello');) >>",
            b"<< /Type /Annot /Subtype /Link /Rect [0 0 100 100] "
            b"/A << /S /Launch /F (cmd.exe) >> >>",
            b"<< /Type /Filespec /F (payload.exe) /EF << /F 8 0 R >> >>",
            _stream(b"MZ not really a program", b"/Type /EmbeddedFile "),
        ]
    )


def password_pdf(algorithm: str, *, needs_password: bool = True) -> bytes:
    import secrets

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(one_page_pdf())))
    writer.encrypt(
        user_password=secrets.token_hex(8) if needs_password else "",
        owner_password=secrets.token_hex(8),
        algorithm=algorithm,
    )
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def padded_pdf(size: int) -> bytes:
    """A valid one-page PDF of exactly `size` bytes: the rest is an unused stream."""
    padding = size
    for _ in range(4):  # the /Length's own digits move the total; a few rounds settle it
        data = one_page_pdf(_stream(b"\0" * max(padding, 0)))
        padding += size - len(data)
        if len(data) == size:
            return data
    raise ValueError(f"cannot pad a PDF to {size} bytes")


# --- Office files ----------------------------------------------------------------------------

_CONTENT_TYPES = {
    "docx": b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/'
    b'2006/content-types"><Default Extension="xml" ContentType="application/xml"/>'
    b'<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
    b'officedocument.wordprocessingml.document.main+xml"/></Types>',
    "xlsx": b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/'
    b'2006/content-types"><Default Extension="xml" ContentType="application/xml"/>'
    b'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
    b'officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/'
    b'sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.'
    b'worksheet+xml"/></Types>',
}
_RELS = {
    "docx": b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
    b'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats'
    b'.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    b"</Relationships>",
    "xlsx": b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
    b'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats'
    b'.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    b"</Relationships>",
}
_W = b'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_S = b'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
_R = b'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
_WORKBOOK = (
    b'<?xml version="1.0"?><workbook ' + _S + b" " + _R + b'><sheets><sheet name="Sheet1" '
    b'sheetId="1" r:id="rId1"/></sheets></workbook>'
)
_WORKBOOK_RELS = (
    b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
    b'2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
    b'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    b"</Relationships>"
)


def _office(kind: str, parts: dict[str, Callable[[], Iterator[bytes]]]) -> bytes:
    """An office file whose large parts are streamed in, so no part is held whole."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr(zipfile.ZipInfo("[Content_Types].xml", _EPOCH), _CONTENT_TYPES[kind])
        archive.writestr(zipfile.ZipInfo("_rels/.rels", _EPOCH), _RELS[kind])
        if kind == "xlsx":
            archive.writestr(zipfile.ZipInfo("xl/workbook.xml", _EPOCH), _WORKBOOK)
            archive.writestr(zipfile.ZipInfo("xl/_rels/workbook.xml.rels", _EPOCH), _WORKBOOK_RELS)
        for name, chunks in parts.items():
            info = zipfile.ZipInfo(name, _EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            with archive.open(info, "w", force_zip64=True) as part:
                for chunk in chunks():
                    part.write(chunk)
    return out.getvalue()


def _document(body: Callable[[], Iterator[bytes]]) -> Callable[[], Iterator[bytes]]:
    def chunks() -> Iterator[bytes]:
        yield b'<?xml version="1.0"?><w:document ' + _W + b"><w:body>"
        yield from body()
        yield b"</w:body></w:document>"

    return chunks


def _paragraph_text(megabytes: int) -> Iterator[bytes]:
    run = b"<w:p><w:r><w:t>" + b"a" * (MB - 64) + b"</w:t></w:r></w:p>"
    for _ in range(megabytes):
        yield run


def docx_bomb() -> bytes:
    return _office("docx", {"word/document.xml": _document(partial(_paragraph_text, 300))})


def xlsx_bomb() -> bytes:
    def sheet() -> Iterator[bytes]:
        yield b'<?xml version="1.0"?><worksheet ' + _S + b"><sheetData>"
        cell = b'<row><c t="inlineStr"><is><t>' + b"a" * (MB - 64) + b"</t></is></c></row>"
        for _ in range(300):
            yield cell
        yield b"</sheetData></worksheet>"

    return _office("xlsx", {"xl/worksheets/sheet1.xml": sheet})


def xlsx_rows(rows: int) -> bytes:
    def sheet() -> Iterator[bytes]:
        yield b'<?xml version="1.0"?><worksheet ' + _S + b"><sheetData>"
        batch = []
        for row in range(1, rows + 1):
            batch.append(b'<row r="%d"><c r="A%d"><v>%d</v></c></row>' % (row, row, row))
            if len(batch) == 10_000:
                yield b"".join(batch)
                batch = []
        yield b"".join(batch) + b"</sheetData></worksheet>"

    return _office("xlsx", {"xl/worksheets/sheet1.xml": sheet})


def docx_pages(count: int) -> bytes:
    def body() -> Iterator[bytes]:
        page = b'<w:p><w:r><w:t>Page</w:t><w:br w:type="page"/></w:r></w:p>'
        for _ in range(count):
            yield page

    return _office("docx", {"word/document.xml": _document(body)})


def docx_entries(count: int) -> bytes:
    def small() -> Iterator[bytes]:
        yield b"<x/>"

    parts: dict[str, Callable[[], Iterator[bytes]]] = {
        "word/document.xml": _document(partial(_paragraph_text, 0))
    }
    parts.update({f"word/media/part{i:05d}.xml": small for i in range(count)})
    return _office("docx", parts)


def docx_bomb_sized(megabytes: int) -> bytes:
    """Without zip64 extras, so the 32-bit size fields are the ones a reader uses."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr(zipfile.ZipInfo("[Content_Types].xml", _EPOCH), _CONTENT_TYPES["docx"])
        archive.writestr(zipfile.ZipInfo("_rels/.rels", _EPOCH), _RELS["docx"])
        info = zipfile.ZipInfo("word/document.xml", _EPOCH)
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, b"".join(_document(partial(_paragraph_text, megabytes))()))
    return out.getvalue()


def docx_understated() -> bytes:
    """A part of 8 MB whose headers say 4 KB: a check trusting them would let it through."""
    data = bytearray(docx_bomb_sized(8))
    declared = 4096
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
        info = archive.getinfo("word/document.xml")
    local = info.header_offset
    data[local + 22 : local + 26] = declared.to_bytes(4, "little")
    name = b"word/document.xml"
    central = data.index(b"PK\x01\x02", local)
    while data[central + 46 : central + 46 + len(name)] != name:
        central = data.index(b"PK\x01\x02", central + 4)
    data[central + 24 : central + 28] = declared.to_bytes(4, "little")
    return bytes(data)


def corrupt_docx() -> bytes:
    data = docx_pages(3)
    return data[: len(data) // 2]


_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def ole(stream_name: str) -> bytes:
    """An OLE compound file's header and a directory entry naming `stream_name`."""
    header = _OLE + b"\0" * 16 + b"\x3e\x00\x03\x00\xfe\xff\x09\x00" + b"\0" * 480
    entry = stream_name.encode("utf-16-le").ljust(64, b"\0")
    return header + entry + b"\0" * (4096 - len(header) - len(entry))


# --- Images ---------------------------------------------------------------------------------


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")


def huge_png(side: int = 50_000) -> bytes:
    """A 1-bit grey PNG `side` pixels square: small on disk, gigapixels when opened."""
    row = b"\0" * (1 + (side + 7) // 8)

    def rows() -> Iterator[bytes]:
        block = row * 256
        for _ in range(side // 256):
            yield block
        yield row * (side % 256)

    ihdr = side.to_bytes(4, "big") * 2 + b"\x01\x00\x00\x00\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", _deflated(rows()))
        + _png_chunk(b"IEND", b"")
    )


def tiff_frames(count: int) -> bytes:
    frames = [Image.new("L", (100, 100), color=(i * 5) % 256) for i in range(count)]
    out = io.BytesIO()
    frames[0].save(out, format="TIFF", save_all=True, append_images=frames[1:])
    return out.getvalue()


def _printed(size: tuple[int, int] = (900, 300)) -> Image.Image:
    """A white image with an invoice's words on it, large enough for OCR to read."""
    from PIL import ImageDraw, ImageFont

    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=40)
    draw.text((30, 60), "TAX INVOICE  INV-0001", fill="black", font=font)
    draw.text((30, 160), "Grand Total  1,180.00", fill="black", font=font)
    return image


def _jpeg(mode: str) -> bytes:
    out = io.BytesIO()
    _printed().convert(mode).save(out, format="JPEG", quality=90)
    return out.getvalue()


def truncated_jpeg() -> bytes:
    data = _jpeg("RGB")
    return data[: len(data) // 2]


def rotated_png() -> bytes:
    """Stored on its side, with EXIF saying to turn it upright, as phone cameras do."""
    exif = Image.Exif()
    exif[0x0112] = 6  # orientation: turn 90 degrees clockwise to show
    out = io.BytesIO()
    _printed().rotate(90, expand=True).save(out, format="PNG", exif=exif)
    return out.getvalue()


# --- The cases -------------------------------------------------------------------------------

_REFUSED_OR_FAILED = frozenset({"413", "415", "422", "failed"})
_ANY_SAFE = frozenset({"413", "415", "422", "failed", "processed"})


def cases() -> list[Case]:
    def case(
        name: str, filename: str, build: Callable[[], bytes], expect: set[str], why: str
    ) -> Case:
        return Case(name, filename, "invoice", build, frozenset(expect), why)

    return [
        case("pdf_flate_bomb", "bomb.pdf", flate_bomb, set(_ANY_SAFE),
             "content stream inflating to 256 MB"),
        case("pdf_nested_flate_bomb", "bomb2.pdf", nested_flate_bomb, set(_ANY_SAFE),
             "content stream compressed twice"),
        case("pdf_xref_stream_bomb", "xref.pdf", xref_stream_bomb, set(_ANY_SAFE),
             "cross-reference stream of ten million entries"),
        case("pdf_5000_pages", "long.pdf", partial(pages_pdf, 5000), {"413"},
             "5,000 pages: over the page limit"),
        case("pdf_count_lies", "count.pdf", count_lies, set(_ANY_SAFE),
             "page tree says a million pages, holds one"),
        case("pdf_deep_page_tree", "deep.pdf", deep_page_tree, set(_ANY_SAFE),
             "page tree 10,000 levels deep"),
        case("pdf_broken_xref", "xref-off.pdf", broken_xref, set(_ANY_SAFE),
             "every cross-reference offset wrong"),
        case("pdf_circular_prev", "prev.pdf", circular_prev, set(_ANY_SAFE),
             "trailer /Prev points at itself"),
        case("pdf_truncated", "cut.pdf", truncated_pdf, set(_ANY_SAFE),
             "the last third missing"),
        case("pdf_javascript_and_attachment", "active.pdf", active_content, {"processed"},
             "JavaScript, a launch action, an attached program: ignored"),
        case("pdf_password_rc4", "locked-rc4.pdf", partial(password_pdf, "RC4-128"), {"422"},
             "needs a password to open (RC4)"),
        case("pdf_password_aes128", "locked-aes128.pdf", partial(password_pdf, "AES-128"),
             {"422"}, "needs a password to open (AES-128)"),
        case("pdf_password_aes256", "locked-aes256.pdf", partial(password_pdf, "AES-256"),
             {"422"}, "needs a password to open (AES-256)"),
        case("pdf_owner_password_only", "owner.pdf",
             partial(password_pdf, "AES-128", needs_password=False), {"processed"},
             "restricted, but opens without a password"),
        case("docx_zip_bomb", "bomb.docx", docx_bomb, set(_REFUSED_OR_FAILED),
             "a 300 MB part, honestly declared"),
        case("xlsx_zip_bomb", "bomb.xlsx", xlsx_bomb, set(_REFUSED_OR_FAILED),
             "a 300 MB sheet, honestly declared"),
        case("docx_understated_sizes", "small.docx", docx_understated, set(_REFUSED_OR_FAILED),
             "an 8 MB part declared as 4 KB"),
        case("docx_10000_entries", "many.docx", partial(docx_entries, 10_000), set(_ANY_SAFE),
             "10,000 parts in one file"),
        case("xlsx_million_rows", "rows.xlsx", partial(xlsx_rows, 1_000_000),
             set(_REFUSED_OR_FAILED), "a million rows: too long to convert or to read"),
        case("docx_5000_pages", "long.docx", partial(docx_pages, 5000), {"413", "failed"},
             "5,000 pages once converted"),
        case("docx_corrupt", "corrupt.docx", corrupt_docx, set(_REFUSED_OR_FAILED),
             "a zip cut in half"),
        case("legacy_doc", "old.doc", partial(ole, "WordDocument"), {"415"},
             "Word 97-2003"),
        case("legacy_xls", "old.xls", partial(ole, "Workbook"), {"415"}, "Excel 97-2003"),
        case("encrypted_docx", "locked.docx", partial(ole, "EncryptionInfo"), {"415"},
             "a password-protected .docx (an OLE container)"),
        case("heic_image", "photo.heic",
             lambda: b"\0\0\0\x18ftypheic\0\0\0\0mif1heic" + b"\0" * 1000, {"415"},
             "an iPhone photo: not accepted"),
        case("webp_image", "photo.webp",
             lambda: b"RIFF\xe8\x03\0\0WEBPVP8 " + b"\0" * 1000, {"415"}, "WebP: not accepted"),
        case("png_50000_square", "huge.png", huge_png, {"413", "422", "failed"},
             "2.5 gigapixels when opened"),
        case("tiff_150_frames", "frames.tiff", partial(tiff_frames, 150), {"413", "failed"},
             "150 pages once converted"),
        case("jpeg_truncated", "cut.jpg", truncated_jpeg, set(_ANY_SAFE),
             "the second half missing"),
        case("jpeg_cmyk", "cmyk.jpg", partial(_jpeg, "CMYK"), {"processed"},
             "a print shop's CMYK JPEG"),
        case("png_exif_rotated", "rotated.png", rotated_png, {"processed"},
             "a phone photo that must be turned"),
        case("empty", "empty.pdf", lambda: b"", {"415", "422"}, "no bytes at all"),
        case("one_byte", "one.pdf", lambda: b"%", {"415", "422"}, "one byte"),
        case("big_exactly_10mb", "ten.pdf", partial(padded_pdf, 10 * MB), {"processed"},
             "exactly the size limit"),
        case("big_10mb_plus_1", "eleven.pdf", partial(padded_pdf, 10 * MB + 1), {"413"},
             "one byte over the size limit"),
    ]  # fmt: skip
