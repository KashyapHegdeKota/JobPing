"""Stage a complete Nginx configuration tree without editing live configuration."""

import shutil
import sys
from pathlib import Path


def main() -> None:
    config, upstream, candidate, destination = map(Path, sys.argv[1:])
    root = config.parent
    if not upstream.is_relative_to(root):
        raise SystemExit("Upstream must be inside the Nginx configuration directory")
    shutil.copytree(root, destination, symlinks=False, dirs_exist_ok=True)
    for path in destination.rglob("*"):
        if path.is_file():
            value = path.read_text(encoding="utf-8")
            path.write_text(
                value.replace(str(root) + "/", str(destination) + "/"), encoding="utf-8"
            )
    (destination / upstream.relative_to(root)).write_bytes(candidate.read_bytes())


if __name__ == "__main__":
    main()
