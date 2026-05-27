"""Structural OpenAPI drift gate.

The committed `docs/openapi.yaml` is the human-facing contract: it documents
the full set of responses (success + 4xx + 5xx + 422), parameter shapes,
and component schemas. FastAPI's auto-emitted `app.openapi()` won't be
byte-identical — it doesn't know about HTTPException raises and uses its
own conventions for security scheme names.

This test asserts the two agree on the things that matter for catching
drift:

  1. Same set of paths.
  2. Same HTTP methods per path.
  3. Same set of response status codes documented per (path, method) — the
     committed spec is the floor; runtime must implement every status code
     the spec advertises (extras like 422 from FastAPI are allowed).
  4. Same set of `components.schemas` names (per the committed spec — the
     runtime is allowed to have extras like the auto-extracted `Status`
     enum, but it must not be MISSING any committed schema OTHER than
     `ErrorResponse` which lives only in error-response refs).
  5. Each path's request params (path + query) match the committed spec by
     name + required flag.

If you add a new route or rename a parameter, this test fails. If you add
documentation (new tag, new server, refined description) to the committed
spec only, it does NOT fail.
"""

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMMITTED_SPEC_PATH = REPO_ROOT / "docs" / "openapi.yaml"


def _params_set(spec_for_op):
    """Return {(name, in_, required)} for params on an operation, resolving
    $refs against the spec's components.parameters."""
    out = set()
    for p in spec_for_op.get("parameters", []) or []:
        if "$ref" in p:
            # Path-style $ref into components.parameters
            ref = p["$ref"].split("/")[-1]
            p = _params_set._committed_params[ref]  # type: ignore[attr-defined]
        out.add((p.get("name"), p.get("in"), bool(p.get("required", False))))
    return out


def test_paths_match(app):
    runtime = app.openapi()
    committed = yaml.safe_load(COMMITTED_SPEC_PATH.read_text())

    assert set(runtime["paths"].keys()) == set(committed["paths"].keys()), (
        f"paths differ:\n  runtime-only: {set(runtime['paths']) - set(committed['paths'])}\n"
        f"  committed-only: {set(committed['paths']) - set(runtime['paths'])}"
    )


def test_methods_match_per_path(app):
    runtime = app.openapi()
    committed = yaml.safe_load(COMMITTED_SPEC_PATH.read_text())
    for path, ops in committed["paths"].items():
        runtime_ops = runtime["paths"].get(path, {})
        committed_methods = {m for m in ops if m in {"get", "post", "put", "patch", "delete"}}
        runtime_methods = {m for m in runtime_ops if m in {"get", "post", "put", "patch", "delete"}}
        assert committed_methods == runtime_methods, (
            f"method mismatch at {path}: runtime={runtime_methods}, committed={committed_methods}"
        )


def test_committed_response_codes_are_implemented(app):
    """Every documented response status code must exist on the runtime route."""
    runtime = app.openapi()
    committed = yaml.safe_load(COMMITTED_SPEC_PATH.read_text())
    failures = []
    for path, ops in committed["paths"].items():
        for method, op in ops.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            committed_codes = set(op.get("responses", {}).keys())
            runtime_codes = set(
                runtime["paths"][path][method].get("responses", {}).keys()
            )
            missing = committed_codes - runtime_codes
            if missing:
                failures.append(f"{method.upper()} {path}: committed advertises {sorted(missing)} but runtime is missing them")
    assert not failures, "documented response codes not implemented:\n  " + "\n  ".join(failures)


def test_request_params_match_per_route(app):
    runtime = app.openapi()
    committed = yaml.safe_load(COMMITTED_SPEC_PATH.read_text())

    committed_params_lookup = (committed.get("components", {}) or {}).get("parameters", {}) or {}
    _params_set._committed_params = committed_params_lookup  # type: ignore[attr-defined]

    failures = []
    for path, ops in committed["paths"].items():
        for method, op in ops.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            committed_params = _params_set(op)
            runtime_params = {
                (p.get("name"), p.get("in"), bool(p.get("required", False)))
                for p in runtime["paths"][path][method].get("parameters", []) or []
            }
            if committed_params != runtime_params:
                failures.append(
                    f"{method.upper()} {path}:\n"
                    f"    committed-only: {sorted(committed_params - runtime_params)}\n"
                    f"    runtime-only:   {sorted(runtime_params - committed_params)}"
                )
    assert not failures, "request parameter drift:\n  " + "\n  ".join(failures)


def test_committed_schemas_have_runtime_implementations(app):
    """Every committed schema name must be present in the runtime, EXCEPT
    error-only schemas (ErrorResponse) which FastAPI doesn't emit because
    we raise HTTPException with dict detail instead of declaring response
    models for 4xx/5xx."""
    runtime = app.openapi()
    committed = yaml.safe_load(COMMITTED_SPEC_PATH.read_text())

    runtime_schemas = set(runtime["components"]["schemas"].keys())
    committed_schemas = set(committed["components"]["schemas"].keys())

    # ErrorResponse is referenced ONLY by 4xx/5xx responses in the committed
    # spec; FastAPI doesn't surface it as a component because errors are
    # raised, not returned. Skipping that one schema is OK.
    missing = committed_schemas - runtime_schemas - {"ErrorResponse"}
    assert not missing, f"runtime missing committed schemas: {sorted(missing)}"
