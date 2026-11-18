"""Prepare and verify API image handoffs without running installers with release credentials."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import TypedDict

ROOT = Path(__file__).resolve().parents[1]
ROLES = {"api": "proace-api", "stream": "proace-api-stream"}


class ImageRecord(TypedDict):
    tag: str
    identity: str


class Manifest(TypedDict):
    commit: str
    platform: str
    images: dict[str, ImageRecord]


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_commit(commit: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Expected an exact reviewed Git commit")


def image_identity(tag: str) -> tuple[str, str]:
    result = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}} {{.Os}}/{{.Architecture}}",
            tag,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    identity, platform = result.stdout.strip().split()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", identity) or platform not in {
        "linux/amd64",
        "linux/arm64",
    }:
        raise ValueError("Unsupported image identity/platform")
    return identity, platform


def pack(output: Path, commit: str) -> None:
    validate_commit(commit)
    images: dict[str, ImageRecord] = {}
    platforms = set()
    for role, repository in ROLES.items():
        tag = repository + ":" + commit
        identity, platform = image_identity(tag)
        images[role] = {"tag": tag, "identity": identity}
        platforms.add(platform)
    if len(platforms) != 1:
        raise ValueError("API and stream platforms differ")
    manifest: Manifest = {
        "commit": commit,
        "platform": platforms.pop(),
        "images": images,
    }
    with tempfile.TemporaryDirectory(prefix="proace-image-pack-") as directory:
        work = Path(directory)
        with (work / "images.tar").open("wb") as stream:
            subprocess.run(
                ["docker", "save", *[record["tag"] for record in images.values()]],
                stdout=stream,
                check=True,
            )
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        with tarfile.open(output, "w:gz") as archive:
            archive.add(work / "manifest.json", arcname="manifest.json")
            archive.add(work / "images.tar", arcname="images.tar")


def checked_manifest(value: object, commit: str) -> Manifest:
    validate_commit(commit)
    if (
        not isinstance(value, dict)
        or value.get("commit") != commit
        or value.get("platform") not in {"linux/amd64", "linux/arm64"}
    ):
        raise ValueError("Unexpected release manifest")
    records = value.get("images")
    if not isinstance(records, dict) or set(records) != set(ROLES):
        raise ValueError("Expected API and stream images")
    images: dict[str, ImageRecord] = {}
    for role, repository in ROLES.items():
        record = records[role]
        if (
            not isinstance(record, dict)
            or record.get("tag") != repository + ":" + commit
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(record.get("identity", "")))
        ):
            raise ValueError("Unreviewed image identity")
        images[role] = {"tag": record["tag"], "identity": record["identity"]}
    return {"commit": commit, "platform": value["platform"], "images": images}


def verify(archive: Path, expected: str, commit: str) -> Manifest:
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("Expected an exact image handoff digest")
    if file_digest(archive) != expected:
        raise ValueError("Image handoff digest mismatch")
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if (
            len(members) != 2
            or {member.name for member in members} != {"manifest.json", "images.tar"}
            or not all(member.isfile() for member in members)
        ):
            raise ValueError("Unexpected image handoff contents")
        member = bundle.getmember("manifest.json")
        if member.size > 65536:
            raise ValueError("Oversized image manifest")
        stream = bundle.extractfile(member)
        if stream is None:
            raise ValueError("Missing image manifest")
        return checked_manifest(json.load(stream), commit)


def activate(archive: Path, expected: str, commit: str) -> None:
    manifest = verify(archive, expected, commit)
    result = subprocess.run(
        ["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    host = (
        result.stdout.strip().replace("/x86_64", "/amd64").replace("/aarch64", "/arm64")
    )
    if host != manifest["platform"]:
        raise ValueError("Release image platform does not match the deployment host")
    with tempfile.TemporaryDirectory(prefix="proace-image-load-") as directory:
        with tarfile.open(archive, "r:gz") as bundle:
            source = bundle.extractfile("images.tar")
            if source is None:
                raise ValueError("Missing image payload")
            images = Path(directory) / "images.tar"
            with images.open("wb") as destination:
                shutil.copyfileobj(source, destination)
        with images.open("rb") as source:
            subprocess.run(["docker", "load"], stdin=source, check=True)
    for record in manifest["images"].values():
        identity, platform = image_identity(record["tag"])
        if identity != record["identity"] or platform != manifest["platform"]:
            raise ValueError("Loaded image differs from the reviewed handoff")
    state = ROOT / ".release"
    state.mkdir(mode=0o700, exist_ok=True)
    active = state / "active.json"
    if any(path.is_symlink() for path in [state, active, state / "previous.json"]):
        raise ValueError("Release state must not use symbolic links")
    if active.exists() and json.loads(active.read_text()).get("commit") != commit:
        shutil.copyfile(active, state / "previous.json")
    history = state / "manifests"
    if history.is_symlink():
        raise ValueError("Release history must not use symbolic links")
    history.mkdir(mode=0o700, exist_ok=True)
    receipt = history / (commit + "-" + expected + ".json")
    if receipt.is_symlink():
        raise ValueError("Release receipt must not use symbolic links")
    receipt.write_text(json.dumps(manifest, indent=2) + "\n")
    handle, name = tempfile.mkstemp(prefix="active-", dir=state)
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(manifest, stream, indent=2)
            stream.write("\n")
        os.replace(name, active)
    finally:
        Path(name).unlink(missing_ok=True)


def active_image(role: str) -> str:
    value = json.loads((ROOT / ".release/active.json").read_text())
    if not isinstance(value, dict):
        raise ValueError("Invalid active release state")
    manifest = checked_manifest(value, value.get("commit", ""))
    record = manifest["images"][role]
    if image_identity(record["tag"]) != (record["identity"], manifest["platform"]):
        raise ValueError("Active image identity changed")
    return record["identity"]


if __name__ == "__main__":
    try:
        command = sys.argv[1]
        if command == "pack" and len(sys.argv) == 4:
            pack(Path(sys.argv[2]), sys.argv[3])
        elif command in {"verify", "activate"} and len(sys.argv) == 5:
            (verify if command == "verify" else activate)(
                Path(sys.argv[2]), sys.argv[3], sys.argv[4]
            )
        elif command == "image" and len(sys.argv) == 3:
            print(active_image(sys.argv[2]))
        else:
            raise ValueError("Unsupported handoff command")
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
            "API image handoff failed; check its digest, commit, platform and Docker image identities."
        ) from None
