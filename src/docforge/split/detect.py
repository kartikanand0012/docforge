"""Where one PDF holds several invoices: evidence read from each page, never a guess.

The evidence is the parser's text, page by page (`ParsedDocument`: every block carries its
page and its box). A page *starts an invoice* when its upper part prints an invoice number
("Invoice No.", "Inv. Number", "Bill No" and the like, followed by a value with a digit) and
the page prints a GSTIN: the header block every Indian tax invoice has. Pages are then
grouped in order:

- a page whose header repeats the invoice number of the invoice before it continues that
  invoice (a header printed on every page): certain;
- a page with no invoice number that says "Page n of m", consistent with the invoice before
  it, continues that invoice: certain;
- any other page without a header - a continuation that may belong to the invoice before it,
  a covering letter, a blank page - is placed where it most likely belongs and marked
  uncertain, with the reason in words;
- an invoice number printed lower on a page (a reference to another bill) never starts an
  invoice; it makes the page uncertain.

With one invoice found, or none, there is nothing to split (`propose` returns None) and the
document is read exactly as before. With two or more, the proposal is certain only when every
page is; otherwise a person confirms the grouping (Split Review) before anything is made.
"""

import re
from dataclasses import dataclass

from docforge.parsing.base import Block, ParsedDocument

COULD_NOT_DETECT = "could not detect automatically"

_LABEL = re.compile(
    r"(?<![A-Za-z])(?:tax\s+|gst\s+)?(?:invoice|inv|bill)\s*\.?\s*(?:no|number|num|#)(?![A-Za-z])"
    r"\s*\.?\s*[:#.\-]?\s*([A-Za-z0-9][A-Za-z0-9/\-_.]{0,39})",
    re.IGNORECASE,
)
_GSTIN = re.compile(r"(?<![0-9A-Z])(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])(?![0-9A-Z])")
_PAGE_MARK = re.compile(r"\bpage\s*(\d{1,3})\s*(?:of|/)\s*(\d{1,3})\b", re.IGNORECASE)
_DATE = re.compile(
    r"^\d{1,2}[/\-.](?:\d{1,2}|[A-Za-z]{3})[/\-.]\d{2,4}$|^\d{4}-\d{2}-\d{2}$", re.IGNORECASE
)
_HEADER_SHARE = 0.45  # an invoice's header is printed in the upper part of the page
_MIN_TEXT = 10  # fewer printed characters and the page is treated as blank


@dataclass(frozen=True)
class PageEvidence:
    page: int
    invoice_no: str | None  # printed in the page's header, on a page with a GSTIN
    gstins: tuple[str, ...]  # every GSTIN on the page, in reading order
    stray_no: str | None  # an invoice number printed elsewhere on the page only
    mark: tuple[int, int] | None  # "Page n of m"
    blank: bool


@dataclass(frozen=True)
class Group:
    """One invoice's pages, in the parent's order."""

    pages: tuple[int, ...]
    invoice_no: str | None
    seller_gstin: str | None  # the first GSTIN on its first page: shown, not relied on


@dataclass(frozen=True)
class Proposal:
    page_count: int
    groups: tuple[Group, ...]
    non_invoice: tuple[int, ...]  # pages proposed as "not an invoice page"
    uncertain: tuple[str, ...]  # why a person must confirm; empty when certain
    pages_in_doubt: tuple[int, ...] = ()

    @property
    def certain(self) -> bool:
        return not self.uncertain

    @property
    def reason(self) -> str | None:
        return "; ".join(self.uncertain) if self.uncertain else None


class PagesDoNotReconcile(ValueError):
    """Some page is in no group, or in two, or a page that does not exist is named."""


def reconcile(page_count: int, groups: list[list[int]], non_invoice: list[int]) -> None:
    """Every page of the parent in exactly one invoice or in "not an invoice page":
    parent pages = invoice pages + non-invoice pages. Raises `PagesDoNotReconcile`."""
    placed = [page for group in groups for page in group] + list(non_invoice)
    if any(not group for group in groups):
        raise PagesDoNotReconcile("An invoice has no pages. Remove it, or move a page into it.")
    outside = sorted({p for p in placed if not 1 <= p <= page_count})
    if outside:
        raise PagesDoNotReconcile(
            f"The file has {page_count} pages; there is no page {outside[0]}."
        )
    twice = sorted({p for p in placed if placed.count(p) > 1})
    if twice:
        raise PagesDoNotReconcile(f"Page {twice[0]} is placed twice. Each page goes in one place.")
    missing = sorted(set(range(1, page_count + 1)) - set(placed))
    if missing:
        listed = ", ".join(str(p) for p in missing[:10])
        raise PagesDoNotReconcile(
            f"Page {listed} {'is' if len(missing) == 1 else 'are'} in no invoice. Put every "
            'page in an invoice or in "not an invoice page".'
        )
    if len(placed) != page_count:  # cannot happen after the checks above; kept as the rule
        raise PagesDoNotReconcile("The pages do not add up.")


