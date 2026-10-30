"""One native .pkg; Installer selects one of the two independently built apps.

The PyInstaller app executables are never merged with lipo. Standard Installer
component payloads handle replacement and receipts. Existing per-user data is
outside the payload and is preserved.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from verify_macos_bundle import audit_bundle

APP_NAME = "中犇微信助手.app"
PACKAGE_PREFIX = "net.saizhuge.zhongben.wechatassistant"
ARCHITECTURES = ("arm64", "x86_64")
PROJECT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def validate_input(directory, architecture, version):
    archives = list(directory.glob("*.app.zip"))
    manifests = list(directory.glob("*.sha256.txt"))
    if len(archives) != 1 or len(manifests) != 1:
        raise ValueError("Expected exactly one app archive and checksum manifest: " + architecture)
    archive = archives[0]
    hashes = {}
    for line in manifests[0].read_text(encoding="utf-8-sig").splitlines():
        expected, filename = line.split(None, 1)
        hashes[Path(filename.lstrip("*")).name] = expected.lower()
    if hashes.get(archive.name) != digest(archive):
        raise ValueError("Source archive hash mismatch: " + architecture)
    audit_files = list(directory.glob("*.兼容性校验.json"))
    smoke_files = list(directory.glob("*.启动自检.json"))
    if len(audit_files) != 1 or len(smoke_files) != 1:
        raise ValueError("Missing source verification reports: " + architecture)
    audit = json.loads(audit_files[0].read_text(encoding="utf-8"))
    smoke = json.loads(smoke_files[0].read_text(encoding="utf-8"))
    if not (audit.get("ok") and audit.get("architecture") == architecture
            and audit.get("version") == version and audit.get("expected_minimum_os") == "12.0"
            and smoke.get("ok") and smoke.get("frozen") and smoke.get("webengine")
            and smoke.get("arch") == architecture):
        raise ValueError("Source verification did not pass: " + architecture)
    return archive


def distribution_xml(version):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Invalid application version")
    root = ET.Element("installer-gui-script", minSpecVersion="2")
    ET.SubElement(root, "title").text = "中犇微信助手 " + version
    ET.SubElement(root, "options", customize="never", hostArchitectures="arm64,x86_64",
                  **{"require-scripts": "true"})
    ET.SubElement(root, "domains", enable_anywhere="false", enable_currentUserHome="false",
                  enable_localSystem="true")
    ET.SubElement(root, "welcome", file="welcome.html")
    ET.SubElement(root, "conclusion", file="conclusion.html")
    ET.SubElement(root, "installation-check", script="checkRequirements()")
    allowed = ET.SubElement(ET.SubElement(root, "volume-check"), "allowed-os-versions")
    ET.SubElement(allowed, "os-version", min="12.0")
    ET.SubElement(root, "script").text = (PROJECT / "packaging/installer_checks.js").read_text(encoding="utf-8")
    outline = ET.SubElement(root, "choices-outline")
    for architecture in ARCHITECTURES:
        ET.SubElement(outline, "line", choice=architecture)
        choice = ET.SubElement(root, "choice", id=architecture, visible="false", enabled="false",
                               title=architecture, selected="targetArchitecture() == '%s'" % architecture)
        identifier = PACKAGE_PREFIX + "." + architecture
        ET.SubElement(choice, "pkg-ref", id=identifier)
        ET.SubElement(root, "pkg-ref", id=identifier, version=version, onConclusion="none").text = architecture + ".pkg"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def preinstall_script(architecture):
    if architecture not in ARCHITECTURES:
        raise ValueError("Unsupported architecture")
    return """#!/bin/sh
set -eu
major=$(/usr/bin/sw_vers -productVersion | /usr/bin/cut -d . -f 1)
if [ "$major" -lt 12 ]; then
    echo '中犇微信助手需要macOS 12或更新版本。' >&2
    exit 1
fi
arm=$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null || true)
translated=$(/usr/sbin/sysctl -n sysctl.proc_translated 2>/dev/null || true)
machine=$(/usr/sbin/sysctl -n hw.machine 2>/dev/null || true)
actual=''
if [ "$arm" = '1' ] || [ "$translated" = '1' ] || [ "$machine" = 'arm64' ] || [ "$machine" = 'arm64e' ]; then
    actual=arm64
elif [ "$machine" = 'x86_64' ] && { [ "$arm" = '0' ] || [ -z "$arm" ]; } && { [ "$translated" = '0' ] || [ -z "$translated" ]; }; then
    actual=x86_64
fi
if [ "$actual" != '%s' ]; then
    echo '安装组件与电脑芯片不匹配，已停止安装。' >&2
    exit 1
