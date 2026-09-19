"""Git helpers: commit timestamps (for the lookahead guard) and commit-and-push with retry."""

from __future__ import annotations

import logging
import subprocess
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


def _run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


def is_repo(cwd: Path) -> bool:
    try:
        return _run(["rev-parse", "--is-inside-work-tree"], cwd).stdout.strip() == "true"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def committed_ts(path: Path, cwd: Path) -> datetime | None:
    """Committer timestamp of the last commit touching `path`, or None if never committed."""
    if not is_repo(cwd):
        return None
    try:
        out = _run(["log", "-1", "--format=%cI", "--", str(path)], cwd).stdout.strip()
    except subprocess.CalledProcessError:
        return None
    if not out:
        return None
    return datetime.fromisoformat(out)


def commit_and_push(
    cwd: Path, message: str, paths: list[str], push: bool = True, attempts: int = 4
) -> bool:
    """Stage `paths`, commit if anything changed, then push with pull --rebase retries."""
    _run(["add", "-A", "--", *paths], cwd)
    if _run(["diff", "--cached", "--quiet"], cwd, check=False).returncode == 0:
        log.info("nothing to commit")
        return False
    _run(["commit", "-q", "-m", message], cwd)
    if not push:
        return True
    for i in range(attempts):
        res = _run(["push"], cwd, check=False)
        if res.returncode == 0:
            return True
        log.warning("push failed (%s); rebasing and retrying", res.stderr.strip()[:200])
        _run(["pull", "--rebase", "--autostash"], cwd, check=False)
        time.sleep(2 * (i + 1))
    raise RuntimeError("git push failed after retries")
