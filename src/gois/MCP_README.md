# MCP Server — qclaw-cards

Servidor MCP (Model Context Protocol) que expõe boards Kanban e artigos LaTeX para IDEs com suporte a MCP (Kiro, Cursor, VS Code + Continue, etc.).

## Visão Geral

O `qclaw-cards` é um servidor JSON-RPC sobre stdio que permite que agentes de IA interajam com:
- **Boards Kanban** — listar, consultar, filtrar e mover cards entre colunas
- **Artigos LaTeX** — ler, buscar, editar e compilar artigos acadêmicos

## Configuração

### No projeto atual (gois)

A configuração está em `.kiro/settings/mcp.json`:

```json
{
  "mcpServers": {
    "qclaw-cards": {
      "command": "python",
      "args": ["-m", "gois.mcp_cards_server"],
      "env": {
        "QCLAW_KANBAN_WORKDIRS": "/Users/naubergois/gois"
      },
      "disabled": false,
      "autoApprove": [
        "list_kanban_boards",
        "get_cards",
        "get_card_detail",
        "get_my_cards",
        "get_cards_todo",
        "move_card"
      ]
    }
  }
}
```

### Em outro projeto (conexão remota)

Para outro projeto/IDE se comunicar com os kanbans do gois, adicione ao `mcp.json` da IDE:

```json
{
  "mcpServers": {
    "qclaw-cards": {
      "command": "/Users/naubergois/gois/.venv/bin/python",
      "args": ["-m", "gois.mcp_cards_server"],
      "env": {
        "QCLAW_KANBAN_WORKDIRS": "/Users/naubergois/gois"
      },
      "disabled": false,
      "autoApprove": ["list_kanban_boards", "get_cards", "get_card_detail", "get_my_cards", "get_cards_todo"]
    }
  }
}
```

Locais do arquivo de configuração por IDE:
- **Kiro**: `.kiro/settings/mcp.json`
- **Cursor**: `.cursor/mcp.json`
- **VS Code**: `.vscode/mcp.json`
- **Global (user-level)**: `~/.kiro/settings/mcp.json`

### Variáveis de Ambiente

| Variável | Descrição | Exemplo |
|----------|-----------|---------|
| `QCLAW_KANBAN_WORKDIRS` | Diretórios base separados por `:` | `/path/projeto1:/path/projeto2` |

Se não definida, o servidor escaneia `~/gois`, `~/projects` e o diretório atual.

## Como verificar se está ativo

O MCP server roda **sob demanda** — a IDE o inicia quando precisa. Para testar manualmente:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' | python -m gois.mcp_cards_server
```

Se retornar JSON com `serverInfo`, está funcionando.

## Tools Disponíveis

### Kanban

| Tool | Descrição | Parâmetros |
|------|-----------|-----------|
| `list_kanban_boards` | Lista todos os boards kanban | — |
| `get_cards` | Cards de um board | `workdir?`, `column?`, `assignee?` |
| `get_card_detail` | Detalhes de um card | `card_id`, `workdir?` |
| `get_my_cards` | Cards de um assignee | `assignee`, `exclude_done?` |
| `get_cards_todo` | Cards pendentes (backlog + todo) | `workdir?` |
| `move_card` | Mover card entre colunas | `card_id`, `column`, `workdir?` |

### Artigos LaTeX

| Tool | Descrição | Parâmetros |
|------|-----------|-----------|
| `list_article_workspaces` | Lista workspaces de artigos | — |
| `list_articles` | Artigos de um workspace | `workspace_id` |
| `read_article` | Lê conteúdo de um .tex | `workspace_id`, `article_id`, `max_chars?` |
| `search_articles` | Busca em artigos | `query`, `workspace_id?` |
| `write_article` | Salva conteúdo .tex | `workspace_id`, `article_id`, `content` |
| `compile_article` | Compila LaTeX para PDF | `workspace_id`, `article_id` |
| `edit_article_tex` | Edita .tex (título, seções, replace) | `workspace_id`, `article_id`, `action`, … |

## Estrutura dos Kanbans

Os boards ficam em `.stack/accounts/teams/<team_id>/kanban.yaml`:

```
.stack/accounts/teams/
├── whatsapp-consulta/kanban.yaml    ← board nomeado
├── dc7c2bf5c80d/kanban.yaml         ← board hash (canônico)
├── queimadas/kanban.yaml
└── ...
```

### Colunas padrão

| ID | Título |
|----|--------|
| `backlog` | Backlog |
| `todo` | A fazer |
| `doing` | Em progresso |
| `testes-usabilidade` | Testes e usabilidade |
| `review` | Em revisão |
| `done` | Concluído |

### Sync automático

O servidor sincroniza automaticamente boards nomeados com seus equivalentes hash quando o hash é mais recente e tem mais tasks. Isso garante que o MCP e a interface gráfica mostrem os mesmos dados.

## Exemplos de uso via IDE

```
"mostre os cards do projeto whatsapp-consulta"
"quais tasks estão no backlog?"
"mova TASK-041 para doing"
"quais cards são do orelhao-dev?"
"liste os artigos do workspace queimadas"
```

## Requisitos

- Python 3.11+
- PyYAML (`pip install pyyaml`)
- Pacote `gois` instalado (`pip install -e .`)

## Execução

### Local (stdio — padrão para IDEs)

```bash
python -m gois.mcp_cards_server
```

O servidor espera mensagens JSON-RPC via stdin e responde via stdout.

### Remoto (HTTP/SSE — para outras máquinas)

```bash
python -m gois.mcp_http_server --host 0.0.0.0 --port 9200
```

Isso expõe o MCP via rede com 3 endpoints:

| Endpoint | Método | Descrição |
|----------|--------|-----------|
| `/sse` | GET | Conexão SSE (MCP transport padrão) |
| `/jsonrpc` | POST | JSON-RPC direto (sem SSE) |
| `/health` | GET | Health check |

### Configuração do cliente remoto

Na máquina remota, no `mcp.json` da IDE:

```json
{
  "mcpServers": {
    "qclaw-cards": {
      "url": "http://IP_DO_SERVIDOR:9200/sse"
    }
  }
}
```

### Instalar dependências do servidor HTTP

```bash
pip install aiohttp aiohttp-sse
```

### Rodar como serviço (produção)

```bash
nohup python -m gois.mcp_http_server --port 9200 > /dev/null 2>&1 &
```

Ou com systemd/launchd para persistir após reboot.
