"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type Envelope<T> = { data?: T; error?: { code: string; message: string } };
type Candidate = { candidate_id: string; sequence: string; source_route?: string; final_status?: string; layers: boolean[]; all_layers_pass: boolean; artifact_id?: string; last_updated?: string };
type Evidence = { event_id?: string; timestamp?: string; agent?: string; event_type?: string; phase?: string; candidate_id?: string; [key: string]: unknown };
type Workflow = { status?: string; stage?: string; started_at?: string; updated_at?: string; finished_at?: string; critic_verdict?: string; runs?: { stage?: string; status?: string }[]; log_tail?: string; process_log_tail?: string; error?: { code?: string; message?: string }; process?: { pid?: number; alive?: boolean } };
type ProjectSummary = { slug: string; project_id?: string; name?: string; targets?: string[]; has_runtime?: boolean; phase?: string; run_status?: string; last_updated?: string };
type ProjectDraft = { draft_id: string; name?: string; project_id?: string; targets: { id?: string; uniprot?: string }[]; review?: { blocking_issues?: string[]; warnings?: string[] } };
type Snapshot = { source: { mode: "local" | "ssh"; connected: boolean; host?: string }; project: { project_id?: string; name?: string; targets: string[] }; state: { phase?: string; round?: number; candidate_count?: number; iteration_history: unknown[]; thresholds_ready: boolean; workflow?: Workflow }; stats: { total_candidates?: number; all_layers_pass?: number; finalized?: number }; candidates: Candidate[]; recent_evidence: Evidence[]; integrity_warnings: string[]; projects?: ProjectSummary[] };
type Settings = { apiBase: string; polling: number; autoRefresh: boolean };
type RightTab = "评分" | "证据" | "决策记录" | "参数";
type BottomTab = "运行日志" | "GPU 队列" | "Artifacts";
type MolViewer = { addModel(data: string, format: string): void; setStyle(selection: object, style: object): void; zoomTo(): void; render(): void; clear(): void; addSurface(type: unknown, style: object): void };

declare global { interface Window { $3Dmol?: { createViewer(element: HTMLElement, options: object): MolViewer; SurfaceType: { VDW: unknown } } } }

// The UI is served by Vinext, while the CycPep JSON adapter is a separate
// local process. Keep the adapter URL explicit so `/api/v1` is not mistaken
// for a route on the frontend dev server (which returns HTML).
const DEFAULT_SETTINGS: Settings = { apiBase: "http://127.0.0.1:8765/api/v1", polling: 5, autoRefresh: true };
const join = (base: string, path: string) => `${base.replace(/\/$/, "")}/${path.replace(/^\//, "")}`;
const formatTimestamp = (value?: string) => value ? new Date(value).toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : "—";
const AGENTS = [
  { id: "research", label: "Research", detail: "靶点、热点、正对照" },
  { id: "design", label: "Design", detail: "候选生成与闭环几何" },
  { id: "evaluate", label: "Prediction", detail: "结构预测与 L1–L7" },
  { id: "critic", label: "Critic", detail: "失败审查与策略调整" },
];

async function api<T>(base: string, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(join(base, path), { ...init, headers: { "Content-Type": "application/json", ...(init?.headers || {}) }, cache: "no-store" });
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) throw new Error("该地址没有提供 CycPep JSON 数据服务");
  const body = await response.json() as Envelope<T>;
  if (!response.ok || body.error) throw new Error(body.error?.message || `HTTP ${response.status}`);
  return body.data as T;
}

function useSettings() {
  const [value, setValue] = useState(DEFAULT_SETTINGS);
  useEffect(() => { try { const raw = localStorage.getItem("cycpep-studio-settings"); if (raw) setValue({ ...DEFAULT_SETTINGS, ...JSON.parse(raw) }); } catch {} }, []);
  const save = (next: Settings) => { setValue(next); localStorage.setItem("cycpep-studio-settings", JSON.stringify(next)); };
  return { value, save };
}

