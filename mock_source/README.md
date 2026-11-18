# Local announcement source used by the delivery smoke check.
# Run:  python3 -m uvicorn mock_source.server:app --port 8001
# API:  DATABASE_PATH=./data/smoke.db SOURCE_BASE_URL=http://127.0.0.1:8001 \
#       python3 -m uvicorn app.main:app --port 8000
