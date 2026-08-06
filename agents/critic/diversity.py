"""diversity - split from agents/critic.py (PR6)."""

from __future__ import annotations

from collections import defaultdict
from .metrics import _median, _sequence_similarity

def _diversity_summary(records: list[dict]) -> dict:
    by_sequence: dict[str, list[str]] = defaultdict(list)
    for item in records:
        sequence = str(
            (item["record"].get("candidate") or {}).get("sequence") or ""
        ).upper()
        by_sequence[sequence].append(item["candidate_id"])
    sequences = list(by_sequence)
    similarities = [
        _sequence_similarity(sequences[left], sequences[right])
        for left in range(len(sequences))
        for right in range(left + 1, len(sequences))
    ]
    duplicates = {
        sequence: sorted(candidate_ids)
        for sequence, candidate_ids in by_sequence.items()
        if sequence and len(candidate_ids) > 1
    }
    return {
        "candidate_count": len(records),
        "unique_sequence_count": len(sequences),
        "unique_fraction": len(sequences) / len(records) if records else 0.0,
        "duplicate_sequences": duplicates,
        "pairwise_similarity_method": "1-normalized_levenshtein_distance",
        "pairwise_similarity_n": len(similarities),
        "pairwise_similarity_median": _median(similarities),
        "pairwise_similarity_max": max(similarities) if similarities else None,
    }