export default function Home() {
  const { value: settings, save } = useSettings();
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [rightTab, setRightTab] = useState<RightTab>("评分");
  const [bottomTab, setBottomTab] = useState<BottomTab>("运行日志");
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [connectionId, setConnectionId] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [toast, setToast] = useState("");
  const [candidateQuery, setCandidateQuery] = useState("");
  const [passedOnly, setPassedOnly] = useState(false);
  const [switchingProject, setSwitchingProject] = useState(false);
  const notify = (text: string) => { setToast(text); window.setTimeout(() => setToast(""), 3200); };

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = connectionId
        ? await api<Snapshot>(settings.apiBase, "connections/ssh/snapshot", { method: "POST", body: JSON.stringify({ connection_id: connectionId }) })
        : await api<Snapshot>(settings.apiBase, "snapshot");
      setSnapshot(data); setError("");
    } catch (cause) { setSnapshot(null); setError(cause instanceof Error ? cause.message : "连接失败"); }
    finally { setLoading(false); }
  }, [connectionId, settings.apiBase]);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => { if (!settings.autoRefresh) return; const id = window.setInterval(() => void refresh(), Math.max(2, settings.polling) * 1000); return () => window.clearInterval(id); }, [refresh, settings.autoRefresh, settings.polling]);
  useEffect(() => { const rows = snapshot?.candidates || []; if (!rows.some(row => row.candidate_id === selectedId)) setSelectedId(rows[0]?.candidate_id || ""); }, [snapshot, selectedId]);

  const selected = snapshot?.candidates.find(row => row.candidate_id === selectedId) || null;
  const visibleCandidates = useMemo(() => (snapshot?.candidates || []).filter(row => {
    const query = candidateQuery.trim().toLowerCase();
    return (!passedOnly || row.all_layers_pass) && (!query || row.candidate_id.toLowerCase().includes(query) || row.sequence.toLowerCase().includes(query));
  }), [snapshot, candidateQuery, passedOnly]);
  const artifactCount = snapshot?.candidates.filter(row => row.artifact_id).length || 0;
  const source = snapshot?.source.mode === "ssh" ? `SSH · ${snapshot.source.host}` : snapshot ? "同机数据层" : "未连接";
  const activeProject = snapshot?.projects?.find(project => project.project_id === snapshot.project.project_id);
  const switchProject = async (slug: string) => {
    if (!connectionId) return;
    setSwitchingProject(true);
    try {
      const result = await api<{ snapshot: Snapshot }>(settings.apiBase, "connections/ssh/projects/switch", { method: "POST", body: JSON.stringify({ connection_id: connectionId, slug }) });
      setSnapshot(result.snapshot); setSelectedId(result.snapshot.candidates[0]?.candidate_id || ""); notify("已切换项目");
    } catch (cause) { notify(cause instanceof Error ? cause.message : "切换失败"); }
    finally { setSwitchingProject(false); }
  };
  const workflowAction = async (action: "stop" | "retry") => {
    if (!connectionId) return;
    try {
      await api(settings.apiBase, `connections/ssh/workflow/${action}`, { method: "POST", body: JSON.stringify({ connection_id: connectionId, max_design_proposals: 4, max_prediction_candidates: 4, max_gpu_minutes: 360, max_rounds: 2 }) });
      notify(action === "stop" ? "已请求停止工作流" : "已重新启动工作流"); await refresh();
    } catch (cause) { notify(cause instanceof Error ? cause.message : "操作失败"); }
  };

  return <main className="workbench">
    <header className="workbench-header">
      <div className="wordmark"><span className="mark"><i/><i/><i/></span><div><b>CycPep Studio</b><small>AI DRUG DISCOVERY WORKBENCH</small></div></div>
      <div className="project-context"><span>项目</span>{connectionId && snapshot?.projects?.length ? <select aria-label="切换项目" value={activeProject?.slug || ""} disabled={switchingProject} onChange={event => void switchProject(event.target.value)}><option value="" disabled>选择项目</option>{snapshot.projects.map(project => <option key={project.slug} value={project.slug}>{project.name || project.project_id}{project.has_runtime ? " · 已运行" : " · 新项目"}</option>)}</select> : <b>{snapshot?.project.name || "尚未开始项目"}</b>}<small>{switchingProject ? "正在切换…" : snapshot?.project.targets?.join(" · ") || "等待项目初始化"}</small></div>
      <div className="header-actions"><span className={`connection-pill ${snapshot ? "online" : ""}`}><i/>{source}</span><button onClick={() => void refresh()} disabled={loading}>{loading ? "同步中" : "↻ 同步"}</button>{snapshot?.state.workflow?.process?.alive && <button onClick={() => void workflowAction("stop")}>停止</button>}{snapshot?.state.workflow?.status === "failed" && <button onClick={() => void workflowAction("retry")}>重试</button>}<button onClick={() => setConnectionOpen(true)}>连接</button><button className="accent" disabled={!connectionId} onClick={() => setCreateOpen(true)}>新建项目</button></div>
    </header>

    <section className="workbench-main">
      <aside className="agent-rail">
        <div className="section-label"><span>AGENT WORKFLOW</span><em>{snapshot?.state.phase || "NO RUN"}</em></div>
        <div className="agent-flow">{AGENTS.map((agent, index) => {
          const events = snapshot?.recent_evidence.filter(event => event.phase === agent.id || event.agent === agent.id).length || 0;
          const active = snapshot?.state.phase === agent.id || (agent.id === "evaluate" && snapshot?.state.phase === "prediction");
          return <button key={agent.id} className={active ? "active" : events ? "observed" : ""} onClick={() => { setRightTab("证据"); }}><span className="agent-index">0{index + 1}</span><div><b>{agent.label}</b><small>{agent.detail}</small></div><em>{active ? "运行态" : events ? `${events} events` : "无记录"}</em></button>;
        })}</div>
        <div className="candidate-rail"><div className="section-label"><span>CANDIDATES</span><em>{visibleCandidates.length}/{snapshot?.candidates.length || 0}</em></div><input value={candidateQuery} onChange={event => setCandidateQuery(event.target.value)} placeholder="搜索候选…" aria-label="搜索候选"/><label className="candidate-filter"><input type="checkbox" checked={passedOnly} onChange={event => setPassedOnly(event.target.checked)}/>只显示七层通过</label><div className="candidate-list">{visibleCandidates.slice(0, 100).map(row => <button key={row.candidate_id} className={selectedId === row.candidate_id ? "active" : ""} onClick={() => setSelectedId(row.candidate_id)}><div><b>{row.candidate_id}</b><code>{row.sequence}</code></div><span>{row.layers.filter(Boolean).length}/7</span></button>)}{!visibleCandidates.length && <p>没有符合筛选条件的候选</p>}</div></div>
      </aside>

      <section className="structure-stage">
        <div className="stage-toolbar"><div><span>STRUCTURE WORKSPACE</span><b>{selected?.candidate_id || "No candidate selected"}</b></div><select value={selectedId} onChange={event => setSelectedId(event.target.value)} disabled={!snapshot?.candidates.length}><option value="">选择候选</option>{snapshot?.candidates.map(row => <option key={row.candidate_id}>{row.candidate_id}</option>)}</select><div className="stage-meta"><span>{selected?.source_route || "route 未记录"}</span><span>{selected?.artifact_id ? "HASH VERIFIED" : "NO ARTIFACT"}</span></div></div>
        <StructureViewer candidate={selected} apiBase={settings.apiBase}/>
        <div className="structure-status"><span><i className="receptor"/>Receptor</span><span><i className="peptide"/>Cyclic peptide</span><em>{selected?.artifact_id ? `artifact ${selected.artifact_id}` : "等待 Prediction 注册真实坐标 artifact"}</em></div>
        {snapshot?.integrity_warnings.length ? <div className="integrity-alert"><b>STATE INTEGRITY</b><span>{snapshot.integrity_warnings.join(" · ")}</span></div> : null}
      </section>

      <aside className="inspector">
        <div className="tab-strip">{(["评分", "证据", "决策记录", "参数"] as RightTab[]).map(tab => <button key={tab} className={rightTab === tab ? "active" : ""} onClick={() => setRightTab(tab)}>{tab}</button>)}</div>
        <Inspector tab={rightTab} snapshot={snapshot} candidate={selected}/>
      </aside>
    </section>

    <section className="bottom-dock"><div className="dock-tabs">{(["运行日志", "GPU 队列", "Artifacts"] as BottomTab[]).map(tab => <button key={tab} className={bottomTab === tab ? "active" : ""} onClick={() => setBottomTab(tab)}>{tab}{tab === "Artifacts" && <em>{artifactCount}</em>}</button>)}<span>LIVE SOURCE · {source}</span></div><BottomDock tab={bottomTab} snapshot={snapshot} apiBase={settings.apiBase}/></section>

    {!snapshot && <div className="offline-overlay"><div><span>NO VERIFIED SOURCE</span><h1>连接真实工作环境</h1><p>Workbench 不会用示例候选、假进度或演示模型填充空白。</p><code>{error || "等待数据适配层"}</code><button onClick={() => setConnectionOpen(true)}>配置连接</button></div></div>}
    {connectionOpen && <ConnectionDialog settings={settings} save={save} onClose={() => setConnectionOpen(false)} onConnected={(data, id) => { setSnapshot(data); setConnectionId(id); setError(""); setConnectionOpen(false); notify("已连接真实数据源"); }} notify={notify}/>}
    {createOpen && <CreateDialog settings={settings} connectionId={connectionId} onClose={() => setCreateOpen(false)} onActivated={data => { setSnapshot(data); setSelectedId(""); setCreateOpen(false); notify("项目已批准并开始全自动运行"); }}/>} {toast && <div className="toast">{toast}</div>}
  </main>;
}

