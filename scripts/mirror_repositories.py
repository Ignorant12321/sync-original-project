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
    failures: tuple = ()


@dataclass(frozen=True)
class MirrorFailure:
    name: str
    upstream: str
    target: str
    reason: str


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    reason: str = ""


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
        stderr=subprocess.STDOUT,
        stdout=subprocess.PIPE,
        text=True,
    )
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    reason = command_failure_reason(result.stdout, result.returncode)
    return CommandResult(result.returncode == 0, reason)


def command_failure_reason(output, returncode):
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    if lines:
        return lines[-1]
    return f"Command exited with status {returncode}"


def normalize_command_result(result):
    if isinstance(result, CommandResult):
        return result
    return CommandResult(bool(result))


def mirror_repositories(repositories, environ=None, runner=None):
    env = environ if environ is not None else os.environ
    run = runner if runner is not None else default_runner
    total = 0
    succeeded = 0
    failed = 0
    failures = []

    with tempfile.TemporaryDirectory() as work_dir:
        for repository in repositories:
            total += 1
            repo_dir = Path(work_dir) / f"repo-{total}.git"
            token = env.get(repository.token_env, "")

            print(f"::group::Mirror {repository.name}")
            if not token:
                reason = f"Missing token environment variable: {repository.token_env}"
                print(f"::error::{reason}")
                failures.append(
                    MirrorFailure(
                        name=repository.name,
                        upstream=repository.upstream,
                        target=repository.target,
                        reason=reason,
                    )
                )
                failed += 1
                print("::endgroup::")
                continue

            print(f"::add-mask::{token}")
            target_url = build_target_url(repository.target, token)
            clone_result = normalize_command_result(
                run(["git", "clone", "--mirror", repository.upstream, str(repo_dir)])
            )
            push_result = CommandResult(False)
            if clone_result.ok:
                push_result = normalize_command_result(
                    run(["git", "-C", str(repo_dir), "push", "--mirror", target_url])
                )

            if clone_result.ok and push_result.ok:
                print(f"Mirror succeeded: {repository.upstream} -> {repository.target}")
                succeeded += 1
            else:
                if clone_result.ok:
                    reason = push_result.reason or "git push --mirror failed"
                else:
                    reason = clone_result.reason or "git clone --mirror failed"
                print(
                    f"::error::Mirror failed: {repository.name}: "
                    f"{repository.upstream} -> {repository.target} ({reason})"
                )
                failures.append(
                    MirrorFailure(
                        name=repository.name,
                        upstream=repository.upstream,
                        target=repository.target,
                        reason=reason,
                    )
                )
                failed += 1

            shutil.rmtree(repo_dir, ignore_errors=True)
            print("::endgroup::")

    return MirrorResult(total=total, succeeded=succeeded, failed=failed, failures=tuple(failures))


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
        print("Failed repositories:")
        for failure in result.failures:
            print(f"- {failure.name}: {failure.upstream} -> {failure.target} ({failure.reason})")
        print("::error::Some repositories failed.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
