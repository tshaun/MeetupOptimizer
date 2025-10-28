from fastapi import FastAPI
from src.routers import route_router  # <- absolute import
from fastapi.middleware.cors import CORSMiddleware



app = FastAPI(title="Smart Route Planner API")
app.include_router(route_router.router)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # your React app dev URL
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"message": "API is running"}
