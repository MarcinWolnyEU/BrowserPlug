import argparse
import json
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description="Configure BrowserPlug's local Codex plugin.")
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--uv", type=Path)
    parser.add_argument("--python", type=Path, help="Existing BrowserPlug venv interpreter")
    args = parser.parse_args()
    launcher = args.checkout.resolve() / "server" / "launcher.py"
    if not launcher.is_file():
        parser.error(f"BrowserPlug launcher not found: {launcher}")
    if args.uv and args.python:
        parser.error("Choose --uv or --python, not both")
    if args.python:
        executable = args.python.resolve()
        server = {
            "type": "stdio", "command": str(executable), "args": [str(launcher)],
            "env": {"BROWSERPLUG_SERVER_PYTHON": str(executable)},
        }
    else:
        found = args.uv or shutil.which("uv")
        if not found:
            parser.error("Install uv or pass --python with your BrowserPlug venv interpreter")
        executable = Path(found).resolve()
        server = {
            "type": "stdio", "command": str(executable),
            "args": ["run", "--script", str(launcher)],
        }
    if not executable.is_file():
        parser.error(f"Executable not found: {executable}")
    destination = Path(__file__).resolve().parent.parent / ".mcp.json"
    payload = {
        "mcpServers": {"browserplug": server},
    }
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Configured {destination}")


if __name__ == "__main__":
    main()
