"""Atomically move a staged artifact directory with bounded Windows retries."""
import os
from pathlib import Path
import time

_WINDOWS = os.name == "nt"


def move_artifact_directory(source: Path, target: Path) -> None:
    source, target = Path(source), Path(target)
    if source.parent != target.parent:
        raise ValueError("artifact staging and destination must share a directory")
    for attempt in range(4):
        if target.exists():
            raise FileExistsError("artifact destination already exists")
        try:
            os.rename(source, target)
            return
        except OSError as exc:
            if (not _WINDOWS or getattr(exc, "winerror", None) not in (5, 32, 33)
                    or attempt == 3 or target.exists()):
                raise
            time.sleep(0.05 * (attempt + 1))
