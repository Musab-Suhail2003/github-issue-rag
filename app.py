"""Stage 8: Streamlit UI.

Reads artifacts/ and nothing else -- no database, no GitHub API, no secrets, so
nothing external can break the demo. One Python process renders server-side over
a websocket; there is no API layer and no client build.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import numpy as np
import streamlit as st

ART = Path(__file__).parent / "artifacts"
# The stage 5b fine-tune. Override with MODEL_ID to fall back to the base model.
MODEL_ID = os.getenv("MODEL_ID", "Musab6969/bge-small-vscode-dup")
FALLBACK_MODEL = "BAAI/bge-small-en-v1.5"
MAX_CHARS = 2000

st.set_page_config(page_title="VS Code duplicate issue finder",
                   page_icon="🔎", layout="wide")


@st.cache_resource(show_spinner="Loading index…")
def load_index():
    mat = np.load(ART / "embeddings.npy", mmap_mode="r")
    ids = np.load(ART / "issue_ids.npy")
    manifest = json.loads((ART / "manifest.json").read_text())
    return mat, ids, manifest


@st.cache_resource(show_spinner="Loading model…")
def load_model():
    from sentence_transformers import SentenceTransformer
    try:
        return SentenceTransformer(MODEL_ID), MODEL_ID
    except Exception:
        # The Space still works without the fine-tune, just less well. Better a
        # degraded demo than a stack trace.
        return SentenceTransformer(FALLBACK_MODEL), FALLBACK_MODEL + " (fallback)"


@st.cache_resource
def meta_db():
    return sqlite3.connect(ART / "issues.sqlite", check_same_thread=False)


def strip_boilerplate(body: str) -> str:
    """Mirror of db.strip_boilerplate. The serving path must not import src/."""
    import re
    body = re.sub(r"<details>.*?</details>", " ", body, flags=re.S | re.I)
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    body = re.sub(r"^\s*Type:\s*<b>.*?</b>\s*$", "", body, flags=re.I | re.M)
    body = re.sub(
        r"^\s*(?:VS Code version|Extension version|OS version|Modes|Remote OS version"
        r"|Local OS version|Extension Host Version|Steps to Reproduce)\s*:.*$",
        "", body, flags=re.I | re.M)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def search(query: str, k: int = 10):
    mat, ids, _ = load_index()
    model, _ = load_model()
    vec = model.encode(query[:MAX_CHARS], normalize_embeddings=True,
                       convert_to_numpy=True).astype(np.float32)
    sims = np.asarray(mat) @ vec
    top = np.argpartition(-sims, k)[:k]
    top = top[np.argsort(-sims[top])]
    rows = []
    cur = meta_db().cursor()
    for i in top:
        n = int(ids[i])
        r = cur.execute(
            "SELECT number,title,snippet,state,state_reason,url,created_at,labels "
            "FROM issues WHERE number=?", (n,)).fetchone()
        if r:
            rows.append((float(sims[i]), r))
    return rows


mat, ids, manifest = load_index()

st.title("🔎 VS Code duplicate issue finder")
st.caption(
    f"Semantic search over **{manifest['issues']:,}** `microsoft/vscode` issues "
    f"created since {manifest['corpus_start']} · snapshot {manifest['snapshot_date']}"
)

with st.sidebar:
    st.subheader("How well does it work?")
    st.metric("recall@10", f"{manifest['recall_at_10']:.3f}")
    st.metric("MRR", f"{manifest['mrr']:.3f}")
    st.caption(
        f"Measured on {manifest['test_pairs']} maintainer-marked duplicate pairs "
        "the model never saw, with a time filter so only issues predating each "
        "query are searchable."
    )
    st.divider()
    st.caption(
        f"**Model** · `{manifest['base_model']}` fine-tuned on 2,000 duplicate "
        "pairs with MultipleNegativesRankingLoss."
    )
    st.caption(
        "**Why 0.32 and not 0.9** · ground truth is maintainer-marked, so real "
        "duplicates nobody linked count as misses. The number is a floor."
    )
    k = st.slider("Results", 5, 25, 10)

st.markdown("Paste an issue — title on the first line, body below.")
query = st.text_area(
    "New issue", height=180, label_visibility="collapsed",
    placeholder="Terminal freezes when running a long command\n\n"
                "Steps to reproduce:\n1. Open a terminal\n2. Run a build…",
)

col1, col2 = st.columns([1, 6])
go = col1.button("Find duplicates", type="primary")
clean = col2.checkbox("Strip template boilerplate", value=True,
                      help="Measured +2.4 recall@10: VS Code's bug template is "
                           "~47% of the text the model reads.")

if go and query.strip():
    text = query if not clean else (
        query.split("\n", 1)[0] + "\n\n" +
        strip_boilerplate(query.split("\n", 1)[1] if "\n" in query else "")
    )
    with st.spinner("Searching…"):
        results = search(text, k)
    if not results:
        st.info("No matches.")
    for score, (n, title, snippet, state, reason, url, created, labels) in results:
        with st.container(border=True):
            a, b = st.columns([6, 1])
            a.markdown(f"**[#{n} — {title}]({url})**")
            b.markdown(f"`{score:.3f}`")
            bits = [created[:10], state]
            if reason:
                bits.append(reason)
            st.caption(" · ".join(bits) + (f" · {labels}" if labels else ""))
            if snippet:
                st.markdown(
                    f"<div style='opacity:.75;font-size:.9em'>{snippet[:320]}…</div>",
                    unsafe_allow_html=True)
elif go:
    st.warning("Paste an issue first.")
