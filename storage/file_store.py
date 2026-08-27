"""Legacy file backend marker used during the migration period."""

from pathlib import Path


class FileStore:
    """Describes the existing JSON/CSV/JSONL backend without deleting it."""

    def __init__(self, data_dir: str | Path, evidence_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.evidence_dir = Path(evidence_dir)
        self.state_path = self.data_dir / "state.json"
        self.candidate_path = self.data_dir / "candidate_index.csv"
        self.evidence_path = self.evidence_dir / "evidence_log.jsonl"
