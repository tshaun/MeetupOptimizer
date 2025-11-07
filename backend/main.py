from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Import routers with a fallback so the app can be started both when the
# current working directory is the repository root (recommended) and when
# uvicorn is invoked from the `backend/` folder.
try:
    # Normal case: package import works when running from repo root
    from backend.routers import route_router, venues_router
except ModuleNotFoundError:
    # Fallback: add project root to sys.path and retry
    import sys
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from backend.routers import route_router, venues_router



app = FastAPI(title="Smart Route Planner API")
app.include_router(route_router.router)
app.include_router(venues_router.router)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # your React app dev URL
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"message": "API is running"}
