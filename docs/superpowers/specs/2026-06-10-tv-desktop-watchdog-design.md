# Design — TV Desktop Watchdog

**Data:** 2026-06-10
**Autor:** brainstorm conduzido por Claude com sander@tickerbeats.com
**Status:** aprovado pra implementação

---

## Contexto

O `data-svc` (writer do `hybrid-data-svc`) inicializa o `DataFetcher`, que por sua vez resolve um endpoint Chrome DevTools Protocol (CDP) chamando `discover_cdp()` em [`data_svc/cdp_discover.py`](../../../data_svc/cdp_discover.py). A descoberta exige duas condições simultâneas no TradingView Desktop:

1. processo rodando com `--remote-debugging-port=<PORT>` aberto em alguma porta no intervalo `9222..9230`;
2. ao menos uma aba cujo URL casa com `tradingview\.com/chart` (regex `_TV_CHART_PATTERN`) ou contém `tradingview` no fallback (`_TV_GENERIC_PATTERN`).

Quando qualquer das condições falha, `discover_cdp` levanta `DiscoveryError`, o `__init__` do `DataFetcher` propaga e `__main__.py` morre com exit 1. O `restart: unless-stopped` do compose faz o container reiniciar a cada ~10s, perpetuando o crash-loop até intervenção humana.

Estado observado em produção em 2026-06-10:

```
data-svc-1  | data_svc.cdp_discover.DiscoveryError: No TradingView CDP endpoint
  found at 192.168.5.2:9222..9230. Ensure TradingView Desktop is running with
  --remote-debugging-port=<PORT> AND at least one chart tab is open.
data-svc-1  Up Less than a second (health: starting)
```

TradingView.app estava rodando — mas lançado sem o flag de CDP. Caso típico: o operador (ou o macOS após reboot/update) abriu TV pela bandeja ou Dock sem passar os args.

## Objetivo

Um watchdog **autônomo no host** que detecta TV Desktop num estado inválido pro `data-svc` e o relança com os params certos, eliminando o crash-loop sem precisar do operador.

### Sucesso é

- TV Desktop volta a expor CDP em alguma porta dentro de `TV_CDP_PORT_RANGE` (9222..9230 default) com qualquer aba TradingView presente, sem intervenção humana, dentro de ≤5min após qualquer regressão do estado.
- Quando TV já está saudável, o watchdog não faz absolutamente nada além de logar `ok` e sair.
- A primeira execução do `install.sh` no host fixa o crash-loop atual em ≤2min.

### Não é objetivo (explícito)

- Não reabrir abas de chart manualmente fechadas — se TV está com CDP up mas sem chart tab, watchdog só loga WARN e sai. O operador tem o direito de ter fechado a aba de propósito (debug, troca de conta).
- Não notificar via Telegram / e-mail / push. Operador vê logs se data-svc ficar fora; alerta externo entra em fase futura se virar requisito.
- Não monitora qualidade dos dados (gaps, off-grid) — isso é responsabilidade do `audit_integrity` que já roda diariamente dentro do data-svc.

## Decisões consolidadas

