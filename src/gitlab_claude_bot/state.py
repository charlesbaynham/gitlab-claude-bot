import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class State:
    mr_cursors: dict[str, int] = field(default_factory=dict)
    last_mr_poll: str | None = None

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        return cls(mr_cursors=dict(data.get("mr_cursors", {})), last_mr_poll=data.get("last_mr_poll"))

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2) + "\n")
        os.replace(tmp, path)
