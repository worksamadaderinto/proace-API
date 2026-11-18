"""Registry identity and lock generation regression tests; no network access."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from hash_python_lock import artifact_hashes, add_hashes


class PythonLockTests(unittest.TestCase):
    def test_registry_identity_and_hashes_must_match(self) -> None:
        metadata = {
            "info": {"name": "fixture-package", "version": "1.0.0"},
            "urls": [{"digests": {"sha256": "a" * 64}}],
        }
        with patch(
            "hash_python_lock.urllib.request.urlopen",
            return_value=io.StringIO(json.dumps(metadata)),
        ):
            self.assertEqual(artifact_hashes("fixture_package", "1.0.0"), ["a" * 64])
        metadata["info"]["name"] = "wrong-package"
        with patch(
            "hash_python_lock.urllib.request.urlopen",
            return_value=io.StringIO(json.dumps(metadata)),
        ):
            with self.assertRaises(ValueError):
                artifact_hashes("fixture-package", "1.0.0")

    def test_incomplete_or_unresolved_inputs_cannot_replace_a_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.txt"
            output = Path(directory) / "output.lock"
            source.write_text("fixture>=1\n")
            output.write_text("existing reviewed lock")
            with self.assertRaises(ValueError):
                add_hashes(source, output)
            self.assertEqual(output.read_text(), "existing reviewed lock")
            source.write_text("fixture==1.0.0\n")
            with patch("hash_python_lock.artifact_hashes", return_value=["a" * 64]):
                add_hashes(source, output)
            self.assertIn("--hash=sha256:" + "a" * 64, output.read_text())


if __name__ == "__main__":
    unittest.main()
