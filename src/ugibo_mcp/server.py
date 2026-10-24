"""The Ugibo MCP server (MCP-01..04).

Every tool calls the Ugibo API with the person's own API key, so an agent can
do exactly what that person can do in the web app and nothing more (API-02).
Secrets never come through here: the API refuses vault access to API keys
(VLT-04), and this server has no tool for it.

Run:  UGIBO_API_KEY=ugk_... UGIBO_API_URL=https://api.ugibo.com ugibo-mcp
"""

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

INSTRUCTIONS = """Ugibo is a software firm's client platform: organizations (clients), engagements \
(requests for work and the sales pipeline), projects with phases, milestones and tasks, \
requests to clients (asks), documents, quotes, contracts and invoices.

Team memory (staff only): before you start work on a task, call memory_brief with the task's \
reference (ALP-12) and the files you expect to touch, and heed every warning, failed attempt and \
invariant it returns. When you finish, or the moment something fails in a way the next person \
would repeat, call memory_record. When a memory you relied on no longer matches the code, call \
memory_verify. Paths are always relative to the repository root.

Everything you see and do is limited to what the key's owner can see and do. Ids look like \
32 hex characters; references like ALP-12 (task), BUG-3 (issue), INV-2026-27-004 (invoice) are \
for people. Prefer reading before writing, and say what you changed. Write plainly and never \
use em dashes or en dashes in anything you create."""


class Api:
    """A small client for the Ugibo API that turns errors into readable text."""

    def __init__(self, base_url: str, key: str, client: httpx.AsyncClient | None = None):
        self.base = base_url.rstrip("/") + "/api"
        self.headers = {"Authorization": f"Bearer {key}"}
        self.client = client or httpx.AsyncClient(timeout=30)

    async def call(self, method: str, path: str, **kwargs) -> Any:
        r = await self.client.request(method, self.base + path, headers=self.headers, **kwargs)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:300]
            # Field checks answer a list ([{loc, msg}, ...]); name each field
            # in plain words instead of passing on the raw structure.
            if isinstance(detail, list):
                detail = "; ".join(f"{'.'.join(str(p) for p in e.get('loc', [])[1:]) or 'request'}: {e.get('msg', '')}" if isinstance(e, dict) else str(e) for e in detail)
            # A ToolError reaches the agent with its message, so it can correct
            # itself ("Unknown status"); any other exception arrives as a bare
            # "Error executing tool".
            raise ToolError(f"Ugibo said {r.status_code}: {detail}")
        return r.json() if r.content else {"ok": True}


# ---- the local checkout: what the server can see that the API cannot ----


def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=os.environ.get("UGIBO_REPO_DIR") or os.getcwd(), capture_output=True, text=True, timeout=3)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def git_context() -> dict:
    """The repository this agent is working in, so memories say where they
    came from without the agent having to look it up."""
    found = {"repo": _git("remote", "get-url", "origin"), "branch": _git("rev-parse", "--abbrev-ref", "HEAD"), "commit": _git("rev-parse", "--short=12", "HEAD")}
    return {k: v for k, v in found.items() if v}


def repo_root() -> Path | None:
    top = _git("rev-parse", "--show-toplevel")
    return Path(top) if top else None


def relative(path: str) -> str:
    """Whatever path the agent has, as one from the repository root."""
    p = Path(path)
    root = repo_root()
    if p.is_absolute() and root:
        try:
            return p.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return path
    return path.replace("\\", "/")


