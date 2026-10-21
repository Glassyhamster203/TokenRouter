from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path
from types import TracebackType

_TMP_ROOT = Path(__file__).resolve().parent.parent / ".test-tmp"


class WorkspaceTempDir:
    """Workspace-local temporary directory.

    ``tempfile.mkdtemp`` creates directories with mode 0700 on this Python
    build, which the Windows sandbox cannot subsequently write into. Creating
    the directory with the default mode keeps it usable and cleanable.
    """

    def __init__(self) -> None:
        _TMP_ROOT.mkdir(parents=True, exist_ok=True)
        for _ in range(10):
            candidate = _TMP_ROOT / f"tmp_{os.getpid()}_{secrets.token_hex(6)}"
            try:
                os.mkdir(candidate)
            except FileExistsError:
                continue
            self.name = str(candidate)
            break
        else:
            raise RuntimeError("无法创建测试临时目录")

    def cleanup(self) -> None:
        shutil.rmtree(self.name, ignore_errors=True)

    def __enter__(self) -> str:
        return self.name

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.cleanup()


def workspace_temp_dir() -> WorkspaceTempDir:
    return WorkspaceTempDir()
