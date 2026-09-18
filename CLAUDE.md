# Project: GitHub Issue Duplicate Detection + RAG

## What this is

Hybrid retrieval over `microsoft/vscode` GitHub issues. Given a new issue, find
existing issues that already cover it.

Ground truth comes from maintainer-marked duplicates, so retrieval quality is
**measured, not asserted**. Every stage of this project is justified by a number
in NOTES.md.

**Corpus:** `microsoft/vscode` issues created on or after 2024-01-01. The repo has
~18.5k *open* issues at any time, but well over 200k created across its lifetime —
the vast majority closed. The date bound keeps the fetch to one sitting while
leaving plenty of duplicate pairs. This bound is a deliberate scoping decision and
is documented in the README.

> **Measured (stage 0):** the window holds **84,877** issues, not the ~40k first
> estimated. At 384 dims float32 that is **~130MB** of vectors, not 60MB. Still
> viable for brute-force numpy. See NOTES.md.

**Fetch both open and closed issues.** Duplicates are closed by definition, so the
entire test set lives in the closed set. A fetch restricted to open issues returns
zero duplicate pairs. Do not filter on `states: OPEN`.

**Issues only, never pull requests.** In GitHub's data model a PR is an issue with
extra fields, and the REST issues endpoint returns both. We use GraphQL, where
`issues` and `pullRequests` are separate connections, so PRs are excluded by
construction. When extracting duplicate pairs, verify the canonical target exists
in the `issues` table and drop the pair if it does not.

> **Measured (stage 0):** resolve canonical targets with
> `repository.issueOrPullRequest(number:)`, **not** `repository.issue(number:)`.
> The latter returns `null` for a PR number, making "target is a PR"
> indistinguishable from "target was deleted".

## Stack

- Python 3.11+
- **MariaDB 12.3** — already installed locally. Native `VECTOR` type and HNSW
  `VECTOR INDEX`. No Docker, no Postgres, no pgvector.
- `mysql-connector-python` or `PyMySQL`
- `sentence-transformers` (`BAAI/bge-small-en-v1.5`, 384 dims)
- `rank_bm25`
- Streamlit (UI, stage 8 only)
- `requirements.txt`, pinned versions

> **Environment note:** this machine has **only Python 3.14.6** — no 3.11/3.12,
> no pyenv, no uv. Verified that the whole stack has 3.14 wheels
> (`torch-2.14.0-cp314-cp314-manylinux_2_28_x86_64.whl`; glibc here is 2.44 vs the
> 2.28 required). One venv, one interpreter, no version manager needed.

## Rules

1. **Build ONE stage per session.** Do not implement future stages, even if
   trivial. Do not scaffold files for stages not yet reached.
2. **No stage is complete without an eval number recorded in NOTES.md.**
3. **Do not write or edit `src/eval.py` or `src/fusion.py`.** I write those
   myself. If a stage needs them, assume the interface described below.
4. **No LangChain, no LlamaIndex, no vector-store wrapper libraries.** Retrieval
   logic stays explicit and readable.
5. Explain non-obvious design choices in comments. I need to defend every
   decision in this repo in an interview.
6. Before writing code for a stage, state your approach and wait for my review.

## File structure

```
issue-rag/
├── CLAUDE.md              # this file — read every session
├── NOTES.md               # running log of eval numbers per stage (see below)
├── INTERVIEW.md           # decisions + concepts to defend, grown per stage
├── README.md              # written last; ablation table + failure analysis
├── .env                   # GITHUB_TOKEN, DB credentials — gitignored
├── requirements.txt
├── schema.sql             # MariaDB DDL
├── app.py                 # Streamlit UI (stage 8)
├── scripts/
│   └── probe.py           # throwaway corpus probe, deleted after stage 0
└── src/
    ├── db.py              # connection handling, idempotent upserts
    ├── fetch.py           # GitHub GraphQL ingestion → MariaDB
    ├── embed.py           # sentence-transformers encoding → embeddings table
    ├── retrieve.py        # vector search, BM25, and the combined retriever
    ├── fusion.py          # [I WRITE THIS] Reciprocal Rank Fusion
    ├── eval.py            # [I WRITE THIS] recall@k and MRR harness
    ├── rerank.py          # cross-encoder reranking (stage 5)
    ├── finetune.py        # contrastive embedding fine-tuning (stage 5b)
    └── agent.py           # tool-calling triage agent (stage 9)
```

