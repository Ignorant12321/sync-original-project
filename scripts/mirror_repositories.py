import argparse
import datetime
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
    output: str = ""


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
    return CommandResult(result.returncode == 0, reason, result.stdout or "")


def command_failure_reason(output, returncode):
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    if lines:
        return lines[-1]
    return f"Command exited with status {returncode}"


def normalize_command_result(result):
    if isinstance(result, CommandResult):
        return result
    return CommandResult(bool(result))


def remove_pull_request_refs(repo_dir, runner):
    list_result = normalize_command_result(
        runner(["git", "-C", str(repo_dir), "for-each-ref", "--format=%(refname)", "refs/pull"])
    )
    if not list_result.ok:
        return list_result

    refs = [line.strip() for line in list_result.output.splitlines() if line.strip()]
    if refs:
        print(f"Removing {len(refs)} GitHub pull request refs before mirror push.")

    for ref in refs:
        delete_result = normalize_command_result(
            runner(["git", "-C", str(repo_dir), "update-ref", "-d", ref])
        )
        if not delete_result.ok:
            return CommandResult(False, delete_result.reason or f"Failed to remove {ref}")

    return CommandResult(True)


def current_utc_timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_mirror_log_commit(target_url, worktree_dir, timestamp, runner):
    clone_result = normalize_command_result(
        runner(["git", "clone", target_url, str(worktree_dir)])
    )
    if not clone_result.ok:
        return clone_result

    worktree_dir.mkdir(parents=True, exist_ok=True)
    (worktree_dir / "mirror-upstream.log").write_text(
        f"Mirror upstream action run at: {timestamp}\n",
        encoding="utf-8",
    )

    commands = [
        ["git", "-C", str(worktree_dir), "config", "user.name", "github-actions[bot]"],
        [
            "git",
            "-C",
            str(worktree_dir),
            "config",
            "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com",
        ],
        ["git", "-C", str(worktree_dir), "add", "-f", "mirror-upstream.log"],
        ["git", "-C", str(worktree_dir), "commit", "-m", "Update mirror upstream log"],
        ["git", "-C", str(worktree_dir), "push"],
    ]
    for command in commands:
        result = normalize_command_result(runner(command))
        if not result.ok:
            return result

    return CommandResult(True)


def mirror_repositories(repositories, environ=None, runner=None, timestamp_provider=None):
    env = environ if environ is not None else os.environ
    run = runner if runner is not None else default_runner
    get_timestamp = timestamp_provider if timestamp_provider is not None else current_utc_timestamp
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
            log_result = CommandResult(False)
            if clone_result.ok:
                cleanup_result = remove_pull_request_refs(repo_dir, run)
                if cleanup_result.ok:
                    push_result = normalize_command_result(
                        run(["git", "-C", str(repo_dir), "push", "--mirror", target_url])
                    )
                    if push_result.ok:
                        worktree_dir = Path(work_dir) / f"repo-{total}-worktree"
                        log_result = write_mirror_log_commit(
                            target_url,
                            worktree_dir,
                            get_timestamp(),
                            run,
                        )
                else:
                    push_result = cleanup_result

            if clone_result.ok and push_result.ok and log_result.ok:
                print(f"Mirror succeeded: {repository.upstream} -> {repository.target}")
                succeeded += 1
            else:
                if clone_result.ok and push_result.ok:
                    reason = log_result.reason or "mirror-upstream.log commit failed"
                elif clone_result.ok:
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