| Decisão | Escolha | Razão |
|---|---|---|
| Params alvo (relaunch) | `--remote-debugging-port=9222` | Porta canônica usada quando o watchdog precisa relançar TV. |
| PROBE — escopo | Varre todo o `TV_CDP_PORT_RANGE` (default `9222-9230`) | data-svc também varre o range. Se TV está saudável em 9223+ (porque outra Chromium pegou 9222), watchdog precisa aceitar — caso contrário, mataria TV a cada ciclo. |
| Validação adicional | qualquer aba em `/json/list` com URL contendo `tradingview` (regex genérica, case-insensitive) | Replica fielmente `_is_tv_endpoint` em `cdp_discover.py:142`. Aba `tradingview.com/chart` é o caso forte mas não é exigência — qualquer aba `tradingview` no URL serve. |
| Scheduler | LaunchAgent (`com.tickerbeats.tv-desktop-watchdog.plist`) | `RunAtLoad=true` fixa o estado atual no install. `StartInterval=300` (5min) limita blast radius de qualquer regressão futura. Cron é deprecated no macOS e não sobrevive sleep/wake. |
| Linguagem | Bash | Validação cabe em ~30 linhas; zero deps além do que macOS já tem (`curl`, `osascript`, `kill`, `ps`, `open`, `mkdir`, `logger`, `stat`). Trade-off: reimplementa em shell o que `cdp_discover.py` faz em Python; aceitável porque a heurística é estável. |
| Localização no repo | `hybrid-data-svc/ops/tv-watchdog/` | Mesmo repo do data-svc afetado. Operadores que clonam pra rodar o stack já recebem o watchdog. |
| Quit strategy | `osascript ... quit` (graceful, com stderr capturado) → 10s timeout → `env kill -TERM <main_pid>` → 5s → `env kill -KILL` (fallback) | Limpo no caso comum; força quando trava. PID exato (não pkill regex) evita matar Electron Helpers. `env kill` força lookup externo ignorando o builtin do Bash (necessário pra mockabilidade nos testes). |
| Stale lock | Recovery por PID liveness + idade (>180s) | `trap EXIT` não dispara em SIGKILL / kernel panic. Lock dir órfão poderia desabilitar o watchdog permanentemente. Acquire grava `$$` em `owner.pid` dentro do dir; próxima tentativa checa se PID vive e se idade ≤ `_LOCK_MAX_AGE_S` (default 180s = 3min, comfortably > worst-case 80s run) antes de reclamar. |
| Aba TradingView ausente (CDP up) | Logar WARN apenas, sem intervir | Watchdog não atropela workflow do operador. |
| Notificações | Nenhuma nesta versão | YAGNI; pode entrar depois se virar requisito. |

## Arquitetura

```
hybrid-data-svc/
└── ops/tv-watchdog/
    ├── tv-watchdog.sh                              # script principal (Bash)
    ├── com.tickerbeats.tv-desktop-watchdog.plist   # LaunchAgent (placeholder @WATCHDOG_SH@)
    ├── install.sh                                  # cópia + bootstrap idempotente
    ├── uninstall.sh                                # simétrico ao install
    ├── README.md                                   # instalar / desinstalar / troubleshoot
    └── tests/
        └── tv-watchdog.bats                        # 6 casos cobrindo a state machine
```

Não há outros arquivos dentro do repo do `data_svc/` Python — o watchdog é totalmente externo ao processo do data-svc. Ele roda no host (não no container) e só interage com TradingView.app via CDP HTTP e AppleScript/pkill.

## State machine do `tv-watchdog.sh`

Uma execução = um shot, retornando exit code significativo pro launchd.

```
                ┌──────────────────────────────┐
                │  start (lock /tmp/...lock)   │
                └──────────────┬───────────────┘
                               ▼
                ┌──────────────────────────────┐
                │  PROBE: GET /json/version    │
                │  (curl --max-time 2)         │
                └──────┬─────────────┬─────────┘
                       │             │
                  200 OK             non-200 / timeout
                       │             │
                       ▼             ▼
        ┌──────────────────────┐   ┌──────────────────────────┐
        │ VALIDATE:            │   │ ACT: TV needs (re)launch │
        │ GET /json/list       │   └──────────┬───────────────┘
        │ has tradingview      │              │
        │ chart tab?           │              ▼
        └──┬───────────────┬───┘   ┌──────────────────────────┐
           │               │       │  is TV process running?  │
         yes             no        └──────┬───────────────┬───┘
           │               │              │               │
           ▼               ▼            yes              no
        OK exit 0    WARN "no chart      │               │
                     tab — manual"       ▼               │
                     exit 0       ┌─────────────────┐    │
                                  │ osascript quit  │    │
                                  │ wait <=10s      │    │
                                  └────┬────────────┘    │
                                       │                 │
                                  process dead? ──no──▶ pkill -f
                                       │                 │
                                      yes ◀──────────────┘
                                       │
                                       ▼
                              ┌────────────────────────────┐
                              │ open -a TradingView        │
                              │   --args                   │
                              │   --remote-debugging-port  │
                              │   =9222                    │
                              └────────────┬───────────────┘
                                           ▼
                              ┌────────────────────────────┐
                              │ poll /json/version every 2s│
                              │ up to 60s                  │
                              └──────┬─────────────┬───────┘
                                     │             │
                                  reachable     timeout
                                     │             │
                                     ▼             ▼
                              "recovered"    ERROR exit 1
                              exit 0         (launchd re-tenta
                                              em 5min)
```

