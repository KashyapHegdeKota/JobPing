"""Network-free execution of the real controller with isolated command fixtures."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux deployment controller")
SHA1, SHA2, SHA3 = "1" * 40, "2" * 40, "3" * 40
DIGEST = "sha256:" + "a" * 64

FAKE = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ['JOBPING_ROOT'])
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
with (root/'calls').open('a') as stream: stream.write(name+' '+' '.join(args)+'\n')
fail = os.environ.get('FAIL', '')
if name == 'docker':
    if args[:2] == ['image', 'inspect']:
        print(os.environ.get('FAKE_SHA', '1'*40)); sys.exit(0)
    if args[0] == 'inspect': print('healthy' if fail != 'worker' else 'unhealthy'); sys.exit(0)
    if args[0] == 'compose':
        project = args[args.index('-p')+1]
        if 'run' in args and fail == 'migration': sys.exit(1)
        if 'up' in args and project != 'jobping-workers' and fail == 'startup': sys.exit(1)
        if 'up' in args and project in ('jobping-blue','jobping-green'):
            (root/project).write_text(os.environ['FAKE_SHA'])
        if 'ps' in args and '--status' not in args and project == 'jobping-workers': print('worker')
elif name == 'nginx':
    # First validation can fail; recovery validation may succeed.
    marker = root/'validation-failed'
    if fail == 'validation' and not marker.exists(): marker.touch(); sys.exit(1)
elif name == 'systemctl':
    marker = root/'reload-failed'
    if fail == 'reload' and not marker.exists(): marker.touch(); sys.exit(1)
    value = pathlib.Path(os.environ['UPSTREAM_FILE']).read_text()
    (root/'proxy').write_text('blue' if ':8001' in value else 'green')
elif name == 'python3':
    if args[0].endswith('proxy_candidate.py'):
        target=pathlib.Path(args[-1]); target.mkdir(parents=True); (target/'nginx.conf').touch()
    elif args[0].endswith('smoke.py'):
        sha, color = args[2:4]
        if fail == 'readiness' and sha == os.environ.get('FAKE_SHA'): sys.exit(1)
        if '--proxy' in args:
            if (root/'proxy').read_text() != color: sys.exit(1)
            if fail == 'post-smoke' and sha == os.environ.get('FAKE_SHA'): sys.exit(1)
        elif not (root/('jobping-'+color)).exists(): sys.exit(1)
elif name == 'sleep': pass
"""


@pytest.fixture
def environment(tmp_path: Path) -> dict[str, str]:
    state = tmp_path / "state"
    state.mkdir()
    (state / "active-color").write_text("none\n")
    (state / "workers.env").write_text("COLOR=none\n")
    (tmp_path / "upstream.conf").write_text("upstream jobping_backend { server 127.0.0.1:8001; }\n")
    (tmp_path / "nginx.conf").touch()
    for sha in (SHA1, SHA2, SHA3):
        shutil.copytree(ROOT / "deploy", tmp_path / "releases" / sha / "deploy")
    binary = tmp_path / "bin"
    binary.mkdir()
    # A fixture interpreter using an absolute path avoids recursively invoking itself.
    fake = FAKE.replace("#!/usr/bin/env python3", f"#!{sys.executable}")
    for name in ("docker", "nginx", "systemctl", "python3", "sleep"):
        file = binary / name
        file.write_text(fake)
        file.chmod(0o755)
    return {
        **os.environ,
        "JOBPING_ROOT": str(tmp_path),
        "JOBPING_DEPLOY_CONFIG": str(tmp_path / "absent.conf"),
        "UPSTREAM_FILE": str(tmp_path / "upstream.conf"),
        "NGINX_CONFIG": str(tmp_path / "nginx.conf"),
        "PATH": str(binary) + ":" + os.environ["PATH"],
        "READINESS_ATTEMPTS": "2",
        "READINESS_INTERVAL": "0",
        "NOTIFICATION_WORKER_ENABLED": "false",
    }


def command(env: dict[str, str], *args: str, fail: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(ROOT / "deploy/scripts/deploy.sh"), *args],
        env={**env, "FAKE_SHA": args[1] if len(args) > 1 else SHA1, "FAIL": fail},
        text=True,
        capture_output=True,
        timeout=15,
    )


def current(env: dict[str, str]) -> str:
    return (Path(env["JOBPING_ROOT"]) / "state/active-color").read_text().strip()


def seed(env: dict[str, str]) -> None:
    result = command(env, "deploy", SHA1, DIGEST)
    assert result.returncode == 0, result.stdout + result.stderr


