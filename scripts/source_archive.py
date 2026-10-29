"""Create a source-only transfer ZIP on Windows; this is not a Mac installer."""
import argparse
from datetime import datetime
from pathlib import Path
import zipfile

EXCLUDED_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "build", "dist",
                 "release", "下载的安装包", "源码包", ".idea", ".vscode", ".test-temp"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log", ".zip", ".dmg"}


def source_files(root):
    root = Path(root)
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in EXCLUDED_DIRS or part.endswith(".app") for part in relative.parts[:-1]):
            continue
        if path.name.startswith(".env") and path.name != ".env.example":
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES or path.name == "wechat_friend_cache.json":
            continue
        yield path, relative


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output or root / "源码包" / ("中犇微信助手-Mac源码-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".zip")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, relative in source_files(root):
            if path.resolve() != output.resolve():
                archive.write(path, Path(root.name) / relative)
    print(output)
    print("这是源码压缩包，不是 Mac 安装包。真实 .dmg 需要 macOS 构建任务。")


if __name__ == "__main__":
    main()
