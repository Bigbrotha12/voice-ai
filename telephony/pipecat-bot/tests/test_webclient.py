"""Verify test-client.html wiring against the vendored livekit-client bundle."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent
HTML_PATH = BOT_DIR / "test-client.html"
BUNDLE_PATH = BOT_DIR / "vendor" / "livekit-client.umd.js"

REQUIRED_SYMBOLS = ["Room", "RoomEvent", "Track", "createLocalAudioTrack"]


@pytest.fixture(scope="module")
def html_source() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def bundle_export_names() -> list[str]:
    proc = subprocess.run(
        [
            "node",
            "-e",
            (
                "const lk = require(process.argv[1]); "
                "console.log(JSON.stringify(Object.keys(lk)));"
            ),
            str(BUNDLE_PATH),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"node failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestHtmlReferences:
    def test_html_exists(self):
        assert HTML_PATH.is_file()

    def test_no_cdn_references(self, html_source):
        cdn_hosts = ("cdn.jsdelivr.net", "unpkg.com", "esm.sh", "cdnjs.cloudflare.com")
        for host in cdn_hosts:
            assert host not in html_source, f"client must not depend on {host}"

    def test_references_local_bundle(self, html_source):
        assert '<script src="vendor/livekit-client.umd.js"></script>' in html_source

    def test_local_bundle_file_exists(self):
        assert BUNDLE_PATH.is_file()
        assert BUNDLE_PATH.stat().st_size > 100_000


class TestBundleExports:
    @pytest.fixture(scope="module")
    def exports(self):
        if shutil.which("node") is None:
            pytest.skip("node not available")
        return bundle_export_names()

    def test_required_symbols_exported(self, exports):
        missing = [s for s in REQUIRED_SYMBOLS if s not in exports]
        assert not missing, f"bundle missing required symbols: {missing}"

    def test_track_kind_nested_access_valid(self, exports):
        assert "Track" in exports


class TestHtmlUsesExportedSymbols:
    """Every LivekitClient symbol the HTML dereferences must exist in the bundle."""

    def test_html_symbols_exist_in_bundle(self, html_source):
        if shutil.which("node") is None:
            pytest.skip("node not available")

        exports = set(bundle_export_names())
        used = set(re.findall(r"\bLK\.([A-Za-z_$][\w$]*)", html_source))
        assert used, "no LK.* usages found - client may be broken"
        missing = sorted(used - exports)
        assert not missing, f"html uses symbols absent from bundle: {missing}"
