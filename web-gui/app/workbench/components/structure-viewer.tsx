"use client";

import { useLayoutEffect, useRef, useState } from "react";

import type { ArtifactView } from "../domain";
import { artifactContentState } from "../scientific-selectors";

type ViewerStyle = "cartoon" | "sticks" | "surface";
type ViewerLoadState = "idle" | "unavailable" | "loading" | "ready" | "failed";
type MolViewer = {
  addModel(data: string, format: string): void;
  addSurface(type: unknown, style: object): void;
  clear(): void;
  render(): void;
  setStyle(selection: object, style: object): void;
  zoomTo(): void;
};

declare global {
  interface Window {
    $3Dmol?: {
      createViewer(element: HTMLElement, options: object): MolViewer;
      SurfaceType: { VDW: unknown };
    };
  }
}

const THREE_DMOL_SCRIPT =
  "https://cdnjs.cloudflare.com/ajax/libs/3Dmol/2.4.2/3Dmol-min.js";
const VIEWER_BACKGROUND = "#07110f";
const CARTOON_EMPTY_THRESHOLD = 0.0005;

function requestFrame(callback: () => void): void {
  if (
    typeof window !== "undefined" &&
    typeof window.requestAnimationFrame === "function"
  ) {
    window.requestAnimationFrame(callback);
  } else {
    setTimeout(callback, 0);
  }
}

function countVisiblePixels(canvas: HTMLCanvasElement): number | null {
  try {
    const width = canvas.width;
    const height = canvas.height;
    if (!width || !height) return null;
    const offscreen = document.createElement("canvas");
    offscreen.width = width;
    offscreen.height = height;
    const context = offscreen.getContext("2d");
    if (!context) return null;
    context.drawImage(canvas, 0, 0);
    const data = context.getImageData(0, 0, width, height).data;
    const background = parseInt(VIEWER_BACKGROUND.slice(1), 16);
    const bgRed = (background >> 16) & 255;
    const bgGreen = (background >> 8) & 255;
    const bgBlue = background & 255;
    let visible = 0;
    for (let index = 0; index < data.length; index += 4) {
      const red = data[index];
      const green = data[index + 1];
      const blue = data[index + 2];
      if (
        Math.abs(red - bgRed) > 6 ||
        Math.abs(green - bgGreen) > 6 ||
        Math.abs(blue - bgBlue) > 6
      ) {
        visible += 1;
      }
    }
    return visible;
  } catch {
    return null;
  }
}

function loadViewerLibrary(): Promise<void> {
  if (window.$3Dmol) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>("script[data-3dmol]");
    if (existing) {
      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener(
        "error",
        () => reject(new Error("3Dmol.js could not be loaded")),
        { once: true },
      );
      return;
    }
    const script = document.createElement("script");
    script.src = THREE_DMOL_SCRIPT;
    script.dataset["3dmol"] = "true";
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("3Dmol.js could not be loaded"));
    document.head.appendChild(script);
  });
}

function modelFormat(contentType: string | null): "cif" | "pdb" {
  return contentType?.includes("mmcif") || contentType?.includes("cif")
    ? "cif"
    : "pdb";
}