function StructureViewer({ candidate, apiBase }: { candidate: Candidate | null; apiBase: string }) {
  const element = useRef<HTMLDivElement>(null);
  const viewer = useRef<MolViewer | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [message, setMessage] = useState("");
  const [style, setStyleMode] = useState<"cartoon" | "sticks" | "surface">("cartoon");

  useEffect(() => {
    if (!candidate?.artifact_id || !element.current) { setState("idle"); setMessage(""); viewer.current?.clear(); viewer.current?.render(); return; }
    let cancelled = false;
    const load = async () => {
      setState("loading");
      try {
        if (!window.$3Dmol) await new Promise<void>((resolve, reject) => { const existing = document.querySelector("script[data-3dmol]"); if (existing) { existing.addEventListener("load", () => resolve(), { once: true }); return; } const script = document.createElement("script"); script.src = "https://cdnjs.cloudflare.com/ajax/libs/3Dmol/2.4.2/3Dmol-min.js"; script.dataset["3dmol"] = "true"; script.onload = () => resolve(); script.onerror = () => reject(new Error("3Dmol.js 加载失败")); document.head.appendChild(script); });
        const response = await fetch(join(apiBase, `artifacts/${candidate.artifact_id}/coordinates`), { cache: "no-store" });
        if (!response.ok) throw new Error(`坐标接口返回 HTTP ${response.status}`);
        const coordinates = await response.text();
        if (cancelled || !element.current || !window.$3Dmol) return;
        const instance = window.$3Dmol.createViewer(element.current, { backgroundColor: "#07110f", antialias: true });
        instance.addModel(coordinates, response.headers.get("content-type")?.includes("mmcif") ? "cif" : "pdb");
        instance.setStyle({}, { cartoon: { color: "spectrum" } }); instance.zoomTo(); instance.render(); viewer.current = instance; setState("ready");
      } catch (cause) { if (!cancelled) { setState("error"); setMessage(cause instanceof Error ? cause.message : "坐标加载失败"); } }
    };
    void load(); return () => { cancelled = true; };
  }, [candidate?.artifact_id, apiBase]);

  const applyStyle = (next: "cartoon" | "sticks" | "surface") => { setStyleMode(next); const instance = viewer.current; if (!instance || !window.$3Dmol) return; instance.setStyle({}, next === "cartoon" ? { cartoon: { color: "spectrum" } } : next === "sticks" ? { stick: { colorscheme: "Jmol" } } : { cartoon: { color: "#4bb891" } }); if (next === "surface") instance.addSurface(window.$3Dmol.SurfaceType.VDW, { opacity: 0.72, color: "#235c4c" }); instance.render(); };
  return <div className="viewer-shell"><div ref={element} className="viewer-canvas"/>{state === "idle" && <div className="viewer-empty"><div className="empty-orbit"><i/><i/><i/></div><span>VERIFIED ARTIFACT REQUIRED</span><h2>尚无可加载的真实结构</h2><p>只有七层全清、manifest 存在且 SHA-256 匹配的 PDB/mmCIF 才会在此显示。</p></div>}{state === "loading" && <div className="viewer-message">正在验证并加载坐标…</div>}{state === "error" && <div className="viewer-message error">{message}</div>}{state === "ready" && <div className="viewer-controls">{(["cartoon", "sticks", "surface"] as const).map(item => <button key={item} className={style === item ? "active" : ""} onClick={() => applyStyle(item)}>{item}</button>)}</div>}</div>;
}

