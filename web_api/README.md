# CycPep Studio HTTP adapter

Run from the repository root:

```powershell
python web_api/server.py --host 127.0.0.1 --port 8765
```

The adapter reads runtime data only through `State`, `CandidateIndex`, and
`EvidenceLogger`. It does not create a parallel dashboard database.

For SSH mode, register private keys on the adapter host, never in the browser:

```powershell
$env:CYCPEP_SSH_KEY_GPU1="C:\secure\keys\gpu1_ed25519"
```

The matching UI key alias is `gpu1`. The remote host must already exist in the
adapter user's `known_hosts`; strict host-key checking is mandatory.
