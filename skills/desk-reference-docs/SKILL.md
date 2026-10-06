---
name: desk-reference-docs
description: Offer visual explanations and designed HTML reference documents, then save approved material in the local Agent Desk Library.
---

# Reference docs for Desk

When an explanation will be useful again—a process, architecture, decision, comparison, or troubleshooting guide—consider a small visual in the conversation. Offer a designed HTML reference document when a durable, browsable version would help. Keep simple answers in chat; do not turn every reply into a document offer.

If the person wants a document, ground it in current evidence and distinguish confirmed facts from proposals. Make one self-contained, responsive HTML file with a clear title, readable text, keyboard-accessible controls, and a useful print view. Prefer CSS and inline SVG over remote assets so it opens offline and in Desk's restricted preview. Keep source links and a last-checked date when facts may change. Do not put credentials, private messages, or unrelated data in an example.

Before saving, inspect the configured Library root with `agent-desk doctor` or the Library tab. Use an appropriate subfolder under that root; avoid overwriting an existing document without checking its ownership and source. The local Desk view discovers `.html`, `.htm`, `.md`, and `.markdown` files there. Do not place the library inside the private SQLite state directory or scan a person's entire Desktop. If the person wants a Desktop location, use a dedicated `Agent Desk/Library` folder. Never move an existing Desk installation or source-of-truth documents merely to adopt this layout.

After saving, refresh the Library tab and open the actual document. Report the local path and whether it was verified in Desk. Saving a file locally is not public publication or cloud sync. Ask separately before putting it in an external service, sharing it, or changing a canonical repository document.