function Inspector({ tab, snapshot, candidate }: { tab: RightTab; snapshot: Snapshot | null; candidate: Candidate | null }) {
  if (tab === "评分") return <div className="inspector-body"><div className="score-head"><span>SEVEN-LAYER BATTERY</span><b>{candidate ? `${candidate.layers.filter(Boolean).length}/7` : "—"}</b></div><div className="layer-list">{["环肽质量", "界面置信度", "界面物理", "环化几何", "设计意图", "鲁棒性", "可设计性"].map((label, index) => <div key={label}><span>L{index + 1}<small>{label}</small></span><b className={candidate?.layers[index] ? "pass" : "pending"}>{candidate?.layers[index] ? "PASS" : "NO PASS"}</b></div>)}</div><div className={`delivery-gate ${candidate?.all_layers_pass ? "pass" : ""}`}><span>DELIVERY GATE</span><b>{candidate?.all_layers_pass ? "SCIENTIFICALLY CLEARED" : "NOT DELIVERABLE"}</b></div></div>;
  if (tab === "证据") { const events = snapshot?.recent_evidence.filter(event => !candidate || !event.candidate_id || event.candidate_id === candidate.candidate_id).slice(-18).reverse() || []; return <div className="inspector-body event-list">{events.map((event, index) => <article key={event.event_id || index}><i/><div><b>{event.event_type || "unknown_event"}</b><span>{event.agent || "unknown"} · {event.phase || "no phase"}</span></div><time>{formatTimestamp(event.timestamp)}</time></article>)}{!events.length && <p className="empty-copy">没有对应证据事件</p>}</div>; }
  if (tab === "决策记录") { const decisions = snapshot?.recent_evidence.filter(event => ["critic", "planner", "orchestrator", "execution_worker"].includes(String(event.agent)) || String(event.event_type).includes("error")).slice(-20).reverse() || []; return <div className="inspector-body"><div className="reasoning-note"><b>DECISION TRACE</b><p>展示计划、审批、执行和审查产生的真实记录。</p></div><div className="decision-list">{decisions.map((event, index) => <article key={event.event_id || index}><span>{event.agent}</span><b>{event.event_type}</b><small>{formatTimestamp(event.timestamp)}</small></article>)}{!decisions.length && <p className="empty-copy">暂无决策记录</p>}</div></div>; }
  return <div className="inspector-body parameter-list"><div><span>Project ID</span><code>{snapshot?.project.project_id || "缺失"}</code></div><div><span>Round</span><code>{snapshot?.state.round ?? "未记录"}</code></div><div><span>Thresholds</span><code>{snapshot?.state.thresholds_ready ? "已载入" : "未载入"}</code></div><div><span>Polling</span><code>由本机连接偏好控制</code></div><p>参数写入 API 尚未实现；此处保持只读，避免界面改值却未影响真实运行。</p></div>;
}

