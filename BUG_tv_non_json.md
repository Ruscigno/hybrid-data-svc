# Bug Report — data-svc: `tv CLI returned non-JSON` em produção

- **Repositório:** `Ruscigno/hybrid-data-svc`
- **Componente:** `data_svc/fetcher.py` (writer / fetcher de barras)
- **Severidade:** 🔴 Alta — bloqueia a pipeline de dados; o trading bot não pode operar
- **Descoberto:** 2026-05-20, durante a recuperação de uma outage da stack
- **Status:** aberto — `data-svc` parado de propósito até a correção

---

## Resumo

Em produção, o `data-svc` falha **intermitentemente** ao parsear a saída do `tv` CLI:
`json.loads(result.stdout)` lança `JSONDecodeError`, tratado como `tv CLI returned non-JSON`.
Ocorre nos fetches grandes (gap-fill / full-refetch de 500 barras) durante o ciclo normal
multi-feed.

Combinado com um segundo defeito de design (invalidação de cache **antes** do refetch
bem-sucedido), o resultado é **erosão de dados**: o banco caiu de 32.745 → 9.716 barras
em ~3 minutos de operação.

---

## Comportamento observado

Log de produção (representativo, repetido em vários feeds):

```
[cache] validating BTC/USDT:USDT/30 — gap=365 bars, fetching 366 from TV
[cache] close mismatch at ts=1778619600: cached=80655.81 fresh=80620.80 diff=0.043%
[cache] overlap mismatch for BTC/USDT:USDT/30 — invalidating and full refetch
[cache] invalidated BTC/USDT:USDT/30
[BTC/USDT:USDT/30] fetch error (retrying next cycle): tv CLI returned non-JSON: {
  "success": true,
  "bar_count": 500,
  "total_available": 2490,
  ...
```

A saída logada **começa como JSON válido** (`{"success": true, "bar_count": 500, ...`)
— ou seja, o `tv` CLI rodou e produziu um payload. O `json.loads` falha mesmo assim,
portanto há corrupção/conteúdo extra **depois** dos primeiros 200 caracteres.

---

## O que foi confirmado no diagnóstico

| Teste | Resultado |
|---|---|
| `tv ohlcv -n 500` isolado (chart carregado) | ✅ 76.824 bytes, **JSON válido**, 500 barras |
| 6× `tv ohlcv -n 500` consecutivos (back-to-back) | ✅ Todos válidos |
| `tv ohlcv` sem chart carregado | Retorna `{"success": false, "error": "Could not extract OHLCV data. The chart may still be loading."}` — JSON válido |
| Fetch no runtime real do data-svc (16 feeds ciclando) | ❌ `non-JSON` intermitente |

**Conclusão:** o `tv` CLI por si só é robusto (chamada única, back-to-back e fetch grande
de 500 barras sempre produzem JSON válido). A falha é específica do **contexto de runtime
do data-svc** e **não reproduz em isolamento**.

---

## BUG 1 — `_tv_fetch` falha o parse de JSON (intermitente)

**Local:** `data_svc/fetcher.py:90-110`

```python
def _tv_fetch(count, tv_cli, tv_env=None):
    cmd = [tv_cli, "ohlcv", "-n", str(min(count, 500))]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=tv_env)
    ...
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise DataFetchError(f"tv CLI returned non-JSON: {result.stdout[:200]}")  # ← linha 110
```

### Problema gatilho de tudo — observabilidade cega

A mensagem de erro loga **apenas `result.stdout[:200]`**. Os primeiros 200 caracteres
são sempre JSON válido, então **a corrupção real nunca aparece no log**. Ninguém consegue
corrigir a causa-raiz sem ver a saída completa. **Isto precisa ser corrigido primeiro.**

### Hipóteses de causa-raiz (ordenadas por probabilidade)

1. **Poluição de stdout pelos patches de CDP discovery (mais provável).**
   O contrato do `tv` CLI é *JSON puro no stdout*. Os patches de 2026-05-19
   (`cdp_discover.js`, `connection.js` com `invalidateEndpoint()`, `core/tab.js`)
   adicionaram logging. As linhas `[cdp_discover]` observadas vão para **stderr**
   (correto) — mas o caminho de **retry / re-discovery mid-session**
   (`invalidateEndpoint()`, disparado em falha do retry-loop) pode ter um `console.log`
   que escreve no **stdout**. Isso anexaria uma linha não-JSON ao payload →
   `json.loads` falha. Casa com: intermitência (só em retry de CDP), stdout começando
   válido, e a correlação temporal com o trabalho recente de CDP.
   → **Ação:** auditar `external/tradingview-mcp/src/` (e os espelhos em
   `data_svc/patches/`) — todo `console.log` no caminho do comando `ohlcv` deve virar
   `console.error`. Nenhum diagnóstico pode ir para stdout.

