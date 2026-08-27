"""Shared natural-amino-acid cyclic-peptide sequence contract."""

from __future__ import annotations


STANDARD_AMINO_ACIDS = frozenset("ACDEFGHIKLMNPQRSTVWY")
MIN_CYCLIC_PEPTIDE_LENGTH = 7
MAX_CYCLIC_PEPTIDE_LENGTH = 20


def is_supported_cyclic_sequence(sequence: object) -> bool:
    """Return whether ``sequence`` is representable by the current pipeline."""
    if not isinstance(sequence, str):
        return False
    normalized = sequence.strip().upper()
    return (
        MIN_CYCLIC_PEPTIDE_LENGTH
        <= len(normalized)
        <= MAX_CYCLIC_PEPTIDE_LENGTH
        and all(residue in STANDARD_AMINO_ACIDS for residue in normalized)
    )


def supported_length_message(subject: str = "cyclic peptide") -> str:
    return (
        f"{subject} length must be between {MIN_CYCLIC_PEPTIDE_LENGTH} "
        f"and {MAX_CYCLIC_PEPTIDE_LENGTH} residues"
    )
