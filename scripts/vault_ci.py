#!/usr/bin/env python3
"""Fetch one KV v2 document through GitHub OIDC without exporting secrets."""

import argparse
import json
import os
import re
import ssl
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, HTTPSHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def https_address(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("A trusted HTTPS address is required.")
    return value.rstrip("/")


def request_json(
    url: str, headers: dict[str, str], payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    context = ssl.create_default_context(cafile=os.environ.get("VAULT_CACERT") or None)
    opener = build_opener(NoRedirect(), HTTPSHandler(context=context))
    body = None if payload is None else json.dumps(payload).encode()
    request = Request(
        https_address(url),
        data=body,
        headers={"Content-Type": "application/json", **headers},
    )
    with opener.open(request, timeout=30) as response:
        raw = response.read(1048577)
    if len(raw) > 1048576:
        raise ValueError("Response exceeds the configured limit.")
    if not raw:
        return {}
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("Invalid service response.")
    return document


def atomic_secret(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".vault-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(document, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def fetch(
    address: str,
    role: str,
    secret_path: str,
    output: Path,
    audience: str = "proace-deploy",
) -> None:
    if not role or not re.fullmatch(
        r"kv/data/proace/production/[a-z][a-z0-9-]*", secret_path
    ):
        raise ValueError("Configure a scoped production role and KV v2 path.")
    base = https_address(address)
    if urlsplit(base).query or urlsplit(base).path not in {"", "/"}:
        raise ValueError("Vault address must contain only its HTTPS origin.")
    oidc_url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    parts = urlsplit(https_address(oidc_url))
    query = dict(parse_qsl(parts.query))
    query["audience"] = audience
    oidc_url = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), "")
    )
    identity = request_json(
        oidc_url,
        {"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]},
    )
    jwt = identity.get("value")
    if not isinstance(jwt, str) or not jwt:
        raise ValueError("GitHub did not provide a workload identity.")
    authenticated = request_json(
        base + "/v1/auth/github/login", {}, {"role": role, "jwt": jwt}
    )
    token = authenticated.get("auth", {}).get("client_token")
    if not isinstance(token, str) or not token:
        raise ValueError("Vault did not authorize this workload.")
    try:
        response = request_json(base + "/v1/" + secret_path, {"X-Vault-Token": token})
        document = response.get("data", {}).get("data")
        if not isinstance(document, dict) or not document:
            raise ValueError("The configured Vault document is empty or invalid.")
        atomic_secret(output, document)
    finally:
        request_json(base + "/v1/auth/token/revoke-self", {"X-Vault-Token": token}, {})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--output", required=True, type=Path)
    options = parser.parse_args()
    try:
        fetch(options.address, options.role, options.path, options.output)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        HTTPError,
        URLError,
    ):
        options.output.unlink(missing_ok=True)
        print(
            "Vault credential retrieval failed. Check TLS, workload role bindings and the scoped secret path.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print("Vault deployment credentials retrieved to a restricted temporary file.")


if __name__ == "__main__":
    main()
