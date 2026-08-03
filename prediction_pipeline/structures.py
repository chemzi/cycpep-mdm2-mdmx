"""Small, strict PDB reader and geometry routines used by Prediction."""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .contracts import ContractError


THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
    "MSE": "M",
}
BACKBONE_ATOMS = ("N", "CA", "C")


@dataclass(frozen=True)
class Atom:
    name: str
    coord: np.ndarray
    bfactor: float
    element: str
    occupancy: float


@dataclass
class Residue:
    chain: str
    number: int
    insertion_code: str
    name: str
    atoms: dict[str, Atom] = field(default_factory=dict)

    @property
    def one_letter(self) -> str:
        return THREE_TO_ONE.get(self.name, "X")

    @property
    def key(self) -> tuple[int, str]:
        return self.number, self.insertion_code


@dataclass
class Structure:
    path: Path
    chains: "OrderedDict[str, list[Residue]]"
    model_number: int

    @property
    def residues(self) -> list[Residue]:
        return [residue for values in self.chains.values() for residue in values]

    def sequence(self, chain: str) -> str:
        if chain not in self.chains:
            raise ContractError(
                "chain_missing", f"chain {chain!r} missing from {self.path}"
            )
        return "".join(residue.one_letter for residue in self.chains[chain])


def parse_pdb(path: str | Path) -> Structure:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ContractError("pdb_missing", f"PDB not found: {path}")

    chains: "OrderedDict[str, list[Residue]]" = OrderedDict()
    residue_lookup: dict[tuple[str, int, str], Residue] = {}
    model_number = 1
    saw_model = False
    active_model = True
    atom_count = 0

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        record = line[0:6].strip()
        if record == "MODEL":
            if saw_model:
                active_model = False
                continue
            saw_model = True
            active_model = True
            try:
                model_number = int(line[10:14].strip() or "1")
            except ValueError:
                model_number = 1
            continue
        if record == "ENDMDL" and active_model:
            break
        if not active_model or record not in {"ATOM", "HETATM"}:
            continue
        if len(line) < 54:
            raise ContractError("pdb_atom_malformed", f"short atom line in {path}")
        residue_name = line[17:20].strip().upper()
        if residue_name not in THREE_TO_ONE:
            continue
        atom_name = line[12:16].strip().upper()
        altloc = line[16:17].strip()
        if altloc not in {"", "A"}:
            continue
        chain = line[21:22].strip() or "_"
        insertion = line[26:27].strip()
        try:
            number = int(line[22:26].strip())
            coord = np.array(
                [float(line[30:38]), float(line[38:46]), float(line[46:54])],
                dtype=float,
            )
            occupancy = float(line[54:60].strip() or "0")
            bfactor = float(line[60:66].strip() or "0")
        except ValueError as exc:
            raise ContractError(
                "pdb_atom_malformed", f"invalid numeric atom field in {path}"
            ) from exc
        if not np.isfinite(coord).all() or not math.isfinite(bfactor):
            raise ContractError("pdb_atom_nonfinite", f"non-finite atom value in {path}")
        element = line[76:78].strip().upper() if len(line) >= 78 else atom_name[:1]
        key = (chain, number, insertion)
        residue = residue_lookup.get(key)
        if residue is None:
            residue = Residue(chain, number, insertion, residue_name)
            residue_lookup[key] = residue
            chains.setdefault(chain, []).append(residue)
        elif residue.name != residue_name:
            raise ContractError(
                "pdb_residue_conflict",
                f"conflicting residue names at {chain}:{number}{insertion} in {path}",
            )
        previous = residue.atoms.get(atom_name)
        if previous is None or (altloc == "" and occupancy >= previous.occupancy):
            residue.atoms[atom_name] = Atom(
                atom_name, coord, bfactor, element, occupancy
            )
        atom_count += 1

    if atom_count == 0 or not chains:
        raise ContractError("pdb_empty", f"no protein atoms found in {path}")
    return Structure(path=path, chains=chains, model_number=model_number)


def exact_sequence_chain(structure: Structure, sequence: str) -> str:
    matches = [
        chain for chain in structure.chains
        if structure.sequence(chain) == sequence
    ]
    if len(matches) != 1:
        detail = {chain: structure.sequence(chain) for chain in structure.chains}
        raise ContractError(
            "structure_sequence_mismatch",
            f"{structure.path} must contain exactly one chain matching {sequence}; "
            f"observed={detail}",
        )
    return matches[0]


