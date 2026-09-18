"""Local cross-encoder reranker server, OpenAI-rerank-API-compatible.

Model, host and port are all configurable via env vars so you can point
this at any sentence-transformers CrossEncoder model, not just the
bge-reranker-base default:

    RERANK_MODEL=BAAI/bge-reranker-large python reranker_server.py
    RERANK_MODEL=mixedbread-ai/mxbai-rerank-base-v1 RERANK_PORT=9000 python reranker_server.py
"""
import os

from fastapi import FastAPI
from pydantic import BaseModel
from typing import List
from sentence_transformers import CrossEncoder
import uvicorn

MODEL_NAME = os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-base")
HOST = os.environ.get("RERANK_HOST", "0.0.0.0")
PORT = int(os.environ.get("RERANK_PORT", "8080"))

app = FastAPI()

print(f"Loading {MODEL_NAME}...")
model = CrossEncoder(MODEL_NAME)
print("Reranker ready!")

class RerankRequest(BaseModel):
    query: str
    documents: List[str]
    model: str = ""

@app.get("/v1/models")
def get_models():
    return {"data": [{"id": MODEL_NAME}]}

@app.post("/v1/rerank")
def rerank(req: RerankRequest):
    if not req.documents:
        return {"results": []}

    # CrossEncoder scores query-document pairs
    pairs = [[req.query, doc] for doc in req.documents]
    scores = model.predict(pairs)

    results = []
    for i, score in enumerate(scores):
        results.append({"index": i, "relevance_score": float(score)})

    # Sort by score descending
    results.sort(key=lambda x: x["relevance_score"], reverse=True)
    return {"results": results}

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
