"""Offline image handoff validation and SSH trust-boundary tests."""

import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import release_artifact as release
import deploy_hostgator as deploy

COMMIT = "a" * 40
IDENTITY = "sha256:" + "1" * 64


def manifest() -> release.Manifest:
    return {
        "commit": COMMIT,
        "platform": "linux/amd64",
        "images": {
            role: {"tag": repository + ":" + COMMIT, "identity": IDENTITY}
            for role, repository in release.ROLES.items()
        },
    }


def archive(
    path: Path, value: release.Manifest | None = None, extra: str | None = None
) -> str:
    with tarfile.open(path, "w:gz") as bundle:
        for name, data in [
            ("manifest.json", json.dumps(value or manifest()).encode()),
            ("images.tar", b"synthetic image fixture"),
        ] + ([] if extra is None else [(extra, b"x")]):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            bundle.addfile(member, io.BytesIO(data))
    return release.file_digest(path)


class ImageHandoffTests(unittest.TestCase):
    def test_pack_binds_both_prebuilt_images_to_the_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "handoff.tgz"

            def command(args, **options):
                self.assertEqual(args[:2], ["docker", "save"])
                options["stdout"].write(b"synthetic image fixture")
                return SimpleNamespace(returncode=0)

            with (
                patch.object(
                    release, "image_identity", return_value=(IDENTITY, "linux/amd64")
                ),
                patch.object(release.subprocess, "run", side_effect=command),
            ):
                release.pack(path, COMMIT)
            self.assertEqual(
                release.verify(path, release.file_digest(path), COMMIT), manifest()
            )

    def test_digest_commit_and_payload_members_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "handoff.tgz"
            expected = archive(path)
            self.assertEqual(release.verify(path, expected, COMMIT), manifest())
            for digest, commit in [("0" * 64, COMMIT), (expected, "b" * 40)]:
                with self.assertRaises(ValueError):
                    release.verify(path, digest, commit)
            expected = archive(path, extra="../escape")
            with self.assertRaises(ValueError):
                release.verify(path, expected, COMMIT)

    def test_manifest_cannot_select_an_unreviewed_repository_or_image(self) -> None:
        value = manifest()
        value["images"]["api"]["tag"] = "attacker/image:latest"
        with self.assertRaises(ValueError):
            release.checked_manifest(value, COMMIT)
        with self.assertRaises(ValueError):
            release.checked_manifest([], COMMIT)

    def test_wrong_platform_never_loads_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "handoff.tgz"
            expected = archive(path)
            with patch.object(
                release.subprocess,
                "run",
                return_value=SimpleNamespace(stdout="linux/arm64\n"),
            ) as run:
                with self.assertRaises(ValueError):
                    release.activate(path, expected, COMMIT)
                self.assertEqual(run.call_count, 1)

    def test_load_must_match_expected_identity_before_active_state_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "handoff.tgz"
            expected = archive(path)

            def command(args, **options):
                return SimpleNamespace(stdout="linux/x86_64\n", returncode=0)

            with (
                patch.object(release, "ROOT", root),
                patch.object(release.subprocess, "run", side_effect=command),
                patch.object(
                    release,
                    "image_identity",
                    return_value=("sha256:" + "2" * 64, "linux/amd64"),
                ),
            ):
                with self.assertRaises(ValueError):
                    release.activate(path, expected, COMMIT)
                self.assertFalse((root / ".release/active.json").exists())

    def test_success_uses_immutable_image_ids_and_retains_prior_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "handoff.tgz"
            expected = archive(path)
            state = root / ".release"
            state.mkdir()
            previous = {**manifest(), "commit": "b" * 40}
            (state / "active.json").write_text(json.dumps(previous))
            with (
                patch.object(release, "ROOT", root),
                patch.object(
                    release.subprocess,
                    "run",
                    return_value=SimpleNamespace(stdout="linux/x86_64\n", returncode=0),
                ),
                patch.object(
                    release, "image_identity", return_value=(IDENTITY, "linux/amd64")
                ),
            ):
                release.activate(path, expected, COMMIT)
                self.assertEqual(release.active_image("api"), IDENTITY)
                self.assertEqual(
                    json.loads((state / "previous.json").read_text()), previous
                )
                release.activate(path, expected, COMMIT)
                self.assertEqual(
                    json.loads((state / "previous.json").read_text()), previous
                )

    def test_unverified_handoff_never_uses_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "handoff.tgz"
            archive(path)
            credentials = root / "credentials.json"
            credentials.write_text("{}")
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "deploy_hostgator.py",
                        str(credentials),
                        str(path),
                        "0" * 64,
                        COMMIT,
                    ],
                ),
                patch.object(deploy.subprocess, "run") as run,
            ):
                with self.assertRaises(SystemExit):
                    deploy.main()
                run.assert_not_called()
            self.assertFalse(credentials.exists())

    def test_host_deploy_never_inherits_oidc_or_builds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "handoff.tgz"
            expected = archive(path)
            credentials = root / "credentials.json"
            credentials.write_text(
                json.dumps(
                    {
                        "HOSTGATOR_HOST": "deploy.fixture.test",
                        "HOSTGATOR_USER": "fixture",
                        "HOSTGATOR_DEPLOY_PATH": "/srv/fixture",
                        "HOSTGATOR_SSH_PRIVATE_KEY": "synthetic-key",
                        "HOSTGATOR_SSH_KNOWN_HOSTS": "synthetic-host-entry",
                    }
                )
            )
            calls = []

            def command(args, **options):
                calls.append(args)
                self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", options["env"])
                self.assertNotIn("SSH_AUTH_SOCK", options["env"])
                return SimpleNamespace(returncode=0)

            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "deploy_hostgator.py",
                        str(credentials),
                        str(path),
                        expected,
                        COMMIT,
                    ],
                ),
                patch.dict(
                    os.environ,
                    {
                        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic-oidc",
                        "SSH_AUTH_SOCK": "/fixture/agent",
                    },
                ),
                patch.object(deploy.subprocess, "run", side_effect=command),
            ):
                deploy.main()
            joined = " ".join(" ".join(call) for call in calls)
            self.assertIn("IdentitiesOnly=yes", joined)
            self.assertIn("ForwardAgent=no", joined)
            self.assertIn("host_release.py", joined)
            self.assertIn("--root /srv/fixture deploy", joined)
            self.assertIn("--source", joined)
            self.assertNotIn("compose build", joined)
            self.assertFalse(credentials.exists())


if __name__ == "__main__":
    unittest.main()
