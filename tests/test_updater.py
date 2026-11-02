import json
import os
import unittest
from unittest import mock

from peak_alphaloop import __version__, updater


def release(tag: str, names: list[str]) -> bytes:
    return json.dumps({
        "tag_name": tag, "html_url": f"https://github.com/x/y/releases/tag/{tag}", "body": "notes",
        "assets": [{"name": n, "browser_download_url": f"https://dl/{n}", "size": 10} for n in names],
    }).encode()


ASSETS = ["PeakAlphaloop-9.0.0-windows-x64-setup.exe", "PeakAlphaloop-9.0.0-macos-arm64.zip",
          "PeakAlphaloop-9.0.0-linux-x64.tar.xz", "SHA256SUMS.txt"]


class UpdaterTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("ALPHALOOP_NO_UPDATE_CHECK", None)

    def test_version_parsing(self) -> None:
        self.assertGreater(updater.parse_version("v1.10.0"), updater.parse_version("1.9.9"))
        self.assertEqual(updater.parse_version("v1.2.0"), updater.parse_version("1.2.0"))
        self.assertEqual(updater.parse_version("1.2.0-beta.1"), (1, 2, 0))

    def test_newer_release_is_reported_with_platform_asset(self) -> None:
        for platform_name, suffix in updater.ASSET_SUFFIXES.items():
            with self.subTest(platform=platform_name), \
                    mock.patch.object(updater, "_get", return_value=release("v9.0.0", ASSETS)), \
                    mock.patch.object(updater, "platform_key", return_value=platform_name):
                result = updater.check()
                self.assertTrue(result["available"])
                self.assertEqual(result["latest"], "9.0.0")
                self.assertTrue(result["asset"]["name"].endswith(suffix))
                self.assertEqual(result["checksums_url"], "https://dl/SHA256SUMS.txt")
                self.assertFalse(result["can_install"])  # not a standalone build in tests

    def test_windows_standalone_can_self_install(self) -> None:
        with mock.patch.object(updater, "_get", return_value=release("v9.0.0", ASSETS)), \
                mock.patch.object(updater, "platform_key", return_value="windows"), \
                mock.patch.object(updater, "is_standalone", return_value=True):
            self.assertTrue(updater.check()["can_install"])

    def test_same_or_older_release_is_not_offered(self) -> None:
        for tag in (f"v{__version__}", "v0.0.1"):
            with self.subTest(tag=tag), \
                    mock.patch.object(updater, "_get", return_value=release(tag, ASSETS)):
                self.assertFalse(updater.check()["available"])

    def test_offline_and_disabled(self) -> None:
        with mock.patch.object(updater, "_get", side_effect=updater.UpdateError("offline")):
            result = updater.check()
        self.assertFalse(result["available"])
        self.assertIn("offline", result["error"])
        with mock.patch.dict(os.environ, {"ALPHALOOP_NO_UPDATE_CHECK": "1"}), \
                mock.patch.object(updater, "_get") as get:
            self.assertTrue(updater.check()["disabled"])
            get.assert_not_called()

    def test_checksum_lookup(self) -> None:
        sums = "abc123  PeakAlphaloop-9.0.0-windows-x64-setup.exe\ndef456 *other.zip\n"
        self.assertEqual(updater.expected_checksum(sums, "PeakAlphaloop-9.0.0-windows-x64-setup.exe"),
                         "abc123")
        self.assertEqual(updater.expected_checksum(sums, "other.zip"), "def456")
        with self.assertRaises(updater.UpdateError):
            updater.expected_checksum(sums, "missing.exe")

    def test_install_refuses_when_not_installable(self) -> None:
        with mock.patch.object(updater, "check", return_value={"can_install": False, "page": "P"}):
            with self.assertRaises(updater.UpdateError):
                updater.install_update()


if __name__ == "__main__":
    unittest.main()
