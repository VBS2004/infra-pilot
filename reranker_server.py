import os
import sys
sys.path.insert(0, os.path.abspath("src"))
from terra_pilot.server.reranker_server import app
import uvicorn

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
