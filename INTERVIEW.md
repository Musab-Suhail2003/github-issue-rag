# Interview prep

Things to have in your head, not just in the repo. Grown one section per stage.
NOTES.md is the evidence; this is the argument.

---

## The 30-second pitch

> Hybrid retrieval over 85k GitHub issues from microsoft/vscode, for finding
> duplicates of a newly filed issue. What makes it different from a toy RAG demo
> is that it has real ground truth — maintainers mark duplicates, so I have
> ~2,500 labelled pairs. Every stage is justified by a recall number, not by
> vibes. I can tell you exactly what dense retrieval alone scored, and exactly
> how much BM25 and reranking each added on top.

If you remember one framing: **this project is an experiment, not a feature.**
The ablation table is the deliverable.

---

## Numbers to have memorized

| | |
|---|---|
| corpus | 84,877 issues, created ≥ 2024-01-01 |
| duplicate-marked | 8,038 |
| usable labelled pairs | ~2,507 |
| canonical extractable from comments | 40.4% |
| canonical is an Issue, not a PR | 100% |
| canonical inside the date window | 77.3% |
| embedding size | 384 dims × 84,877 ≈ 130MB float32 |
| median issue body | 1,773 chars (p90 4,472) |

---

## Stage 0 — decisions you must be able to defend

### "Why did you build a throwaway probe before writing any real code?"

Because the entire project rests on an assumption I hadn't checked: that enough
maintainer-marked duplicate pairs exist and are machine-extractable. If that were
false, I'd have discovered it at stage 2 — after building a schema and running an
850-request ingestion. Twenty minutes of probing de-risked days of work.

It also caught three wrong assumptions in my own design doc, which is the real
argument for doing it.

### "Why did you throw away 25% of your candidate pairs?"

This is the best question they can ask you. The answer:

There were 150 cases where a bare `#123` appeared somewhere in the thread. Taking
them would have grown the test set by about a quarter. I read a sample by hand
and roughly half were false — one was *"Essential cause: `<url>`"* (a causal
link, not a duplicate), another was *"there might be a number of related issues"*
followed by three different links.

**The tradeoff is precision vs. recall on ground truth, not on retrieval.** A
noisy test set doesn't just add noise — it silently caps your measurable ceiling
and you can't tell the difference between "my retriever missed it" and "the label
was wrong." I'd rather have 2,500 clean pairs than 4,000 dirty ones.

### "How did you know the duplicate target wasn't in a structured field?"

I assumed it was — GitHub has a `MarkedAsDuplicateEvent` with a `canonical`
field, which is exactly what you'd want. I queried 25 duplicate-closed issues and
got `null` back on all 25.

So I pulled one issue's raw timeline and found the close reason was set to
`DUPLICATE` but no such event existed. The target was only in a bot comment:
*"This issue is a duplicate of https://github.com/microsoft/vscode/issues/253137."*

vscode's triage bot closes via API without firing the UI path that populates the
event. **That finding changed the schema** — `fetch.py` has to persist comments,
not just title and body. Good thing it surfaced before the ingestion run.

### "Why stratify your sample by year?"

Because the bot's comment wording has changed over three years, and I was
extracting with regex. Sampling only recent issues would have measured the
current phrasing and overstated coverage on the older half of the corpus.

I also drew from both ends of each year (`sort:created-asc` and `created-desc`).
The concern was real: extraction came out at 40.0% / 35.8% / 46.5% for
2024 / 2025 / 2026.

### "34.9% of duplicates had no extractable target — did you try harder?"

Yes, and I established the limit is in the data, not the regex:

- The bot's most common closing comment names **no number at all**: *"We figured
  it's covering the same as another one we already have."*
- 38% of the unmatched issues have **zero comments** — closed silently.

I did find one real regex gap — the markdown link form `[#123](url)` broke a
pattern that assumed a bare `#`. Fixing it moved extraction 37.6% → 40.4%.

### Small ones worth having ready

- **Union of two search signals.** GitHub search ANDs qualifiers and won't OR
  them, so I got the union by inclusion–exclusion: `label + reason − both`
  (5,370 + 3,194 − 526 = 8,038).