Files appear as their stage is built. Do not create empty placeholders.

### Interfaces I own

`src/fusion.py` exposes:

```python
def reciprocal_rank_fusion(
    ranked_lists: list[list[int]], k: int = 60
) -> list[tuple[int, float]]:
    """Merge ranked lists of issue numbers. Returns (issue_number, score)
    sorted descending."""
```

`src/eval.py` exposes:

```python
def evaluate(
    retriever: Callable[[str, datetime, int], list[int]],
    test_pairs: list[tuple[int, int]],
    ks: tuple[int, ...] = (1, 5, 10),
) -> dict:
    """retriever(query_text, before_date, n) -> ranked issue numbers.
    Returns {'recall@1': ..., 'recall@5': ..., 'recall@10': ..., 'mrr': ...}"""
```

Any retriever you build must match that signature. The `before_date` argument is
not optional — the eval is time-aware and only searches issues created before the
query issue.

## NOTES.md

**This file is the point of the project.** It is a running log, appended to at the
end of every stage, recording:

- which stage was built
- the eval numbers it produced (recall@1, recall@5, recall@10, MRR)
- latency where relevant
- any config that affects the numbers (model name, chunk size, top-k, RRF k)
- two or three concrete examples of queries that improved or regressed

It becomes the README ablation table and the interview prep in one file. A stage
that ran but was not logged did not happen — if NOTES.md was not updated, the
stage is not finished. Prompt me for the numbers if I forget.

## Stages

- **0.** Corpus probe — confirm duplicate density. *(throwaway)* ✅ **done**
- **1.** Schema + GraphQL ingestion into MariaDB.
- **2.** Eval harness and test-set extraction. *(I write this)*
- **3.** Baseline vector search. Record the number even though it is bad.
- **4.** BM25 + RRF hybrid.
- **5.** Cross-encoder reranking (`BAAI/bge-reranker-v2-m3`).
- **5b.** Contrastive fine-tuning of the embedding model on duplicate pairs.
- **6.** Field-aware handling — stack traces separated, title weighting, metadata filters.
- **7.** Q&A layer over issue comment threads (chunking matters here).
- **8.** Streamlit UI + README.
- **9.** Tool-calling triage agent (`search_duplicates`, `fetch_issue`, `suggest_labels`).

## Domain notes

- Retrieval here is **symmetric** — both sides are issue text, not a short query
  against long passages. Embed both sides identically. Do not apply the `query:`
  / `passage:` prefixes that asymmetric retrieval models expect.
- MariaDB uses the vector index only when the query is a bare
  `ORDER BY VEC_DISTANCE_COSINE(...) LIMIT n` and the index was built with
  `DISTANCE=cosine`. A mismatch silently falls back to a full table scan. Verify
  with `EXPLAIN` once data is loaded.
- At this corpus size (84,877 issues × 384 dims ≈ 130MB of float32) brute-force
  cosine in numpy is also viable and exact. Both paths are worth having; the
  README should say why the index exists anyway.
- vscode issue text is full of extension IDs, file paths and version strings.
  Embeddings smear these; BM25 catches them. That is the whole reason stage 4
  works, and the improvement should be visible in specific examples.

  > **Measured (stage 0):** the original wording of this note said "stack
  > traces." Stack traces appear in only **2.0%** of issues. The tokens that
  > actually carry the lexical signal are **file paths (53.2%)**, **fenced code
  > blocks (47.2%)** and **version strings (38.9%)**. Stage 4 should be justified
  > on those, and ablation examples chosen accordingly.

- **The duplicate target is not in any structured field.**
  `MarkedAsDuplicateEvent.canonical` is `null` throughout this repo — the triage
  bot sets the close reason without firing the UI path that populates it. The
  target exists only as prose in a comment, so `fetch.py` must persist comments
  and stage 2 must regex them. The pattern battery is preserved in NOTES.md.
