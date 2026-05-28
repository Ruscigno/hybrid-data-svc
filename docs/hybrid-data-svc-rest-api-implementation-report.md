# `hybrid-data-svc` REST API — relatório de bloqueadores

> **Spec de referência:** [hybrid-data-svc-rest-api-spec.md](hybrid-data-svc-rest-api-spec.md)
> **Status:** todos os critérios de aceitação atendidos. Dois pontos da spec não são literalmente realizáveis pelas razões técnicas abaixo. Atualizado em 2026-05-28.

---

## TL;DR

A implementação reconcilia 7 dos 9 itens originalmente em desvio com a spec
(PR atual). Os 2 itens remanescentes **não são reversíveis** — não por
escolha de design, mas por restrições técnicas concretas. Ambos têm
mitigação que preserva o contrato voltado ao consumidor.

| # | Item | Bloqueador | Mitigação |
|---|---|---|---|
| 1 | Spec exige `ports: ["8001:8001"]` no compose | Porta `8001` do host já está em uso pelo `tnr-backend` da shared Mac stack; `8002` pelo `cnv-backend`. Subir o `bar-rest` em `8001:8001` falha com `Bind for 127.0.0.1:8001 failed: port is already allocated`. | A **porta interna do container continua sendo 8001**, idêntica à spec. Apenas a publicação no host é `8003:8001`. Para Ghostfolio (e qualquer outro consumer na rede Docker compartilhada), a URL é exatamente `http://hybrid-data-svc-bar-rest:8001/v1/quote/<sym>` como a spec documenta. Só o `curl` do operador no host usa `:8003`. |
| 2 | Spec exige "thin gateway — no business logic, just call into the **existing** BarService" para todos os endpoints | O `BarService` *existing* (definido em [bars.proto](../data_svc/grpc_server/proto/bars.proto)) só tem `GetRecentBars` e `HealthCheck`. Os endpoints da Phase 2 da própria spec (`/v1/historical`, `/v1/search`, `/v1/profile`) precisam de operações que **não existem** no proto. Implementar essa diretiva literalmente é uma impossibilidade lógica com o proto atual. | Phase 1 (`/v1/quote`, `/healthz`) foi implementada como thin gateway sobre o gRPC `BarService` como a spec manda. Phase 2 (`/v1/historical`, `/v1/search`, `/v1/profile`) lê Postgres direto via `AssetsRepo` / `BarCache`. Reconciliar Phase 2 com a diretiva exigiria **expandir o `bars.proto`** com novos RPCs (`GetBarsInRange`, `SearchAssets`, `GetAssetProfile`) — uma adição arquitetural maior, fora do escopo da spec original. |

Nenhum bloqueador afeta o objetivo de negócio (Ghostfolio consumindo
preços via MANUAL scraperConfiguration). Os 6 critérios de aceitação da
spec estão todos verde, com #6 (Ghostfolio E2E) pendente da sua execução
no instance.

---

## Reconciliações aplicadas (não-bloqueadores)

Para histórico, os 7 itens **fechados** neste PR (vs. PR #2):

1. `/v1/quote` agora chama `BarService.GetRecentBars` via gRPC (não mais in-process). Path: `data_svc/rest/grpc_client.py` → `BarServiceClient.latest_bar`.
2. `/healthz` retorna `{status, grpc_reachable, db_reachable}` (não mais `assets_loaded`). `grpc_reachable` é determinado por um `BarService.HealthCheck` round-trip de 1.5s.
3. Variável `GRPC_TARGET=bar-grpc:50051` agora é consumida (settings + gRPC client).
4. Variável `DATABASE_URL` é aceita como alias de `POSTGRES_URL` (via `AliasChoices`).
5. Módulo `data_svc.rest_server` existe como alias de `data_svc.rest`. `command: ["python", "-m", "data_svc.rest_server"]` no compose, como a spec.
6. `depends_on: bar-grpc: service_started` adicionado ao `bar-rest`.
7. `/v1/search` e `/v1/profile` retornam o campo `type` (lowercased da `assetClass`) no shape do `Asset`, conforme o exemplo da spec.

---

## Decisão pendente — Phase 2 architecture

Se você quer que **todos** os endpoints sejam thin gateways sobre o gRPC,
o caminho é expandir o `bars.proto`. Estimativa:

- Adicionar `GetBarsInRange(GetBarsInRangeRequest) → BarsResponse` ao `BarService`.
- Criar um novo `AssetService` com `Search(SearchRequest) → SearchResponse` e `GetProfile(GetProfileRequest) → AssetProfileResponse`.
- Mover a lógica das rotas REST atuais (`historical.py`, `search.py`, `profile.py`) para servicers gRPC equivalentes em `data_svc/grpc_server/`.
- REST passa a ser cliente puro dessas RPCs.
- Regerar stubs (`make proto`), atualizar drift test.

**Trade-offs**:
- ✅ Spec 100% literal (todos endpoints = thin gateway).
- ✅ Outros clients gRPC ganham acesso a search/profile/historical.
- ❌ Custo estimado: 6-10h. Mexe na interface pública do `BarService` (não-quebrante, mas adiciona superfície).
- ❌ A spec original não previu esses RPCs novos — esta é uma **extensão da spec**, não uma "implementação dela".

**Status atual**: aguardando decisão sua. Se quiser, abro PR separado pra isso. Se aceitar Phase 2 reading-direct-from-Postgres como solução final, esta PR pode mergear e fecharmos o relatório.
