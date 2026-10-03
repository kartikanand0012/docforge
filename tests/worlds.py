"""A document service whose parser and model reproduce a fixture pair exactly, unless edited."""

import copy
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from docforge.db import DEFAULT_TENANT_ID
from docforge.db.session import SessionFactory
from docforge.documents import DocumentService, EventSink
from docforge.extraction.pipeline import ExtractionPipeline, InvoicePipeline
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC, PurchaseOrderExtraction
from docforge.parsing.base import ParsedDocument
from docforge.storage import MemoryObjectStore
from fakes import MappedParser, ScriptedProvider, cited, reprint

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "synthetic"
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


class World:
    """A service whose parser and model reproduce a fixture pair exactly, unless edited."""

    def __init__(
        self,
        sessions: SessionFactory,
        invoice: RawFromLabel,
        order: RawFromLabel,
        pair: str,
        events: EventSink | None = None,
    ) -> None:
        self.events = events
        self.sessions = sessions
        label = json.loads((FIXTURES / pair / "label.json").read_text(encoding="utf-8"))
        self.invoice_pdf = (FIXTURES / pair / "invoice.pdf").read_bytes()
        self.order_pdf = (FIXTURES / pair / "purchase_order.pdf").read_bytes()
        self.invoice_raw = copy.deepcopy(invoice(label))
        self.order_raw = copy.deepcopy(order(label))
        self.invoice_parsed: ParsedDocument = cited(label, "invoice", self.invoice_raw)
        self.order_parsed: ParsedDocument = cited(label, "purchase_order", self.order_raw)
        self.queued: list[uuid.UUID] = []
        self.store = MemoryObjectStore()  # kept across rebuilds, so reprocessing finds the file
        self.service: DocumentService | None = None

    def reprint_invoice(self, path: str, value: str) -> None:
        self.invoice_parsed = reprint(self.invoice_parsed, self.invoice_raw, path, value)

    def build(self) -> DocumentService:
        parser = MappedParser()
        parser.add(self.invoice_pdf, self.invoice_parsed)
        parser.add(self.order_pdf, self.order_parsed)
        replies = [json.dumps(self.invoice_raw)] * 3
        order_replies = [json.dumps(self.order_raw)] * 3
        order_pipeline: ExtractionPipeline[PurchaseOrderExtraction] = ExtractionPipeline(
            parser, ScriptedProvider(order_replies), PURCHASE_ORDER_SPEC
        )
        self.service = DocumentService(
            self.sessions,
            self.store,
            {
                "invoice": InvoicePipeline(parser, ScriptedProvider(replies)),
                "purchase_order": order_pipeline,
            },
            lambda session, version: self.queued.append(version.id),
            events=self.events,
        )
        return self.service

    def process(self, doc_type: str) -> uuid.UUID:
        service = self.service or self.build()
        data = self.invoice_pdf if doc_type == "invoice" else self.order_pdf
        result = service.ingest(
            tenant_id=DEFAULT_TENANT_ID,
            doc_type=doc_type,
            filename=f"{doc_type}.pdf",
            data=data,
            actor="api:upload",
        )
        assert result.version is not None
        assert service.process(result.version.id) == "succeeded"
        return result.document.id

    def assessment(self, document_id: uuid.UUID) -> Any:
        assert self.service is not None
        return self.service.assessment(DEFAULT_TENANT_ID, document_id)

    def actions(self, document_id: uuid.UUID) -> list[str]:
        assert self.service is not None
        return [e.action for e in self.service.audit_trail(DEFAULT_TENANT_ID, document_id)]
