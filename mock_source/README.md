# Local announcement source used by the delivery smoke check.
# Run:  python3 -m uvicorn mock_source.server:app --port 8001
# API:  DATABASE_PATH=./data/smoke.db SOURCE_BASE_URL=http://127.0.0.1:8001 \
#       python3 -m uvicorn app.main:app --port 8000
#
# Pages are served from immutable snapshots:
#   page=1                       -> starts a page set, returns snapshot_id
#   page=N&snapshot=<id>         -> continues that exact page set
#   page=N (N>1, no snapshot)    -> 400
#   unknown/expired snapshot id  -> 410 (e.g. after a source restart)
# POST /scenario freezes a new snapshot for subsequent page-1 requests while
# in-flight page sets keep reading their original snapshot.

