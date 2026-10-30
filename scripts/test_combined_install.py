"""Install/reinstall the combined .pkg on an ephemeral macOS CI runner only."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import plistlib
import subprocess
import sys

from verify_macos_bundle import audit_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != "darwin" or not __import__("os").environ.get("GITHUB_ACTIONS"):
        raise RuntimeError("This installs into /Applications: only run on an ephemeral GitHub macOS runner")
    architecture = platform.machine()
    assert architecture in ("arm64", "x86_64")
    packages = list(args.release.glob("*.pkg"))
    assert len(packages) == 1, "Expected one combined installer"
    package = packages[0]
    manifest = json.loads(next(args.release.glob("*.构建校验.json")).read_text(encoding="utf-8"))
    with package.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == manifest["sha256"]
    version = manifest["version"]
    selected_xml = subprocess.check_output([
        "/usr/sbin/installer", "-showChoiceChangesXML", "-pkg", str(package.resolve()), "-target", "/"])
    selections = plistlib.loads(selected_xml)
    selected = [entry["choiceIdentifier"] for entry in selections
                if entry.get("choiceAttribute") == "selected" and entry.get("attributeSetting")]
    assert selected == [architecture], "Installer selected wrong components: %r" % selected
    for _ in range(2):
        subprocess.run(["sudo", "/usr/sbin/installer", "-pkg", str(package.resolve()), "-target", "/"],
                       check=True, timeout=300)
    app = Path("/Applications/中犇微信助手.app")
    audit = audit_bundle(app, architecture, "12.0", version)
    assert audit["ok"], audit["violations"]
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    prefix = "net.saizhuge.zhongben.wechatassistant."
    receipt = plistlib.loads(subprocess.check_output(["/usr/sbin/pkgutil", "--pkg-info-plist", prefix + architecture]))
    assert receipt["pkg-version"] == version
    other = "x86_64" if architecture == "arm64" else "arm64"
    unselected = subprocess.run(["/usr/sbin/pkgutil", "--pkg-info", prefix + other], capture_output=True)
    assert unselected.returncode != 0, "Both architecture payloads were installed"
    smoke_path = args.output.with_name(args.output.stem + ".启动自检.json").resolve()
    smoke_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(app / "Contents/MacOS/ZhongBenAssistant"), "--smoke-test", str(smoke_path)],
                   check=True, timeout=75)
    smoke = json.loads(smoke_path.read_text(encoding="utf-8"))
    assert smoke["ok"] and smoke["frozen"] and smoke["webengine"] and smoke["arch"] == architecture
    report = {"ok": True, "architecture": architecture, "os": platform.mac_ver()[0],
              "version": version, "selected_components": selected, "installations": 2,
              "only_native_component_installed": True, "binary_count": audit["mach_o_files"],
              "highest_binary_minimum_os": audit["highest_minimum_os"], "smoke": smoke,
              "scope": "Real package install/reinstall and app startup on CI; no real WeChat verification."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
