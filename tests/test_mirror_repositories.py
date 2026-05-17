import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.mirror_repositories import (
    ConfigError,
    MirrorResult,
    build_target_url,
    load_repositories,
    main,
    mirror_repositories,
)


class MirrorRepositoriesTest(unittest.TestCase):
    def write_config(self, payload):
        path = Path(tempfile.mkdtemp()) / "mirror-repositories.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_load_repositories_validates_required_fields(self):
        path = self.write_config([{"name": "missing fields"}])

        with self.assertRaisesRegex(ConfigError, "upstream"):
            load_repositories(path)

    def test_load_repositories_rejects_token_values_in_config(self):
        path = self.write_config(
            [
                {
                    "name": "unsafe",
                    "upstream": "https://github.com/example/source.git",
                    "target": "example/target",
                    "token_env": "secret-token-value",
                }
            ]
        )

        with self.assertRaisesRegex(ConfigError, "token_env"):
            load_repositories(path)

    def test_build_target_url_uses_token_without_logging_config_secrets(self):
        target_url = build_target_url("owner/repo", "secret-token")

        self.assertEqual(
            target_url,
            "https://x-access-token:secret-token@github.com/owner/repo.git",
        )

    def test_mirror_repositories_continues_after_failed_repository(self):
        repositories = load_repositories(
            self.write_config(
                [
                    {
                        "name": "first",
                        "upstream": "https://github.com/example/first.git",
                        "target": "example/first",
                        "token_env": "GH_PAT_FIRST",
                    },
                    {
                        "name": "second",
                        "upstream": "https://github.com/example/second.git",
                        "target": "example/second",
                        "token_env": "GH_PAT_SECOND",
                    },
                ]
            )
        )
        calls = []

        def runner(command):
            calls.append(command)
            return command[3] != "https://github.com/example/first.git"

        result = mirror_repositories(
            repositories,
            {"GH_PAT_FIRST": "first-token", "GH_PAT_SECOND": "second-token"},
            runner=runner,
        )

        self.assertEqual(result.total, 2)
        self.assertEqual(result.succeeded, 1)
        self.assertEqual(result.failed, 1)
        self.assertEqual(len([call for call in calls if call[:3] == ["git", "clone", "--mirror"]]), 2)

    def test_mirror_repositories_records_failed_repository_reasons(self):
        repositories = load_repositories(
            self.write_config(
                [
                    {
                        "name": "missing-token",
                        "upstream": "https://github.com/example/missing.git",
                        "target": "example/missing",
                        "token_env": "GH_PAT_MISSING",
                    },
                    {
                        "name": "clone-fails",
                        "upstream": "https://github.com/example/source.git",
                        "target": "example/target",
                        "token_env": "GH_PAT",
                    },
                ]
            )
        )

        result = mirror_repositories(
            repositories,
            {"GH_PAT": "secret-token"},
            runner=lambda command: False,
        )

        self.assertEqual(result.failed, 2)
        self.assertTrue(hasattr(result, "failures"))
        self.assertEqual(result.failures[0].name, "missing-token")
        self.assertEqual(result.failures[0].reason, "Missing token environment variable: GH_PAT_MISSING")
        self.assertEqual(result.failures[1].name, "clone-fails")
        self.assertEqual(result.failures[1].reason, "git clone --mirror failed")

    def test_main_returns_failure_when_any_repository_fails(self):
        path = self.write_config(
            [
                {
                    "name": "failing",
                    "upstream": "https://github.com/example/source.git",
                    "target": "example/target",
                    "token_env": "GH_PAT",
                }
            ]
        )

        with patch(
            "scripts.mirror_repositories.mirror_repositories",
            return_value=MirrorResult(total=1, succeeded=0, failed=1),
        ):
            exit_code = main(["--config", str(path)])

        self.assertEqual(exit_code, 1)

    def test_main_prints_summary_and_failed_repository_details(self):
        path = self.write_config(
            [
                {
                    "name": "failing",
                    "upstream": "https://github.com/example/source.git",
                    "target": "example/target",
                    "token_env": "GH_PAT",
                }
            ]
        )
        result = SimpleNamespace(
            total=2,
            succeeded=1,
            failed=1,
            failures=[
                SimpleNamespace(
                    name="failing",
                    upstream="https://github.com/example/source.git",
                    target="example/target",
                    reason="git push --mirror failed",
                )
            ],
        )

        output = StringIO()
        with patch("scripts.mirror_repositories.mirror_repositories", return_value=result):
            with redirect_stdout(output):
                exit_code = main(["--config", str(path)])

        self.assertEqual(exit_code, 1)
        self.assertIn("Mirror summary: 1/2 succeeded, 1 failed.", output.getvalue())
        self.assertIn(
            "- failing: https://github.com/example/source.git -> example/target (git push --mirror failed)",
            output.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
