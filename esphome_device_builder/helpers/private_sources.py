from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re

_LOGGER = logging.getLogger(__name__)

SETTINGS_ENV = "ESPHOME_PRIVATE_SOURCES"
_SYNC_MARK = "ESPHOME_PRIVATE_SOURCES_SYNC"
DEFAULT_SETTINGS = Path.home() / ".config" / "esphome-device-builder" / "private_sources.json"
_SOURCE_RE = re.compile(r"github://([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?@([A-Za-z0-9._/-]+)")


def _settings_path() -> Path:
    return Path(os.environ.get(SETTINGS_ENV, str(DEFAULT_SETTINGS)))


def _load() -> tuple[Path, dict[str, str]]:
    path = _settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return path.parent / "private-sources", {}
    except (OSError, ValueError) as err:
        _LOGGER.error("Private sources: %s unreadable: %s", path, err)
        return path.parent / "private-sources", {}
    mirror_dir = Path(data.get("mirror_dir") or path.parent / "private-sources")
    repos = {}
    for entry in data.get("repos", []):
        name = entry.get("repo", "").strip("/")
        if name.count("/") == 1:
            repos[name] = entry.get("fetch_url") or f"git@github.com:{name}.git"
    return mirror_dir, repos


def _mirror(mirror_dir: Path, repo: str) -> Path:
    return mirror_dir / f"{repo}.git"


def _insteadof_pairs() -> list[tuple[str, str]]:
    mirror_dir, repos = _load()
    pairs = []
    for repo in repos:
        mirror = _mirror(mirror_dir, repo)
        if (mirror / "HEAD").is_file():
            pairs.append((f"url.{mirror.as_uri()}.insteadOf", f"https://github.com/{repo}.git"))
    return pairs


def apply_to(env: dict[str, str] | None) -> dict[str, str] | None:
    if env is not None and env.get(_SYNC_MARK):
        return env
    pairs = _insteadof_pairs()
    if not pairs:
        return env
    base = dict(os.environ if env is None else env)
    start = int(base.get("GIT_CONFIG_COUNT", "0") or 0)
    for i, (key, value) in enumerate(pairs, start):
        base[f"GIT_CONFIG_KEY_{i}"] = key
        base[f"GIT_CONFIG_VALUE_{i}"] = value
    base["GIT_CONFIG_COUNT"] = str(start + len(pairs))
    return base


def _referenced(config_path: Path, repos: dict[str, str]) -> dict[str, set[str]]:
    files = [config_path]
    folder = config_path.parent / config_path.stem
    if folder.is_dir():
        files += sorted(folder.rglob("*.yaml"))
    found: dict[str, set[str]] = {}
    for file in files:
        try:
            text = file.read_text(encoding="utf-8")
        except OSError:
            continue
        for owner, repo, ref in _SOURCE_RE.findall(text):
            name = f"{owner}/{repo}"
            if name in repos:
                found.setdefault(name, set()).add(ref)
    return found


async def _git(*args: str) -> tuple[int, str]:
    from .subprocess import create_subprocess_exec

    proc = await create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", _SYNC_MARK: "1"},
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace").strip()


def _plan(config_path: Path) -> tuple[Path, dict[str, str], dict[str, set[str]]]:
    mirror_dir, repos = _load()
    return mirror_dir, repos, _referenced(config_path, repos) if repos else {}


def _base() -> tuple[Path, str] | None:
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    base = data.get("base")
    if not isinstance(base, dict) or not base.get("path"):
        return None
    return Path(os.path.expanduser(base["path"])), base.get("branch") or "dev"


async def sync_base() -> None:
    from .async_ import run_in_executor

    base = await run_in_executor(_base)
    if base is None:
        return
    path, branch = base
    code, out = await _git("-C", str(path), "fetch", "-q", "origin", branch)
    if code:
        _LOGGER.error("Build base %s: fetch failed: %s", path, out)
        return
    _, head = await _git("-C", str(path), "rev-parse", "HEAD")
    _, latest = await _git("-C", str(path), "rev-parse", f"origin/{branch}")
    if head == latest:
        return
    code, out = await _git("-C", str(path), "checkout", "-q", "-f", "-B", branch, f"origin/{branch}")
    if code:
        _LOGGER.error("Build base %s: update failed: %s", path, out)
    else:
        _LOGGER.info("Build base %s updated to %s", path, latest[:10])


async def sync_for(config_path: str | Path) -> None:
    from .async_ import run_in_executor

    await sync_base()
    mirror_dir, repos, referenced = await run_in_executor(_plan, Path(config_path))
    for repo, refs in referenced.items():
        mirror = _mirror(mirror_dir, repo)
        if not await run_in_executor((mirror / "HEAD").is_file):
            await run_in_executor(lambda: mirror.parent.mkdir(parents=True, exist_ok=True))
            code, out = await _git("init", "-q", "--bare", str(mirror))
            if code:
                _LOGGER.error("Private source %s: mirror not created: %s", repo, out)
                continue
        specs = [f"+refs/heads/{ref}:refs/heads/{ref}" for ref in sorted(refs)]
        code, out = await _git("-C", str(mirror), "fetch", "-q", "--depth=1", repos[repo], *specs)
        if code:
            _LOGGER.error("Private source %s: sync failed: %s", repo, out)
        else:
            _LOGGER.info("Private source %s synced (%s)", repo, ", ".join(sorted(refs)))
