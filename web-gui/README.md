# CycPep Studio Web GUI

The UI is intentionally empty until a real CycPep HTTP adapter is connected.
It contains no example projects, candidates, progress, evidence, or molecular
models.

## Local use

From PowerShell:

```powershell
.\start-local.ps1
```

Open the local URL printed by the UI server. In **连接**, use
`http://127.0.0.1:8765/api/v1` and choose **服务器同机模式**.

For remote compute, register the SSH key on the adapter host as described in
`../web_api/README.md`, then choose **SSH 远端模式** in the same page.

The full truthfulness contract, architecture, deployment choices, and remaining
production work are documented in `../docs/web_gui_implementation.md`.
