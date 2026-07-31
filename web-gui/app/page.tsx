"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

type View = "总览" | "靶点与审核" | "候选池" | "最终交付" | "证据链" | "连接";
type Envelope<T> = { data?: T; error?: { code: string; message: string } };
type Target = { id: string; uniprot?: string; required?: boolean; binding_site?: { residues?: number[]; status?: string }; structure?: { pdb_id?: string; chain?: string; coordinate_artifact_id?: string }; structure_plan?: { coordinates_ready?: boolean; chain_reviewed?: boolean; binding_site_reviewed?: boolean; ready_for_design?: boolean } };
type Draft = { draft_id: string; project_id?: string; name?: string; objective?: string; targets: Target[]; review?: { status?: string; revision?: number; content_digest?: string; blocking_issues?: string[]; warnings?: string[] } };
type Candidate = { candidate_id: string; sequence: string; source_route?: string; final_status?: string; layers: boolean[]; all_layers_pass: boolean; artifact_id?: string; last_updated?: string };
type Evidence = { event_id?: string; timestamp?: string; agent?: string; event_type?: string; phase?: string; candidate_id?: string };
type Snapshot = { source: { mode: "local" | "ssh"; connected: boolean; host?: string }; project: { project_id?: string; name?: string; config?: { targets?: Target[] }; targets: string[] }; state: { phase?: string; round?: number; candidate_count?: number; iteration_history: unknown[]; thresholds_ready: boolean }; stats: { total_candidates?: number; all_layers_pass?: number; finalized?: number }; candidates: Candidate[]; recent_evidence: Evidence[]; integrity_warnings: string[] };
type Settings = { apiBase: string; polling: number; autoRefresh: boolean };

const DEFAULT_SETTINGS: Settings = { apiBase: "/api/v1", polling: 5, autoRefresh: true };
const NAV: View[] = ["总览", "靶点与审核", "候选池", "最终交付", "证据链", "连接"];
const join = (base: string, path: string) => `${base.replace(/\/$/, "")}/${path.replace(/^\//, "")}`;
const layerCount = (candidate: Candidate) => candidate.layers.filter(Boolean).length;

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
  const [view, setView] = useState<View>("总览");
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [connectionId, setConnectionId] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [toast, setToast] = useState("");
  const notify = (message: string) => { setToast(message); window.setTimeout(() => setToast(""), 3200); };
  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const next = connectionId ? await api<Snapshot>(settings.apiBase, "connections/ssh/snapshot", { method: "POST", body: JSON.stringify({ connection_id: connectionId }) }) : await api<Snapshot>(settings.apiBase, "snapshot");
      setSnapshot(next); setError("");
    } catch (cause) { setSnapshot(null); setError(cause instanceof Error ? cause.message : "无法连接数据服务"); }
    finally { setLoading(false); }
  }, [connectionId, settings.apiBase]);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => { if (!settings.autoRefresh) return; const id = window.setInterval(() => void refresh(), Math.max(2, settings.polling) * 1000); return () => window.clearInterval(id); }, [refresh, settings.autoRefresh, settings.polling]);

  const projectName = draft?.name || snapshot?.project.name || "未连接项目";
  const sourceLabel = snapshot?.source.mode === "ssh" ? `SSH · ${snapshot.source.host}` : snapshot ? "同机工作目录" : "数据服务未连接";
  return <main className="shell">
    <aside className="sidebar"><div className="brand"><div className="brand-mark"><span/><span/><span/></div><div><strong>CycPep Studio</strong><small>真实运行态控制台</small></div></div><div className="project-switcher"><span className="eyebrow">当前上下文</span><button onClick={() => setView("靶点与审核")}><span><b>{projectName}</b><small>{draft ? `草稿 · ${draft.draft_id}` : sourceLabel}</small></span><i>→</i></button></div><nav>{NAV.map(item => <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}><span className="nav-icon">{item === "连接" ? "⌁" : "◇"}</span>{item}{item === "候选池" && snapshot && <em>{snapshot.candidates.length}</em>}</button>)}</nav><div className="sidebar-bottom"><div className={`system-status ${snapshot ? "" : "offline"}`}><i/> {snapshot ? "真实数据已连接" : "未连接真实数据"}<span>{sourceLabel}</span></div></div></aside>
    <section className="workspace"><header className="topbar"><div><span className="eyebrow">SOURCE OF TRUTH · DATA LAYER</span><h1>{view}</h1></div><div className="top-actions"><button className="ghost" onClick={() => void refresh()} disabled={loading}>↻ {loading ? "读取中" : "刷新"}</button><button className="primary" disabled={Boolean(connectionId)} title={connectionId ? "SSH 模式当前为只读" : ""} onClick={() => setCreateOpen(true)}>＋ 新建靶点草稿</button></div></header>
      {error && view !== "连接" && <Disconnected message={error} onConnect={() => setView("连接")}/>}
      {!error && view === "总览" && <Overview snapshot={snapshot}/>}
      {!error && view === "靶点与审核" && <TargetReview draft={draft} snapshot={snapshot} settings={settings} onDraft={setDraft} notify={notify}/>}
      {!error && view === "候选池" && <Candidates snapshot={snapshot}/>}
      {!error && view === "最终交付" && <Deliverables snapshot={snapshot} settings={settings}/>}
      {!error && view === "证据链" && <EvidencePanel snapshot={snapshot}/>}
      {view === "连接" && <ConnectionPanel settings={settings} save={save} onSnapshot={(next, id) => { setSnapshot(next); setConnectionId(id); setError(""); notify("已连接并读取真实运行态"); }} notify={notify}/>}
    </section>
    {createOpen && <CreateDialog settings={settings} onClose={() => setCreateOpen(false)} onCreated={(next) => { setDraft(next); setCreateOpen(false); setView("靶点与审核"); notify(`已切换到草稿 ${next.draft_id}`); }}/>} {toast && <div className="toast">✓ {toast}</div>}
  </main>;
}

