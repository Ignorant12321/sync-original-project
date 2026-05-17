import json
import tempfile
import unittest
from pathlib import Path

from scripts.mirror_repositories import (
    ConfigError,
    build_target_url,
    load_repositories,
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


if __name__ == "__main__":
    unittest.main()
