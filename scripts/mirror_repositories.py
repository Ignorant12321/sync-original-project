import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


TOKEN_ENV_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
TARGET_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class MirrorRepository:
    name: str
    upstream: str
    target: str
    token_env: str


@dataclass(frozen=True)
class MirrorResult:
    total: int
    succeeded: int
    failed: int


def load_repositories(config_path):
    path = Path(config_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(payload, list):
        raise ConfigError("Config must be a JSON array.")

    repositories = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"Entry #{index} must be an object.")
        if item.get("enabled", True) is False:
            continue

        name = require_text(item, "name", index)
        upstream = require_text(item, "upstream", index)
        target = require_text(item, "target", index)
        token_env = require_text(item, "token_env", index)

        if not upstream.startswith(("https://", "ssh://", "git@")):
            raise ConfigError(f"Entry #{index} upstream must be a git URL.")
        if not TARGET_PATTERN.fullmatch(target):
            raise ConfigError(f"Entry #{index} target must use owner/repo format.")
        if not TOKEN_ENV_PATTERN.fullmatch(token_env):
            raise ConfigError(
                f"Entry #{index} token_env must be an environment variable name, not a token value."
            )

        repositories.append(
            MirrorRepository(
                name=name,
                upstream=upstream,
                target=target,
                token_env=token_env,
            )
        )

    if not repositories:
        raise ConfigError("Config does not contain any enabled repositories.")

    return repositories


def require_text(item, field, index):
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Entry #{index} missing required field: {field}.")
    return value.strip()


def build_target_url(target, token):
    return f"https://x-access-token:{token}@github.com/{target}.git"


def default_runner(command):
    result = subprocess.run(
        command,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return result.returncode == 0


def mirror_repositories(repositories, environ=None, runner=None):
    env = environ if environ is not None else os.environ
    run = runner if runner is not None else default_runner
    total = 0
    succeeded = 0
    failed = 0

    with tempfile.TemporaryDirectory() as work_dir:
        for repository in repositories:
            total += 1
            repo_dir = Path(work_dir) / f"repo-{total}.git"
            token = env.get(repository.token_env, "")

            print(f"::group::Mirror {repository.name}")
            if not token:
                print(f"::error::Missing token environment variable: {repository.token_env}")
                failed += 1
                print("::endgroup::")
                continue

            print(f"::add-mask::{token}")
            target_url = build_target_url(repository.target, token)
            clone_ok = run(["git", "clone", "--mirror", repository.upstream, str(repo_dir)])
            push_ok = False
            if clone_ok:
                push_ok = run(["git", "-C", str(repo_dir), "push", "--mirror", target_url])

            if clone_ok and push_ok:
                print(f"Mirror succeeded: {repository.upstream} -> {repository.target}")
                succeeded += 1
            else:
                print(f"::error::Mirror failed: {repository.upstream} -> {repository.target}")
                failed += 1

            shutil.rmtree(repo_dir, ignore_errors=True)
            print("::endgroup::")

    return MirrorResult(total=total, succeeded=succeeded, failed=failed)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Mirror configured upstream repositories.")
    parser.add_argument(
        "--config",
        default=".github/mirror-repositories.json",
        help="Path to the JSON mirror configuration file.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        repositories = load_repositories(args.config)
    except ConfigError as exc:
        print(f"::error::{exc}")
        return 1

    result = mirror_repositories(repositories)
    print(f"Mirror summary: {result.succeeded}/{result.total} succeeded, {result.failed} failed.")
    if result.failed:
        print("::warning::Some repositories failed, but the job is configured to continue.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
