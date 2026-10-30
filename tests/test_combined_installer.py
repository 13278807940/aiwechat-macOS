import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from build_combined_installer import digest, distribution_xml, preinstall_script, validate_input


class CombinedInstallerTests(unittest.TestCase):
    def test_components_are_hidden_exclusive_and_non_relocatable_target(self):
        root = ET.fromstring(distribution_xml("2.0.3"))
        self.assertEqual(root.find("options").get("hostArchitectures"), "arm64,x86_64")
        self.assertEqual(root.find("domains").get("enable_currentUserHome"), "false")
        choices = root.findall("choice")
        self.assertEqual({c.get("id") for c in choices}, {"arm64", "x86_64"})
        for choice in choices:
            self.assertEqual(choice.get("visible"), "false")
            self.assertEqual(choice.get("enabled"), "false")
            self.assertEqual(choice.get("selected"), "targetArchitecture() == '%s'" % choice.get("id"))
        self.assertEqual(root.find("volume-check/allowed-os-versions/os-version").get("min"), "12.0")
        self.assertEqual(root.find("installation-check").get("script"), "checkRequirements()")

    def test_preinstall_has_second_architecture_and_min_os_guard(self):
        for architecture in ("arm64", "x86_64"):
            script = preinstall_script(architecture)
            self.assertIn('if [ "$actual" != \'%s\' ]' % architecture, script)
            self.assertIn('if [ "$major" -lt 12 ]', script)
            self.assertIn('sysctl.proc_translated', script)
        with self.assertRaises(ValueError):
            preinstall_script("arm64; bad")

    def test_source_checksum_and_report_identity_are_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "source.app.zip"
            archive.write_bytes(b"test archive bytes")
            manifest = root / "source.sha256.txt"
            manifest.write_text(digest(archive) + "  release/source.app.zip\n", encoding="utf-8")
            audit = root / "source.兼容性校验.json"
            audit.write_text(json.dumps({"ok": True, "architecture": "arm64", "version": "2.0.3",
                                         "expected_minimum_os": "12.0"}), encoding="utf-8")
            smoke = root / "source.启动自检.json"
            smoke.write_text(json.dumps({"ok": True, "arch": "arm64", "frozen": True, "webengine": True}), encoding="utf-8")
            self.assertEqual(validate_input(root, "arm64", "2.0.3"), archive)
            with self.assertRaises(ValueError):
                validate_input(root, "x86_64", "2.0.3")
            archive.write_bytes(b"different payload")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                validate_input(root, "arm64", "2.0.3")


if __name__ == "__main__":
    unittest.main()