function BottomDock({ tab, snapshot, apiBase }: { tab: BottomTab; snapshot: Snapshot | null; apiBase: string }) {
  const workflow = snapshot?.state.workflow;
  if (tab === "GPU 队列") return <div className="workflow-summary"><div><span>工作流</span><b>{workflow?.status || "尚未启动"}</b></div><div><span>当前阶段</span><b>{workflow?.stage || "—"}</b></div><div><span>进程</span><b>{workflow?.process?.alive ? `运行中 · PID ${workflow.process.pid}` : "未运行"}</b></div><div><span>更新时间</span><b>{formatTimestamp(workflow?.updated_at)}</b></div>{workflow?.error?.message && <p className="modal-error">{workflow.error.message}</p>}</div>;
  if (tab === "Artifacts") { const rows = snapshot?.candidates.filter(row => row.artifact_id) || []; return <div className="artifact-table">{rows.map(row => <div key={row.candidate_id}><b>{row.candidate_id}</b><code>{row.artifact_id}</code><span>SHA-256 verified</span><a href={join(apiBase, `artifacts/${row.artifact_id}/coordinates`)} download>下载坐标</a></div>)}{!rows.length && <div className="dock-empty"><b>NO VERIFIED ARTIFACTS</b><span>没有满足 all_layers_pass、manifest 与 hash 校验的坐标文件。</span></div>}</div>; }
  if (workflow?.process_log_tail || workflow?.log_tail) return <pre className="log-console workflow-log">{[workflow.process_log_tail, workflow.log_tail].filter(Boolean).join("\n\n")}</pre>;
  const events = snapshot?.recent_evidence.slice(-12).reverse() || []; return <div className="log-console">{events.map((event, index) => <div key={event.event_id || index}><time>{formatTimestamp(event.timestamp)}</time><span>{event.agent || "system"}</span><b>{event.event_type || "event"}</b><em>{event.candidate_id || event.phase || ""}</em></div>)}{!events.length && <div className="dock-empty"><b>等待运行记录</b><span>项目启动后，Research、Design、Prediction 与 Critic 的日志会显示在这里。</span></div>}</div>;
}
function ConnectionDialog({ settings, save, onClose, onConnected, notify }: { settings: Settings; save: (s: Settings) => void; onClose: () => void; onConnected: (s: Snapshot, id: string) => void; notify: (s: string) => void }) {
  const [apiBase, setApiBase] = useState(settings.apiBase); const [mode, setMode] = useState<"local" | "ssh">("ssh"); const [host, setHost] = useState("cn-north-b.ssh.damodel.com"); const [username, setUsername] = useState("root"); const [port, setPort] = useState(40584); const [password, setPassword] = useState(""); const [root, setRoot] = useState("/root/workspace/NovaPeptide/cycpep-mdm2-mdmx-full-workflow"); const [busy, setBusy] = useState(false);
  const connect = async () => { setBusy(true); try { if (mode === "local") { save({ ...settings, apiBase }); onConnected(await api<Snapshot>(apiBase, "snapshot"), ""); } else { const result = await api<{ connection_id: string; snapshot: Snapshot }>(settings.apiBase, "connections/ssh", { method: "POST", body: JSON.stringify({ host, username, port, password, workspace_root: root }) }); onConnected(result.snapshot, result.connection_id); } } catch (cause) { notify(cause instanceof Error ? cause.message : "连接失败"); } finally { setBusy(false); } };
  const incomplete = mode === "ssh" && (!host.trim() || !username.trim() || !password || !root.trim());
  return <div className="modal-backdrop"><section className="modal"><button className="modal-close" onClick={onClose}>×</button><span className="modal-kicker">连接工作环境</span><h2>{mode === "local" ? "连接本地项目" : "登录 SSH 服务器"}</h2><div className="mode-tabs"><button className={mode === "local" ? "active" : ""} onClick={() => setMode("local")}>本地项目</button><button className={mode === "ssh" ? "active" : ""} onClick={() => setMode("ssh")}>SSH 服务器</button></div>{mode === "local" ? <label>本地 API 地址<input value={apiBase} onChange={event => setApiBase(event.target.value)} placeholder="http://127.0.0.1:8765/api/v1"/></label> : <><div className="modal-grid"><label>服务器地址<input value={host} onChange={event => setHost(event.target.value)}/></label><label>端口<input type="number" value={port} onChange={event => setPort(Number(event.target.value))}/></label><label>用户名<input value={username} onChange={event => setUsername(event.target.value)}/></label><label>密码<input type="password" value={password} onChange={event => setPassword(event.target.value)}/></label><label className="wide">项目代码目录<input value={root} onChange={event => setRoot(event.target.value)}/></label></div><p className="security-copy">密码仅保存在当前适配层进程的内存中。</p></>}<button className="modal-primary" disabled={busy || incomplete} onClick={() => void connect()}>{busy ? "正在连接…" : mode === "local" ? "连接本地项目" : "登录服务器"}</button></section></div>;
}

