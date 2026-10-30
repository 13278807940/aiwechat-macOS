import io
from pathlib import Path
import plistlib
import struct
import tempfile
import unittest

from scripts.verify_macos_bundle import (
    BinaryAuditError, CPU_TYPES, LC_BUILD_VERSION, LC_VERSION_MIN_MACOSX,
    audit_bundle, inspect_macho, main,
)


def packed_version(value):
    parts = [int(part) for part in value.split(".")]
    major, minor, patch = (parts + [0, 0])[:3]
    return (major << 16) | (minor << 8) | patch


def thin(arch="arm64", minimum="12.0", *, endian="<", modern=True, commands=None):
    if commands is None:
        if modern:
            commands = [struct.pack(endian + "6I", LC_BUILD_VERSION, 24, 1,
                                    packed_version(minimum), packed_version("15.0"), 0)]
        else:
            commands = [struct.pack(endian + "4I", LC_VERSION_MIN_MACOSX, 16,
                                    packed_version(minimum), packed_version("15.0"))]
    size = sum(len(command) for command in commands)
    return struct.pack(endian + "8I", 0xFEEDFACF, CPU_TYPES[arch], 0, 2,
                       len(commands), size, 0, 0) + b"".join(commands)


def fat(slices, *, wide=False, endian=">"):
    header = struct.pack(endian + "2I", 0xCAFEBABF if wide else 0xCAFEBABE, len(slices))
    entry_size = 32 if wide else 20
    offset = 8 + entry_size * len(slices)
    entries = []
    payload = b""
    for arch, image in slices:
        fields = (CPU_TYPES[arch], 0, offset, len(image), 0)
        entries.append(struct.pack(endian + ("IIQQII" if wide else "5I"),
                                   *(fields + (0,) if wide else fields)))
        payload += image
        offset += len(image)
    return header + b"".join(entries) + payload


class MachOParserTests(unittest.TestCase):
    def test_regular_resources_are_ignored(self):
        for contents in (b"", b"abc", b"normal resource", b"PK\x03\x04zip"):
            with self.subTest(contents=contents):
                self.assertIsNone(inspect_macho(io.BytesIO(contents), "arm64"))

    def test_thin_modern_and_legacy_version_commands_both_endians(self):
        for endian in ("<", ">"):
            for modern in (True, False):
                with self.subTest(endian=endian, modern=modern):
                    result = inspect_macho(io.BytesIO(thin(minimum="11.3.1", endian=endian,
                                                         modern=modern)), "arm64")
                    self.assertEqual(result["minimum_os"], "11.3.1")

    def test_universal_uses_requested_slice_minimum_only(self):
        for endian in ("<", ">"):
            for wide in (False, True):
                with self.subTest(endian=endian, wide=wide):
                    image = fat([("arm64", thin(minimum="14.0")),
                                 ("x86_64", thin("x86_64", "11.0"))],
                                wide=wide, endian=endian)
                    result = inspect_macho(io.BytesIO(image), "x86_64")
                    self.assertEqual(result["architectures"], ["arm64", "x86_64"])
                    self.assertEqual(result["minimum_os"], "11.0")

    def test_wrong_thin_and_missing_fat_architecture_are_rejected(self):
        for image in (thin("x86_64"), fat([("x86_64", thin("x86_64"))])):
            with self.assertRaisesRegex(BinaryAuditError, "architecture missing"):
                inspect_macho(io.BytesIO(image), "arm64")

    def test_truncated_headers_and_commands_are_rejected(self):
        good = thin()
        for image in (good[:4], good[:31], good[:-1], b"\xca\xfe\xba\xbe"):
            with self.subTest(size=len(image)):
                with self.assertRaises(BinaryAuditError):
                    inspect_macho(io.BytesIO(image), "arm64")

    def test_bad_load_command_ranges_are_rejected(self):
        invalid = [
            struct.pack("<2I", 0x1, 0),
            struct.pack("<2I", 0x1, 7),
            struct.pack("<2I", 0x1, 64),
            struct.pack("<2I", LC_BUILD_VERSION, 8),
            struct.pack("<6I", LC_BUILD_VERSION, 24, 1, packed_version("12.0"), 0, 1),
        ]
        for command in invalid:
            with self.subTest(command=command):
                with self.assertRaises(BinaryAuditError):
                    inspect_macho(io.BytesIO(thin(commands=[command])), "arm64")

    def test_non_macos_and_absent_deployment_metadata_fail_closed(self):
        ios = struct.pack("<6I", LC_BUILD_VERSION, 24, 2, packed_version("12.0"), 0, 0)
        for commands in ([], [ios], [struct.pack("<2I", 0x1, 8)]):
            with self.assertRaises(BinaryAuditError):
                inspect_macho(io.BytesIO(thin(commands=commands)), "arm64")

    def test_fat_slice_range_overlap_and_cpu_mismatch_are_rejected(self):
        good = bytearray(fat([("arm64", thin()), ("x86_64", thin("x86_64"))]))
        invalids = []
        overlap = bytearray(good)
        overlap[36:40] = good[16:20]  # second slice starts at first slice
        invalids.append(overlap)
        out_of_bounds = bytearray(good)
        out_of_bounds[16:20] = struct.pack(">I", len(good) + 100)
        invalids.append(out_of_bounds)
        into_table = bytearray(good)
        into_table[16:20] = struct.pack(">I", 8)
        invalids.append(into_table)
        mismatch = bytearray(good)
        first_offset, = struct.unpack(">I", good[16:20])
        mismatch[first_offset + 4:first_offset + 8] = struct.pack("<I", CPU_TYPES["x86_64"])
        invalids.append(mismatch)
        for image in invalids:
            with self.assertRaises(BinaryAuditError):
                inspect_macho(io.BytesIO(image), "arm64")

    def test_load_command_count_and_size_must_agree(self):
        image = bytearray(thin())
        image[16:20] = struct.pack("<I", 0)
        with self.assertRaisesRegex(BinaryAuditError, "count"):
            inspect_macho(io.BytesIO(image), "arm64")


class BundleAuditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "Sample.app"
        (self.root / "Contents/MacOS").mkdir(parents=True)
        self.plist = {"CFBundleExecutable": "Assistant", "CFBundleShortVersionString": "2.0.3",
                      "LSMinimumSystemVersion": "12.0"}
        self.write_plist()
        self.write("Contents/MacOS/Assistant", thin(minimum="11.0"))

    def write_plist(self):
        (self.root / "Contents/Info.plist").write_bytes(plistlib.dumps(self.plist))

    def write(self, relative, contents):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def audit(self):
        return audit_bundle(self.root, "arm64", "12.0", "2.0.3")

    def test_audits_framework_and_webengine_helpers_not_only_main(self):
        self.write("Contents/Frameworks/QtCore.framework/Versions/A/QtCore", thin(minimum="12.0"))
        helper = "Contents/Frameworks/QtWebEngineCore.framework/Helpers/QtWebEngineProcess.app/Contents/MacOS/QtWebEngineProcess"
        self.write(helper, thin(minimum="13.0"))
        self.write("Contents/Resources/config.json", b"{}")
        result = self.audit()
        self.assertFalse(result["ok"])
        self.assertEqual(result["mach_o_files"], 3)
        self.assertEqual(result["highest_minimum_os"], "13.0")
        self.assertEqual(result["highest_minimum_os_files"], [helper])
        self.assertEqual(result["violations"][0]["path"], helper)

    def test_correct_binary_versions_and_plist_pass(self):
        self.write("Contents/Frameworks/module.so", thin(minimum="12.0"))
        result = self.audit()
        self.assertTrue(result["ok"], result["violations"])
        self.assertEqual(result["mach_o_files"], 2)
        self.assertEqual(result["highest_minimum_os"], "12.0")

    def test_correct_plist_cannot_hide_wrong_dependency_architecture(self):
        self.write("Contents/Frameworks/module.so", thin("x86_64"))
        result = self.audit()
        self.assertFalse(result["ok"])
        self.assertTrue(any("architecture missing" in item["message"] for item in result["violations"]))

    def test_helper_bundle_minimum_is_checked_even_when_macho_is_compatible(self):
        helper = "Contents/Frameworks/QtWebEngineProcess.app/Contents/Info.plist"
        self.write(helper, plistlib.dumps({"LSMinimumSystemVersion": "13.0"}))
        result = self.audit()
        self.assertFalse(result["ok"])
        self.assertTrue(any(item["path"] == helper and "13.0" in item["message"]
                            for item in result["violations"]))

    def test_missing_optional_framework_minimum_is_allowed_but_broken_plist_fails(self):
        framework = "Contents/Frameworks/QtCore.framework/Resources/Info.plist"
        self.write(framework, plistlib.dumps({"CFBundleName": "QtCore"}))
        self.assertTrue(self.audit()["ok"])
        self.write(framework, b"not a plist")
        result = self.audit()
        self.assertFalse(result["ok"])
        self.assertTrue(any(item["path"] == framework for item in result["violations"]))

    def test_wrong_plist_version_and_os_are_both_reported(self):
        self.plist.update(LSMinimumSystemVersion="14.0", CFBundleShortVersionString="2.0.2")
        self.write_plist()
        result = self.audit()
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["violations"]), 2)

    def test_script_or_missing_main_is_rejected_even_with_valid_dependency(self):
        self.write("Contents/MacOS/Assistant", b"#!/bin/sh\nexit 0\n")
        self.write("Contents/Frameworks/module.so", thin())
        self.assertFalse(self.audit()["ok"])
        (self.root / "Contents/MacOS/Assistant").unlink()
        self.assertFalse(self.audit()["ok"])

    def test_internal_symlinks_are_deduplicated_and_external_never_opened(self):
        real = self.write("Contents/Frameworks/QtCore.framework/Versions/A/QtCore", thin())
        internal = real.parents[2] / "QtCore"
        external = self.root / "Contents/Frameworks/outside"
        private = Path(self.directory.name) / "external-file"
        private.write_bytes(thin(minimum="15.0"))
        try:
            internal.symlink_to(real)
            external.symlink_to(private)
        except OSError:
            self.skipTest("creating symbolic links is unavailable on this Windows host")
        result = self.audit()
        self.assertEqual(result["mach_o_files"], 2)
        self.assertTrue(any("outside .app" in item["message"] for item in result["violations"]))
        self.assertNotEqual(result["highest_minimum_os"], "15.0")

    def test_cli_writes_reviewable_report_and_failure_status(self):
        output = Path(self.directory.name) / "report.json"
        args = ["--app", str(self.root), "--arch", "arm64", "--min-os", "12.0",
                "--version", "2.0.3", "--output", str(output)]
        self.assertEqual(main(args), 0)
        self.assertIn('"ok": true', output.read_text(encoding="utf-8"))
        self.write("Contents/Frameworks/module.so", thin(minimum="14.0"))
        self.assertEqual(main(args), 1)
        self.assertIn('"highest_minimum_os": "14.0"', output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