def fingerprint(path: str, symbol: str) -> str:
    """A short hash of the lines from the symbol on, so a later agent can
    tell whether the code it describes has changed since."""
    root = repo_root()
    if not root or not symbol:
        return ""
    try:
        lines = (root / path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for i, line in enumerate(lines):
        if symbol in line:
            chunk = "\n".join(x.strip() for x in lines[i : i + 30])
            return hashlib.sha1(chunk.encode()).hexdigest()[:16]
    return ""


def anchors_in(anchors: list[dict] | None) -> list[dict]:
    ctx = git_context()
    out = []
    for a in anchors or []:
        a = dict(a)
        if a.get("path"):
            a["path"] = relative(a["path"])
        a.setdefault("repo", ctx.get("repo", ""))
        a.setdefault("commit", ctx.get("commit", ""))
        if a.get("symbol") and a.get("path") and not a.get("fingerprint"):
            a["fingerprint"] = fingerprint(a["path"], a["symbol"])
        out.append(a)
    return out


def _same_repo(remote: str, key: str) -> bool:
    """Does this checkout's remote match a stored repo key (host/owner/name)?"""
    r = remote.lower().rstrip("/")
    r = r[:-4] if r.endswith(".git") else r
    return r.replace(":", "/").endswith(key.split("/", 1)[-1])


def check_locally(memory: dict) -> tuple[str, str]:
    """Compare a memory's anchors with this checkout: holds, changed or gone."""
    root = repo_root()
    if not root:
        return "", "Not inside a git checkout, so there is nothing to compare."
    here = git_context().get("repo", "")
    mine = [a for a in memory.get("anchors") or [] if not a.get("repo") or _same_repo(here, a["repo"])]
    if not mine:
        return "", "None of its anchors are in this repository."
    notes, results = [], []
    for a in mine:
        f = root / a.get("path", "")
        if a.get("path") and not f.exists():
            results.append("gone")
            notes.append(f"{a['path']} no longer exists")
            continue
        if a.get("symbol"):
            text = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""
            if a["symbol"] not in text:
                results.append("gone")
                notes.append(f"{a['symbol']} is no longer in {a.get('path')}")
                continue
            if a.get("fingerprint") and fingerprint(a["path"], a["symbol"]) != a["fingerprint"]:
                results.append("changed")
                notes.append(f"{a['symbol']} in {a['path']} has changed since it was recorded")
                continue
        results.append("holds")
    if all(r == "gone" for r in results):
        result = "gone"
    elif any(r != "holds" for r in results):
        result = "changed"
    else:
        result = "holds"
    return result, "; ".join(notes) or "Every anchor still matches."


def build(api: Api) -> MCPServer:
    server = MCPServer(name="ugibo", title="Ugibo", instructions=INSTRUCTIONS, version="0.1.0")

    def clean(params: dict) -> dict:
        return {k: v for k, v in params.items() if v not in (None, "")}

    # ---- who and what ----

    @server.tool(description="Who the key belongs to, their roles and organizations.")
    async def whoami() -> dict:
        me = await api.call("GET", "/me")
        return {"person": me["person"], "is_staff": me["is_staff"], "roles": me["staff_roles"], "organizations": me["organizations"]}

    @server.tool(description="List organizations (clients) you can see, optionally filtered by a search string.")
    async def list_organizations(query: str = "") -> dict:
        items = (await api.call("GET", "/orgs"))["items"]
        q = query.lower().strip()
        return {"items": [{"id": o["id"], "name": o["name"], "stage": o.get("stage")} for o in items if not q or q in o["name"].lower()]}

    @server.tool(description="One organization's details and its people.")
    async def get_organization(organization_id: str) -> dict:
        org = await api.call("GET", f"/orgs/{organization_id}")
        members = await api.call("GET", f"/orgs/{organization_id}/members")
        return {**org, "members": members["items"]}

    @server.tool(description="List engagements (requests for work). Filter by organization, stage or who it is waiting on (ugibo or client).")
    async def list_engagements(organization_id: str = "", stage: str = "", waiting_on: str = "", open_only: bool = True) -> dict:
        params = clean({"organization_id": organization_id, "stage": stage, "waiting_on": waiting_on, "include_closed": str(not open_only).lower()})
        return {"items": (await api.call("GET", "/engagements", params=params))["items"]}

    # ---- delivery ----

    @server.tool(description="List projects, with phases and honest progress.")
    async def list_projects(organization_id: str = "") -> dict:
        items = (await api.call("GET", "/projects", params=clean({"organization_id": organization_id})))["items"]
        return {"items": [{"id": p["id"], "ref": p["ref"], "name": p["name"], "status": p["status"], "progress": p["progress"]["percent"], "phases": [{"id": ph["id"], "name": ph["name"], "stage": ph["stage"], "progress": ph["progress"]["percent"]} for ph in p["phases"]]} for p in items]}

    @server.tool(description="A project in full: phases, milestones, progress, scope changes, review rounds.")
    async def get_project(project_id: str) -> dict:
        return await api.call("GET", f"/projects/{project_id}")

    @server.tool(description="List a project's tasks. status: todo, in_progress, in_review, blocked, done, cancelled. mine=true for tasks assigned to you.")
    async def list_tasks(project_id: str, status: str = "", mine: bool = False) -> dict:
        params = clean({"status": status, "mine": "true" if mine else ""})
        return {"items": (await api.call("GET", f"/projects/{project_id}/tasks", params=params))["items"]}

    @server.tool(description="One task with its subtasks, workflow stages, comments and where it came from.")
    async def get_task(task_id: str) -> dict:
        return await api.call("GET", f"/tasks/{task_id}")

    @server.tool(description="Create a task (or a subtask with parent_id). Dates are YYYY-MM-DD.")
    async def create_task(project_id: str, title: str, description: str = "", phase_id: str = "", milestone_id: str = "", parent_id: str = "", due_on: str = "", estimate_hours: float | None = None, priority: str = "normal") -> dict:
        body = clean({"title": title, "description": description, "phase_id": phase_id, "milestone_id": milestone_id, "parent_id": parent_id, "due_on": due_on, "estimate_hours": estimate_hours, "priority": priority})
        return await api.call("POST", f"/projects/{project_id}/tasks", json=body)

    @server.tool(description="Change a task's title, description, due date (YYYY-MM-DD), estimate or priority.")
    async def update_task(task_id: str, title: str = "", description: str = "", due_on: str = "", estimate_hours: float | None = None, priority: str = "") -> dict:
        return await api.call("PATCH", f"/tasks/{task_id}", json=clean({"title": title, "description": description, "due_on": due_on, "estimate_hours": estimate_hours, "priority": priority}))

    @server.tool(description="Finish the task's current workflow stage (its strictness may require evidence first).")
    async def advance_task(task_id: str) -> dict:
        return await api.call("POST", f"/tasks/{task_id}/advance")

    @server.tool(description="Set a task's status: todo, in_progress, in_review, blocked, cancelled (use advance_task to finish stages).")
    async def set_task_status(task_id: str, status: str) -> dict:
        return await api.call("POST", f"/tasks/{task_id}/status", json={"status": status})

    @server.tool(description="Comment on a task, optionally for a workflow stage (research, plan, build, test) and with links such as merge requests. Returns the task with comment_id, the new comment's id for edit_comment.")
    async def comment_on_task(task_id: str, body: str, stage: str = "", links: list[str] | None = None) -> dict:
        return await api.call("POST", f"/tasks/{task_id}/comments", json={"body": body, "stage": stage, "links": [{"url": u} for u in links or []]})

    @server.tool(description="List a project's QA and UAT issues (BUG-n, CR-n, FR-n).")
    async def list_issues(project_id: str, status: str = "") -> dict:
        return {"items": (await api.call("GET", f"/projects/{project_id}/issues", params=clean({"status": status})))["items"]}

    @server.tool(description="Raise an issue on a project. severity: blocker, major, minor, cosmetic.")
    async def raise_issue(project_id: str, title: str, steps: str = "", expected: str = "", actual: str = "", severity: str = "minor", environment: str = "uat") -> dict:
        return await api.call("POST", f"/projects/{project_id}/issues", json={"title": title, "steps": steps, "expected": expected, "actual": actual, "severity": severity, "environment": environment})

    # ---- requests, documents ----

    @server.tool(description="List requests sent to clients (asks). status: draft, sent, answered, closed.")
    async def list_asks(organization_id: str = "", status: str = "") -> dict:
        return {"items": (await api.call("GET", "/asks", params=clean({"organization_id": organization_id, "status": status})))["items"]}

    @server.tool(description="One ask with its items and answers (credentials are never included).")
    async def get_ask(ask_id: str) -> dict:
        return await api.call("GET", f"/asks/{ask_id}")

    @server.tool(description="List documents (SOWs, blueprints, proposals...) for an organization.")
    async def list_documents(organization_id: str = "") -> dict:
        return {"items": (await api.call("GET", "/documents", params=clean({"organization_id": organization_id})))["items"]}

    @server.tool(description="Read a document's blocks (staff get the draft, clients the latest sent version).")
    async def read_document(document_id: str) -> dict:
        return await api.call("GET", f"/documents/{document_id}")

    # ---- money (needs the finance or metrics permissions) ----

    @server.tool(description="List invoices. state: draft, due, overdue, paid, void.")
    async def list_invoices(state: str = "", organization_id: str = "") -> dict:
        return {"items": (await api.call("GET", "/invoices", params=clean({"state": state, "organization_id": organization_id})))["items"]}

    @server.tool(description="One invoice with payments and, for staff, its timeline (issued, emailed, viewed, paid).")
    async def get_invoice(invoice_id: str) -> dict:
        return await api.call("GET", f"/invoices/{invoice_id}")

    @server.tool(description="A project's billing plan: each line (on signing, at a milestone, on delivery, on a date, by hand, every month, every sprint) with its periods and their state: invoiced, drafted, due, upcoming or skipped.")
    async def billing_plan(project_id: str) -> dict:
        return await api.call("GET", f"/projects/{project_id}/billing")

    @server.tool(description="Add a line to a project's billing plan. amount is before tax. trigger: signing, milestone (give trigger_ref, the milestone's name), delivery, date (give due_on YYYY-MM-DD), manual, monthly (due_on is the first month; blank starts with the project), sprint. count: how many months or sprints; blank until stopped. taxes: [{label, rate}] only if asked; nothing is added automatically.")
    async def add_billing_line(project_id: str, label: str, amount: float, trigger: str = "manual", trigger_ref: str = "", due_on: str = "", count: int | None = None, taxes: list[dict] | None = None, remind: bool = True, client_visible: bool = True, note: str = "") -> dict:
        body = clean({"label": label, "amount": amount, "trigger": trigger, "trigger_ref": trigger_ref, "due_on": due_on, "count": count, "note": note})
        return await api.call("POST", f"/projects/{project_id}/billing/items", json={**body, "taxes": taxes or [], "remind": remind, "client_visible": client_visible})

    @server.tool(description="Change a billing plan line. Send every field as it should be (read billing_plan first). stopped=true ends a monthly or sprint line; remind=false keeps it off Today; client_visible=false hides it from the client.")
    async def update_billing_line(item_id: str, label: str, amount: float, trigger: str, trigger_ref: str = "", due_on: str = "", count: int | None = None, taxes: list[dict] | None = None, stopped: bool = False, remind: bool = True, client_visible: bool = True, note: str = "") -> dict:
        body = {"label": label, "amount": amount, "trigger": trigger, "trigger_ref": trigger_ref, "due_on": due_on or None, "count": count, "taxes": taxes or [], "stopped": stopped, "remind": remind, "client_visible": client_visible, "note": note}
        return await api.call("PUT", f"/billing-items/{item_id}", json=body)

    @server.tool(description="Skip one period of a billing plan line (a month, a sprint, or 'once'), or skip=false to bill it after all.")
    async def skip_billing_period(item_id: str, period: str = "once", skip: bool = True) -> dict:
        return await api.call("POST", f"/billing-items/{item_id}/skip", json={"period": period, "skip": skip})

    @server.tool(description="Draft the invoice for one period of a billing plan line (period from billing_plan: 'once', '2026-11', or a sprint's id). It stays a draft for a person to check and send.")
    async def draft_invoice_from_plan(item_id: str, period: str = "once") -> dict:
        return await api.call("POST", f"/billing-items/{item_id}/invoice", json={"period": period})

    @server.tool(description="Overdue invoices: who owes what, since when, who it was sent to and whether they viewed it.")
    async def overdue_invoices() -> dict:
        return await api.call("GET", "/metrics/overdue-invoices")

    @server.tool(description="Today's numbers: quotes accepted, payments received, invoices issued.")
    async def sales_today() -> dict:
        return await api.call("GET", "/metrics/today")

    @server.tool(description="The pipeline by stage, with counts and estimated value.")
    async def pipeline() -> dict:
        return await api.call("GET", "/metrics/pipeline")

    @server.tool(description="Outcome and guardrail metrics over a window of days (turnaround, days to pay, on-time phases, opt-outs, the trust pulse).")
    async def outcomes(days: int = 90) -> dict:
        return await api.call("GET", "/metrics/outcomes", params={"days": days})


    # ---- comments and history ----

    @server.tool(description="Edit a comment you wrote on a task. The earlier wording stays in the task's history.")
    async def edit_comment(task_id: str, comment_id: str, body: str) -> dict:
        return await api.call("PATCH", f"/tasks/{task_id}/comments/{comment_id}", json={"body": body})

    @server.tool(description="Everything that happened to a task: edits, status changes, comments, deletions and restores, who did each and when.")
    async def task_history(task_id: str) -> dict:
        return await api.call("GET", f"/history/task/{task_id}")

    # ---- team memory (staff only) ----

    agent = os.environ.get("UGIBO_AGENT", "mcp")

    @server.tool(description="READ THIS FIRST when you start on a task. Returns what the team learned that applies: memories on the task, its parents, phase, project and client, plus those anchored to the files you will touch. 'heed' lists warnings, failed attempts, invariants and bugs: do not repeat them. ref is a task, project or engagement reference (ALP-12, ALP, ENG-3). paths are relative to the repository root.")
    async def memory_brief(ref: str = "", paths: list[str] | None = None) -> dict:
        params = clean({"ref": ref, "repo": git_context().get("repo", "")})
        if paths:
            params["paths"] = [relative(p) for p in paths]
        return await api.call("GET", "/memories/brief", params=params)

    @server.tool(description="Search team memory. query is fuzzy (typos forgiven); regex is a regular expression over title, body, advice, tags and anchors. Filter by kind (note, decision, warning, failed_attempt, bug, fix, invariant, alert, howto), severity (low, medium, high, critical), status (active, outdated, resolved, all), tag, path, symbol or ref (ALP-12). this_repo=true limits to the current repository.")
    async def memory_search(query: str = "", regex: str = "", kind: list[str] | None = None, severity: list[str] | None = None, status: str = "active", tag: str = "", path: str = "", symbol: str = "", ref: str = "", this_repo: bool = False, limit: int = 20) -> dict:
        params = clean({"q": query, "regex": regex, "status": status, "tag": tag, "path": relative(path) if path else "", "symbol": symbol, "ref": ref, "limit": limit, "repo": git_context().get("repo", "") if this_repo else ""})
        if kind:
            params["kind"] = kind
        if severity:
            params["severity"] = severity
        return await api.call("GET", "/memories", params=params)

    @server.tool(description="One memory in full: body, advice, anchors, what it links to, the memories it connects to (and how), and who checked it against the code.")
    async def memory_get(ref: str) -> dict:
        return await api.call("GET", f"/memories/{ref}")

    @server.tool(description="Record what you learned, in one call, when you finish a piece of work or the moment something fails. summary is one sentence; reasoning says why. Each warning, failed attempt, decision, invariant, bug, fix or how-to becomes its own memory, linked to the summary. Write each so it reads alone months later: the rule, the failure it prevents, the evidence. links are references (ALP-12, ENG-3). anchors are {path, symbol, line} from the repository root; repo, branch and commit are filled in for you. caused_by lists memories this follows from (MEM-12).")
    async def memory_record(summary: str, links: list[str], reasoning: str = "", kind: str = "note", severity: str = "medium", warnings: list[str] | None = None, failed_attempts: list[str] | None = None, decisions: list[str] | None = None, invariants: list[str] | None = None, bugs: list[str] | None = None, fixes: list[str] | None = None, howto: list[str] | None = None, notes: list[str] | None = None, tags: list[str] | None = None, anchors: list[dict] | None = None, caused_by: list[str] | None = None) -> dict:
        body = {
            "summary": summary, "kind": kind, "severity": severity, "reasoning": reasoning,
            "warnings": warnings or [], "failed_attempts": failed_attempts or [], "decisions": decisions or [], "invariants": invariants or [],
            "bugs": bugs or [], "fixes": fixes or [], "howto": howto or [], "notes": notes or [], "tags": tags or [],
            "anchors": anchors_in(anchors), "links": [{"ref": r} for r in links], "caused_by": caused_by or [],
            "context": git_context(), "agent": agent,
        }
        return await api.call("POST", "/memories/record", json=body)

    @server.tool(description="Save a single memory. kind: note, decision, warning, failed_attempt, bug, fix, invariant, alert, howto. advice is the one line the next agent should act on. links are references (ALP-12). related: [{memory: 'MEM-3', kind: 'caused_by', 'fixes', 'supersedes', 'contradicts', 'depends_on', 'refines', 'led_to' or 'relates_to'}].")
    async def memory_save(title: str, kind: str = "note", severity: str = "medium", body: str = "", advice: str = "", links: list[str] | None = None, tags: list[str] | None = None, anchors: list[dict] | None = None, related: list[dict] | None = None) -> dict:
        payload = {"title": title, "kind": kind, "severity": severity, "body": body, "advice": advice, "tags": tags or [], "anchors": anchors_in(anchors), "links": [{"ref": r} for r in links or []], "related": related or [], "context": git_context(), "agent": agent}
        return await api.call("POST", "/memories", json=payload)

    @server.tool(description="Change a memory: status (active, outdated, resolved), severity, advice, body, tags. Mark it resolved when the problem it describes is gone, outdated when it no longer holds.")
    async def memory_update(ref: str, status: str = "", severity: str = "", title: str = "", body: str = "", advice: str = "", tags: list[str] | None = None) -> dict:
        payload = clean({"status": status, "severity": severity, "title": title, "body": body, "advice": advice})
        if tags is not None:
            payload["tags"] = tags
        return await api.call("PATCH", f"/memories/{ref}", json=payload)

    @server.tool(description="Attach a memory to another task, subtask, phase, project or engagement (by reference, such as ALP-14).")
    async def memory_link(ref: str, to: str) -> dict:
        return await api.call("POST", f"/memories/{ref}/links", json={"ref": to})

    @server.tool(description="Connect two memories, read as: from <kind> to. kind: relates_to, caused_by, led_to, fixes, supersedes (marks the older one outdated), contradicts, depends_on, refines.")
    async def memory_connect(from_ref: str, to_ref: str, kind: str = "relates_to", note: str = "") -> dict:
        return await api.call("POST", f"/memories/{from_ref}/edges", json={"to": to_ref, "kind": kind, "note": note})

    @server.tool(description="Crawl the memory graph outward from a memory (root=MEM-3) or from a task or project (ref=ALP-12): along memory links and through the tasks they share.")
    async def memory_graph(root: str = "", ref: str = "", depth: int = 2) -> dict:
        return await api.call("GET", "/memories/graph", params=clean({"root": root, "ref": ref, "depth": depth}))

    @server.tool(description="Check a memory against THIS checkout and report it: do its files and symbols still exist, unchanged? Leave result empty to let the server compare for you, or say holds, changed or gone yourself with a note. gone retires the memory.")
    async def memory_verify(ref: str, result: str = "", note: str = "") -> dict:
        m = await api.call("GET", f"/memories/{ref}")
        found, detail = check_locally(m)
        result = result or found
        if not result:
            return {"checked": False, "why": detail}
        ctx = git_context()
        await api.call("POST", f"/memories/{ref}/checks", json={"result": result, "note": note or detail, "repo": ctx.get("repo", ""), "commit": ctx.get("commit", ""), "agent": agent})
        return {"checked": True, "result": result, "detail": note or detail}

    @server.tool(description="Warnings, invariants and the like that nobody has checked against the code in a while. Good to verify when you are already in those files.")
    async def memory_stale(days: int = 60) -> dict:
        return await api.call("GET", "/memories/stale", params={"days": days})

    @server.tool(description="Move a memory to the Bin (it can be restored). Prefer memory_update with status outdated or resolved: history is worth keeping.")
    async def memory_delete(ref: str) -> dict:
        return await api.call("DELETE", f"/memories/{ref}")

    return server


def main() -> None:
    key = os.environ.get("UGIBO_API_KEY", "")
    if not key.startswith("ugk_"):
        raise SystemExit("Set UGIBO_API_KEY to a personal key from Ugibo (Settings > API keys).")
    build(Api(os.environ.get("UGIBO_API_URL", "https://api.ugibo.com"), key)).run("stdio")


if __name__ == "__main__":
    main()

