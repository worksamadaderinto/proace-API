#!/usr/bin/env python3
"""Deploy API source over verified SSH using one Vault credential document."""

import json
import os
import tarfile
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from release_artifact import verify


def main() -> None:
    source = Path(sys.argv[1])
    try:
        archive = Path(sys.argv[2]).resolve()
        expected, commit = sys.argv[3:5]
        verify(archive, expected, commit)
        values = json.loads(source.read_text())
        host, user, target = (
            values[key]
            for key in ("HOSTGATOR_HOST", "HOSTGATOR_USER", "HOSTGATOR_DEPLOY_PATH")
        )
        port = int(values.get("HOSTGATOR_PORT", 22))
        if not re.fullmatch(r"[a-zA-Z0-9.-]+", host) or not re.fullmatch(
            r"[a-zA-Z0-9_-]+", user
        ):
            raise ValueError("Invalid destination")
        if (
            not re.fullmatch(r"/[a-zA-Z0-9_./-]+", target)
            or ".." in target.split("/")
            or target.rstrip("/") in {"", "/etc", "/var", "/home", "/usr", "/root"}
            or not 1 <= port <= 65535
        ):
            raise ValueError("Invalid deployment path or port")
        child_env = {
            name: value
            for name, value in os.environ.items()
            if name
            in {"PATH", "HOME", "USER", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "CI"}
        }
        with tempfile.TemporaryDirectory(prefix="proace-ssh-") as directory:
            key = Path(directory) / "identity"
            hosts = Path(directory) / "known_hosts"
            for path, name in [
                (key, "HOSTGATOR_SSH_PRIVATE_KEY"),
                (hosts, "HOSTGATOR_SSH_KNOWN_HOSTS"),
            ]:
                value = values[name]
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("Missing SSH credential")
                path.write_text(value.rstrip() + "\n")
                path.chmod(0o600)
            ssh = [
                "ssh",
                "-i",
                str(key),
                "-p",
                str(port),
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "ForwardAgent=no",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UserKnownHostsFile=" + str(hosts),
            ]
            destination = user + "@" + host
            subprocess.run(
                [
                    *ssh,
                    destination,
                    "mkdir -p -- " + shlex.quote(target + "/.release/incoming"),
                ],
                env=child_env,
                check=True,
            )
            staged_source = target + "/.release/incoming/" + commit + "-source"
            subprocess.run([*ssh, destination, "mkdir -p -- " + shlex.quote(staged_source)], env=child_env, check=True)
            excludes = [
                ".git/",
                ".release/",
                ".env*",
                "node_modules/",
                ".next/",
                ".venv/",
                "__pycache__/",
                "media/",
                "*.sqlite3*",
                "qa-reports/",
                "qa-report/",
                "*.pem",
                "*.key",
            ]
            subprocess.run(
                [
                    "rsync",
                    "--archive",
                    "--compress",
                    *["--exclude=" + item for item in excludes],
                    "--rsh",
                    shlex.join(ssh),
                    "./",
                    destination + ":" + staged_source + "/",
                ],
                env=child_env,
                check=True,
            )
            incoming = target + "/.release/incoming/" + commit + ".tgz"
            subprocess.run(
                [
                    "rsync",
                    "--archive",
                    "--rsh",
                    shlex.join(ssh),
                    str(archive),
                    destination + ":" + incoming,
                ],
                env=child_env,
                check=True,
            )
            activate = "python3 " + shlex.quote(staged_source + "/scripts/host_release.py") + " --root " + shlex.quote(target) + " deploy " + " ".join(
                shlex.quote(value) for value in [incoming, expected, commit]
            ) + " --source " + shlex.quote(staged_source)
            subprocess.run(
                [
                    *ssh,
                    destination,
                    "cd "
                    + shlex.quote(target)
                    + " && "
                    + activate
                    + " && rm -- "
                    + shlex.quote(incoming),
                ],
                env=child_env,
                check=True,
            )
    except (
        OSError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        tarfile.TarError,
        subprocess.CalledProcessError,
    ):
        raise SystemExit(
            "HostGator deployment failed; check Vault deployment access, SSH trust and server readiness."
        ) from None
    finally:
        source.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