def _number(text: str) -> str | None:
    for found in _LABEL.finditer(text):
        value = found.group(1).rstrip(".-/_")
        if any(c.isdigit() for c in value) and not _DATE.match(value):
            return value.upper()
    return None


def _text(blocks: list[Block]) -> str:
    return "\n".join(block.text for block in blocks)


def evidence(parsed: ParsedDocument) -> list[PageEvidence]:
    """What each page prints that says where an invoice starts."""
    by_page: dict[int, list[Block]] = {page.number: [] for page in parsed.pages}
    for block in parsed.blocks:
        by_page.setdefault(block.page, []).append(block)
    heights = {page.number: page.height for page in parsed.pages}
    found: list[PageEvidence] = []
    for number in sorted(by_page):
        blocks = by_page[number]
        whole = _text(blocks)
        height = heights.get(number) or max((b.bbox.y1 for b in blocks), default=1.0)
        upper = _text([b for b in blocks if b.bbox.y0 >= height * _HEADER_SHARE])
        gstins = tuple(dict.fromkeys(_GSTIN.findall(whole)))
        header_no = _number(upper) if gstins else None
        anywhere = _number(whole)
        mark = _PAGE_MARK.search(whole)
        found.append(
            PageEvidence(
                page=number,
                invoice_no=header_no,
                gstins=gstins,
                stray_no=anywhere if header_no is None else None,
                mark=(int(mark.group(1)), int(mark.group(2))) if mark else None,
                blank=len("".join(whole.split())) < _MIN_TEXT,
            )
        )
    return found


@dataclass
class _Open:
    pages: list[int]
    invoice_no: str
    gstins: set[str]
    seller: str | None


def propose(parsed: ParsedDocument) -> Proposal | None:
    """The proposed grouping, or None when the file holds one invoice (or none is found)."""
    pages = evidence(parsed)
    groups: list[_Open] = []
    non_invoice: list[int] = []
    doubts: list[str] = []
    in_doubt: list[int] = []
    seen: set[tuple[str, frozenset[str]]] = set()  # invoice number, the page's GSTINs

    def doubt(page: int, why: str) -> None:
        in_doubt.append(page)
        doubts.append(why)

    for ev in pages:
        current = groups[-1] if groups else None
        if ev.invoice_no is not None:
            same_seller = current is not None and (
                not current.gstins or not ev.gstins or bool(current.gstins & set(ev.gstins))
            )
            if current is not None and current.invoice_no == ev.invoice_no and same_seller:
                current.pages.append(ev.page)  # its header printed again: certain
                current.gstins |= set(ev.gstins)
                continue
            key = (ev.invoice_no, frozenset(ev.gstins))
            if any(no == ev.invoice_no and gstins & key[1] for no, gstins in seen):
                doubt(ev.page, f"invoice {ev.invoice_no} appears again on page {ev.page}")
            seen.add(key)
            groups.append(
                _Open([ev.page], ev.invoice_no, set(ev.gstins), ev.gstins[0] if ev.gstins else None)
            )
            continue
        if current is None:
            non_invoice.append(ev.page)
            what = "is blank" if ev.blank else "has no invoice number"
            doubt(ev.page, f"page {ev.page} comes before the first invoice and {what}")
            continue
        if (
            ev.mark is not None
            and ev.stray_no is None
            and ev.mark[0] == len(current.pages) + 1
            and ev.mark[0] <= ev.mark[1]
        ):
            current.pages.append(ev.page)  # "Page n of m" continues it: certain
            continue
        current.pages.append(ev.page)
        if ev.blank:
            doubt(ev.page, f"page {ev.page} is blank")
        elif ev.stray_no is not None:
            doubt(
                ev.page,
                f"page {ev.page} mentions invoice {ev.stray_no} but not in a header; it may "
                f"continue invoice {current.invoice_no}",
            )
        else:
            doubt(
                ev.page,
                f"page {ev.page} has no invoice number; it may continue invoice "
                f"{current.invoice_no} or be a separate page",
            )
    if len(groups) <= 1:
        return None
    proposal = Proposal(
        page_count=len(pages),
        groups=tuple(Group(tuple(g.pages), g.invoice_no, g.seller) for g in groups),
        non_invoice=tuple(non_invoice),
        uncertain=tuple(doubts),
        pages_in_doubt=tuple(in_doubt),
    )
    reconcile(proposal.page_count, [list(g.pages) for g in proposal.groups], non_invoice)
    return proposal


def undetected(page_count: int) -> Proposal:
    """All pages as one proposed invoice, for a person to divide: what the splitter offers
    when it could not read the file's pages."""
    return Proposal(
        page_count=page_count,
        groups=(Group(tuple(range(1, page_count + 1)), None, None),),
        non_invoice=(),
        uncertain=(COULD_NOT_DETECT,),
        pages_in_doubt=tuple(range(1, page_count + 1)),
    )