fi
echo "中犇微信助手：已选择 $actual 组件。"
""" % architecture


def build(inputs, output, source_run, version):
    if sys.platform != "darwin":
        raise RuntimeError("Build the combined .pkg on macOS")
    if not str(source_run).isdigit():
        raise ValueError("Invalid source run ID")
    output.mkdir(parents=True, exist_ok=True)
    work_root = PROJECT / "build"
    work_root.mkdir(exist_ok=True)
    report = {"version": version, "minimum_macos": "12.0", "type": "architecture-selecting-pkg",
              "source_run": str(source_run), "source_apps": [], "signed_installer": False,
              "scope": "Native installer integration; macOS 12/13 WeChat still needs real-machine testing."}
    with tempfile.TemporaryDirectory(prefix="combined-", dir=work_root) as temporary:
        work = Path(temporary)
        components = work / "components"
        components.mkdir()
        for architecture in ARCHITECTURES:
            directory = inputs / ("ZhongBenAssistant-macOS-%s-%s" % (architecture, source_run))
            archive = validate_input(directory, architecture, version)
            unpacked = work / architecture
            unpacked.mkdir()
            subprocess.run(["/usr/bin/ditto", "-x", "-k", str(archive), str(unpacked)], check=True)
            app = unpacked / APP_NAME
            audit = audit_bundle(app, architecture, "12.0", version)
            if not audit["ok"]:
                raise RuntimeError(json.dumps(audit["violations"], ensure_ascii=False))
            subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
            scripts = work / (architecture + "-scripts")
            scripts.mkdir()
            preinstall = scripts / "preinstall"
            preinstall.write_text(preinstall_script(architecture), encoding="utf-8", newline="\n")
            preinstall.chmod(0o755)
            component_plist = work / (architecture + ".plist")
            component_plist.write_bytes(plistlib.dumps([{
                "RootRelativeBundlePath": APP_NAME, "BundleIsRelocatable": False,
                "BundleIsVersionChecked": False, "BundleHasStrictIdentifier": True,
                "BundleOverwriteAction": "upgrade"}]))
            # Use a payload root containing only the app, not ZIP resource forks.
            payload = work / (architecture + "-payload")
            payload.mkdir()
            subprocess.run(["/usr/bin/ditto", str(app), str(payload / APP_NAME)], check=True)
            subprocess.run(["/usr/bin/pkgbuild", "--root", str(payload), "--install-location", "/Applications",
                            "--identifier", PACKAGE_PREFIX + "." + architecture, "--version", version,
                            "--component-plist", str(component_plist), "--scripts", str(scripts),
                            str(components / (architecture + ".pkg"))], check=True)
            report["source_apps"].append({"architecture": architecture, "app_archive_sha256": digest(archive),
                                           "binary_count": audit["mach_o_files"],
                                           "highest_binary_minimum_os": audit["highest_minimum_os"]})
        distribution = work / "Distribution.xml"
        distribution.write_bytes(distribution_xml(version))
        resources = work / "resources"
        resources.mkdir()
        (resources / "welcome.html").write_text(
            "<html><meta charset='utf-8'><h2>中犇微信助手</h2><p>支持macOS 12及以上。"
            "安装程序会自动识别Apple芯片或Intel芯片，只安装对应的助手。</p>"
            "<p>请先退出旧版助手。安装位置为“应用程序”，安装需要电脑管理员授权。</p>"
            "<p>当前为预览测试版，尚未进行Apple公证。</p></html>", encoding="utf-8")
        (resources / "conclusion.html").write_text(
            "<html><meta charset='utf-8'><h2>安装完成</h2><p>请从“应用程序”打开中犇微信助手。</p>"
            "<p>首次使用请授权辅助功能与屏幕录制，然后重新打开助手。</p>"
            "<p>配置、客户名称缓存和日志保存在用户的Library/Application Support/ZhongBenAssistant目录。</p></html>",
            encoding="utf-8")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        base = "中犇微信助手-%s-Mac通用安装包-macOS12以上-%s" % (version, stamp)
        package = output / (base + ".pkg")
        subprocess.run(["/usr/bin/productbuild", "--distribution", str(distribution),
                        "--resources", str(resources), "--package-path", str(components), str(package)], check=True)
        report["file"] = package.name
        report["bytes"] = package.stat().st_size
        report["sha256"] = digest(package)
        (output / (base + ".sha256.txt")).write_text(report["sha256"] + "  " + package.name + "\n", encoding="utf-8")
        (output / (base + ".构建校验.json")).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (output / (base + ".安装说明.txt")).write_text(
            "一个安装包同时适用于Apple芯片和Intel的Mac；系统要求macOS12及以上。\n"
            "先退出旧版助手，再打开.pkg，按照安装向导操作，安装位置为“应用程序”。\n"
            "安装需要电脑管理员授权。旧版用户配置、客户名称缓存和日志会保留。\n"
            "当前是未签名、未公证的预览测试安装包；macOS12/13真实微信功能仍需实机验证。\n"
            "两套原生程序由安装器自动选择，安装后只有一份对应芯片的助手。\n", encoding="utf-8")
        print("Combined installer: " + str(package))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=PROJECT / "release")
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--version", default="2.0.3")
    args = parser.parse_args()
    build(args.inputs, args.output, args.source_run, args.version)


if __name__ == "__main__":
    main()