def test_full_blue_green_failed_release_and_rollback(environment: dict[str, str]) -> None:
    seed(environment)
    assert current(environment) == "blue"
    result = command(environment, "deploy", SHA2, DIGEST)
    assert result.returncode == 0, result.stdout + result.stderr
    assert current(environment) == "green"
    root = Path(environment["JOBPING_ROOT"])
    assert (root / "jobping-blue").read_text() == SHA1
    assert command(environment, "deploy", SHA3, DIGEST, fail="readiness").returncode != 0
    assert current(environment) == "green"
    result = command(environment, "rollback")
    assert result.returncode == 0, result.stdout + result.stderr
    assert current(environment) == "blue"
    assert f"COMMIT_SHA={SHA1}" in (root / "state/blue.env").read_text()


@pytest.mark.parametrize(
    "fail", ["migration", "startup", "readiness", "validation", "reload", "post-smoke", "worker"]
)
def test_failure_preserves_active(environment: dict[str, str], fail: str) -> None:
    seed(environment)
    result = command(environment, "deploy", SHA2, DIGEST, fail=fail)
    assert result.returncode != 0
    assert current(environment) == "blue", result.stdout + result.stderr
    calls = (Path(environment["JOBPING_ROOT"]) / "calls").read_text()
    assert "down" not in calls
    assert "postgres" not in calls and "redis" not in calls


@pytest.mark.parametrize("value", ["", "purple", "blue\ngreen"])
def test_invalid_state(environment: dict[str, str], value: str) -> None:
    (Path(environment["JOBPING_ROOT"]) / "state/active-color").write_text(value)
    assert command(environment, "deploy", SHA1, DIGEST).returncode != 0


def test_missing_state_requires_explicit_bootstrap(environment: dict[str, str]) -> None:
    (Path(environment["JOBPING_ROOT"]) / "state/active-color").unlink()
    assert command(environment, "deploy", SHA1, DIGEST).returncode != 0


@pytest.mark.parametrize("sha,digest", [("main", DIGEST), (SHA1, "latest"), ("", DIGEST)])
def test_invalid_release(environment: dict[str, str], sha: str, digest: str) -> None:
    assert command(environment, "deploy", sha, digest).returncode != 0


def test_same_sha_noop_and_exact_artifact(environment: dict[str, str]) -> None:
    seed(environment)
    result = command(environment, "deploy", SHA1, DIGEST)
    assert result.returncode == 0 and "Already active" in result.stdout
    calls = (Path(environment["JOBPING_ROOT"]) / "calls").read_text()
    assert calls.count("alembic upgrade head") == 1
    assert f"docker pull ghcr.io/kashyaphegdekota/jobping@{DIGEST}" in calls


def test_rollback_refuses_unhealthy_standby(environment: dict[str, str]) -> None:
    seed(environment)
    assert command(environment, "deploy", SHA2, DIGEST).returncode == 0
    assert command(environment, "rollback", fail="readiness").returncode != 0
    assert current(environment) == "green"


def test_concurrent_lock_collision(environment: dict[str, str]) -> None:
    import fcntl

    with (Path(environment["JOBPING_ROOT"]) / "state/deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = command(environment, "deploy", SHA1, DIGEST)
    assert result.returncode != 0 and "owns the lock" in result.stderr


def test_worker_generation_is_stopped_before_replacement(environment: dict[str, str]) -> None:
    seed(environment)
    assert command(environment, "deploy", SHA2, DIGEST).returncode == 0
    calls = (Path(environment["JOBPING_ROOT"]) / "calls").read_text().splitlines()
    stop = next(
        i
        for i, line in enumerate(calls)
        if "jobping-workers" in line and " stop --timeout " in line
    )
    start = next(
        i
        for i, line in enumerate(calls[stop:], stop)
        if "jobping-workers" in line and "up -d" in line
    )
    assert stop < start


def test_pending_cutover_blocks_new_deployment(environment: dict[str, str]) -> None:
    seed(environment)
    root = Path(environment["JOBPING_ROOT"])
    (root / "state/pending").write_text("PREVIOUS=blue\nTARGET=green\n")
    result = command(environment, "deploy", SHA2, DIGEST)
    assert result.returncode != 0 and "Unfinished cutover" in result.stderr
    assert current(environment) == "blue"


def test_digest_change_for_active_sha_is_rejected(environment: dict[str, str]) -> None:
    seed(environment)
    assert command(environment, "deploy", SHA1, "sha256:" + "b" * 64).returncode != 0


def test_image_label_must_match_requested_commit(environment: dict[str, str]) -> None:
    result = subprocess.run(
        ["bash", str(ROOT / "deploy/scripts/deploy.sh"), "deploy", SHA2, DIGEST],
        env={**environment, "FAKE_SHA": SHA1},
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode != 0 and "label mismatch" in result.stderr
    assert current(environment) == "none"