function Disconnected({ message, onConnect }: { message: string; onConnect: () => void }) { return <section className="panel honest-empty"><span className="eyebrow">NO VERIFIED SOURCE</span><h2>未连接真实工作环境</h2><p>当前不展示项目、候选、进度或结构，避免把演示数据误认为运行结果。</p><code>{message}</code><button className="primary" onClick={onConnect}>配置数据连接</button></section>; }

function Overview({ snapshot }: { snapshot: Snapshot | null }) {
  if (!snapshot) return null;
  return <>{snapshot.integrity_warnings.length > 0 && <section className="integrity-warning"><b>state.json 完整性警告</b><span>{snapshot.integrity_warnings.join(" · ")}</span><p>界面不会自行推断或补写这些字段。</p></section>}<section className="hero-grid"><article className="status-card dark-card"><div className="card-heading"><span>真实运行阶段</span><b className="live">{snapshot.state.phase || "未记录"}</b></div><div className="stage-title"><strong>{(snapshot.state.phase || "UNKNOWN").toUpperCase()}</strong><span>{snapshot.state.round ?? "—"}</span></div><p>数据源：{snapshot.source.mode === "ssh" ? `SSH ${snapshot.source.host}` : "适配层所在工作目录"}</p></article><article className="status-card metric-card"><span className="eyebrow">CANDIDATE INDEX</span><div className="big-number">{snapshot.stats.total_candidates ?? snapshot.candidates.length}</div><p>来自 CandidateIndex.load()，不是界面计数器</p></article><article className="status-card metric-card"><span className="eyebrow">ALL LAYERS PASS</span><div className="big-number">{snapshot.stats.all_layers_pass ?? 0}</div><p>只有 all_layers_pass=true 才计入</p></article></section><section className="panel truth-table"><div className="panel-title"><div><span className="eyebrow">RUNTIME STATE</span><h2>{snapshot.project.name || "未命名项目"}</h2></div><span className="tag">{snapshot.project.project_id || "project_id 缺失"}</span></div><div className="fact-grid"><div><span>靶点</span><b>{snapshot.project.targets.length ? snapshot.project.targets.join(" · ") : "未记录"}</b></div><div><span>轮次</span><b>{snapshot.state.round ?? "未记录"}</b></div><div><span>阈值</span><b>{snapshot.state.thresholds_ready ? "已记录" : "未记录"}</b></div><div><span>证据事件</span><b>{snapshot.recent_evidence.length}</b></div></div></section></>;
}

