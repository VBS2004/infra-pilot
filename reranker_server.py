"""Run the bundled reranker from a checkout: `python reranker_server.py`."""
import os
import sys

try:
    import terra_pilot  # noqa: F401
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import uvicorn

from terra_pilot.server.reranker_server import app, HOST, PORT

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
