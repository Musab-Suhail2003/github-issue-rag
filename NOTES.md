# NOTES

Running log of what each stage produced. A stage that ran but was not logged
did not happen.

---

## Stage 0 — Corpus probe (2026-09-18)

**Goal:** confirm there are enough maintainer-marked duplicate pairs in
`microsoft/vscode` issues created >= 2024-01-01 to support a measured eval.
No retrieval yet, so no recall/MRR numbers — the deliverable is a go/no-go.

**Config:** `scripts/probe.py`, 545 unique duplicate-marked issues sampled,
stratified across 2024/2025/2026 and across both duplicate signals, drawn from
both ends of each year (`sort:created-asc` and `created-desc`) to limit recency
bias. ~40 GraphQL requests, well inside the 5,000/hr limit.

### Corpus size

| | count |
|---|---|
| issues created >= 2024-01-01 | **84,877** |
| — of which open | 14,591 |
| `label:*duplicate` | 5,370 |
| `reason:duplicate` (GitHub native close reason) | 3,194 |
| both signals | 526 |
| **union (duplicate-marked)** | **8,038** |

Per year: 2024 → 19,219 issues / 1,941 dup · 2025 → 34,005 / 3,464 ·
2026 (to Sept) → 31,653 / 2,633.

> **Correction to CLAUDE.md:** the corpus is 84,877 issues, not the ~40k
> estimated. Vectors at 384 dims float32 ≈ **130MB**, not 60MB. Still fine for
> brute-force numpy, but stage 1's fetch is ~850 paged requests, not ~400.

### The finding that shapes stage 1

**The canonical target is not in any structured GraphQL field.**
`MarkedAsDuplicateEvent.canonical` returned `null` on every issue sampled —
vscode's triage bot sets the close reason but never fires the "mark as
duplicate" UI path that populates it. The target exists only as prose:

> `vs-code-engineering`: "This issue is a duplicate of
> https://github.com/microsoft/vscode/issues/253137."

So the test set must be built by **regex over comment bodies**, which means
`fetch.py` has to persist comments, not just title/body. Schema implication,
decided before the fetch rather than after it.

### Extraction funnel

```
duplicate-marked in corpus window     8,038
x canonical extractable (strong)      40.4%
x canonical is an Issue not a PR     100.0%
x canonical created >= 2024-01-01     77.3%
--------------------------------------------
ESTIMATED USABLE PAIRS                ~2,507     (measured: 170 of 545 sampled)
```

**Verdict: GO.** Target was >=300 pairs for a stable eval; ~2,500 is ample.
Even recall@1 on a held-out slice will be meaningful.

### Which pattern fired (n=545)

| pattern | n | share |
|---|---|---|
| `NO_MATCH` | 190 | 34.9% |
| `dup_of` — "duplicate/dup of #N \| [#N](url) \| url" | 139 | 25.5% |
| `weak_bare_ref` — bare `#N` anywhere (**excluded**) | 135 | 24.8% |
| `dup_colon` — "/duplicate: #N" | 74 | 13.6% |
| `tracked_in` — "tracked in #N" (weakest, kept) | 7 | 1.3% |
| **strong total** | **220** | **40.4%** |

By year: 2024 40.0% · 2025 35.8% · 2026 46.5%. No collapse on older issues, so
the battery is not overfit to current bot wording.

**The 34.9% `NO_MATCH` is irreducible, not a regex gap.** Two causes, both
checked by hand:
- The bot's most common closing comment names no number at all: *"We figured
  it's covering the same as **another one we already have**."*
- 38% of `NO_MATCH` issues have **zero comments** — closed as duplicate silently.

**`weak_bare_ref` is deliberately excluded.** 149 of 150 sit in human comments,
and inspection showed they are roughly half false: `#236527 → #236647` is a real
duplicate, but `#237059 → #142813` was "Essential cause: <url>" (causal, not
duplicate) and `#236485 → #236390` was "there might be a number of related
issues" followed by three links. Admitting these would corrupt ground truth to
buy ~25% more pairs. Not worth it — the whole project rests on this label.

Adding the markdown-link form `[#123](url)` to the reference alternation moved
extraction 37.6% → 40.4% (+15 pairs). Cheap win, kept.

### Text character (evidence for stage 4)

Body length: median 1,773 chars, p90 4,472, 1 empty of 545.

| signal | share of issues |
|---|---|
| file paths | 53.2% |
| fenced code blocks | 47.2% |
| version strings (`Version: 1.x`) | 38.9% |
| stack frames (`at fn (file:12:34)`) | **2.0%** |

> **Correction to CLAUDE.md:** the stated rationale for BM25 is that vscode
> issues are "full of stack traces." Measured, stack traces appear in only
> **2.0%** of issues. The BM25 argument still holds, but the tokens that carry
> it are **file paths, code blocks and version strings**, not stack traces.
> Stage 4 should be justified on those, and the ablation examples chosen
> accordingly.

### Open decision for stage 1

22.7% of extractable pairs point at a canonical created **before** 2024-01-01
and are currently discarded. Recovering them does not require moving the corpus
bound — it requires letting the corpus include any issue that *is* a canonical
target, regardless of age, while still only using post-2024 issues as queries.
That changes a documented scoping decision, so: **flagged, not taken.**

### Regex battery (preserved — `probe.py` is throwaway)

```python
REF = r"\[?(?:#|https://github\.com/microsoft/vscode/issues/)(\d+)"
PATTERNS = [
    ("dup_of",     re.compile(r"\b(?:duplicate|dup)\s+of\s+" + REF, re.I)),
    ("dup_colon",  re.compile(r"^\s*/?(?:duplicate|dup)\b[:\s]+" + REF, re.I | re.M)),
    ("same_as",    re.compile(r"covering the same as\s+" + REF, re.I)),
    ("tracked_in", re.compile(r"\b(?:tracked|fixed|covered)\s+(?:in|by|here)\b[^.\n]{0,40}?" + REF, re.I)),
]
```
Resolve targets with `repository.issueOrPullRequest(number:)`, **not**
`repository.issue(number:)` — the latter returns `null` for a PR number, making
"target is a PR" indistinguishable from "target was deleted".