function TargetReview({ draft, snapshot, settings, onDraft, notify }: { draft: Draft | null; snapshot: Snapshot | null; settings: Settings; onDraft: (d: Draft) => void; notify: (s: string) => void }) {
  const targets = draft?.targets || snapshot?.project.config?.targets || [];
  const [selectedId, setSelectedId] = useState("");
  useEffect(() => { if (!targets.some(t => t.id === selectedId)) setSelectedId(targets[0]?.id || ""); }, [targets, selectedId]);
  const target = targets.find(t => t.id === selectedId);
  const [chain, setChain] = useState(""); const [residues, setResidues] = useState("");
  useEffect(() => { setChain(target?.structure?.chain || ""); setResidues((target?.binding_site?.residues || []).join(", ")); }, [target]);
  if (!draft) return <section className="panel honest-empty"><span className="eyebrow">READ ONLY</span><h2>当前是运行态，不是可编辑草稿</h2><p>新建草稿后，界面会使用服务端返回的 draft_id 和 targets 切换到该草稿；这里不再硬编码 MDM2/MDMX。</p></section>;
  const saveTarget = async () => { if (!target) return; try { const next = await api<Draft>(settings.apiBase, `project-drafts/${draft.draft_id}/targets/${encodeURIComponent(target.id)}`, { method: "PATCH", body: JSON.stringify({ structure: { ...(target.structure || {}), chain }, binding_site: { ...(target.binding_site || {}), residues: residues.split(",").map(x => Number(x.trim())).filter(Number.isFinite), status: "user_reviewed" } }) }); onDraft(next); notify("服务端已保存新 revision"); } catch (cause) { notify(cause instanceof Error ? cause.message : "保存失败"); } };
  const approve = async () => { try { const next = await api<Draft>(settings.apiBase, `project-drafts/${draft.draft_id}/approve`, { method: "POST", body: "{}" }); onDraft(next); notify("服务端已批准并固化摘要"); } catch (cause) { notify(cause instanceof Error ? cause.message : "批准失败"); } };
  return <section className="content-two-col review-layout"><article className="panel"><div className="panel-title"><div><span className="eyebrow">SERVER DRAFT · REVISION {draft.review?.revision ?? "—"}</span><h2>{draft.name || draft.project_id}</h2></div><span className="tag">{draft.review?.status || "draft"}</span></div><div className="form-grid"><label>当前靶点<select value={selectedId} onChange={e => setSelectedId(e.target.value)}>{targets.map(t => <option key={t.id} value={t.id}>{t.id}{t.required === false ? "（可选）" : ""}</option>)}</select></label><label>目标类型<input value={draft.objective || "未记录"} readOnly/></label><label>UniProt<input value={target?.uniprot || "未记录"} readOnly/></label><label>结构来源<input value={target?.structure?.pdb_id || "未选择"} readOnly/></label><label>Target chain<input value={chain} onChange={e => setChain(e.target.value.toUpperCase())}/></label><label>结合位点 residues<input value={residues} onChange={e => setResidues(e.target.value)}/></label></div><div className="review-checks"><Check ok={Boolean(target?.uniprot)} label="靶点身份已解析"/><Check ok={Boolean(target?.structure_plan?.chain_reviewed)} label="Target chain 已审核"/><Check ok={Boolean(target?.structure_plan?.binding_site_reviewed)} label="结合位点已审核"/><Check ok={Boolean(target?.structure_plan?.coordinates_ready)} label="真实坐标已物化并校验 hash"/></div><div className="form-actions"><button className="ghost" onClick={() => void saveTarget()}>保存到后端</button><button className="primary" disabled={Boolean(draft.review?.blocking_issues?.length)} onClick={() => void approve()}>批准并固化摘要</button></div></article><aside className="panel audit-panel"><span className="eyebrow">REVIEW GATE</span><h2>{draft.review?.blocking_issues?.length ? "仍有阻断项" : "可提交批准"}</h2><p>{draft.review?.blocking_issues?.join(" · ") || "服务端未报告阻断项"}</p><div className="digest"><span>content digest</span><code>{draft.review?.content_digest || "未生成"}</code></div>{draft.review?.warnings?.map(x => <div className="audit-note" key={x}>{x}</div>)}</aside></section>;
}
function Check({ ok, label }: { ok: boolean; label: string }) { return <div className={ok ? "check ok" : "check"}><i>{ok ? "✓" : "!"}</i><span>{label}</span></div>; }

