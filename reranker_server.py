import os
import sys
sys.path.insert(0, os.path.abspath("src"))
from terra_pilot.server.reranker_server import app, HOST, PORT
import uvicorn

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
