---
name: knowledge-wiki
description: Manage the personal knowledge wiki. Use when the user shares articles, documents, or asks to organize knowledge; when a conversation produces insights worth preserving as structured knowledge; or when the user asks about the knowledge base.
metadata:
  smart_assistant:
    always: true
---

# Knowledge Wiki

Maintain persistent, structured knowledge through SmartAssistant's governed knowledge
tools. The `knowledge/` directory is a machine-generated compatibility view,
not a writable source of truth.

## Core Operations

### 1. Ingest — User shares an article, document, or resource

1. Read and understand the source material.
2. Extract key facts, insights, and structured knowledge.
3. Use `knowledge_search` to find related material and avoid duplicates.
4. Use `knowledge_write` with a stable `source_ref`, a suitable
   `collection_id`, and the narrowest appropriate scope.
5. Keep the returned document ID and version in the response when useful.

### 2. Synthesize — Conversation produces valuable structured knowledge

1. Search for an existing governed document with `knowledge_search`.
2. Use `knowledge_write` to create or version the relevant document.
3. Preserve source provenance in the content and `source_ref`.

### 3. Query — User asks about accumulated knowledge

1. Use `knowledge_search` with a focused query.
2. Preserve the complete Citation v3 `knowledge://` URI verbatim when grounding an answer.
3. Never shorten, reorder, or manually reconstruct a citation. If it is rejected, search again.
4. Use `knowledge_get` with the returned URI when more context is needed.

## Page Format

```markdown
# Page Title

> Source: <URL or description of the original material>

Content here. Cross-reference related pages with markdown links:
[Related Page](../category/related-page.md)

## Key Points

- ...

## Related

- [Page A](../category/page-a.md) — how it relates
- [Page B](../category/page-b.md) — how it relates
```

The `> Source:` line records where the knowledge came from (URL, document name, conversation, etc.). Always include it when the material originates from a specific source.

Cross-references build a knowledge graph. When creating or updating a page, link to related pages and update those pages to link back. **Only link to pages that already exist** — if a concept deserves its own page, create it first, then add the link.

## Lifecycle Rules

- Never read, list, search, write, rename, or delete files under `knowledge/`
  with generic file, memory, or shell tools.
- Never edit `knowledge/index.md`, `knowledge/log.md`, or Markdown projections.
- Use `knowledge_write` for creation and updates, `knowledge_rollback` for a
  prior version, and `knowledge_revoke` for removal.
- A search hit is evidence only when its `knowledge://` citation is preserved;
  do not invent file paths or citations.
- `source_ref_hash` binds the stored source declaration; it does not prove an
  external source is authentic.

## Guidelines

- **One topic per page**: link between pages rather than duplicating
- **Update, don't duplicate**: if a page exists, update it
- **Be concise**: capture essence, not copy entire sources
- **Cite sources**: preserve verified `knowledge://` citations in grounded answers
- **Respect scope**: do not widen user/session knowledge to shared scope
- **Treat projections as derived data**: only the governed lifecycle tools may
  change the fact source
