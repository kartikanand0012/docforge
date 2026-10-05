import { describe, expect, it } from "vitest";
import { chatScope } from "@/lib/chat";

describe("chatScope", () => {
  it("asks within one document, one knowledge base, or the organisation, never two", () => {
    expect(chatScope({ documentId: "d" })).toEqual({ document_id: "d" });
    expect(chatScope({ collectionId: "k" })).toEqual({ collection_id: "k" });
    expect(chatScope({})).toEqual({});
  });

  it("follows up in the conversation's own scope", () => {
    expect(chatScope({ documentId: "d", conversationId: "c" })).toEqual({ conversation_id: "c" });
  });
});

import { readEvents } from "@/lib/chat";

describe("readEvents", () => {
  it("reads complete server-sent events and keeps a partial one for the next chunk", () => {
    const first = readEvents('event: stage\ndata: {"stage":"searching"}\n\nevent: stage\ndata: {"sta');
    expect(first.events).toEqual([{ name: "stage", data: { stage: "searching" } }]);
    const second = readEvents(`${first.rest}ge":"reading","passages":3}\n\n`);
    expect(second.events).toEqual([{ name: "stage", data: { stage: "reading", passages: 3 } }]);
    expect(second.rest).toBe("");
  });

  it("skips an event whose data is not JSON", () => {
    expect(readEvents("event: stage\ndata: nope\n\n").events).toEqual([]);
  });
});