**Exit codes:**

| Code | Significado | LaunchAgent reage como |
|---|---|---|
| 0 | healthy ou recuperado com sucesso | sem alarme; próxima execução em 5min |
| 1 | tentou relaunch + verify falhou em 60s | log ERROR em `/tmp/tv-watchdog.launchd.err` |

**Tunáveis (todos override-áveis via env):**

| Var | Default | Uso |
|---|---|---|
| `TV_CDP_PORT` | `9222` | Porta passada pro `--remote-debugging-port` no relaunch |
| `TV_CDP_PORT_RANGE` | `9222-9230` | Range varrido durante PROBE — espelha `cdp_discover.py` default |
| `TV_PROBE_TIMEOUT_S` | `2` | `curl --max-time` no probe e validate |
| `TV_QUIT_TIMEOUT_S` | `10` | Espera pelo graceful quit antes do force kill |
| `TV_KILL_TIMEOUT_S` | `5` | Espera pelo SIGTERM antes de escalar pra SIGKILL |
| `TV_VERIFY_TIMEOUT_S` | `60` | Janela pra TV expor CDP pós-relaunch |
| `TV_APP_PATH` | `/Applications/TradingView.app` | Bundle path do TV.app |
| `LOCK_DIR` | `/tmp/tv-watchdog.lock.d` | Dir usado como mutex atomic. Inclui `owner.pid` |
| `LOG_FILE` | `/tmp/tv-watchdog.log` | Append-only file log com timestamp + `[run=...]` |
| `_LOCK_MAX_AGE_S` | `180` | Idade após a qual um lock dir vivo é considerado stale e reclamado defensivamente. `_` prefix indica "interno; mude só se souber o que está fazendo" |
| `DRY_RUN` | unset | Quando `=1`, imprime ações mas não executa `osascript`/`kill`/`open` |

## Componentes

### `tv-watchdog.sh`

Script Bash com `set -euo pipefail`. Estrutura:

