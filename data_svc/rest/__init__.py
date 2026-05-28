"""REST gateway package.

Two adapter layers depending on the route:

- /v1/quote and /healthz are **thin gateways over BarService gRPC** (spec
  §Motivation): they hold a long-lived gRPC channel via
  data_svc.rest.grpc_client.BarServiceClient and translate proto messages
  to/from the Pydantic models.

- /v1/historical, /v1/search, /v1/profile read Postgres directly via
  data_svc.db.AssetsRepo + BarCache. The existing BarService proto has no
  RPCs for range queries, asset search, or profile lookups — expanding
  the proto to cover Phase 2 is out of scope here.

Entry points (both work):
  - `python -m data_svc.rest`
  - `python -m data_svc.rest_server` (alias; spec §Deployment)
"""
