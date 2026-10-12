"""Explicit one-time integration with an existing JobPing Certbot virtual host."""

import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from proxy_candidate import main as stage_config


def main() -> None:
    site = Path(sys.argv[1]).resolve(strict=True)
    original = site.read_bytes()
    value = original.decode("utf-8")
    include = "include /etc/nginx/jobping/upstream.conf;"
    if include in value and "proxy_pass http://jobping_backend;" in value:
        print("JobPing Nginx already adopted")
        return
    if "server_name api.jobping.website;" not in value:
        raise SystemExit("Unexpected JobPing site; review manually")
    if "proxy_pass http://127.0.0.1:8020;" not in value:
        raise SystemExit("Unexpected old upstream; review manually")
    updated = (
        include
        + "\n"
        + value.replace(
            "proxy_pass http://127.0.0.1:8020;", "proxy_pass http://jobping_backend;"
        ).replace("$proxy_add_x_forwarded_for", "$remote_addr")
    )
    backup = Path("/var/backups/jobping") / (
        "nginx-pre-bluegreen-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".conf"
    )
    shutil.copy2(site, backup)
    with tempfile.TemporaryDirectory(prefix="jobping-nginx-", dir="/srv/jobping/state") as temp:
        destination = Path(temp) / "config"
        sys.argv = [
            "proxy_candidate.py",
            "/etc/nginx/nginx.conf",
            "/etc/nginx/jobping/upstream.conf",
            "/etc/nginx/jobping/upstream.conf",
            str(destination),
        ]
        stage_config()
        (destination / site.relative_to("/etc/nginx")).write_text(updated, encoding="utf-8")
        subprocess.run(["nginx", "-t", "-c", str(destination / "nginx.conf")], check=True)
    temporary = site.with_suffix(".next")
    temporary.write_text(updated, encoding="utf-8")
    temporary.chmod(site.stat().st_mode)
    temporary.replace(site)
    try:
        subprocess.run(["nginx", "-t"], check=True)
        subprocess.run(["systemctl", "reload", "nginx"], check=True)
    except BaseException:
        temporary.write_bytes(original)
        temporary.replace(site)
        subprocess.run(["nginx", "-t"], check=True)
        subprocess.run(["systemctl", "reload", "nginx"], check=True)
        raise
    print("JobPing upstream adopted; existing 8020 traffic/TLS preserved")


if __name__ == "__main__":
    main()