2. **Saída concatenada de duas execuções.** Se o `tv` CLI faz retry interno e imprime
   dois objetos JSON (`{...}{...}`), `json.loads` falha com "Extra data".

3. **Race de chart-switch parcial.** O data-svc espera apenas `time.sleep(3)` após
   trocar o chart (`data_svc/fetcher.py:186`). Em teste, o chart precisou de ~18 s para
   servir dados limpos. Com 3 s, o `tv ohlcv` pode pegar o chart em estado transitório
   e o CLI emitir saída malformada. (Observado também: fetch de BTC retornando preços
   de BNB — `"open": 683.15` — sintoma direto de chart meio-trocado.)

### Correções recomendadas — BUG 1

- **(obrigatória, primeiro) Observabilidade.** Em `data_svc/fetcher.py:110`, logar o
  `result.stdout` **completo** (ou despejar num arquivo `/tmp/tv_fail_<ts>.json`), mais
  `result.stderr`, `result.returncode`, e `JSONDecodeError.pos/lineno/colno`. Sem isso,
  o resto é chute.
- Auditar todos os `console.log` do `tv` CLI e dos patches → mover diagnósticos para
  `console.error` (hipótese 1).
- Tornar o parse tolerante: usar `json.JSONDecoder().raw_decode()` para aceitar JSON
  válido com lixo no final, OU `result.stdout.strip()` + extrair do primeiro `{` ao
  último `}`.
- Subir o `time.sleep(3)` pós-switch (`data_svc/fetcher.py:186`) para ~10-15 s, ou
  fazer polling do estado de "chart pronto" antes de fazer fetch.
- Revisar `timeout=30` (`data_svc/fetcher.py:94`) — fetch de 500 barras sob carga pode
  encostar nele.

---

## BUG 2 — Invalidação de cache antes do refetch bem-sucedido (erosão de dados)

**Local:** fluxo `BarCache.get_bars` → `_fetcher` (`data_svc/db/cache.py`, chamado em
`data_svc/fetcher.py:206`)

Quando a barra de overlap diverge (`close mismatch`), o data-svc **invalida (DELETE) o
bucket inteiro e só então tenta o full-refetch**. Se o refetch falhar (BUG 1), o bucket
fica **vazio permanentemente** até um fetch futuro dar certo. Foi por isso que o banco
erodiu 32.745 → 9.716 barras.

### Correção recomendada — BUG 2

Inverter a ordem: **fetch → validar → só então substituir.** Buscar as barras novas para
um staging, validar, e fazer o swap atômico (`DELETE` + `INSERT` na mesma transação)
**somente se o fetch teve sucesso**. Nunca deixar um bucket vazio por causa de um fetch
falho. É um defeito de robustez independente do BUG 1.

---

## Como reproduzir

1. Garantir um gap grande no banco (≥500 barras): parar o data-svc por algumas horas,
   ou apagar barras recentes de um bucket.
2. Subir o data-svc: `docker compose up -d data-svc`.
3. `docker compose logs -f data-svc` → `tv CLI returned non-JSON` aparece nos fetches
   de gap-fill, e a contagem de barras (`SELECT count(*) FROM bars`) **cai** a cada ciclo.

---

## Critério de aceite

- `_tv_fetch` loga a saída completa em qualquer falha de parse.
- Em ciclo normal multi-feed com gaps, zero `non-JSON` em 100+ fetches consecutivos.
- A contagem de barras no Postgres **nunca diminui** por causa de um fetch falho (BUG 2).

---

## Estado do ambiente quando o bug foi documentado (2026-05-20)

- **Postgres:** recuperado, limpo, migrado para o volume Docker nomeado
  `hybrid-data-svc_pgdata` (a versão anterior em bind-mount macOS corrompeu o WAL —
  causa da outage). 9.716 barras. Re-seedável para 32.745 via
  `python -m scripts.seed_from_sqlite --src <bars.db>` (snapshot de 12-mai disponível).
- **data-svc:** **parado de propósito** (estava erodindo o cache). Subir apenas depois
  da correção do BUG 1 + BUG 2.
- **bar-grpc:** no ar, servindo `:50051`.
- **bot-multi (repo trading):** não iniciado — depende de barras frescas do data-svc.
- Backup do cluster Postgres corrompido: `hybrid-data-svc/backups/pgdata-corrupt-20260520.tar.gz`.
