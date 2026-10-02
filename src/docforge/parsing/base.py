"""Parser output: the characters and coordinates every later stage works from."""

from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, model_validator


class ParseError(Exception):
    """The file could not be read as a PDF or the parser failed on it."""


class DocumentTooLarge(ParseError):
    """The document exceeds a configured limit (pages)."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BBox(_Model):
    """A rectangle in PDF points with the origin at the bottom-left of the page."""

    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode="after")
    def _has_area(self) -> Self:
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("bbox must have x1 > x0 and y1 > y0")
        return self

    def contains(self, x: float, y: float, tolerance: float = 0.0) -> bool:
        return (
            self.x0 - tolerance <= x <= self.x1 + tolerance
            and self.y0 - tolerance <= y <= self.y1 + tolerance
        )


class Block(_Model):
    """One piece of text on a page. `id` is what an extracted field cites."""

    id: str
    kind: Literal["text", "table_cell"]
    text: str
    page: int
    bbox: BBox
    table: int | None = None
    row: int | None = None
    col: int | None = None
    header: bool = False

    @model_validator(mode="after")
    def _table_cells_are_placed(self) -> Self:
        placed = None not in (self.table, self.row, self.col)
        if self.kind == "table_cell" and not placed:
            raise ValueError("a table_cell block needs table, row and col")
        return self


class Page(_Model):
    number: int
    width: float
    height: float


class ParsedDocument(_Model):
    parser: str
    parser_version: str
    pages: tuple[Page, ...]
    blocks: tuple[Block, ...]

    @model_validator(mode="after")
    def _block_ids_are_unique(self) -> Self:
        seen: set[str] = set()
        for block in self.blocks:
            if block.id in seen:
                raise ValueError(f"duplicate block id {block.id!r}")
            seen.add(block.id)
        return self

    def block(self, block_id: str) -> Block | None:
        return next((block for block in self.blocks if block.id == block_id), None)


class Parser(Protocol):
    name: str
    version: str

    def parse(self, pdf: bytes) -> ParsedDocument:
        """Blocks in reading order. Raises `ParseError` if the file cannot be parsed."""
        ...
