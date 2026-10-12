"""Predefined page_eval scripts: run a vetted script or executable, use its stdout as the JS.

Only reachable when $BROWSERPLUG_EXT_SCRIPTS is set in the SERVER's environment; without
it `page_eval` does not advertise `script_name` / `script_args` at all. The rules and the
bundled examples are in docs/predefined-scripts.md.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
from typing import NamedTuple


from server.tools.tool_outcome import fail, invalid


# cmd.exe re-parses its command line, so no argument quoting is safe for a batch file
# (`"&echo x` runs `echo`). The only sound rule is to never start one.
_BATCH_EXTENSIONS = ('.bat', '.cmd')
BATCH_REFUSED = ("batch files (.bat/.cmd) are not supported: cmd.exe re-parses arguments, "
                 "so script_args could run arbitrary commands. Use a .py script, an .exe, "
                 "or a script with a shebang on POSIX")


# Windows starts a file by extension: CreateProcess runs a PE image and nothing else, and
# os.access(X_OK) is true for any existing file there. A .js in the directory is data unless
# something documents an interpreter for it, and only Python has one (below).
_WINDOWS_RUNNABLE = ('.exe', '.com')

# A .py script runs on every OS under the server's own interpreter, execute bit or not, so
# one set of generators works wherever the server does. `-I` (isolated) keeps the scripts
# directory off sys.path, so a stray json.py there cannot shadow the standard library, and
# ignores PYTHON* variables and the user site; `-X utf8` makes print() UTF-8 whatever the
# console code page is. The argv is still a list and no shell is involved.
_PYTHON_SUFFIX = '.py'
_PYTHON_FLAGS = ('-I', '-X', 'utf8')


class ScriptResult(NamedTuple):
    """Outcome of the runner. `ok` is the verdict; never infer it from `text`, because
    generated JavaScript can start with any word, including "Error"."""
    ok: bool
    text: str  # the generated JavaScript when ok, else the message for the agent


def _invalid(message: str) -> ScriptResult:
    return ScriptResult(False, invalid(message))


def _failed(message: str) -> ScriptResult:
    return ScriptResult(False, fail(message))


def _locate_script(scripts_dir: str, script_name: str):
    """Blocking half of the lookup: (path, None) for a runnable script, else (None, message)."""
    scripts_dir = os.path.abspath(scripts_dir)
    script_path = os.path.abspath(os.path.join(scripts_dir, script_name))

    if not script_path.startswith(scripts_dir + os.sep) and script_path != scripts_dir:
        return None, f"Error: Script path '{script_name}' escapes the allowed directory"

    if not os.path.exists(script_path):
        return None, f"Error: Script '{script_name}' not found in {scripts_dir}"

    if script_path.lower().endswith(_PYTHON_SUFFIX):
        runnable = os.path.isfile(script_path)
    elif os.name == 'nt':
        runnable = script_path.lower().endswith(_WINDOWS_RUNNABLE)
    else:
        runnable = os.access(script_path, os.X_OK)
    if not runnable:
        return None, (f"Error: Script '{script_name}' is not executable"
                      + (" (Windows runs only .py, .exe or .com files; anything else is data)"
                         if os.name == 'nt' else ""))
    return script_path, None


async def run_predefined_script(script_name: str, script_args: str) -> ScriptResult:
    """Run a vetted external script from $BROWSERPLUG_EXT_SCRIPTS; its stdout is JS.

    Returns ScriptResult(ok, text): the generated JavaScript, or the failure message
    (already recorded as a tool failure). The subprocess runs in a worker thread so a slow
    script cannot stall the event loop (every client's calls and
    /health share it); `subprocess.run` kills the child when its 30 s timeout expires.
    """
    scripts_dir = os.environ.get('BROWSERPLUG_EXT_SCRIPTS')
    if not scripts_dir:
        return _invalid("Error: BROWSERPLUG_EXT_SCRIPTS is not set in the SERVER's environment, "
                        "so predefined scripts are disabled (setting it in the MCP client's "
                        "environment is not enough; the shared server process needs it)")
    if not script_name or '..' in script_name or '/' in script_name or '\\' in script_name:
        return _invalid(f"Error: Invalid script name '{script_name}'. Script names cannot contain path separators or '..' sequences")

    if not re.match(r'^[a-zA-Z0-9._-]+$', script_name):
        return _invalid(f"Error: Invalid script name '{script_name}'. Only alphanumeric characters, underscore, dash, and dot are allowed")

    # Windows drops trailing dots and spaces from a file name, so "gen.bat." starts gen.bat.
    if script_name.rstrip('. ').lower().endswith(_BATCH_EXTENSIONS):
        return _invalid(f"Error: Script '{script_name}' refused: {BATCH_REFUSED}")

    # The path checks touch the filesystem (abspath, exists, access), so they run off the loop.
    script_path, problem = await asyncio.to_thread(_locate_script, scripts_dir, script_name)
    if problem:
        return _invalid(problem)

    try:
        if script_args.strip() == "":
            args_list = []
        else:
            args_list = json.loads(script_args)
            if not isinstance(args_list, list):
                return _invalid(f"Error: script_args must be a JSON array of strings or empty string, got: {type(args_list).__name__}")
            for i, arg in enumerate(args_list):
                if not isinstance(arg, str):
                    return _invalid(f"Error: All arguments must be strings. Argument {i} is {type(arg).__name__}: {arg}")
                if any(c in arg for c in '\x00\r\n'):
                    return _invalid(f"Error: Argument {i} contains a NUL or newline character, which is not allowed in script arguments")
    except json.JSONDecodeError as e:
        return _invalid(f"Error: Invalid JSON in script_args: {e}")

    command = [script_path]
    if script_path.lower().endswith(_PYTHON_SUFFIX):
        command = [sys.executable, *_PYTHON_FLAGS, script_path]
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            command + args_list,
            capture_output=True,
            # The JS is Unicode text for a browser. Decoding with the locale's code page
            # (cp1252 on Windows) would mangle every non-ASCII character a script emits.
            encoding='utf-8',
            errors='replace',
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return _failed(f"Error: Script '{script_name}' failed with exit code {result.returncode}. stderr: {result.stderr}")
        javascript_code = result.stdout.strip()
        if not javascript_code:
            return _failed(f"Error: Script '{script_name}' produced no output")
        return ScriptResult(True, javascript_code)
    except subprocess.TimeoutExpired:
        return _failed(f"Error: Script '{script_name}' timed out after 30 seconds")
    except subprocess.SubprocessError as e:
        return _failed(f"Error executing script '{script_name}': {e}")
    except Exception as e:
        return _failed(f"Unexpected error running script '{script_name}': {e}")