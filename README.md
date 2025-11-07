# MeetupOptimizer

## Run & development (Windows / PowerShell)

Minimal steps to get the project running locally (PowerShell):

1. Create and activate a Python virtual environment (from repo root):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install Python dependencies:

```powershell
pip install -r requirements.txt
```

3. Start the backend (FastAPI) server (recommended: run from the repository root):

```powershell
# from repository root (recommended)
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload
```

If you prefer to run from inside the `backend/` directory, make sure to reference the package import (or set PYTHONPATH to the project root). For example (from repo root):

```powershell
cd .\backend
# ensure the package import resolves by invoking uvicorn with the package module
..\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload
```
4. Start the frontend (React):

```powershell
cd ..\frontend
npm install
npm start
```

Notes and troubleshooting
- If the backend fails to import `fastapi` or `pydantic`, ensure the virtualenv is activated and `pip install -r requirements.txt` completed successfully.
- Data files (JSON/CSV) are currently in `src/` and the backend routers read from `src/`. If you want the data moved to `data/`, I can do that in a follow-up change and update the router data root.
- The frontend `npm install` produced some audit warnings (some transitive deps are deprecated); these are common in React projects. Run `npm audit` and `npm audit fix` if you want to address vulnerabilities.