#!/usr/bin/env python3
"""Retired production .env writer; local development still uses server/.env."""


def main() -> None:
    raise SystemExit(
        "Production .env generation is retired. Configure Vault Agent and follow docs/vault.md."
    )


if __name__ == "__main__":
    main()