function Candidates({ snapshot }: { snapshot: Snapshot | null }) {
  const [query, setQuery] = useState(""); const [selected, setSelected] = useState<Candidate | null>(null);
  const rows = useMemo(() => (snapshot?.candidates || []).filter(c => `${c.candidate_id} ${c.sequence} ${c.source_route}`.toLowerCase().includes(query.toLowerCase())), [snapshot, query]);
  useEffect(() => { if (selected && !rows.some(x => x.candidate_id === selected.candidate_id)) setSelected(null); }, [rows, selected]);
  return <section className="content-two-col"><article className="panel candidate-browser"><div className="toolbar"><input value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索真实候选 ID、序列或路线…"/></div><div className="table-wrap"><table><thead><tr><th>候选</th><th>序列</th><th>路线</th><th>七层进度</th><th>状态</th></tr></thead><tbody>{rows.map(c => <tr key={c.candidate_id} onClick={() => setSelected(c)} className={selected?.candidate_id === c.candidate_id ? "selected" : ""}><td><b>{c.candidate_id}</b></td><td className="sequence">{c.sequence}</td><td>{c.source_route || "—"}</td><td>{layerCount(c)}/7</td><td>{c.final_status || "未记录"}</td></tr>)}</tbody></table>{rows.length === 0 && <div className="empty">CandidateIndex 中没有候选</div>}</div></article><aside className="panel detail-panel">{selected ? <><span className="eyebrow">CANDIDATE DETAIL</span><h2>{selected.candidate_id}</h2><div className="gate-list">{selected.layers.map((pass, i) => <div key={i}><span>L{i + 1}</span><b className={pass ? "pass" : "pending-dot"}>{pass ? "通过" : "未通过 / 未运行"}</b></div>)}</div><div className="not-deliverable">{selected.all_layers_pass ? "七层判定已全清" : "不得标记为科学交付"}</div></> : <p className="empty">选择候选查看数据层记录</p>}</aside></section>;
}

function Deliverables({ snapshot, settings }: { snapshot: Snapshot | null; settings: Settings }) {
  const deliverable = (snapshot?.candidates || []).filter(c => c.all_layers_pass); const withArtifacts = deliverable.filter(c => c.artifact_id);
  return <section className="deliverable-grid"><article className="panel viewer-panel"><div className="panel-title"><div><span className="eyebrow">VERIFIED STRUCTURE ARTIFACTS</span><h2>真实结构产物</h2></div></div>{withArtifacts.length ? <div className="artifact-list">{withArtifacts.map(c => <button key={c.candidate_id}>{c.candidate_id}<small>{c.artifact_id}</small></button>)}</div> : <div className="viewer-empty"><b>没有可验证的结构 artifact</b><p>不会加载 1YCR、3DAB 或任何示例模型。只有候选七层全清，且后端返回 opaque artifact ID 后才启用三维查看。</p></div>}</article><aside className="panel delivery-card"><span className="eyebrow">DELIVERY GATE</span><h2>最终产物</h2><div className="delivery-state"><b>{deliverable.length}</b><span>all_layers_pass=true</span></div><div className="api-contract"><b>坐标读取接口</b><code>GET {join(settings.apiBase, "artifacts/{artifact_id}/coordinates")}</code><span>当前适配层尚未开放该接口，所以查看器保持禁用。</span></div></aside></section>;
}

function EvidencePanel({ snapshot }: { snapshot: Snapshot | null }) { const events = snapshot?.recent_evidence || []; return <section className="panel"><div className="panel-title"><div><span className="eyebrow">APPEND-ONLY LOG</span><h2>EvidenceLogger 最近事件</h2></div><span className="tag">{events.length}</span></div><div className="evidence-list">{[...events].reverse().map((e, i) => <article key={e.event_id || i}><time>{e.timestamp || "无时间"}</time><b>{e.event_type || "unknown_event"}</b><span>{e.agent || "unknown_agent"} · {e.phase || "无阶段"}{e.candidate_id ? ` · ${e.candidate_id}` : ""}</span></article>)}{!events.length && <div className="empty">EvidenceLogger 中没有事件</div>}</div></section>; }

