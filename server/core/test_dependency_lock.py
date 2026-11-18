"""Keep CI's dependency lock compatible with the Docker runtime contract."""

import json
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def test_locked_packages_satisfy_runtime_requirements() -> None:
    server = Path(__file__).resolve().parents[1]
    lock = json.loads((server / "Pipfile.lock").read_text())
    packages = lock["default"]

    for line in (server / "requirements.txt").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue

        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue

        entry = packages[canonicalize_name(requirement.name)]
        version = entry["version"]
        assert version.startswith("=="), requirement.name
        assert entry["hashes"], requirement.name
        assert version[2:] in requirement.specifier, (
            f"{requirement.name}: locked {version}, runtime requires "
            f"{requirement.specifier}"
        )
