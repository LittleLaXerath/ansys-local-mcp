from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .config import ConfigurationError, require_allowed_path


STEP_REF_PATTERN = re.compile(
    r"\$\{steps\.([A-Za-z][A-Za-z0-9_-]{0,63})\.outputs(?:\.([A-Za-z0-9_.-]+))?\}"
)


class PathDeniedError(ValueError):
    pass


@dataclass
class PathChecks:
    workdir: Path
    mode: Literal["runtime", "schema"] = "runtime"
    allow_references: bool = False
    produced: set[Path] = field(default_factory=set)

    def resolve(
        self, value: str | Path, *, input_file: bool = False, expect_file: bool | None = None
    ) -> Path | None:
        text = str(value)
        if STEP_REF_PATTERN.search(text):
            if self.allow_references:
                return None
            raise ValueError(f"Unresolved step output in path: {text}")
        try:
            path = require_allowed_path(text, base=self.workdir, expect_file=expect_file)
        except ConfigurationError:
            raise
        except ValueError as exc:
            raise PathDeniedError(str(exc)) from exc
        if input_file and self.mode == "runtime" and path not in self.produced:
            return require_allowed_path(path, must_exist=True, expect_file=expect_file)
        return path

    def declare(self, value: str | Path) -> None:
        path = self.resolve(value)
        if path is not None:
            self.produced.add(path)