function ConnectionPanel({ settings, save, onSnapshot, notify }: { settings: Settings; save: (s: Settings) => void; onSnapshot: (s: Snapshot, id: string) => void; notify: (s: string) => void }) {
  const [apiBase, setApiBase] = useState(settings.apiBase); const [mode, setMode] = useState<"local" | "ssh">("local"); const [host, setHost] = useState(""); const [username, setUsername] = useState(""); const [port, setPort] = useState(22); const [keyAlias, setKeyAlias] = useState(""); const [root, setRoot] = useState(""); const [busy, setBusy] = useState(false);
  const connect = async () => { setBusy(true); try { save({ ...settings, apiBase }); if (mode === "local") onSnapshot(await api<Snapshot>(apiBase, "snapshot"), ""); else { const result = await api<{ connection_id: string; snapshot: Snapshot }>(apiBase, "connections/ssh", { method: "POST", body: JSON.stringify({ host, username, port, key_alias: keyAlias, workspace_root: root }) }); onSnapshot(result.snapshot, result.connection_id); } } catch (cause) { notify(cause instanceof Error ? cause.message : "连接失败"); } finally { setBusy(false); } };
  return <section className="content-two-col connection-layout"><article className="panel"><span className="eyebrow">ADAPTER</span><h2>数据服务</h2><label className="field">API 地址<input value={apiBase} onChange={e => setApiBase(e.target.value)} placeholder="http://127.0.0.1:8765/api/v1"/></label><div className="mode-tabs"><button className={mode === "local" ? "active" : ""} onClick={() => setMode("local")}>服务器同机模式</button><button className={mode === "ssh" ? "active" : ""} onClick={() => setMode("ssh")}>SSH 远端模式</button></div>{mode === "local" ? <div className="connection-note"><b>UI 与计算环境在同一台服务器</b><p>适配层直接通过项目的 data_layer.py 读取 state、候选索引和证据日志。UI 本身不接触文件路径。</p></div> : <div className="form-grid ssh-grid"><label>主机<input value={host} onChange={e => setHost(e.target.value)} placeholder="gpu.example.edu"/></label><label>端口<input type="number" value={port} onChange={e => setPort(Number(e.target.value))}/></label><label>用户名<input value={username} onChange={e => setUsername(e.target.value)} placeholder="researcher"/></label><label>密钥别名<input value={keyAlias} onChange={e => setKeyAlias(e.target.value)} placeholder="gpu1"/></label><label className="wide">远端项目目录<input value={root} onChange={e => setRoot(e.target.value)} placeholder="/srv/cycpep/project"/></label></div>}<button className="primary full" disabled={busy} onClick={() => void connect()}>{busy ? "连接中…" : "测试并使用此连接"}</button></article><aside className="panel security-card"><span className="eyebrow">SSH SECURITY</span><h2>浏览器不接触密钥</h2><p>密码与私钥输入被刻意移除。管理员先在适配层主机登记密钥文件，再给网页一个不敏感的别名。</p><ul><li>强制 known_hosts 主机指纹校验</li><li>强制 BatchMode，禁止交互式密码</li><li>SSH 参数不经过 shell 拼接</li><li>连接 ID 仅保存在适配层内存</li></ul></aside></section>;
}

function CreateDialog({ settings, onClose, onCreated }: { settings: Settings; onClose: () => void; onCreated: (d: Draft) => void }) {
  const [identifier, setIdentifier] = useState(""); const [kind, setKind] = useState("auto"); const [objective, setObjective] = useState("binder"); const [epitope, setEpitope] = useState(""); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const submit = async () => { setBusy(true); setError(""); try { const next = await api<Draft>(settings.apiBase, "project-drafts", { method: "POST", body: JSON.stringify({ identifier, identifier_type: kind, organism_id: 9606, objective, epitope: epitope || undefined }) }); if (!next.draft_id || !Array.isArray(next.targets)) throw new Error("服务端响应缺少 draft_id 或 targets，已拒绝伪造草稿"); onCreated(next); } catch (cause) { setError(cause instanceof Error ? cause.message : "创建失败"); } finally { setBusy(false); } };
  return <div className="dialog-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}><section className="dialog"><button className="dialog-close" onClick={onClose}>×</button><span className="eyebrow">SERVER-SIDE BOOTSTRAP</span><h2>新建真实靶点草稿</h2><p>创建成功后立即切换到服务端返回的草稿；后端失败时不会生成本地替代品。</p><div className="form-grid"><label>标识符<input value={identifier} onChange={e => setIdentifier(e.target.value)} placeholder="Gene / UniProt / PDB" autoFocus/></label><label>标识类型<select value={kind} onChange={e => setKind(e.target.value)}><option value="auto">自动识别</option><option value="gene">Gene</option><option value="uniprot">UniProt</option><option value="pdb">PDB</option></select></label><label>目标模式<select value={objective} onChange={e => setObjective(e.target.value)}><option value="binder">单靶 binder</option><option value="multi_target_binder">多靶 binder</option></select></label><label>表位描述<input value={epitope} onChange={e => setEpitope(e.target.value)} placeholder="可留空，后续审核"/></label></div>{error && <div className="inline-error">{error}</div>}<div className="form-actions"><button className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={!identifier.trim() || busy} onClick={() => void submit()}>{busy ? "服务端解析中…" : "创建并切换"}</button></div></section></div>;
}
