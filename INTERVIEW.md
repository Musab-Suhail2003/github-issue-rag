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
| corpus | **84,942** issues ingested, created ≥ 2024-01-01 |
| duplicate-marked | 8,045 |
| usable labelled pairs | **2,516** (3,194 before enforcing time-ordering) |
| test split | 504 pairs, chronological 80/20 |
| canonical extractable from comments | 46.4% |
| canonical is an Issue, not a PR | 100% |
| canonical inside the date window | 85.6% |
| embedding size | 384 dims × 84,877 ≈ 130MB float32 |
| median issue body | 1,773 chars (p90 4,472) |
| comments ingested | 208,753 (avg 2.56/issue) |
| database on disk | ~400MB (+226MB embeddings) |
| **dense baseline** | **recall@10 0.2778, MRR 0.1627** |
| hybrid (BM25+RRF) | recall@10 0.2560 — *no improvement* |
| union ceiling @50 | 0.4206 (57.9% found by neither) |

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

## Stage 1 — decisions you must be able to defend

### "Your stage 0 estimate was off by 27%. Doesn't that undermine the probe?"

Worth having a straight answer, because the honest one is better than a defence.

Stage 0 sampled 545 issues and projected ~2,507 usable pairs. The real ingested
corpus yields **3,194**. Both underlying rates came in higher than sampled:
extraction 46.4% vs 40.4%, canonical-in-corpus 85.6% vs 77.3%.

It doesn't undermine the probe, for two reasons. First, the probe's job was a
go/no-go on whether enough labelled pairs existed at all — the threshold was 300
and the answer was "thousands" either way. Second, **the error was in the
conservative direction**, which is the right way for a feasibility estimate to be
wrong.

The likely cause is interesting and I'd volunteer it: the probe deliberately
sampled from both ends of each year to guard against the triage bot's wording
drifting over time. But issues near a year boundary are disproportionately likely
to point at a canonical filed in the *previous* year — outside the corpus window.
So the stratification that protected against one bias introduced another. I'd
flag that as a hypothesis I haven't separately verified, not a conclusion.

The general point: a sample of 545 gives roughly ±4% on a rate. If a decision
needed better precision than that, the probe was the wrong instrument.

### "How do you know fetching only 20 comments per issue didn't lose you data?"

