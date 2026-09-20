# Notebooks

The GPU half of the pipeline. Everything else — ingestion, evaluation,
retrieval development — runs locally against MariaDB.

## Why the split

The development machine is an i7-8650U with no GPU. Embedding 84,942 issues and
fine-tuning are the only GPU-shaped jobs, so they run on a free Colab T4; the
database never leaves the laptop.

```
laptop                          Colab (T4)                    laptop
──────                          ──────────                    ──────
embed.py --export-texts   ──►   encode / fine-tune      ──►   embed.py --import-vectors
finetune.py --export-pairs      push model to HF Hub          retrieve.py  (the numbers)
rerank.py --export-task         score reranker candidates     scripts/export.py
```

Each notebook takes a small `.jsonl.gz` produced locally and returns `.npy`
shards. Nothing here touches the database or any credential except your HF
token.

## Order

| notebook | stage | produces | runtime |
|---|---|---|---|
| [01 · Encode corpus](01_encode_corpus.ipynb) | 3 | base vectors | ~2 min |
| [02 · Contrastive fine-tune](02_finetune.ipynb) | 5b | recall@10 0.3016 → **0.3214** | ~5 min |
| [03 · Hard-negative mining](03_finetune_hardneg.ipynb) | 5c | recall@10 0.3214 → **0.3393** | ~8 min |
| [04 · Cross-encoder reranking](04_rerank.ipynb) | 5 | a **negative** result (−6.7) | ~5 min |

**03 is the model that ships.** 01 and 02 are the steps that led there and are
kept so the progression is reproducible. 04 is kept because the result was
negative and the ablation table would be dishonest without it.

## Things that will bite you

These are in the notebooks at the point they matter; collected here so they are
findable.

- **Shards, not one array.** Free Colab runtimes get reclaimed without warning.
  Each notebook writes numbered shards, so a disconnect costs one shard.
- **`max_seq_length = 256` when training.** Training text is p50 150 tokens; the
  512 default pads every batch to its longest member and attention is quadratic.
  Notebook 03 OOMs on a 15GB T4 without this.
- **`batch_size = 16` for triplets.** Three texts per example means 96 sequences
  per step at batch 32.
- **Encode with the *same* `model` object you trained.** A fresh
  `SentenceTransformer("BAAI/bge-small-en-v1.5")` silently encodes with the
  untrained model — the vectors load fine and retrieval is just worse.
- **`create_repo(exist_ok=True)` then `upload_folder`**, not `push_to_hub`,
  which returns 409 if the repo exists.
- **Your HF username is probably not your GitHub username.** A wrong namespace
  gives 403. Check `huggingface_hub.whoami()`.
- **OOM at 0 MB allocated?** `Runtime > Disconnect and delete runtime`.
  "Restart session" keeps your files but does not always release the GPU.
- **No bge query prefix.** Retrieval here is symmetric — issue against issue, not
  short query against long passage. The prefix would split the space.

Full reasoning and every measurement: [`../NOTES.md`](../NOTES.md).
