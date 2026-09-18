def reciprocal_rank_fusion(ranked_lists, k=60):
    scores = {}
    for lst in ranked_lists:
        for rank, issue in enumerate(lst, start=1):
            scores[issue] = scores.get(issue, 0) + 1 / (k + rank)
    
    return sorted(scores.items(), key=lambda x: -x[1])