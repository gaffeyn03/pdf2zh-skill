# Job format

All persisted payloads are UTF-8 JSON and carry `"schema_version": 1`. Reject unknown newer versions instead of guessing. Write mutable files through a temporary file followed by atomic replacement.

```text
<job-dir>/
  manifest.json
  source.pdf
  units.json
  context.json
  batches/
  responses/
  translations/
  output/
  validation.json
```

## States

- `PREPARED`: capture completed and no batch has been accepted.
- `TRANSLATING`: at least one batch is pending or accepted, but exact coverage is incomplete.
- `READY_TO_RENDER`: every batch has one validated translation file.
- `COMPLETED`: staged output passed validation and was published under a non-conflicting name.
- `FAILED`: a terminal capture, replay, render, or validation error was recorded with diagnostics.

A batch is accepted only when `translations/<batch-id>.json` exists and passes schema, immutable manifest batch-hash, exact-ID/order, source-hash, placeholder multiset, and nonempty-text validation. Accepted files must form a contiguous manifest-order prefix. Ignore and remove incomplete `*.tmp` files. `status` and `next` resume from the first unaccepted batch.

## Payload contracts

- `manifest.json`: `{schema_version, state, source, source_hash, source_lang, target_lang, page_range, output_mode, batch_ids, batch_hashes}`. `batch_hashes` is the immutable batch-ID-to-hash ownership map.
- `units.json`: `{schema_version, units:[{id, occurrence_index, page, source, source_hash, placeholders}]}`. IDs identify occurrences; repeated source strings remain separate rows.
- `context.json`: `{schema_version, document_context, previous_context, next_context}`. Context is read-only.
- `batches/<batch-id>.json`: `{schema_version, batch_id, batch_hash, policy, glossary, style, document_context, previous_context, next_context, units}`. `units` is the only writable translation set.
- `next` owns `responses/<batch-id>.json` as `response_path`. Model response at exactly that path is `{"translations":[{"id":"...","text":"..."}]}` with exactly one ordered, nonempty row for every batch unit and no other IDs. `submit` rejects unrelated or stale response paths. An identical retry for an already accepted response is idempotent; changed duplicate content is rejected.
- `translations/<batch-id>.json`: `{schema_version, batch_id, batch_hash, translations:[{id, source_hash, text}]}`. `submit` creates this accepted record after validating the model response.
- `validation.json`: `{schema_version, state, structural_ok, structural_errors, linguistic_warnings, checks, preview_pairs, staged_pdf, final_pdf}`. Structural errors block publication. Linguistic warnings remain advisory. Each preview pair records the selected source/output page PNG paths, dimensions match, and nonblank coverage.

`next --json` returns `{ok, state, batch_path, response_path}` for exactly one pending batch. `status --json` returns `{ok, state, accepted_batches, total_batches, next_action}`. `render --json` reports staged translated-only and, when requested, bilingual PDFs. `validate --json` reports `validation.json` and returns `final_pdf` only when signature, parseability, encryption, page count, selected-range geometry/nonblank text, and first/middle/last preview checks pass and the job reaches `COMPLETED`.

In this adaptation, bilingual mode validates and publishes both versions. `final_pdfs` contains translated-only then bilingual PDF paths; `final_pdf` remains the bilingual path for compatibility. `translated_preview_pairs` records the translated-only previews. Final filenames use the job directory name and Chinese labels by default.
