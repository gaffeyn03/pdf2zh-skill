# Translation policy

The active chat model translates only the exact, writable units returned by `next`. Context rows are read-only.

- Preserve unit IDs, formulas, citations, numbers, URLs, code, and protected placeholders exactly.
- Translate meaning, headings, captions, and prose into the requested target language with an academic style unless the request says otherwise.
- Apply an explicit glossary before general wording preferences; use adjacent context only to resolve meaning and continuity.
- Keep repeated source text as separate occurrences; never merge or infer translations across IDs.
- Do not translate figure-internal text, perform OCR, invent missing content, or call an external model or service.
- Emit exactly one ordered, nonempty translation for every requested ID, with no extra IDs, commentary, or Markdown fences.
- Return only `{"translations":[{"id":"...","text":"..."}]}`. This Skill defaults to both translated-only and bilingual output; honor a user request for only one version.
