"use client";

import { useEffect, useRef, useState } from "react";

export interface ProjectLaunchSheetProps { onClose: () => void; }
type ApprovalMode = "manual" | "automatic";
type Ceilings = { minutes: string; slots: string; designs: string; candidates: string };

const IDENTIFIER_TYPES = [
  ["auto", "Auto detect"], ["gene", "Gene symbol"],
  ["uniprot", "UniProt ID"], ["pdb", "PDB ID"],
] as const;

export function isLaunchRequestReady(target: string, mode: ApprovalMode, ceilings: Ceilings): boolean {
  if (!target.trim()) return false;
  if (mode === "manual") return true;
  return Object.values(ceilings).every((value) => Number.isFinite(Number(value)) && Number(value) > 0);
}

function useDialogFocus(onClose: () => void) {
  const targetInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    targetInput.current?.focus();
    function handleKeys(event: KeyboardEvent) {
      if (event.key === "Escape") return onClose();
      if (event.key !== "Tab") return;
      const controls = Array.from(document.querySelectorAll<HTMLElement>(
        ".launch-overlay button:not(:disabled), .launch-overlay input:not(:disabled), .launch-overlay select:not(:disabled)",
      ));
      const first = controls[0]; const last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    window.addEventListener("keydown", handleKeys);
    return () => { window.removeEventListener("keydown", handleKeys); previous?.focus(); };
  }, [onClose]);
  return targetInput;
}

function TargetStage({ target, onTargetChange, inputRef }: {
  target: string; onTargetChange: (value: string) => void; inputRef: React.RefObject<HTMLInputElement | null>;
}) {
  const [identifierType, setIdentifierType] = useState("auto");
  return <section className="launch-ledger-stage is-active" aria-labelledby="launch-target-heading">
    <header><span className="launch-stage-marker" aria-hidden="true">01</span><div><p>Target</p><h2 id="launch-target-heading">Biological identity</h2></div></header>
    <label className="launch-field launch-target-field"><span>Target identifier</span><input ref={inputRef} value={target} onChange={(event) => onTargetChange(event.target.value)} placeholder="e.g. MDM2, Q00987 or 1YCR" autoComplete="off" /></label>
    <div className="launch-inline-fields">
      <label className="launch-field"><span>Identifier type</span><select value={identifierType} onChange={(event) => setIdentifierType(event.target.value)}>{IDENTIFIER_TYPES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label className="launch-field"><span>Organism</span><input value="Homo sapiens · 9606" readOnly /></label>
    </div>
    <p className="launch-field-note">Entering a target creates a review draft. It does not start scientific or GPU work.</p>
  </section>;
}

function ReviewStage({ target }: { target: string }) {
  return <section className="launch-ledger-stage" aria-labelledby="launch-review-heading">
    <header><span className="launch-stage-marker" aria-hidden="true">02</span><div><p>Project</p><h2 id="launch-review-heading">Review before launch</h2></div></header>
    <dl className="launch-review-register">
      <div><dt>Requested target</dt><dd>{target.trim() || "Not entered"}</dd></div>
      <div><dt>Objective</dt><dd>Cyclic-peptide binder</dd></div>
      <div><dt>Identity & structure</dt><dd>Awaiting target resolution</dd></div>
    </dl>
    <p className="launch-pending-copy">Resolved facts, uncertainties and structure readiness will appear here before project approval.</p>
  </section>;
}

function ComputeStage({ mode, ceilings, onModeChange, onCeilingChange }: {
  mode: ApprovalMode; ceilings: Ceilings; onModeChange: (mode: ApprovalMode) => void;
  onCeilingChange: (key: keyof Ceilings, value: string) => void;
}) {
  const fields: Array<[keyof Ceilings, string]> = [["minutes", "GPU minutes"], ["slots", "GPU slots"], ["designs", "Design proposals"], ["candidates", "Candidates"]];
  return <section className="launch-ledger-stage" aria-labelledby="launch-compute-heading">
    <header><span className="launch-stage-marker" aria-hidden="true">03</span><div><p>Compute</p><h2 id="launch-compute-heading">First GPU gate</h2></div></header>
    <fieldset className="launch-approval-options"><legend>Approval mode</legend>
      <label data-selected={mode === "manual" || undefined}><input type="radio" name="approval-mode" checked={mode === "manual"} onChange={() => onModeChange("manual")} /><span><strong>Review manually</strong><small>Pause before heavy Prediction.</small></span></label>
      <label data-selected={mode === "automatic" || undefined}><input type="radio" name="approval-mode" checked={mode === "automatic"} onChange={() => onModeChange("automatic")} /><span><strong>Auto-approve first GPU gate</strong><small>Only within these ceilings.</small></span></label>
    </fieldset>
    <div className="launch-ceilings" aria-disabled={mode !== "automatic"}>{fields.map(([key, label]) => <label className="launch-field" key={key}><span>{label}</span><input type="number" value={ceilings[key]} placeholder="Set limit" min="1" disabled={mode !== "automatic"} onChange={(event) => onCeilingChange(key, event.target.value)} /></label>)}</div>
    <p className="launch-field-note">This gate follows Initial Design and precedes heavy Prediction. Later plans still require review.</p>
  </section>;
}

export function ProjectLaunchSheet({ onClose }: ProjectLaunchSheetProps) {
  const targetInput = useDialogFocus(onClose);
  const [target, setTarget] = useState("");
  const [mode, setMode] = useState<ApprovalMode>("manual");
  const [ceilings, setCeilings] = useState<Ceilings>({ minutes: "", slots: "", designs: "", candidates: "" });
  const [notice, setNotice] = useState<string | null>(null);
  const ready = isLaunchRequestReady(target, mode, ceilings);
  function updateCeiling(key: keyof Ceilings, value: string) { setCeilings((current) => ({ ...current, [key]: value })); setNotice(null); }

  return <section className="launch-overlay" role="dialog" aria-modal="true" aria-labelledby="launch-title"><div className="launch-sheet">
    <header className="launch-sheet-header"><div className="launch-sheet-kicker" aria-label="Project ignition"><span aria-hidden="true" />CycPep / project ignition</div><div className="launch-sheet-dismiss"><button type="button" className="launch-text-button" onClick={onClose}>View existing tasks</button><button type="button" className="launch-close-button" onClick={onClose} aria-label="Close new project"><span aria-hidden="true">×</span></button></div></header>
    <form className="launch-sheet-body" onSubmit={(event) => { event.preventDefault(); setNotice("Project request prepared. Ready for workflow handoff."); }}>
      <div className="launch-introduction"><p className="launch-eyebrow">New cyclic-peptide campaign</p><h1 id="launch-title">Start with a target.</h1><p>Resolve one biological identity, review the project it creates, then hand the approved campaign to the formal workflow.</p></div>
      <div className="launch-ledger" aria-label="Target to compute launch ledger"><TargetStage target={target} inputRef={targetInput} onTargetChange={(value) => { setTarget(value); setNotice(null); }} /><ReviewStage target={target} /><ComputeStage mode={mode} ceilings={ceilings} onModeChange={(value) => { setMode(value); setNotice(null); }} onCeilingChange={updateCeiling} /></div>
      <footer className="launch-sheet-footer"><div className="launch-interface-status" role="status"><span data-ready={ready || undefined} aria-hidden="true" />{notice ?? (ready ? "Target request ready for project resolution." : "Enter a target and valid approval ceilings to prepare the request.")}</div><button className="launch-primary-action" type="submit" disabled={!ready}>Create and launch<span aria-hidden="true">→</span></button></footer>
    </form>
  </div></section>;
}
