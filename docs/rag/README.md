# RAG Corpora

This directory stores repository-tracked document corpora used by the local doc-RAG pipeline.

Current maintained corpus:

- `bioadaptive_lab/`: general English-language documents for the BioAdaptive Interface Lab assistant experience

Notes:

- Runtime-generated retrieval caches are written to a local `.doc_rag/` directory next to a corpus root and are ignored by Git.
- The maintained default `DOC_RAG_ROOT` is `docs/rag/bioadaptive_lab`.
- `LocalDocsRAG` also supports the older nested layout where documents live under `<root>/docs/`, but new corpora should prefer placing source files directly in the corpus root.
- Optional curated ASR/pronunciation aliases can be stored in `entity_aliases.json` at the corpus root.