export function StructureViewer({ artifact }: { artifact: ArtifactView | null }) {
  const element = useRef<HTMLDivElement>(null);
  const viewer = useRef<MolViewer | null>(null);
  const content = artifactContentState(artifact);
  const identity = artifact && content.available
    ? `${artifact.artifact_id}\u0000${content.contentLink}`
    : null;
  const [requested, setRequested] = useState<{
    identity: string | null;
    value: boolean;
  }>({ identity: null, value: false });
  const [loadResult, setLoadResult] = useState<{
    identity: string | null;
    state: ViewerLoadState;
    message: string;
  }>({ identity: null, state: "unavailable", message: "" });
  const [representation, setRepresentation] = useState<{
    identity: string | null;
    style: ViewerStyle;
  }>({ identity: null, style: "cartoon" });
  const requestedNow = requested.identity === identity && requested.value;
  const state: ViewerLoadState = !content.available
    ? "unavailable"
    : loadResult.identity === identity
      ? loadResult.state
      : requestedNow
        ? "loading"
        : "idle";
  const message = loadResult.identity === identity ? loadResult.message : "";
  const style = representation.identity === identity
    ? representation.style
    : "cartoon";

  useLayoutEffect(() => {
    // A new artifact identity always starts idle: clear the previous canvas
    // and load the preview only after an explicit user request.
    const previous = viewer.current;
    viewer.current = null;
    previous?.clear();
    previous?.render();
    setRequested({ identity, value: false });
    setLoadResult({ identity: null, state: "unavailable", message: "" });
    setRepresentation({ identity, style: "cartoon" });
  }, [identity]);

  useLayoutEffect(() => {
    if (!requestedNow || !content.available || !element.current) {
      return;
    }
    const contentLink = content.contentLink;

    let cancelled = false;
    async function load() {
      try {
        await loadViewerLibrary();
        const response = await fetch(contentLink, { cache: "no-store" });
        if (!response.ok) throw new Error(`Artifact content returned HTTP ${response.status}`);
        const coordinates = await response.text();
        if (cancelled || !element.current || !window.$3Dmol) return;
        const instance = window.$3Dmol.createViewer(element.current, {
          antialias: true,
          backgroundColor: VIEWER_BACKGROUND,
          preserveDrawingBuffer: true,
        });
        instance.addModel(coordinates, modelFormat(response.headers.get("content-type")));
        instance.setStyle({}, { cartoon: { color: "spectrum" } });
        instance.zoomTo();
        instance.render();
        viewer.current = instance;
        setLoadResult({ identity, state: "ready", message: "" });
        // Some WebGL drivers drop the first frame after a fresh model add;
        // re-render once the loading overlay has been removed, then verify
        // that the requested representation produced visible geometry.
        requestFrame(() => {
          if (cancelled || viewer.current !== instance || !element.current) return;
          instance.render();
          requestFrame(() => {
            if (cancelled || viewer.current !== instance) return;
            verifyRepresentationRendered(instance, "cartoon");
          });
        });
      } catch (cause) {
        if (!cancelled) {
          setLoadResult({
            identity,
            state: "failed",
            message: cause instanceof Error
              ? cause.message
              : "Artifact content could not be loaded",
          });
        }
      }
    }

    void load();
    return () => { cancelled = true; };
  }, [requestedNow, content.available, content.contentLink, identity]);

  function verifyRepresentationRendered(
    instance: MolViewer,
    style: ViewerStyle,
  ): void {
    // 3Dmol's cartoon renderer silently produces no geometry for synthetic
    // reference helices whose carbonyl geometry is degenerate (constant
    // backbone C-O direction collapses the spline frame to NaN); fall back
    // so the canvas is never blank for the user.
    if (style !== "cartoon") return;
    const container = element.current;
    const canvas = container?.querySelector?.("canvas");
    if (!canvas) return;
    const visible = countVisiblePixels(canvas);
    if (visible === null) return;
    const total = canvas.width * canvas.height;
    if (total <= 0 || visible / total >= CARTOON_EMPTY_THRESHOLD) return;
    setRepresentation({ identity, style: "sticks" });
    instance.setStyle({}, { stick: { colorscheme: "Jmol" } });
    instance.render();
    setLoadResult({
      identity,
      state: "ready",
      message: "Cartoon did not render for this structure; showing sticks instead.",
    });
  }

  function applyStyle(next: ViewerStyle) {
    setRepresentation({ identity, style: next });
    setLoadResult({ identity, state: "ready", message: "" });
    const instance = viewer.current;
    if (!instance || !window.$3Dmol) return;
    instance.setStyle(
      {},
      next === "cartoon"
        ? { cartoon: { color: "spectrum" } }
        : next === "sticks"
          ? { stick: { colorscheme: "Jmol" } }
          : { cartoon: { color: "#4bb891" } },
    );
    if (next === "surface") {
      instance.addSurface(window.$3Dmol.SurfaceType.VDW, {
        color: "#235c4c",
        opacity: 0.72,
      });
    }
    instance.render();
    if (next === "cartoon") {
      requestFrame(() => {
        if (viewer.current === instance) {
          verifyRepresentationRendered(instance, "cartoon");
        }
      });
    }
  }

  return (
    <section className="structure-viewer" aria-labelledby="structure-heading">
      <header>
        <h4 id="structure-heading">Artifact content</h4>
        <span>{artifact?.artifact_id ?? "No artifact selected"}</span>
      </header>
      <div className="viewer-shell">
        <div ref={element} className="viewer-canvas" />
        {state === "unavailable" && (
          <div className="viewer-message">
            Recorded in the formal Store; browser preview was not published.
          </div>
        )}
        {state === "idle" && (
          <button
            type="button"
            className="viewer-message viewer-load-trigger"
            aria-label="Load structure preview"
            onClick={() => setRequested({ identity, value: true })}
          >
            <span>Recorded in the formal Store; preview loads on demand.</span>
            <strong>Load structure preview</strong>
          </button>
        )}
        {state === "loading" && <div className="viewer-message">Loading returned artifact content…</div>}
        {state === "failed" && <div className="viewer-message error">{message}</div>}
        {state === "ready" && (
          <>
            {message !== "" && <div className="viewer-note">{message}</div>}
            <div className="viewer-controls" aria-label="Structure representation">
              {(["cartoon", "sticks", "surface"] as const).map((item) => (
                <button
                  type="button"
                  key={item}
                  className={style === item ? "active" : undefined}
                  aria-pressed={style === item}
                  onClick={() => applyStyle(item)}
                >
                  {item}
                </button>
              ))}
            </div>
          </>
        )}
      </div>
    </section>
  );
}
