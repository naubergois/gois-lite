<p align="center">
  <img src="assets/gois-lite.svg" width="128" alt="Gois Lite — mascote com microscópio">
</p>

<h1 align="center">Gois Lite</h1>

<p align="center">
  <strong>G</strong>enerative <strong>O</strong>rchestration <strong>A</strong>rtificial <strong>I</strong>ntelligence <strong>S</strong>warm — <strong>lite edition</strong>
</p>

<p align="center">
  <strong>Chat + Kanban + MCP IDE</strong> — versão mínima do Gois, <strong>sem OpenClaw nem QClaw</strong>.
</p>

<p align="center">
  <a href="https://github.com/naubergois/gois-lite">GitHub</a>
  ·
  <a href="https://github.com/naubergois/gois">Gois completo</a>
</p>

| Item | Gois (completo) | Gois Lite |
|------|-----------------|-----------|
| Local | repositório `gois` | `/Volumes/NAUBER/gois-lite` |
| UI | Dashboard completo | **Chat**, **Kanban**, **MCP IDE** |
| Porta HTTP | 9101 | **9102** |
| Base de dados | MongoDB `gois` | **SQLite** (padrão) ou **PostgreSQL** |
| MCP | vários servidores | **`gois-cards`** |
| Git | histórico completo | **1 commit** (snapshot do dia) |
| OpenClaw / QClaw | integrado | **ausente** |

---

## O que faz

1. **Kanban** — boards e demandas em `.stack/accounts/teams/*/kanban.yaml` (SQLite/PostgreSQL para contas)
2. **MCP `gois-cards`** — Cursor/Kiro/VS Code leem e atualizam o kanban
3. **Handoff IDE** — `kanban_ide_handoff` prepara o card e abre a IDE
4. **Chat** — DeepSeek com tools `gois_cards_*` e `gois_kanban_ide_handoff`

---

## Requisitos

