# Developer convenience targets. CI invokes the same commands directly.
#
# Tools required on PATH:
#   - buf            (proto codegen + lint)              .woodpecker/pr.yml step `buf-lint` / `proto-codegen-verify`
#   - datamodel-codegen  (OpenAPI -> Pydantic v2)        .woodpecker/pr.yml step `pydantic-codegen-verify`
#   - spectral       (OpenAPI lint)                       .woodpecker/pr.yml step `openapi-lint`
#   - python (3.12)  (running app + tests)
#   - docker         (only for ephemeral pg in tests)

.PHONY: proto codegen openapi-lint test verify-codegen-clean

BUF ?= buf
DATAMODEL_CODEGEN ?= datamodel-codegen
SPECTRAL ?= spectral

# Regenerate gRPC Python stubs from data_svc/grpc_server/proto/*.proto.
proto:
	$(BUF) generate

# Regenerate Pydantic v2 models from docs/openapi.yaml.
# Output is committed (data_svc/rest/models/_generated.py); the verify-codegen-clean
# target asserts no drift.
codegen:
	$(DATAMODEL_CODEGEN) \
		--input docs/openapi.yaml \
		--input-file-type openapi \
		--output data_svc/rest/models/_generated.py \
		--output-model-type pydantic_v2.BaseModel \
		--target-python-version 3.12 \
		--use-field-description \
		--field-include-all-keys \
		--use-schema-description \
		--snake-case-field \
		--use-default \
		--use-double-quotes \
		--allow-population-by-field-name \
		--disable-timestamp

# Lint the OpenAPI spec against the standard OAS ruleset.
openapi-lint:
	$(SPECTRAL) lint --ruleset spectral:oas docs/openapi.yaml

# Run all tests against an ephemeral Postgres (see tests/conftest.py).
test:
	pytest -q tests/

# Run codegens and fail if they produced any diff. Used by CI as the
# drift gate; mirrors the .woodpecker/pr.yml steps.
verify-codegen-clean: proto codegen
	@git diff --exit-code data_svc/grpc_server/proto/ data_svc/rest/models/_generated.py \
		|| { echo "codegen produced uncommitted diff — run 'make proto codegen' and commit"; exit 1; }
