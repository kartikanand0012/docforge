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
