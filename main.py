"""Entry point. --check is safe to run on Windows without Qt or macOS APIs."""
import ast
from pathlib import Path
import sys


def main():
    if "--check" in sys.argv:
        root = Path(__file__).resolve().parent
        sources = [root / "main.py", *sorted((root / "mac_assistant").rglob("*.py"))]
        for source in sources:
            ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
        print("Python syntax checked: %d files. This is not a macOS runtime test." % len(sources))
        return 0
    if sys.platform != "darwin":
        print("此入口用于 macOS；Windows 上可运行 python main.py --check 检查源码。", file=sys.stderr)
        return 2
    if "--smoke-test" in sys.argv:
        import argparse
        parser = argparse.ArgumentParser(description="Offline native bundle startup check")
        parser.add_argument("--smoke-test", required=True, type=Path, metavar="OUTPUT.json")
        args = parser.parse_args()
        from mac_assistant.smoke_test import run as run_smoke_test
        return run_smoke_test(args.smoke_test)
    from mac_assistant.app import run
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