1. **Bootstrap.** Gera `run_id=$(openssl rand -hex 4)`. Adquire lock via `mkdir LOCK_DIR` (atomic em POSIX; `flock` não vem com macOS). Grava `$$` em `${LOCK_DIR}/owner.pid`. Se `mkdir` falha, checa stale recovery: PID do `owner.pid` ainda vive? Idade ≤ `_LOCK_MAX_AGE_S`? Se ambos OK → outra instância roda; exit 0 com log "another instance running". Senão → reclama o lock e prossegue.
2. **Probe.** Para cada porta `p` em `TV_CDP_PORT_RANGE`: `curl --max-time $TV_PROBE_TIMEOUT_S http://localhost:$p/json/version` + `curl ... /json/list` procurando `tradingview` (case-insensitive). Primeira porta que satisfaz ambas → healthy, exit 0 imediato.
3. **WARN ramo.** Se alguma porta tem CDP up mas zero abas tradingview em `/json/list`, loga WARN e exit 0 (decisão explícita: não força aba aberta).
4. **Act** (nenhuma porta saudável). Verifica `ps -ax -o pid,command` filtrando por `${TV_BIN}` excluindo "Helper". Decide entre branches "TV vivo" e "TV morto".
5. **Quit + kill.** Em modo "TV vivo", chama `osascript -e 'tell application "TradingView" to quit'` (com stderr capturado pra log actionable), loopa esperando até `$TV_QUIT_TIMEOUT_S`. Se osascript falhou OU TV não saiu na janela → `env kill -TERM <pid>` (PID exato capturado em `tv_main_pid`, evita matar Electron Helpers; `env kill` força lookup externo ignorando o builtin do Bash, necessário pra mockabilidade nos testes). Se SIGTERM não derruba em `$TV_KILL_TIMEOUT_S` → escala pra `env kill -KILL`. Se nenhum dos dois funcionar → exit 1 (`failed to kill TV process; aborting`).
6. **Relaunch.** `open -a TradingView --args --remote-debugging-port=$TV_CDP_PORT`. Se `open` retorna não-zero → exit 1 (`open -a TradingView failed`).
7. **Verify.** Loop polling `http://localhost:$TV_CDP_PORT/json/version` a cada 2s até `$TV_VERIFY_TIMEOUT_S`. **Apenas** verifica porta — não revalida aba TV nessa janela, porque TV restaura workspace de forma assíncrona e a aba pode aparecer só depois do verify retornar. Se TV falhar em restaurar a aba, a próxima execução do watchdog (5min) detecta "CDP up sem aba" e emite WARN. Sucesso (porta OK) → exit 0. Estouro → exit 1.

Cada passo emite uma linha de log via função `log()` interna que escreve em três destinos (ver seção Logging).

### `com.tickerbeats.tv-desktop-watchdog.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.tickerbeats.tv-desktop-watchdog</string>

    <key>ProgramArguments</key>
    <array>
        <string>@WATCHDOG_SH@</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>StartInterval</key>
    <integer>300</integer>

    <key>StandardOutPath</key>
    <string>/tmp/tv-watchdog.launchd.out</string>
    <key>StandardErrorPath</key>
    <string>/tmp/tv-watchdog.launchd.err</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
```

O placeholder `@WATCHDOG_SH@` é substituído pelo caminho absoluto no install. O `.plist` versionado no repo nunca contém path do operador.

### `install.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_SRC="$SCRIPT_DIR/com.tickerbeats.tv-desktop-watchdog.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.tickerbeats.tv-desktop-watchdog.plist"
LABEL="com.tickerbeats.tv-desktop-watchdog"
UID_NUM="$(id -u)"

mkdir -p "$HOME/Library/LaunchAgents"
sed "s|@WATCHDOG_SH@|$SCRIPT_DIR/tv-watchdog.sh|g" "$PLIST_SRC" > "$PLIST_DST"
chmod +x "$SCRIPT_DIR/tv-watchdog.sh"

launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST_DST"
launchctl kickstart "gui/$UID_NUM/$LABEL"

echo "[install] watchdog instalado e disparado uma vez."
echo "[install] logs: log show --predicate 'subsystem == \"tv-watchdog\"' --last 5m"
```

Idempotente: `bootout || true` antes do `bootstrap`. Pode rodar quantas vezes quiser.

### `uninstall.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
LABEL="com.tickerbeats.tv-desktop-watchdog"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"

launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
rm -f "$PLIST_DST"
echo "[uninstall] watchdog removido."
```

### `tests/tv-watchdog.bats`

Requer `bats-core` (`brew install bats-core`) pra rodar. Cada teste injeta mocks via `PATH` (script + diretório temporário com binários falsos `curl`/`ps`/`osascript`/`open`/`kill`/`logger`/`launchctl`), de forma a evitar tocar TV.app real.

17 casos cobrindo a state machine + lock + install/uninstall:

| Caso | O que cobre | Exit |
|---|---|---|
| `healthy_full` | CDP up em 9222 + aba tradingview presente | 0 |
| `healthy_off_canonical_port` | TV saudável em 9223 (não-canonical) → aceito, sem thrashing | 0 |
| `cdp_up_no_chart` | CDP up mas zero abas tradingview → WARN, sem intervir | 0 |
| `cdp_down_tv_running_quits_clean` | curl refused; ps mostra TV; osascript exit 0; TV some; open + recover | 0 |
| `cdp_down_quit_accepted_but_tv_persists` | osascript exit 0 mas TV não morre → graceful timeout → SIGTERM fallback | 0 |
| `cdp_down_osascript_denied_kill_falls_back` | Automation negada (stderr surfaced no log) → SIGTERM fallback | 0 |
| `cdp_down_sigterm_fails_sigkill_works` | SIGTERM no-op, SIGKILL mata → relaunch | 0 |
| `cdp_down_unkillable_aborts` | osascript + SIGTERM + SIGKILL todos falham → exit 1, `open` nunca chamado | 1 |
| `cdp_down_launch_fails` | `open -a` retorna não-zero → exit 1 | 1 |
| `cdp_down_tv_off` | TV nem rodando → launch fresh, recover | 0 |
| `relaunch_verify_timeout` | open OK mas CDP nunca volta no janelão → exit 1 | 1 |
| `lock_busy_with_live_owner` | PID dentro do lock está vivo → exit 0, no-op | 0 |
| `lock_stale_dead_owner_reclaimed` | PID 99999 (morto) → reclaim + prossegue | 0 |
| `lock_stale_no_pid_file_reclaimed` | lock dir vazio → reclaim + prossegue | 0 |
| `dry_run` | DRY_RUN=1 → loga "would do X" sem efeito real | 1 (verify continua falhando, mas sem side effects) |
| `install_idempotent` | install.sh rodado 2× → 2 bootouts + 2 bootstraps no log do mock launchctl | 0 |
| `uninstall_keeps_logs` | uninstall.sh deixa `/tmp/tv-watchdog.log` intocado | 0 |

Os testes injetam timeouts curtos via env (`TV_VERIFY_TIMEOUT_S=4 TV_QUIT_TIMEOUT_S=2 TV_KILL_TIMEOUT_S=2`) + range encurtado (`TV_CDP_PORT_RANGE=9222-9224`) pra rodar em ~10s total.

### `README.md`

Mínimo necessário pro operador:

1. Pré-requisitos: macOS, TradingView.app em `/Applications/`, repo clonado em **path estável** (o path absoluto é gravado no plist no install; mover o repo depois exige reinstall).
2. `./install.sh` — instala e dispara já.
3. Aviso sobre **permissão de Automation** no primeiro disparo (System Settings → Privacy & Security → Automation → Terminal → TradingView). Sem isso, o graceful quit cai pro SIGTERM (também funciona, só fica menos limpo).
4. Verificação: `launchctl list | grep tv-desktop-watchdog`, `tail -f /tmp/tv-watchdog.log` (ou `log show --predicate 'eventMessage CONTAINS "tv-watchdog"' --last 10m`).
5. `./uninstall.sh` pra remover.
6. Variáveis de override (TV_CDP_PORT_RANGE, TIMEOUT_S, etc.) listadas com defaults.
7. Smoke test manual.
8. Rotação dos `/tmp/tv-watchdog.{log,launchd.out,launchd.err}` — não é automática; volume típico (~50 linhas/dia) é desprezível, mas em uma máquina que fica meses sem reboot vale o `truncate -s 0` ocasional.

## Error handling, concorrência, logging

**Lock.** `mkdir /tmp/tv-watchdog.lock.d` no topo (atomic em POSIX; `flock` não vem com macOS). Acquire grava `$$` em `owner.pid` dentro do dir. Se o `mkdir` falha (lock tomado), antes de desistir o script tenta **stale recovery**: lê `owner.pid`, faz `kill -0 <pid>` pra checar liveness. Se PID morto OU lock dir tem `mtime` > `_LOCK_MAX_AGE_S` (180s default) → reclama o lock e prossegue (com log WARN). Senão → outra instância roda; exit 0 com log "another instance running". `trap "rm -rf ..." EXIT` libera ao final.

Watchdog tem janela de execução de até ~80s (10s quit + 5s kill + 60s verify). O threshold de stale (180s) é mais que 2× o worst-case pra evitar reclaim acidental de um run lento.

**Logging.** Três destinos por linha:

| Destino | Quando inspecionar | Quem rotaciona |
|---|---|---|
| `logger -t tv-watchdog "..."` (syslog / Unified Logging) | `log show --predicate 'eventMessage CONTAINS "tv-watchdog"' --last 1h`, ou Console.app filtrando por process `logger` | macOS |
| `/tmp/tv-watchdog.log` (append, ISO 8601) | `tail -f /tmp/tv-watchdog.log` | Manual; volume ≈ 50 linhas/dia |
| `/tmp/tv-watchdog.launchd.{out,err}` | stdout/stderr do script + tracebacks | Não rotaciona |

Cada linha carrega o `run_id` em **todos os 3 destinos** (file, stdout/stderr, syslog/Unified Logging via `logger -t tv-watchdog "[run=...] ..."`) pra correlacionar uma execução inteira: `2026-06-10T18:00:01 [run=a1b2c3d4] PROBE range=9222-9230`.

`logger` no macOS não suporta `--subsystem`, então o predicate canônico do Unified Logging não é `subsystem == "tv-watchdog"`. Usa `eventMessage CONTAINS "tv-watchdog"` ou, mais simples, `tail -f /tmp/tv-watchdog.log` direto.

**Permissões macOS.** A primeira invocação de `osascript -e 'tell application "TradingView" to quit'` dispara prompt de Automation em System Settings → Privacy & Security → Automation. Se o operador negar (ou o prompt não chegar a aparecer em sessões launchd), o `osascript` retorna não-zero e o stderr "Not authorized to send Apple events to TradingView." é capturado direto no log WARN — actionable, sem adivinhação. O watchdog cai automaticamente pro `env kill -TERM`, então a função não fica bloqueada pela falta de permissão — só fica menos limpa.

README documenta a primeira execução manual (rodar `./tv-watchdog.sh` no terminal antes de instalar) pra disparar o prompt.

**Falhas idempotentes:**

- TV já estava OK: `PROBE` 200 + `VALIDATE` OK → exit 0. Nenhuma ação. Nenhum log warn.
- Watchdog re-instalado mid-flight: `install.sh` faz `bootout || true` antes de `bootstrap`. Sem dois agentes.
- Disco cheio em `/tmp`: `logger` (syslog) continua, append em arquivo falha silenciosa. Não trava o ACT.
- TV.app em update via auto-updater: PROBE refused → tenta `osascript quit` → o processo do updater ignora → `pkill` mata o TV pai → `open` relança a versão atualizada. Comportamento aceitável.

## Out of scope

- `DELETE`/`PUT` em `/v1/assets` — não relacionado a esse design.
- Notificações externas (Telegram, e-mail, push). Adicionar como follow-up se virar dor.
- Reabrir aba de chart automaticamente quando CDP está up mas sem chart. Decisão explícita: só WARN.
- Monitoramento de saúde de bars (gaps, off-grid). Já coberto pelo `audit_integrity` interno do data-svc.
- Auto-update do próprio watchdog. O operador puxa via `git pull`; launchd vê a nova versão do script na próxima execução (path é estático, conteúdo do script muda).
- Suporte multi-host. O watchdog é por máquina; cada host com TV.app instala o seu.

## Pontos abertos

Nenhum. Todas as decisões de design foram fechadas no brainstorm.