Because I stored `comment_count` (the API's true total) next to
`comments_fetched` (what I actually kept), which makes the question answerable
instead of a guess.

Of 84,942 issues, **436 (0.5%)** have more comments than I fetched. Among the
8,045 duplicate-marked issues — the only ones the test set draws from — it's
**31 (0.4%)**. Average thread length is 2.56 comments and 14,481 issues have none
at all. Paginating every thread in full would have multiplied the request count
to recover almost nothing.

The part worth adding: this is fine *for duplicate detection* and will need
revisiting at stage 7, where Q&A over comment threads cares specifically about
the long tail that duplicate detection ignores. Knowing which 436 issues are
truncated means that's a targeted top-up, not a re-fetch.

### "How did you verify the ingestion was correct?"

I probed the API in stage 0 and counted the same quantities in the database in
stage 1, then compared. Independent paths to the same numbers:

| | probe (API) | DB |
|---|---|---|
| issues | 84,877 | 84,942 |
| `label:*duplicate` | 5,370 | 5,373 |
| `reason:duplicate` | 3,194 | 3,198 |
| overlap | 526 | 526 |

The small surplus is issues filed in the hours between the two runs. Plus
integrity checks: zero orphaned comments, zero rows outside the date bound, zero
null bodies.

### "Tell me about a bug you had to actually debug."

Use this one. It's the best story in the project because the first two
diagnoses were both wrong.

**Symptom.** The ingestion ran fine for 150 issues, then died with a MariaDB
1064 syntax error whose message contained a chunk of an issue body — meaning the
body text had reached the server as *SQL*, not as a bound parameter.

**Wrong diagnosis #1.** I knew `mysql-connector-python` rewrites `executemany`
INSERTs into one multi-row statement using a regex to find the VALUES clause,
and that `ON DUPLICATE KEY UPDATE title=VALUES(title)` gives that regex a second
`VALUES(...)` to match. Plausible, documented, and wrong. I replaced
`executemany` with a hand-built multi-row INSERT — and it failed identically.

**Wrong diagnosis #2.** I assumed bad characters in the body. I tested bodies
with `%`, `%s`, quotes, backslashes, real and escaped newlines. All inserted
fine. Then I tested the offending row alone, with its predecessor, with its
successor — all fine. The data was not the problem.

**What actually found it.** Bisecting on batch size. One row worked, 25 worked,
50 failed. The SQL template was 2,337 bytes with 600 bound parameters, so
neither statement length nor `max_allowed_packet` explained it. I ran the same
50-row upsert twelve times against `mysql-connector` (C extension *and* pure
Python) and twelve times against PyMySQL, with byte-identical parameters:

```
mysql-connector 26.7.0   ok=0   fail=12
PyMySQL 1.2.3            ok=12  fail=0
```

A driver bug in parameter mapping at high parameter counts. I switched to
PyMySQL, which CLAUDE.md already sanctioned as an alternative.

**The point to make when telling it:** two confident, reasonable hypotheses were
both wrong, and what settled it was a controlled comparison — same data, same
statement, one variable changed. Also worth saying out loud: a driver that
interpolates a parameter into SQL instead of binding it is an injection shape.
Here the input came from the GitHub API rather than a user, but I wouldn't leave
that driver in a path that touches untrusted input.

### "Why is the issue number your primary key instead of an auto-increment id?"

Because it's already the identifier everything else speaks. `eval.py` and
`fusion.py` both pass `list[int]` of issue numbers; a surrogate key would mean a
join on every lookup to translate back. Issue numbers are stable and unique
within a repo, which is exactly the contract a natural key needs.

The honest caveat: this breaks the moment I ingest a second repository, because
numbers are only unique *per repo*. At that point the key becomes
`(repo_id, number)`. Scoping decision, not an oversight — and saying so
unprompted is better than being caught by it.

### "You store `comment_count` and `comments_fetched` separately. Why?"

`comment_count` is the API's true total; `comments_fetched` is how many I
actually stored. I take `comments(last: 20)` because the duplicate declaration is
almost always the closing comment, so paginating full threads would multiply the
request count for data I don't need yet.

Keeping both columns means stage 7 can tell a complete thread from a truncated
one, instead of building a Q&A layer on top of silently-missing context. **The
alternative — storing only what I fetched — loses the information that anything
is missing at all.**

### "What happens if the ingestion dies halfway through?"

It resumes. Two mechanisms:

1. Every page commits its cursor to `fetch_state` in the same transaction as the
   rows. A kill costs at most one page.
2. Writes are `INSERT ... ON DUPLICATE KEY UPDATE`, not `INSERT IGNORE`. A
   re-run *refreshes* rows rather than skipping them, which matters because an
   issue's state, labels and comment count all change over time.

`INSERT IGNORE` would have been the lazy choice and would have quietly frozen
every row at whatever it looked like on first fetch.

### "Subtle one: when do you record the refresh watermark?"

**Before the crawl starts, not after.** A 50-minute crawl means 50 minutes of
issues being updated while I'm partway through. If I stamped the watermark at the
end, any issue updated after I'd already passed its page would fall into the gap
between "already crawled" and "newer than the watermark" — invisible forever.

Taking the start time means the next refresh re-reads a little overlap. Since
writes are idempotent, redundant work is free and missed work is not. **When in
doubt, overlap.**

### "Why delete-then-insert for labels instead of just inserting?"

Labels are genuinely *removed* during triage — `needs-more-info` comes off when
the reporter replies. A pure insert would accumulate stale rows and quietly
corrupt any metadata filter built on them in stage 6. The delete is scoped to the
issues in the current batch, so it stays cheap.

### "Why is `fetch_state` a key/value table?"

It holds a handful of crawl bookkeeping values — a cursor, a completion flag, a
last-fetch timestamp. Adding one more shouldn't require a migration. This is the
one place where schemaless beats typed columns, and it's worth being able to say
*why it's the exception* rather than the rule.

### Small ones

- **Naive UTC datetimes.** MariaDB `DATETIME` is timezone-less and the GitHub
  API returns UTC throughout, so tzinfo is stripped on the way in. Mixing aware
  and naive datetimes in Python raises on comparison — and the corpus date bound
  is compared against on every row.
- **Write order: issues, then labels and comments.** Both carry a foreign key to
  `issues`, so the parent has to land first within the transaction.
- **NUL bytes stripped from bodies.** They show up in pasted terminal output and
  upset both the connector and downstream tokenisers.

---

## Stage 4 — decisions you must be able to defend

**This is the most valuable stage to talk about, because it failed.**

### "So you built a hybrid retriever and it didn't work?"

Correct. Dense alone gets recall@10 = 0.2778; the best hybrid gets 0.2560. It
lost 2.2 points. Every gap is inside my ±4-point significance band, so the honest
claim is that **stage 4 changed nothing measurable**, and dense-only is still the
retriever of record.

The reason that's worth presenting rather than hiding: I found out *why*, and the
answer is structural rather than a tuning mistake.

### "How do you know it wasn't just badly tuned?"

I measured the ceiling before touching any knob. At depth 50, across 504 queries:

```
found by both   120  (23.8%)
dense only       77  (15.3%)
BM25 only        15   (3.0%)   <- everything fusion could possibly gain
neither         292  (57.9%)
```

BM25 finds **15 pairs** dense misses. A perfect fusion — one that always
promoted the right answer to the top — gains at most 3 points at depth 50, and
less by depth 10. Against that, merging a weaker ranked list costs dense's own
correct answers their positions.

So the trade is structurally bad, not badly configured. **Tuning a parameter
whose maximum payoff is 3 points isn't where effort belongs** — and knowing that
took one diagnostic instead of a week of grid search.

That's the answer I'd want to give to "tell me about a time you decided *not* to
optimise something."

### "Doesn't BM25 catch identifiers? That was the whole premise."

It does. That's what makes the result credible rather than just disappointing.
Its 15 unique saves are exactly the textbook cases:

> #307563 `[Unhandled Error] potential listener LEAK detected…`
> → #304828 `[042/d9f] potential listener LEAK in chatTerminalToolProgress…`

Literal phrase match, no semantic overlap in the titles. BM25 works precisely as
advertised — it just doesn't fire often enough on this corpus to pay for what
fusion costs elsewhere.

I'd also point out I **wrote the prediction down first**, in stage 3's notes:
"helps, but less than the 'BM25 catches identifiers' story suggests." That turned
out directionally right and still too optimistic. Recording it beforehand is what
makes it a result instead of a story told afterwards.

### "Why did you write your own BM25?"

Because `rank_bm25` was unusable here, and I verified my replacement rather than
trusting it.

`BM25Okapi.get_scores` recomputes the document-length normalisation across all
84,942 documents for every query token. An issue query is ~290 tokens, so that's
**2.7s per query** — 22 minutes per eval, over two hours for the seven configs I
needed.

Everything except the idf lookup depends only on the document, so it's
precomputable. I built an inverted index storing the finished per-(document,
term) weight, which turns a query into a scatter-add over postings lists.

The part that matters is the validation: same formula, and against the library —
**top-10 identical on every query, max score difference 8.8e-05** (float32 vs
float64), **147× faster**. `rank_bm25` stays in requirements.txt as the reference
I check against.

"I rewrote a library function" is a weak answer. "I rewrote it and proved it
matches to five decimal places" is a different conversation.

### "What did the tokenizer experiment show?"

Three ways to split `github/issue_read`:

| tokenizer | bm25 recall@10 |
|---|---|
| whitespace only — identifier stays whole | **0.2143** |
| split on non-alphanumeric | 0.2024 |
| emit both whole and parts | 0.1984 |

Keeping identifiers whole is best, by 1.2–1.6 points — directionally what I'd
predict, though inside the noise band so I won't oversell it. Splitting produces
common tokens (`issue`, `read`, `vs`) that dilute the signal; emitting both does
that *and* inflates vocabulary 61%.

After fusion the tokenizer stops mattering at all, because dense dominates the
merged ranking regardless.

### The number I'd actually lead with

**57.9% of queries have the correct answer in neither retriever's top 50.**

That reframes the whole project. A cross-encoder reranker reorders candidates —
it cannot invent one — so stage 5's recall@10 is capped at 0.4206 no matter how
good the reranker is. The lever for that 57.9% is better first-stage retrieval,
which means the stage 5b fine-tune, not reranking.

Concretely: **stage 5 should be judged on MRR and recall@1, not recall@10.**
Knowing which metric a stage can even move, before building it, is the kind of
thing the ablation table exists to tell you.


---

## Stage 3 — decisions you must be able to defend

### "Your baseline gets 27.8% recall@10. Isn't that terrible?"

Yes, and that's the point of recording it. A dense-only baseline on real
duplicate detection is *supposed* to be weak — if it were 90% there'd be nothing
to build and no ablation table worth showing.

What matters is that it's **honestly measured against a floor**: random retrieval
scores 0.0000, so 27.8% is real signal, not an artefact. Every later stage gets
compared against this exact number on this exact test split.

### "Why did the vector index make things slower?"

Because at 85k vectors it isn't needed, and I'd rather say that than pretend
otherwise.

Brute-force cosine in numpy is 32ms; the MariaDB HNSW path is 86ms. Three
reasons: brute force is a single in-process matmul over a 124MB matrix; the HNSW
path costs two SQL round trips per query plus protocol overhead; and it
over-fetches 10× candidates to survive post-filtering.

Accuracy is a wash — the biggest gap is 1.0 point, inside my own ±4-point
significance band, so I won't claim either is more accurate.

The index earns its place when the matrix stops fitting in RAM. Keeping both
paths is what lets me say that as a measurement instead of an assumption.

### The best technical finding in this stage: time filtering defeats the index

MariaDB uses a `VECTOR INDEX` only for a bare
`ORDER BY VEC_DISTANCE_COSINE(...) LIMIT n`. Add `WHERE created_at < ?` and the
index is silently disqualified — no error, no warning, just a full scan.

My eval is time-aware by construction, so **every single query needs that
filter**. The workaround over-fetches 10× neighbours from the bare indexed query
and date-filters afterwards, which is approximate a second time: if enough near
neighbours postdate the query, fewer than n survive.

Brute force sidesteps it entirely — vectors are held in `created_at` order, so
the time filter is an array slice rather than a predicate.

This is a good answer to "what surprised you," and it generalises: **ANN indexes
and metadata filters fight each other.** It's the same reason pgvector and
dedicated vector DBs have all had to build pre- vs post-filtering strategies.

### "Why no query prefix? bge's model card recommends one."

Because that instruction is for **asymmetric** retrieval — a short query against
long passages. Here both sides are issue text of similar length and register, so
it's symmetric, and applying the prefix to one side would put query and document
vectors in different regions of the space.

The eval and the embedder share `db.issue_text()` for exactly this reason: if the
two sides composed text differently, the numbers would partly be measuring that
discrepancy instead of the model.

### "Show me a case it fails on."

Three separate issues all point at canonical #302880 and none of them retrieved
it in the top 10:

- #305190 `Unknown tool 'github/issue_read' warning in Copilot Chat`
- #305137 `Unknown tool "problems" with v1.113.0`
- → #302880 "Problems panel showing problems from Copilot configuration files"

The reporters describe a **symptom**; the maintainer titled the canonical after
the **cause**. No shared title vocabulary, and a 33M-parameter embedding model
doesn't bridge that gap.

Meanwhile every rank-1 hit is a near-identical title — #304776 "Never ending
generation." → #304775 "Never ending generation.", filed minutes apart with the
same typo. **The model is winning where string equality would win anyway, and
losing everywhere that needs actual understanding.** That is the honest read of a
27.8% baseline.

### A prediction I wrote down before testing it

Rather than assume "BM25 catches identifiers," I checked the bodies:

| token | query #305190 | canonical #302880 |
|---|---|---|
| `unknown tool` | ✅ | ✅ |
| `issue_read` | ✅ | ❌ |

So BM25 has a genuine hook (`unknown tool` is in both) but *not* the rare
identifier the usual story relies on — `issue_read` isn't in the canonical at
all. Prediction recorded in NOTES.md before running stage 4: **it helps, but less
than the standard narrative suggests.**

Being willing to write a falsifiable prediction down first is worth more in an
interview than any individual number.


---

## Stage 2 — decisions you must be able to defend

### "Walk me through your evaluation setup."

2,516 labelled (duplicate → canonical) pairs, split chronologically 80/20 into
2,012 train and 504 test. Metrics are recall@1/5/10 and MRR. Every query is
time-filtered: the retriever only sees issues created before the query issue.

Two details that make it trustworthy rather than just plausible:

- **The extraction is frozen.** Pairs are extracted once into a committed
  `testset.json`; evaluation reads that file. If extraction ran inside the eval,
  editing a regex would silently move the benchmark and my stage-to-stage numbers
  would stop being comparable. An ablation table is only worth anything if one
  thing changes at a time.
- **The harness is unit-tested by an oracle retriever** that returns the correct
  answer first. It must score exactly 1.0. If it doesn't, the metric is broken
  and every number downstream is noise. I also run a random retriever to
  establish the floor.

### "Why a chronological split instead of a random one?"

Because stage 5b fine-tunes the embedding model on duplicate pairs. With a random
split it would train on pairs that postdate its own test queries — leakage that
would inflate exactly the stage the ablation table exists to measure, and nothing
else. Chronological also mirrors deployment: learn from history, answer new
issues.

### "You threw out another 678 pairs. Why?"

Because their canonical was created **after** the duplicate. Maintainers
sometimes close the *older* issue as a duplicate of the newer one.

A time-aware retriever can never return those, by construction. Scoring against
them would have depressed every stage's recall by ~8 percentage points for a
reason that has nothing to do with retrieval quality — and it would have looked
like a model problem, so I'd have spent time tuning against an artefact of the
labels.

This also corrected an earlier number of mine. Stage 1 reported 3,194 usable
pairs and concluded the stage 0 probe had been 27% conservative. Once
time-ordering is enforced it's 2,516, and the probe's estimate of 2,507 was
almost exactly right. **I was comparing the wrong quantity.** Worth saying out
loud in an interview — catching your own measurement error is a better signal
than never having made one.

### "How confident are you in a one-point improvement?"

Not at all, and the numbers say so. With n=504 and recall around 0.30, the
standard error is roughly ±2 points, so a 95% interval is about ±4. **Anything
under ~4 points between stages is not significant.**

I wrote that into NOTES.md before running any retriever, specifically so there
would be no temptation later to narrate noise as a win. If a stage needs finer
resolution the lever is a bigger test split, not a better story.

### "What stops a retriever from cheating?"

The `before_date` argument is part of the required signature, so it can't be
forgotten silently. On top of that, `eval.py` strips the query issue itself from
every result list — a retriever with a time-filter bug then shows up as a *bad*
score rather than a suspiciously good one. Failures should be loud.

`db.issue_text()` composes title and body in one place, shared by the eval and
the embedder, so the query side and document side can't drift apart. Otherwise
the numbers would partly be measuring that discrepancy instead of the model.

### Honest caveat to raise yourself

I wrote this harness; the project owner originally intended to. If asked what I'd
scrutinise hardest in someone else's eval code, the answer is the same three
things I built guards for: **is the time filter real, is the metric unit-tested,
and is the test set frozen.**


---

## Architecture — decisions you must be able to defend

### "Why doesn't your live demo talk to your database?"

The project splits into a batch side and a serving side. Batch does everything
expensive and stateful: fetch from the GitHub API, store in MariaDB, embed,
fine-tune, evaluate, then export four files. Serving loads those four files into
RAM and answers queries.

The serving path touches **no database, no API, no secret**. That's deliberate:
a Hugging Face Space restarts on its own schedule, and anything that could fail
at startup is something that can fail *during an interview*. The demo depends on
four files and nothing else.

The honest cost: the corpus is a frozen snapshot. I surface the snapshot date
and issue count in the UI so it reads as a deliberate choice rather than
something I forgot to refresh.

### "Why Streamlit instead of a React frontend and an API?"

One Python process renders the UI server-side over a websocket — no API layer,
no client build step, no CORS. A separate frontend would be real work that
demonstrates nothing this project is about. The interesting engineering is in
retrieval, and every hour spent on a client is an hour not spent on the ablation
table.

### "How do you refresh the snapshot without refetching all 85k issues?"

`fetch.py --since`, keyed on **`updatedAt`, not `createdAt`.**

This is the part worth saying out loud, because the naive version is wrong: an
issue created in January 2024 can be closed as a duplicate in 2026. The
duplicate marking — the exact thing my ground truth depends on — happens long
after creation. A refresh keyed on creation date would never see it, and my test
set would silently stop growing while looking like it worked.

So the two modes order by different fields:

| mode | order by | stop at |
|---|---|---|
| initial backfill | `CREATED_AT DESC` | `createdAt < 2024-01-01` |
| `--since` refresh | `UPDATED_AT DESC` | `updatedAt < last_fetch` |

`fetch_state` stores a cursor so an interrupted backfill resumes, and a
completed-at timestamp so `--since` has a floor.

### "Why MariaDB with a native VECTOR type instead of pgvector or a vector DB?"

It was already installed, and MariaDB has had a native `VECTOR` column type with
HNSW indexing since 11.7 — so no Docker, no extra service, no pgvector
extension to manage.

Be honest about the second half of this answer: at 84,877 × 384 float32 (~130MB)
a brute-force cosine scan in numpy is both exact and fast enough. The vector
index isn't strictly necessary at this scale. It's there because it's the path
that *would* matter at 10× the corpus, and because approximate-vs-exact is worth
being able to compare. Claiming I needed it would be overselling.

One real gotcha: MariaDB only uses the vector index when the query is a bare
`ORDER BY VEC_DISTANCE_COSINE(...) LIMIT n` **and** the index was built with
`DISTANCE=cosine`. Any mismatch silently falls back to a full scan — no error,
just slow. Verify with `EXPLAIN`.

### "Why no LangChain or LlamaIndex?"

Because the retrieval logic *is* the project. A framework would hide the exact
things an interviewer wants to probe — how RRF merges two ranked lists, how the
time filter is applied, why the embedding prefixes were left off. At this scale
the wrapper saves maybe fifty lines and costs the ability to explain my own
system.

### "Why does the hosted demo use a weaker reranker than your eval?"

`bge-reranker-v2-m3` is ~568M parameters and too slow on a free Space's 2 vCPU,
so serving uses `bge-reranker-base` or `ms-marco-MiniLM-L-6-v2` instead.

The answer that matters is that **I measured both**. Shipping the smaller model
silently would be a shortcut; recording what it costs in recall points makes it
an engineering decision with a stated price.

### Small ones

- **Dedicated DB user, not root.** Least privilege — a bug in ingestion can't
  reach another schema. Costs one minute.
- **Idempotent upserts.** `INSERT ... ON DUPLICATE KEY UPDATE` means an 850-request
  ingestion that dies at request 600 resumes instead of restarting, and a
  re-run refreshes rather than duplicating.

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

**Bi-encoder vs. cross-encoder.** A bi-encoder embeds the query and each
document separately, so document vectors can be precomputed — that's what makes
retrieval over 85k issues fast. A cross-encoder reads the query and one document
*together* and scores the pair, which is far more accurate and far too slow to
run over the whole corpus. Hence the two-stage shape: bi-encoder retrieves ~50
candidates, cross-encoder reorders them. Know why you can't just use the
cross-encoder for everything.

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
