# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the HROne engineering assignment. The service reads `MONGO_URI` and `MONGO_DB` from environment variables (a local `.env` may be used but must not be committed), creates indexes idempotently at startup, and exposes the endpoints in `openapi.yaml`.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'
python sample_seed.py
uvicorn app.main:app --port 8000
```

MongoDB 6.0+ is required. For a local test database, start MongoDB separately. The seed script replaces the contents of the two sample collections, so use it only against a disposable development database.

Open `http://localhost:8000/docs` to inspect and exercise the API. `GET /health` returns 200 only when MongoDB responds.

## Files

- `app/main.py` — API implementation.
- `requirements.txt` — runtime dependencies.
- `REVIEW.md` — review status and missing-starter limitation.
- `DECISIONS.md` — implementation decisions.
- `DATA_MODEL.md`, `openapi.yaml`, `sample_seed.py`, and sample JSON — supplied assignment contract and local sample data.

## Before submission

The original starter `app/main.py` was not present in the provided files. Consequently, `REVIEW.md` is not a verified list of defects in that starter. Run the full contract against MongoDB 7.0 and complete that review before treating this as submission-ready. Do not commit database credentials, `.env`, virtual environments, or secrets.
