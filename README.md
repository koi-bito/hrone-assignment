# Employee Attendance & Analytics API

FastAPI + MongoDB service for employee attendance tracking, shift calculations, and departmental analytics. Built as part of the HROne engineering assignment.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (local Docker container or free Atlas cluster both work; tested with MongoDB 7.0)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

Set these via environment variables or a local `.env` file:

```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Real environment variables always take priority over `.env`.

## Run

```bash
uvicorn app.main:app --port 8000
```

All indexes are created idempotently at startup. No manual database setup needed.

## Verify

```bash
curl http://localhost:8000/health
# {"status": "ok"}
```

## Notes

- All endpoints follow the OpenAPI contract exactly (paths, status codes, field names).
- Seeded data is read directly from MongoDB — no data loading step required.
- Do not commit `.env` or any secrets.
