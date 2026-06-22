# Backwards-compatibility shim.
# The canonical implementation lives in flowprobe.storage.filesystem.
from flowprobe.storage.filesystem import EvidenceCollector

__all__ = ["EvidenceCollector"]