- **`issueOrPullRequest`, not `issue`.** `repository.issue(number:)` returns
  `null` for a PR number, which is indistinguishable from a deleted issue. The
  union type makes "this target is a PR, drop the pair" an explicit branch.
- **The `issues` connection, not `search`.** Search caps at 1,000 results per
  query; cursoring the connection covers all 85k. Ordering `CREATED_AT DESC` and
  stopping at the date bound means never touching the ~200k older issues.

---

## Concepts to be solid on

**Time-aware evaluation / leakage.** The eval only searches issues created
*before* the query issue. If you skip this, you retrieve issues filed after the
duplicate was reported and your recall is inflated by information that wouldn't
have existed at triage time. This is the RAG version of look-ahead bias. Be ready
to explain why it's non-optional — it's a parameter in the retriever signature
precisely so it can't be forgotten.

**Symmetric vs. asymmetric retrieval.** Here both sides are issue text of similar
length, so both are embedded identically. Models like E5 expect `query:` /
`passage:` prefixes because they're trained for short-query-against-long-document.
Applying those prefixes here would hurt. Know why you didn't use them.

**Idempotent upserts.** `INSERT ... ON DUPLICATE KEY UPDATE` means re-running the
fetch refreshes rather than duplicates, so an interrupted 850-request ingestion
resumes instead of restarting.

**Cursor vs. offset pagination.** GraphQL hands you an opaque cursor for the next
page. Unlike `OFFSET`, it stays correct when rows are inserted mid-crawl, and it
doesn't get slower as you go deeper.

**Python packaging** (you asked about this — worth knowing cold):
- A **virtualenv is not a Python installation.** It's a folder with a symlink to
  an interpreter that already exists, plus its own `site-packages`. It isolates
  *packages*, never *versions*. `python3.14 -m venv` can only ever produce 3.14.
- To get a different *version* without touching the system you'd need `uv` or
  pyenv, which download a standalone interpreter.
- A **wheel** is a prebuilt binary package. Its filename encodes compatibility:
  `torch-2.14.0-cp314-cp314-manylinux_2_28_x86_64.whl` means CPython 3.14,
  Linux with glibc ≥ 2.28, x86-64. If no wheel matches your interpreter, pip
  falls back to building from source — which for torch is not viable.
- Lesson learned the hard way: check the **platform** tag, not just the Python
  tag. A `cp314` wheel that only exists for macOS won't help you on Linux.

---

## Honest weaknesses — have answers ready

- **Single repository.** Everything is tuned to microsoft/vscode's conventions,
  especially the regex battery. Generalizing means re-deriving extraction per
  repo. I'd frame that as scoping, not oversight.
- **The date bound discards 22.7% of pairs** whose canonical predates 2024. The
  fix doesn't require refetching the world — let the corpus include any issue
  that *is* a canonical target regardless of age, while still only using
  post-2024 issues as queries.
- **Ground truth is maintainer-marked, so it's a floor, not a ceiling.** Real
  duplicates that no maintainer ever linked count against me as misses. My recall
  numbers understate true performance, and I can't quantify by how much.
- **Extraction is regex over prose.** It's precision-first by design, but it is
  still a heuristic layer between the data and the labels.

---

## What I'd do differently

Probe *first* — which I did — but also check the **structured** field before
assuming it works, instead of after. I designed a schema in my head that didn't
persist comments, and only the probe stopped that from becoming an 850-request
mistake.

---

## To fill in as stages land

Each of these gets the same treatment: the decision, the number that justifies
it, and the question an interviewer would ask.

- Stage 1 — schema + ingestion
- Stage 2 — eval harness, test-set extraction *(you write this)*
- Stage 3 — dense baseline · **the number that makes everything after it mean something**
- Stage 4 — BM25 + RRF · expect the win to come from file paths and version strings, not stack traces
- Stage 5 / 5b — cross-encoder rerank, contrastive fine-tune
- Stage 6 — field-aware retrieval
- Stage 7 — Q&A over comment threads
- Stage 8 — UI + README
- Stage 9 — triage agent
