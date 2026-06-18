# Backwards-compatibility shim.
# The canonical implementation lives in auditor.storage.filesystem.
from auditor.storage.filesystem import EvidenceCollector

__all__ = ["EvidenceCollector"]