- Volume externo em `/Volumes/NAUBER` (deploy local) ou clone do [gois-lite](https://github.com/naubergois/gois-lite)
- **Sem MongoDB** por defeito — SQLite em `.stack/`
- `DEEPSEEK_API_KEY` no `.env`
- Python 3.11+ (`.venv` criado no primeiro start)
- PostgreSQL opcional: `pip install 'gois[postgres]'` + `GOIS_LITE_DATABASE_URL`

---

## Deploy / atualização

No repositório **gois** (completo):

```bash
./scripts/setup-gois-lite.sh --yes
```

O script:

1. Sincroniza só o código necessário para `/Volumes/NAUBER/gois-lite`
2. Enxuga módulos (prune) — chat, kanban, MCP cards
3. Configura MCP **`gois-cards`** (`.cursor/mcp.json`, `.mcp.json`)
4. Gera `config.yaml` e `.env` lite (porta **9102**)
5. Reinicia o **git local** com **um commit datado de hoje**

Só o git (sem rsync completo):

```bash
./scripts/lib/init_gois_lite_git.sh /Volumes/NAUBER/gois-lite
```

---

## Iniciar

```bash
cd /Volumes/NAUBER/gois-lite
./scripts/start.sh --skip-vendor
```

URLs:

| Página | URL |
|--------|-----|
| Chat | http://127.0.0.1:9102/chat |
| Kanban | http://127.0.0.1:9102/kanban |
| MCP IDE | http://127.0.0.1:9102/mcp-cards |

---

## MCP (Cursor / IDE)

Abra **`/Volumes/NAUBER/gois-lite`** no Cursor (ou copie `.cursor/mcp.json`).

| Tool | Função |
|------|--------|
| `list_kanban_boards` / `list_teams` | boards e times |
| `get_cards` / `get_card_detail` / `get_cards_todo` | ler cartões |
| `create_card` | criar demanda |
| `move_card` / `update_card` | atualizar kanban |
| `kanban_ide_handoff` | abrir card na IDE |

Env no MCP (`gois-cards`):

```bash
GOIS_LITE=1
GOIS_LITE_DB_BACKEND=sqlite
GOIS_STACK_ROOT=/Volumes/NAUBER/gois-lite/.stack
GOIS_KANBAN_WORKDIRS=/Volumes/NAUBER/gois-lite
```

---

## Base de dados

| Backend | Quando | Onde ficam os dados |
|---------|--------|---------------------|
| **sqlite** (padrão) | sem config extra | `.stack/accounts/accounts.db`, `.stack/chat/history.sqlite3`, kanban YAML |
| **postgresql** | `GOIS_LITE_DB_BACKEND=postgresql` | URL em `GOIS_LITE_DATABASE_URL` |
| **mongodb** | legado | `GOIS_LITE_DB_BACKEND=mongodb` + `MONGODB_URI` |

### SQLite (padrão)

```bash
GOIS_LITE=1
GOIS_LITE_DB_BACKEND=sqlite
```

### PostgreSQL

```bash
GOIS_LITE=1
GOIS_LITE_DB_BACKEND=postgresql
GOIS_LITE_DATABASE_URL=postgresql://user:pass@localhost:5432/gois_lite
```

Instalar driver: `pip install 'gois[postgres]'` ou `psycopg[binary]`.

Em `config.yaml`:

```yaml
gois_lite:
  enabled: true
  database:
    backend: postgresql
    url: postgresql://user:pass@localhost:5432/gois_lite
```

---

## Variáveis (`.env`)

```bash
GOIS_LITE=1
GOIS_LITE_DB_BACKEND=sqlite
GOIS_HTTP_PORT=9102
DEEPSEEK_API_KEY=sk-...
HERMES_HOME=/Volumes/NAUBER/gois-lite/hermes
GOIS_STACK_ROOT=/Volumes/NAUBER/gois-lite/.stack
```

`.env` não entra no git (segredos).

---

## Estrutura (após deploy)

```
gois-lite/
├── assets/gois-lite.svg   # símbolo (mascote + badge LITE)
├── src/gois/              # código enxuto
├── scripts/           # start.sh, setup_mongo.sh, lib/
├── config.yaml        # porta 9102, gois_lite enabled
├── .cursor/mcp.json   # gois-cards
├── hermes/            # scaffold mínimo (cron vazio)
└── README.md          # este ficheiro
```

---

## Git e GitHub

- Local: `/Volumes/NAUBER/gois-lite/.git`
- Remoto: [github.com/naubergois/gois-lite](https://github.com/naubergois/gois-lite)
- Cada `setup-gois-lite.sh` recria histórico com **commit único** e faz **push** (`GOIS_LITE_GITHUB_PUSH=1` por defeito)
- Desativar push: `GOIS_LITE_GITHUB_PUSH=0 ./scripts/setup-gois-lite.sh --yes`

```bash
cd /Volumes/NAUBER/gois-lite
git log -1 --date=iso-strict
git status
```

---

## O que não inclui

- OpenClaw / QClaw (skills, bridge, shell, monitor)
- Swarm, LaTeX, jobs, memclaw, vendor, `skills/`
- Dashboard completo, WhatsApp, RuFlo

---

## Problemas comuns

| Sintoma | Solução |
|---------|---------|
| Volume não montado | Montar `/Volumes/NAUBER` |
| Porta 9102 ocupada | `lsof -i :9102` |
| Kanban vazio | Verificar `.stack/accounts/` e permissões de escrita |
| PostgreSQL | `GOIS_LITE_DATABASE_URL` + `pip install psycopg[binary]` |
| MCP sem tools | Reiniciar Cursor após deploy; abrir pasta `gois-lite` |
| `.venv` em falta | `./scripts/start.sh --skip-vendor` (cria venv) |

---

## App macOS

No gois completo:

```bash
./scripts/build-macos-lite-app.sh --install
```

---

Gerado/atualizado por `scripts/setup-gois-lite.sh`.
