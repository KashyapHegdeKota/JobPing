"""Export the minimal renderer's required dependency closure from the shared lock."""

import sys
import tomllib
from pathlib import Path


def requirements(lock: Path) -> list[str]:
    packages = {item["name"]: item for item in tomllib.loads(lock.read_text())["package"]}
    pending = ["fastapi", "httpx", "playwright", "uvicorn"]
    selected: set[str] = set()
    while pending:
        name = pending.pop().lower().replace("_", "-")
        if name in selected:
            continue
        package = packages[name]
        selected.add(name)
        for dependency, value in package.get("dependencies", {}).items():
            if isinstance(value, dict) and value.get("optional"):
                continue
            if dependency != "python":
                pending.append(dependency)
    return [f"{name}=={packages[name]['version']}" for name in sorted(selected)]


if __name__ == "__main__":
    print("\n".join(requirements(Path(sys.argv[1]))))