def mean_plddt(structure: Structure, chain: str) -> tuple[float, str]:
    values = []
    for residue in structure.chains.get(chain, []):
        atom = residue.atoms.get("CA")
        if atom is None:
            raise ContractError(
                "plddt_missing_ca",
                f"{structure.path} chain {chain} has a residue without CA",
            )
        values.append(atom.bfactor)
    if not values:
        raise ContractError("plddt_missing", f"no pLDDT values in {structure.path}")
    array = np.asarray(values, dtype=float)
    if np.any(array < 0) or np.any(array > 100):
        raise ContractError("plddt_scale_invalid", f"pLDDT outside 0-100 in {structure.path}")
    if float(array.max()) <= 1.0:
        normalized = array
        scale = "0-1"
    else:
        normalized = array / 100.0
        scale = "0-100"
    value = float(normalized.mean())
    if not 0 <= value <= 1:
        raise ContractError("plddt_scale_invalid", f"normalized pLDDT invalid in {structure.path}")
    return value, scale


def terminal_bond_distance(structure: Structure, chain: str) -> float:
    residues = structure.chains.get(chain, [])
    if len(residues) < 2:
        raise ContractError(
            "terminal_atoms_missing", f"too few residues in {structure.path} chain {chain}"
        )
    first_n = residues[0].atoms.get("N")
    last_c = residues[-1].atoms.get("C")
    if first_n is None or last_c is None:
        raise ContractError(
            "terminal_atoms_missing",
            f"N(first) or C(last) missing in {structure.path} chain {chain}",
        )
    return float(np.linalg.norm(first_n.coord - last_c.coord))


def _coords_for_residues(residues: list[Residue], atom_names=BACKBONE_ATOMS) -> np.ndarray:
    coords = []
    for residue in residues:
        for atom_name in atom_names:
            atom = residue.atoms.get(atom_name)
            if atom is None:
                raise ContractError(
                    "backbone_atom_missing",
                    f"{residue.chain}:{residue.number}{residue.insertion_code} "
                    f"lacks {atom_name}",
                )
            coords.append(atom.coord)
    if len(coords) < 3:
        raise ContractError("too_few_alignment_atoms", "at least three atoms are required")
    return np.asarray(coords, dtype=float)


