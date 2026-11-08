"""Every external firmware source in the manifests names a fixed commit.

A sha256 pins the content, but a URL that follows a branch head stops
resolving to it as soon as upstream changes the file, so a fresh staging
fails for everyone without the cache.  linux-firmware entries use lfw:<commit>
and anything else a URL with the commit in its path.
"""

from pathlib import Path
import re
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = sorted((ROOT / "configs/firmware").glob("*.toml"))
COMMIT = re.compile(r"[0-9a-f]{40}")


def sources(manifest):
    data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    found = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("source"), str):
                found.append(node["source"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return found


class ManifestPinTests(unittest.TestCase):
    def test_manifests_found(self):
        self.assertTrue(MANIFESTS)

    def test_external_sources_name_a_commit(self):
        for manifest in MANIFESTS:
            for source in sources(manifest):
                kind, _, rest = source.partition(":")
                if kind == "lfw":
                    commit = rest.split(":", 1)[0]
                    self.assertRegex(commit, r"^[0-9a-f]{40}$", (manifest.name, source))
                elif kind == "url":
                    self.assertRegex(rest, COMMIT, (manifest.name, source))
                    self.assertNotRegex(rest, r"/(main|master)/", (manifest.name, source))


if __name__ == "__main__":
    unittest.main()
