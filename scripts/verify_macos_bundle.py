#!/usr/bin/env python3
"""Audit every bundled Mach-O target slice, without launching or changing it.

This checks binary deployment metadata, not real-machine macOS compatibility.
It intentionally fails closed when an executable has no macOS version command.
The parser is pure Python so its boundary checks can also be tested on Windows.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import struct
from typing import BinaryIO


CPU_TYPES = {"arm64": 0x0100000C, "x86_64": 0x01000007}
THIN_MAGICS = {
    b"\xfe\xed\xfa\xce": (">", False),
    b"\xce\xfa\xed\xfe": ("<", False),
    b"\xfe\xed\xfa\xcf": (">", True),
    b"\xcf\xfa\xed\xfe": ("<", True),
}
FAT_MAGICS = {
    b"\xca\xfe\xba\xbe": (">", False),
    b"\xbe\xba\xfe\xca": ("<", False),
    b"\xca\xfe\xba\xbf": (">", True),
    b"\xbf\xba\xfe\xca": ("<", True),
}
LC_VERSION_MIN_MACOSX = 0x24
LC_BUILD_VERSION = 0x32


class BinaryAuditError(ValueError):
    """A Mach-O is malformed or cannot meet the declared target."""


def version_tuple(value: str) -> tuple[int, int, int]:
    parts = str(value).split(".")
    if not 1 <= len(parts) <= 3 or any(not part.isdigit() for part in parts):
        raise BinaryAuditError("invalid macOS version: %r" % value)
    return tuple([int(part) for part in parts] + [0] * (3 - len(parts)))


def version_text(value: tuple[int, int, int]) -> str:
    major, minor, patch = value
    return "%d.%d%s" % (major, minor, ".%d" % patch if patch else "")


def _packed_version(value: int) -> tuple[int, int, int]:
    return value >> 16, (value >> 8) & 0xFF, value & 0xFF


def _read(stream: BinaryIO, offset: int, size: int, end: int) -> bytes:
    if offset < 0 or size < 0 or offset > end or size > end - offset:
        raise BinaryAuditError("Mach-O range exceeds file or slice bounds")
    stream.seek(offset)
    result = stream.read(size)
    if len(result) != size:
        raise BinaryAuditError("truncated Mach-O data")
    return result


def _cpu_name(cpu: int) -> str:
    return next((name for name, number in CPU_TYPES.items() if number == cpu),
                "cpu_0x%08x" % cpu)


def _thin(stream: BinaryIO, offset: int, size: int, expected_cpu: int) -> dict:
    end = offset + size
    magic = _read(stream, offset, 4, end)
    if magic not in THIN_MAGICS:
        raise BinaryAuditError("fat entry does not contain a thin Mach-O")
    endian, is_64 = THIN_MAGICS[magic]
    header_size = 32 if is_64 else 28
    fields = struct.unpack(endian + ("8I" if is_64 else "7I"),
                           _read(stream, offset, header_size, end))
    _, cpu, subtype, filetype, ncmds, sizeofcmds, *_ = fields
    if cpu != expected_cpu:
        raise BinaryAuditError("target architecture missing or fat/thin CPU mismatch: %s" % _cpu_name(cpu))
    if not is_64:
        raise BinaryAuditError("target architecture must have a 64-bit Mach-O header")
    commands_start = offset + header_size
    if sizeofcmds > end - commands_start or ncmds > sizeofcmds // 8:
        raise BinaryAuditError("load command table exceeds slice bounds")
    commands_end = commands_start + sizeofcmds
    position = commands_start
    versions = []
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack(endian + "2I", _read(stream, position, 8, commands_end))
        if cmdsize < 8 or cmdsize % 8 or cmdsize > commands_end - position:
            raise BinaryAuditError("invalid 64-bit load command size")
        if cmd == LC_VERSION_MIN_MACOSX:
            if cmdsize < 16:
                raise BinaryAuditError("truncated LC_VERSION_MIN_MACOSX")
            version, _sdk = struct.unpack(endian + "2I", _read(stream, position + 8, 8, commands_end))
            versions.append(_packed_version(version))
        elif cmd == LC_BUILD_VERSION:
            if cmdsize < 24:
                raise BinaryAuditError("truncated LC_BUILD_VERSION")
            platform, minimum, _sdk, tools = struct.unpack(
                endian + "4I", _read(stream, position + 8, 16, commands_end))
            if platform != 1:
                raise BinaryAuditError("target slice is not a macOS binary (platform %d)" % platform)
            if tools > (cmdsize - 24) // 8:
                raise BinaryAuditError("LC_BUILD_VERSION tool list exceeds command bounds")
            versions.append(_packed_version(minimum))
        position += cmdsize
    if position != commands_end:
        raise BinaryAuditError("load command count does not match table size")
    if not versions or max(versions) == (0, 0, 0):
        raise BinaryAuditError("no valid macOS minimum deployment version in target slice")
    return {"architecture": _cpu_name(cpu), "cpu_subtype": subtype, "file_type": filetype,
            "minimum_os": version_text(max(versions))}


def inspect_macho(stream: BinaryIO, architecture: str) -> dict | None:
    """Return target metadata, None for other files, or raise for invalid Mach-O.

    All fat entries are checked for bounds/overlaps. Only requested-architecture
    load commands affect the deployment target, since other slices cannot run.
    """
    if architecture not in CPU_TYPES:
        raise BinaryAuditError("unsupported architecture: %s" % architecture)
    stream.seek(0, os.SEEK_END)
    file_size = stream.tell()
    stream.seek(0)
    magic = stream.read(4)
    expected_cpu = CPU_TYPES[architecture]
    if magic in THIN_MAGICS:
        metadata = _thin(stream, 0, file_size, expected_cpu)
        return {"architectures": [metadata["architecture"]],
                "minimum_os": metadata["minimum_os"], "slices": [metadata]}
    if magic not in FAT_MAGICS:
        return None
    endian, is_64 = FAT_MAGICS[magic]
    count, = struct.unpack(endian + "I", _read(stream, 4, 4, file_size))
    entry_size = 32 if is_64 else 20
    if count == 0 or count > 128 or count > (file_size - 8) // entry_size:
        raise BinaryAuditError("invalid fat architecture table")
    table_end = 8 + count * entry_size
    entries = []
    for index in range(count):
        fields = struct.unpack(endian + ("IIQQII" if is_64 else "5I"),
                               _read(stream, 8 + index * entry_size, entry_size, file_size))
        cpu, subtype, offset, size, alignment, *_ = fields
        if (offset < table_end or size < 28 or offset > file_size or size > file_size - offset
                or alignment > 63 or offset % (1 << alignment)):
            raise BinaryAuditError("fat slice has invalid bounds or alignment")
        entries.append((cpu, subtype, offset, size))
    ranges = sorted((offset, offset + size) for _, _, offset, size in entries)
    if any(right > next_left for (_, right), (next_left, _) in zip(ranges, ranges[1:])):
        raise BinaryAuditError("fat architecture slices overlap")
    selected = []
    for cpu, subtype, offset, size in entries:
        if cpu == expected_cpu:
            metadata = _thin(stream, offset, size, expected_cpu)
            if metadata["cpu_subtype"] != subtype:
                raise BinaryAuditError("fat/thin CPU subtype mismatch")
            selected.append(metadata)
    if not selected:
        raise BinaryAuditError("target architecture missing: %s" % architecture)
    highest = max(version_tuple(item["minimum_os"]) for item in selected)
    return {"architectures": sorted({_cpu_name(item[0]) for item in entries}),
            "minimum_os": version_text(highest), "slices": selected}


def audit_bundle(app: Path, architecture: str, minimum_os: str, version: str) -> dict:
    root = app.resolve(strict=True)
    target_version = version_tuple(minimum_os)
    report = {"schema_version": 1, "app": str(root), "architecture": architecture,
              "expected_minimum_os": version_text(target_version), "version": version,
              "scope": "Static deployment metadata; real macOS and WeChat testing is still required.",
              "scanned_files": 0, "mach_o_files": 0, "highest_minimum_os": None,
              "highest_minimum_os_files": [], "plist": {}, "bundle_plists": [],
              "binaries": [], "violations": []}

    def violation(path, message):
        report["violations"].append({"path": str(path), "message": str(message)})

    try:
        main_plist = root / "Contents/Info.plist"
        if not main_plist.resolve(strict=True).is_relative_to(root):
            raise BinaryAuditError("main Info.plist resolves outside .app")
        with main_plist.open("rb") as source:
            plist = plistlib.load(source)
        if not isinstance(plist, dict):
            raise BinaryAuditError("main Info.plist must contain a dictionary")
        report["plist"] = {key: plist.get(key) for key in (
            "CFBundleExecutable", "CFBundleShortVersionString", "LSMinimumSystemVersion")}
        if version_tuple(plist.get("LSMinimumSystemVersion", "")) != target_version:
            violation("Contents/Info.plist", "LSMinimumSystemVersion must equal %s" % minimum_os)
        if plist.get("CFBundleShortVersionString") != version:
            violation("Contents/Info.plist", "CFBundleShortVersionString must equal %s" % version)
        executable = plist.get("CFBundleExecutable", "")
        if not executable or Path(executable).name != executable:
            violation("Contents/Info.plist", "invalid CFBundleExecutable")
        else:
            main_binary = root / "Contents/MacOS" / executable
            try:
                if not main_binary.resolve(strict=True).is_relative_to(root):
                    raise BinaryAuditError("main executable resolves outside .app")
                with main_binary.open("rb") as source:
                    if inspect_macho(source, architecture) is None:
                        raise BinaryAuditError("main executable is not Mach-O")
            except (OSError, BinaryAuditError) as exc:
                violation(main_binary.relative_to(root), exc)
    except (OSError, ValueError, plistlib.InvalidFileException, TypeError) as exc:
        violation("Contents/Info.plist", exc)

    visited = set()
    def walk_error(error):
        violation(error.filename or ".", error)

    for directory, directories, filenames in os.walk(root, followlinks=False, onerror=walk_error):
        # Check symlink destinations before opening them; do not traverse linked
        # directories, since their real location will be scanned under the root.
        for name in list(directories):
            candidate = Path(directory) / name
            if candidate.is_symlink():
                directories.remove(name)
                try:
                    if not candidate.resolve(strict=True).is_relative_to(root):
                        violation(candidate.relative_to(root), "directory symlink resolves outside .app")
                except (OSError, RuntimeError) as exc:
                    violation(candidate.relative_to(root), exc)
        for name in filenames:
            candidate = Path(directory) / name
            relative = candidate.relative_to(root).as_posix()
            try:
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(root):
                    raise BinaryAuditError("file symlink resolves outside .app")
                if resolved in visited:
                    continue
                visited.add(resolved)
                report["scanned_files"] += 1
                if candidate.name == "Info.plist":
                    try:
                        with resolved.open("rb") as source:
                            bundle_plist = plistlib.load(source)
                        if not isinstance(bundle_plist, dict):
                            raise BinaryAuditError("Info.plist must contain a dictionary")
                        minimum = bundle_plist.get("LSMinimumSystemVersion")
                        report["bundle_plists"].append({"path": relative, "minimum_os": minimum})
                        if (relative != "Contents/Info.plist" and minimum is not None
                                and version_tuple(minimum) > target_version):
                            violation(relative, "bundle plist requires macOS %s, target is %s" %
                                      (minimum, minimum_os))
                    except (ValueError, plistlib.InvalidFileException, TypeError) as exc:
                        violation(relative, "invalid Info.plist: %s" % exc)
                with resolved.open("rb") as source:
                    metadata = inspect_macho(source, architecture)
                if metadata is None:
                    continue
                report["mach_o_files"] += 1
                entry = {"path": relative, **metadata}
                report["binaries"].append(entry)
                found_version = version_tuple(metadata["minimum_os"])
                if found_version > target_version:
                    violation(relative, "requires macOS %s, target is %s" %
                              (metadata["minimum_os"], minimum_os))
                previous = report["highest_minimum_os"]
                if previous is None or found_version > version_tuple(previous):
                    report["highest_minimum_os"] = metadata["minimum_os"]
                    report["highest_minimum_os_files"] = [relative]
                elif found_version == version_tuple(previous):
                    report["highest_minimum_os_files"].append(relative)
            except (OSError, RuntimeError, BinaryAuditError) as exc:
                violation(relative, exc)
    if report["mach_o_files"] == 0:
        violation(".", "no valid target Mach-O files found")
    report["binaries"].sort(key=lambda item: item["path"])
    report["ok"] = not report["violations"]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--arch", required=True, choices=CPU_TYPES)
    parser.add_argument("--min-os", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = audit_bundle(args.app, args.arch, args.min_os, args.version)
    except (OSError, BinaryAuditError) as exc:
        report = {"ok": False, "violations": [{"path": str(args.app), "message": str(exc)}]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Mach-O deployment audit: %s; binaries=%s; highest minimum macOS=%s" %
          ("PASS" if report["ok"] else "FAIL", report.get("mach_o_files", 0),
           report.get("highest_minimum_os", "unknown")))
    for item in report["violations"]:
        print("%s: %s" % (item["path"], item["message"]))
    print("Report: %s" % args.output)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