def kabsch_transform(mobile: np.ndarray, reference: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if mobile.shape != reference.shape or mobile.ndim != 2 or mobile.shape[1] != 3:
        raise ContractError("alignment_shape_mismatch", "coordinate arrays must have equal Nx3 shape")
    mobile_center = mobile.mean(axis=0)
    reference_center = reference.mean(axis=0)
    covariance = (mobile - mobile_center).T @ (reference - reference_center)
    u, _, vt = np.linalg.svd(covariance)
    rotation = u @ vt
    if np.linalg.det(rotation) < 0:
        vt[-1, :] *= -1
        rotation = u @ vt
    translation = reference_center - mobile_center @ rotation
    return rotation, translation


def apply_transform(coords: np.ndarray, transform: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
    rotation, translation = transform
    return coords @ rotation + translation


def rmsd(mobile: np.ndarray, reference: np.ndarray) -> float:
    if mobile.shape != reference.shape or not len(mobile):
        raise ContractError("rmsd_shape_mismatch", "RMSD arrays must be non-empty and equal")
    return float(np.sqrt(np.mean(np.sum((mobile - reference) ** 2, axis=1))))


def backbone_rmsd(
    mobile: Structure,
    mobile_chain: str,
    reference: Structure,
    reference_chain: str,
) -> float:
    mobile_residues = mobile.chains.get(mobile_chain, [])
    reference_residues = reference.chains.get(reference_chain, [])
    if len(mobile_residues) != len(reference_residues):
        raise ContractError(
            "backbone_length_mismatch",
            f"backbone lengths differ: {len(mobile_residues)} vs {len(reference_residues)}",
        )
    left = _coords_for_residues(mobile_residues)
    right = _coords_for_residues(reference_residues)
    return rmsd(apply_transform(left, kabsch_transform(left, right)), right)


def infer_chain_by_length(
    structure: Structure, length: int, excluded: set[str] | None = None
) -> str:
    excluded = excluded or set()
    matches = [
        chain for chain, residues in structure.chains.items()
        if chain not in excluded and len(residues) == length
    ]
    if len(matches) != 1:
        raise ContractError(
            "binder_chain_ambiguous",
            f"expected one chain of length {length} in {structure.path}; found {matches}",
        )
    return matches[0]


def interface_hotspot_metrics(
    structure: Structure,
    target_chain: str,
    binder_chain: str,
    hotspots: list[int],
    cutoff: float,
    target_residue_numbers: list[int] | None = None,
) -> dict:
    if target_chain not in structure.chains or binder_chain not in structure.chains:
        raise ContractError(
            "interface_chain_missing",
            f"target/binder chain missing from {structure.path}",
        )
    target_residues = structure.chains[target_chain]
    binder_residues = structure.chains[binder_chain]
    if (
        target_residue_numbers is not None
        and len(target_residue_numbers) != len(target_residues)
    ):
        raise ContractError(
            "target_residue_mapping_mismatch",
            "canonical target residue numbering must match the predicted target length",
        )
    binder_atoms = np.asarray([
        atom.coord
        for residue in binder_residues
        for atom in residue.atoms.values()
        if atom.element != "H"
    ])
    if binder_atoms.size == 0:
        raise ContractError("interface_atoms_missing", "binder has no heavy atoms")
    contacts: set[int] = set()
    for index, residue in enumerate(target_residues):
        atoms = np.asarray([
            atom.coord for atom in residue.atoms.values() if atom.element != "H"
        ])
        if atoms.size and float(np.min(np.linalg.norm(
            atoms[:, None, :] - binder_atoms[None, :, :], axis=2
        ))) <= cutoff:
            contacts.add(
                target_residue_numbers[index]
                if target_residue_numbers is not None
                else residue.number
            )
    try:
        configured = {int(value) for value in hotspots}
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "hotspots_invalid", "target binding_site.residues must be integers"
        ) from exc
    if not configured:
        raise ContractError("hotspots_missing", "target binding_site.residues is empty")
    covered = configured & contacts
    return {
        "hotspot_cov": len(covered) / len(configured),
        "site_consistency": bool(covered),
        "covered_hotspots": sorted(covered),
        "interface_target_residues": sorted(contacts),
        "contact_cutoff_angstrom": float(cutoff),
    }


def canonical_target_residue_numbers(
    reference: Structure,
    reference_chain: str,
    prediction: Structure,
    prediction_chain: str,
) -> list[int]:
    """Map a predictor's target residues back to reviewed PDB numbering.

    ColabDesign preserves target sequence order but may rewrite PDB residue
    numbers, including negative values for discontinuous source numbering.
    Mapping by verified sequence order keeps hotspot IDs in the approved target
    coordinate system without trusting predictor-specific residue IDs.
    """
    reference_residues = reference.chains.get(reference_chain, [])
    prediction_residues = prediction.chains.get(prediction_chain, [])
    reference_sequence = "".join(item.one_letter for item in reference_residues)
    prediction_sequence = "".join(item.one_letter for item in prediction_residues)
    if (
        not reference_residues
        or len(reference_residues) != len(prediction_residues)
        or reference_sequence != prediction_sequence
    ):
        raise ContractError(
            "target_residue_mapping_mismatch",
            "reviewed and predicted target chains must have identical residue order: "
            f"reference={reference.path}:{reference_chain} "
            f"({len(reference_residues)} residues), "
            f"prediction={prediction.path}:{prediction_chain} "
            f"({len(prediction_residues)} residues)",
        )
    return [residue.number for residue in reference_residues]


def _sequence_aligned_residue_pairs(
    mobile_residues: list[Residue],
    reference_residues: list[Residue],
) -> list[tuple[Residue, Residue]]:
    """Globally align near-identical target chains with terminal overhangs."""
    mobile_sequence = "".join(residue.one_letter for residue in mobile_residues)
    reference_sequence = "".join(residue.one_letter for residue in reference_residues)
    mobile_n, reference_n = len(mobile_sequence), len(reference_sequence)
    if not mobile_n or not reference_n:
        raise ContractError(
            "target_sequence_alignment_mismatch", "target chain is empty"
        )

    match_score, mismatch_score, gap_score = 2, -1, -2
    scores = np.zeros((mobile_n + 1, reference_n + 1), dtype=np.int32)
    trace = np.zeros((mobile_n + 1, reference_n + 1), dtype=np.uint8)
    scores[:, 0] = np.arange(mobile_n + 1) * gap_score
    scores[0, :] = np.arange(reference_n + 1) * gap_score
    trace[1:, 0] = 1  # up: mobile residue aligned to a gap
    trace[0, 1:] = 2  # left: reference residue aligned to a gap

    for mobile_i in range(1, mobile_n + 1):
        for reference_i in range(1, reference_n + 1):
            diagonal = scores[mobile_i - 1, reference_i - 1] + (
                match_score
                if mobile_sequence[mobile_i - 1] == reference_sequence[reference_i - 1]
                else mismatch_score
            )
            up = scores[mobile_i - 1, reference_i] + gap_score
            left = scores[mobile_i, reference_i - 1] + gap_score
            best = max(diagonal, up, left)
            scores[mobile_i, reference_i] = best
            # Prefer a residue pair on ties, then a mobile gap.  This makes the
            # mapping deterministic without relying on PDB residue numbers.
            trace[mobile_i, reference_i] = 0 if diagonal == best else 1 if up == best else 2

    pairs: list[tuple[Residue, Residue]] = []
    matches = 0
    mobile_i, reference_i = mobile_n, reference_n
    while mobile_i or reference_i:
        direction = trace[mobile_i, reference_i]
        if mobile_i and reference_i and direction == 0:
            mobile_i -= 1
            reference_i -= 1
            pairs.append((mobile_residues[mobile_i], reference_residues[reference_i]))
            matches += mobile_sequence[mobile_i] == reference_sequence[reference_i]
        elif mobile_i and (not reference_i or direction == 1):
            mobile_i -= 1
        else:
            reference_i -= 1
    pairs.reverse()

    coverage = len(pairs) / min(mobile_n, reference_n)
    identity = matches / len(pairs) if pairs else 0.0
    if len(pairs) < 3 or coverage < 0.8 or identity < 0.9:
        raise ContractError(
            "target_sequence_alignment_mismatch",
            "target chains are not sufficiently similar for pose alignment: "
            f"mobile={mobile_n}, reference={reference_n}, "
            f"coverage={coverage:.3f}, identity={identity:.3f}",
        )
    return pairs


def target_aligned_binder_rmsd(
    mobile: Structure,
    reference: Structure,
    target_chain: str,
    mobile_binder_chain: str,
    reference_binder_chain: str,
) -> float:
    mobile_residues = mobile.chains.get(target_chain, [])
    reference_residues = reference.chains.get(target_chain, [])
    same_sequence_order = (
        bool(mobile_residues)
        and len(mobile_residues) == len(reference_residues)
        and "".join(item.one_letter for item in mobile_residues)
        == "".join(item.one_letter for item in reference_residues)
    )
    if same_sequence_order:
        residue_pairs = zip(mobile_residues, reference_residues)
    else:
        residue_pairs = _sequence_aligned_residue_pairs(
            mobile_residues, reference_residues
        )
    target_mobile, target_reference = [], []
    for mobile_residue, reference_residue in residue_pairs:
        left = mobile_residue.atoms.get("CA")
        right = reference_residue.atoms.get("CA")
        if left is not None and right is not None:
            target_mobile.append(left.coord)
            target_reference.append(right.coord)
    if len(target_mobile) < 3:
        raise ContractError(
            "target_alignment_insufficient",
            f"fewer than three common target CA atoms for chain {target_chain}",
        )
    transform = kabsch_transform(
        np.asarray(target_mobile), np.asarray(target_reference)
    )
    mobile_binder = _coords_for_residues(mobile.chains[mobile_binder_chain])
    reference_binder = _coords_for_residues(reference.chains[reference_binder_chain])
    if mobile_binder.shape != reference_binder.shape:
        raise ContractError(
            "binder_alignment_shape_mismatch", "binder backbone shapes differ"
        )
    return rmsd(apply_transform(mobile_binder, transform), reference_binder)
