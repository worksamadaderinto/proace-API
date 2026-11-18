#!/usr/bin/env python3
"""Add authoritative PyPI SHA-256 hashes to already-resolved exact requirements."""

import concurrent.futures
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def artifact_hashes(name: str, version: str) -> list[str]:
    url = (
        "https://pypi.org/pypi/"
        + urllib.parse.quote(name, safe="")
        + "/"
        + urllib.parse.quote(version, safe="")
        + "/json"
    )
    with urllib.request.urlopen(url, timeout=30) as response:
        metadata = json.load(response)
    normalize = lambda value: re.sub(r"[-_.]+", "-", value).lower()
    if (
        normalize(metadata["info"]["name"]) != normalize(name)
        or metadata["info"]["version"] != version
    ):
        raise ValueError("Registry identity/version mismatch")
    hashes = sorted(
        {record.get("digests", {}).get("sha256", "") for record in metadata["urls"]}
    )
    if not hashes or any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in hashes):
        raise ValueError("Registry did not provide valid artifact hashes")
    return hashes


def add_hashes(source: Path, output: Path) -> None:
    requirements = []
    for line in source.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)(\s*;.+)?", line.strip())
        if not match:
            raise ValueError(
                "Expected resolved, exact requirements without index overrides"
            )
        requirements.append(match.groups())
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        hashes = list(
            pool.map(lambda row: artifact_hashes(row[0], row[1]), requirements)
        )
    lines = [
        "# Exact versions resolved with pip-tools; artifact hashes verified through PyPI HTTPS JSON.",
        "# Regenerate using docs/supply-chain.md; never hand-edit hashes.",
        "",
    ]
    for (name, version, marker), values in zip(requirements, hashes):
        lines.append(name + "==" + version + (marker or "") + " \\")
        for index, value in enumerate(values):
            lines.append(
                "    --hash=sha256:"
                + value
                + (" \\" if index + 1 < len(values) else "")
            )
    output.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    add_hashes(Path(sys.argv[1]), Path(sys.argv[2]))