function CreateDialog({ settings, connectionId, onClose, onActivated }: { settings: Settings; connectionId: string; onClose: () => void; onActivated: (s: Snapshot) => void }) {
  const [identifier, setIdentifier] = useState(""); const [kind, setKind] = useState("auto"); const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [draft, setDraft] = useState<ProjectDraft | null>(null); const [designCount, setDesignCount] = useState(4); const [predictionCount, setPredictionCount] = useState(4); const [gpuMinutes, setGpuMinutes] = useState(360); const [rounds, setRounds] = useState(2);
  const create = async () => { setBusy(true); setError(""); try { if (!connectionId) throw new Error("请先连接 SSH 服务器"); const job = await api<{ job_id: string }>(settings.apiBase, "connections/ssh/project-drafts", { method: "POST", body: JSON.stringify({ connection_id: connectionId, identifier, identifier_type: kind, organism_id: 9606, objective: "binder" }) }); let result: { status: string; result?: ProjectDraft; error?: string } = { status: "running" }; for (let attempt = 0; attempt < 90 && result.status === "running"; attempt += 1) { await new Promise(resolve => window.setTimeout(resolve, 2000)); result = await api<{ status: string; result?: ProjectDraft; error?: string }>(settings.apiBase, "connections/ssh/project-drafts/status", { method: "POST", body: JSON.stringify({ job_id: job.job_id }) }); } if (result.status !== "complete" || !result.result) throw new Error(result.error || "靶点解析超时"); setDraft(result.result); } catch (cause) { setError(cause instanceof Error ? cause.message : "创建失败"); } finally { setBusy(false); } };
  const activate = async () => { if (!draft) return; setBusy(true); setError(""); try { const approved = await api<{ snapshot: Snapshot }>(settings.apiBase, "connections/ssh/project-drafts/approve", { method: "POST", body: JSON.stringify({ connection_id: connectionId, draft_id: draft.draft_id }) }); await api(settings.apiBase, "connections/ssh/workflow/start", { method: "POST", body: JSON.stringify({ connection_id: connectionId, max_design_proposals: designCount, max_prediction_candidates: predictionCount, max_gpu_minutes: gpuMinutes, max_rounds: rounds, approver: "CycPep Studio user", justification: "Run the approved project end to end" }) }); onActivated({ ...approved.snapshot, state: { ...approved.snapshot.state, workflow: { status: "starting", stage: "research" } } }); } catch (cause) { setError(cause instanceof Error ? cause.message : "启动失败"); } finally { setBusy(false); } };
  const blocking = draft?.review?.blocking_issues || [];
  if (!draft) return <div className="modal-backdrop"><section className="modal compact"><button className="modal-close" onClick={onClose}>×</button><span className="modal-kicker">TARGET BOOTSTRAP</span><h2>新建项目</h2><label>Gene / UniProt / PDB<input value={identifier} onChange={event => setIdentifier(event.target.value)} autoFocus/></label><label>标识类型<select value={kind} onChange={event => setKind(event.target.value)}><option value="auto">自动识别</option><option value="gene">Gene</option><option value="uniprot">UniProt</option><option value="pdb">PDB</option></select></label>{error && <div className="modal-error">{error}</div>}<button className="modal-primary" disabled={!identifier.trim() || busy} onClick={() => void create()}>{busy ? "服务器正在解析靶点…" : "解析并创建项目"}</button></section></div>;
  return <div className="modal-backdrop"><section className="modal compact"><button className="modal-close" disabled={busy} onClick={onClose}>×</button><span className="modal-kicker">项目准备完成</span><h2>{draft.name || draft.project_id}</h2><div className="draft-result"><div><span>草稿编号</span><code>{draft.draft_id}</code></div><div><span>靶点</span><b>{draft.targets.map(target => `${target.id || "未知"}${target.uniprot ? ` · ${target.uniprot}` : ""}`).join("、")}</b></div></div>{blocking.length > 0 && <div className="draft-notes"><b>需要先处理</b>{blocking.map(item => <span key={item}>{item}</span>)}</div>}<div className="modal-grid"><label>Design 候选数<input type="number" min={1} max={100} value={designCount} onChange={event => { const value = Number(event.target.value); setDesignCount(value); setPredictionCount(Math.min(predictionCount, value)); }}/></label><label>Prediction 数量<input type="number" min={1} max={designCount} value={predictionCount} onChange={event => setPredictionCount(Number(event.target.value))}/></label><label>GPU 分钟预算<input type="number" min={1} value={gpuMinutes} onChange={event => setGpuMinutes(Number(event.target.value))}/></label><label>最多迭代轮数<input type="number" min={1} max={10} value={rounds} onChange={event => setRounds(Number(event.target.value))}/></label></div>{error && <div className="modal-error">{error}</div>}<button className="modal-primary" disabled={busy || blocking.length > 0} onClick={() => void activate()}>{busy ? "正在批准并启动…" : "批准并完整运行"}</button></section></div>;
}
