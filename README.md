# Ugibo MCP server

Your Ugibo projects, tasks, requests, documents and invoices inside any AI editor that
speaks the Model Context Protocol (Claude Code, Cursor, VS Code and others).

It uses your personal API key, so it can do exactly what you can do in the web app and
nothing more. It cannot read secrets: the vault is closed to API keys.

## Get a key

In Ugibo, open **Settings > API keys** and create one. The key is shown once, together
with ready-to-paste setup for Claude Code, Cursor and VS Code, and a prompt you can give
an AI agent so it sets the server up for itself.

## Run it

```bash
UGIBO_API_URL=https://api.ugibo.com UGIBO_API_KEY=ugk_... uvx ugibo-mcp
```

Until the package is published, install from the repository instead (set the same source
in the admin setting "MCP server" so new keys show the right command):

```bash
uvx --from "git+https://github.com/<org>/<repo>" ugibo-mcp
```

## Tools

| Area | Tools |
| --- | --- |
| You | `whoami` |
| Clients | `list_organizations`, `get_organization`, `list_engagements` |
| Delivery | `list_projects`, `get_project`, `list_tasks`, `get_task`, `create_task`, `update_task`, `advance_task`, `set_task_status`, `comment_on_task`, `list_issues`, `raise_issue` |
| Requests and documents | `list_asks`, `get_ask`, `list_documents`, `read_document` |
| Money (finance and metrics roles) | `list_invoices`, `get_invoice`, `overdue_invoices`, `sales_today`, `pipeline`, `outcomes` |
| Comments and history | `edit_comment`, `task_history` |
| Team memory (staff) | `memory_brief`, `memory_search`, `memory_get`, `memory_record`, `memory_save`, `memory_update`, `memory_link`, `memory_connect`, `memory_graph`, `memory_verify`, `memory_stale`, `memory_delete` |

Lists come back as `{"items": [...]}`.

## Team memory

The memory tools are what make this more than a remote control: every agent on the team reads what
the others learned and records what it learns. The platform's setup prompt adds the team memory instructions to your project's `AGENTS.md`.
Create an API key in the web app to get the setup commands and prompt.

The server runs on your machine, inside your checkout, so it fills in the repository, branch and
commit from git, turns absolute paths into paths from the repository root, and `memory_verify`
compares a memory's files and symbols with your checkout without sending any code anywhere.

Optional environment: `UGIBO_AGENT` (your agent's name, shown on what it records) and
`UGIBO_REPO_DIR` (when the editor starts the server outside the repository).

## Local development

```bash
uv sync --locked
uv run ugibo-mcp
uv build
```

Set `UGIBO_API_KEY` and, when using a local API, `UGIBO_API_URL` before running.
This repository is independent of the backend and communicates only through its HTTP API.
For backend integration tests, clone this repository as `mcp/` beside the `backend/` checkout.
