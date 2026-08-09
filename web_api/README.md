# CycPep Studio HTTP adapter

Run from the repository root:

```powershell
python web_api/server.py --host 127.0.0.1 --port 8765
```

Frontend V2 observes the current project and validated current execution graph through:

```text
GET /api/v2/workbench
```

This endpoint is read-only. It joins the public Orchestrator/Plan status, Action
Catalog and Action Registry availability, and project Store records into one
browser-safe workbench response. It does not infer workflow or transaction state
from `State.phase`, JSON/CSV/JSONL projections, log text, or a fixed Agent sequence,
and it does not create or refresh state while serving a request.

Existing `/api/v1` routes remain compatibility interfaces. In particular,
`/api/v1/snapshot` is not the Frontend V2 workflow authority. The existing GUI
continues to use its v1 integration until a separate frontend change migrates it;
this backend-first change does not modify `web-gui/` production code.

PR #26 is a UX/product reference for workbench and observability concepts only.
Its code, controller, workflow-state model, and API assumptions are not dependencies
of this adapter.

For SSH mode, register private keys on the adapter host, never in the browser:

```powershell
$env:CYCPEP_SSH_KEY_GPU1="C:\secure\keys\gpu1_ed25519"
```

The matching UI key alias is `gpu1`. The remote host must already exist in the
adapter user's `known_hosts`; strict host-key checking is mandatory.
