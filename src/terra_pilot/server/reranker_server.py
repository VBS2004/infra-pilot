from fastapi import FastAPI
from pydantic import BaseModel
from typing import List
from sentence_transformers import CrossEncoder
import uvicorn

app = FastAPI()

# Load the lightweight reranker into memory
print("Loading BAAI/bge-reranker-base...")
model = CrossEncoder("BAAI/bge-reranker-base")
print("Reranker ready!")

class RerankRequest(BaseModel):
    query: str
    documents: List[str]
    model: str = ""

@app.get("/v1/models")
def get_models():
    return {"data": [{"id": "BAAI/bge-reranker-base"}]}

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
    uvicorn.run(app, host="0.0.0.0", port=8080)
