"""`python -m server`: the same entry point as `python server/server.py`.

Both end in server.server.main(), which hands BrowserPlugServer to lifecycle.run().
"""
import os
import sys

# Run as a plain script (`python server/__main__.py`), sys.path[0] is server/, whose
# server.py would shadow the `server` package. Drop it and put the repo root first.
if __package__ in (None, ""):
    _HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path
                   if os.path.normcase(os.path.abspath(p or os.curdir)) != os.path.normcase(_HERE)]
    sys.path.insert(0, os.path.dirname(_HERE))

from server.server import main  # noqa: E402

if __name__ == "__main__":
    main()
