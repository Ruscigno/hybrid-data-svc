# `hybrid-data-svc` — Phase 3: Catalog Management

**Data do release:** 2026-05-29
**Commit produção:** `f1c34c9` (PR #12)
**OpenAPI:** `0.1.0` (extensão aditiva, sem breaking changes)

---

## TL;DR

A partir de agora dá pra **cadastrar e consultar símbolos do catálogo via REST**, sem editar `assets.yaml`, sem editar `FEEDS`, sem PR, sem deploy. O writer (`data-svc`) descobre os novos feeds em ≤ 30 segundos e começa a coletar bars automaticamente.

- Novo `GET /v1/assets` — lista paginada do catálogo com status de polling, `addedAt` e `lastBarTs` por ativo.
- Novo `POST /v1/assets` — onboarding em runtime; cria o ativo + uma linha de feed por timeframe pedido.
- Nova env `REST_ADMIN_TOKEN` para gating das rotas mutadoras (POST). Reads continuam usando `REST_AUTH_TOKEN`.
- Nova migration `003_feeds_and_added_at.sql` — tabela `feeds` (runtime SoT do que polar) + coluna `assets.added_at`.

Nenhum endpoint existente mudou.

---

## O que mudou

### 1. `GET /v1/assets` — listar o catálogo com status

Antes não dava pra listar todos os símbolos cadastrados. Agora dá, com filtros, paginação por cursor, e **status agregado** (`active` / `pending` / `inactive`) calculado em tempo real a partir da nova tabela `feeds`.

| Query param | Tipo | Default | Descrição |
|---|---|---|---|
| `exchange` | string | — | filtro exato (ex.: `BINANCE`) |
| `asset_class` | enum | — | `EQUITY` \| `CRYPTO` \| `ETF` \| `FUND` |
| `q` | string | — | substring case-insensitive contra `symbol` + `name` |
| `cursor` | string | — | último `symbol` da página anterior |
| `limit` | int | 100 | 1–500 |

Cada item da resposta:

```json
{
  "asset": { "symbol": "BINANCE:BTCUSDT", "name": "...", "exchange": "...", "currency": "USD", "assetClass": "CRYPTO", ... },
  "status": "active",
  "addedAt": 1779996993,
  "lastBarTs": 1780087500
}
```

**Regra de agregação do `status`:** se *qualquer* feed do ativo está `active` → `active`; senão se algum está `pending` → `pending`; senão `inactive`. Ativos sem feeds configurados aparecem como `pending`.

### 2. `POST /v1/assets` — onboarding em runtime

Em vez de:

1. editar `data_svc/assets.yaml`,
2. editar `FEEDS` no `.env`,
3. abrir PR, revisar, mergear,
4. acionar o deploy,
5. esperar o restart,

agora basta um POST:

```bash
curl -X POST \
  -H "Authorization: Bearer $REST_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol":         "BINANCE:DOGEUSDT",
    "storageSymbol":  "DOGE/USDT:USDT",
    "name":           "Dogecoin / Tether USD",
    "exchange":       "BINANCE",
    "currency":       "USD",
    "assetClass":     "CRYPTO",
    "assetSubClass":  "PERP",
    "timeframes":     ["15m", "1h"],
    "tvSymbol":       "BINANCE:DOGEUSDTPERP"
  }' \
  http://localhost:8003/v1/assets
```

Resposta `201 Created`:

```json
{
  "asset": { "asset": { "symbol": "BINANCE:DOGEUSDT", ... }, "status": "pending", "addedAt": 1780090000, "lastBarTs": 0 },
  "created": true,
  "pollEtaSeconds": 30
}
```

**Defaults convenientes:**
- `timeframes` omitido → `["1h"]`.
- `tvSymbol` omitido → usa `symbol`.

**Idempotência:** tentar criar o mesmo `symbol` retorna `409 Conflict` com o registro existente no body — nada é sobrescrito.

```json
{
  "error":   "already_exists",
  "message": "BINANCE:DOGEUSDT already exists in the catalog.",
  "existing": { ... AssetWithStatus do registro atual ... }
}
```

### 3. Polling se ativa sozinho

A diferença arquitetural mais importante é invisível na API: o writer (`data-svc`) **lê os alvos de polling do Postgres no topo de cada ciclo**, em vez de ler do env apenas no startup. Consequências:

- Inserir uma linha em `feeds` (via `POST /v1/assets`) faz o writer começar a coletar no próximo ciclo (~30s típico).
- Quando a primeira inserção de barra acontece, o writer faz `UPDATE feeds SET status='active' WHERE ... AND status='pending'`. Idempotente; vira no-op em rodadas subsequentes.
- `FEEDS` no `.env` continua sendo lido **uma vez no startup** para fazer seed inicial da tabela `feeds` (idempotente, no-op em runs subsequentes). É o caminho greenfield; pra rodada do dia-a-dia o caminho é a API.

---

## Configuração — env vars

Uma nova env var. Nenhuma existente mudou de comportamento.

| Env var | Obrigatória? | Default | Descrição |
|---|---|---|---|
| `REST_ADMIN_TOKEN` | não | _(vazio)_ | Token bearer exigido pelo `POST /v1/assets`. Se vazio, cai pra `REST_AUTH_TOKEN`; se ambos vazios, modo aberto (igual aos reads). |

**Recomendação de produção:** sempre configurar `REST_ADMIN_TOKEN` distinto do `REST_AUTH_TOKEN`, para que tokens distribuídos pra leitura (ex.: Ghostfolio, dashboards) não consigam escrever no catálogo.

---

## Compatibilidade

**Zero breaking changes.** Tudo o que já funcionava continua funcionando:

- `GET /v1/quote/{symbol}`, `GET /v1/historical/{symbol}`, `GET /v1/search`, `GET /v1/profile/{symbol}`: contratos inalterados.
- Schema `Asset` no OpenAPI permanece sem campos novos — `status`, `addedAt` e `lastBarTs` vivem no novo `AssetWithStatus`, que aparece só nas rotas de catálogo.
- `assets.yaml` continua sendo o **seed greenfield** do catálogo (fonte de verdade pra primeira inicialização em ambientes novos).
- `FEEDS` env continua sendo o **seed greenfield** da tabela `feeds`.

---

## Migration & deploy

A migration `003_feeds_and_added_at.sql` é **idempotente** (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`, backfill condicional, `SET NOT NULL`).

**Em ambientes greenfield** (volume `pgdata` ainda não inicializado): nada a fazer — o `docker-entrypoint-initdb.d` aplica automaticamente.

**Em ambientes com `pgdata` existente** (caso da prod atual): aplicar manualmente **antes** do redeploy:

```bash
docker compose exec -T postgres psql -U datasvc -d datasvc \
  -f /docker-entrypoint-initdb.d/003_feeds_and_added_at.sql
```

Saída esperada:
```
CREATE TABLE
CREATE INDEX
ALTER TABLE
UPDATE <N>
ALTER TABLE
```

A linha `UPDATE <N>` reflete quantas linhas de `assets` receberam backfill de `added_at = updated_at`. Em produção foram 5.

Depois disso, gerar e gravar o token admin (sem ecoar no terminal):

```bash
{ printf '\nREST_ADMIN_TOKEN=%s\n' "$(openssl rand -hex 32)"; } >> .env
```

E acionar o deploy via Woodpecker (`DEPLOY_MAIN=true`).

---

## Estado em produção (verificado em 2026-05-29 21:00 UTC)

| Verificação | Resultado |
|---|---|
| Pipeline de deploy | `guard ✅ deploy ✅ healthz ✅` (~30 s) |
| Containers | data-svc + bar-grpc + bar-rest rebuildados, todos healthy |
| Migration | `feeds` criada, 5 linhas de `assets.added_at` com backfill aplicado |
| Seed inicial de `feeds` | 16 linhas (BTC em 4 timeframes — 15m/30m/1h/4h — e os outros 4 símbolos em 3 — 15m/30m/1h), todas em `status=active` (já tinham `cache_meta`) |
| `GET /v1/assets` | 200, retorna `status`, `addedAt`, `lastBarTs` |
| `POST /v1/assets` sem token | 401 |
| Erros nos logs (35 min) | nenhum |
| Gaps novos em bars (3 h) | zero — auditoria reporta só os gaps históricos de 2026-05-12 → 2026-05-15 (incidente pré-existente) |

---

## Internals (resumo arquitetural)

Mantemos o princípio de transporte fino sobre service layer:

```
                ┌────────────────────────────┐
   POST  ──▶    │  /v1/assets (FastAPI)      │
                │  require_bearer_admin      │
                └─────────────┬──────────────┘
                              │ gRPC
                              ▼
                ┌────────────────────────────┐
                │  AssetService.CreateAsset  │
                │  (Python, gRPC servicer)   │
                └─────────────┬──────────────┘
                              │
                              ▼
                ┌────────────────────────────┐
                │  AssetsRepo.create_with_   │
                │  feeds  (Postgres txn)     │
                │  INSERT assets             │
                │  INSERT feeds (pending)    │
                └─────────────┬──────────────┘
                              │
   ┌──────────────────────────┘
   ▼
┌─────────────────────────────────────────┐
│  data-svc (writer)                      │
│  loop: feeds_repo.polling_targets()     │
│        fetch → insert_bars              │
│        if status='pending' mark_active  │
└─────────────────────────────────────────┘
```

- REST nunca toca Postgres diretamente; tudo passa por gRPC.
- A transação do `CreateAsset` mantém o INSERT em `assets` e os INSERTs em `feeds` atômicos.
- O writer não precisa de SIGHUP, LISTEN/NOTIFY ou restart pra ver novos feeds — re-lê a tabela a cada ciclo (custo de 1 `SELECT` por ~30s, desprezível).

---

## Testes

- **Suite REST:** 76/76 verde (52 pré-existentes + 24 novos cobrindo paginação, filtros, agregação de status, transição pending→active, 409 em duplicata, todos os 3 modos de auth, validação de símbolo malformado).
- **Suite total:** 92/92 verde.
- **Drift gates:** `proto-codegen-verify`, `pydantic-codegen-verify`, `openapi-lint` rodando contra a spec — qualquer divergência futura entre OpenAPI e a app falha no CI.

---

## Roadmap / fora de escopo

Explicitamente **não** entregue nesta phase (intencional, pra manter o blast radius pequeno):

- `PUT /v1/assets/{symbol}` (editar metadata)
- `DELETE /v1/assets/{symbol}` (remover do catálogo)
- Editar `timeframes` de um ativo já cadastrado
- Alertas por ativo

Se algum desses for prioridade, abrir issue / spec antes da phase 4.

---

## Links úteis

- Spec original: [`docs/hybrid-data-svc-rest-api-spec.md`](hybrid-data-svc-rest-api-spec.md) §Phase 3
- OpenAPI (source of truth): [`docs/openapi.yaml`](openapi.yaml)
- Swagger UI: `http://localhost:8003/docs` — **público** (FastAPI aplica o bearer por router, e as rotas internas `/docs` e `/openapi.json` não estão atrás de middleware global). Se isso virar requisito, abrir issue pra adicionar gating dedicado.
- Handover ops: [`docs/hybrid-data-svc-rest-api-handover.md`](hybrid-data-svc-rest-api-handover.md) §2.3 (workflow de onboarding atualizado)
- PR: <https://github.com/Ruscigno/hybrid-data-svc/pull/12>
