import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { after, before } from "node:test";

import { createServer } from "vite";

let cacheDir;
let vite;
let StructureViewer;

function createHookHarness(element) {
  const refs = [{ current: element }, { current: null }];
  const states = [];
  const effects = [];
  let cursor = 0;
  let pendingEffect = null;

  return {
    render(renderComponent) {
      cursor = 0;
      return renderComponent();
    },
    useRef(initialValue) {
      const index = cursor++;
      refs[index] ??= { current: initialValue };
      return refs[index];
    },
    useState(initialValue) {
      const index = cursor++;
      if (!(index in states)) states[index] = initialValue;
      return [states[index], (next) => {
        states[index] = typeof next === "function" ? next(states[index]) : next;
      }];
    },
    useEffect(effect, dependencies) {
      const index = cursor++;
      const previous = effects[index];
      const changed = !previous
        || dependencies.length !== previous.dependencies.length
        || dependencies.some((value, dependencyIndex) => !Object.is(
          value,
          previous.dependencies[dependencyIndex],
        ));
      if (changed) pendingEffect = { index, effect, dependencies };
    },
    flushEffect() {
      assert.ok(pendingEffect, "the component scheduled an artifact effect");
      const next = pendingEffect;
      pendingEffect = null;
      effects[next.index]?.cleanup?.();
      effects[next.index] = {
        dependencies: next.dependencies,
        cleanup: next.effect(),
      };
    },
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function linkedArtifact(artifactId, contentLink) {
  return {
    artifact_id: artifactId,
    artifact_type: "structure",
    role: "prediction",
    integrity_identity: null,
    producer: null,
    inputs: [],
    protocol_id: null,
    run_relation: "current_run",
    trace: {
      workflow_id: "workflow-1",
      run_id: "run-1",
      task_id: "task-1",
      attempt_id: "attempt-1",
      candidate_id: "candidate-1",
    },
    content_link: contentLink,
  };
}

function findButton(node, label) {
  if (!node || typeof node !== "object") return null;
  if (node.type === "button" && node.props?.children === label) return node;
  const children = Array.isArray(node.props?.children)
    ? node.props.children
    : [node.props?.children];
  for (const child of children) {
    const found = findButton(child, label);
    if (found) return found;
  }
  return null;
}

function nodeText(node) {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (!node || typeof node !== "object") return "";
  const children = Array.isArray(node.props?.children)
    ? node.props.children
    : [node.props?.children];
  return children.map(nodeText).join("");
}

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
}

before(async () => {
  cacheDir = await mkdtemp(join(tmpdir(), "cycpep-structure-viewer-test-"));
  vite = await createServer({
    appType: "custom",
    cacheDir,
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    ssr: { noExternal: true },
    plugins: [{
      name: "structure-viewer-react-effect-harness",
      enforce: "pre",
      resolveId(id) {
        if (id === "react") return "\0structure-viewer-react";
        if (id === "react/jsx-dev-runtime") return "\0structure-viewer-jsx-runtime";
        return null;
      },
      load(id) {
        if (id === "\0structure-viewer-react") {
          return [
            "export const useLayoutEffect = (...args) => globalThis.__structureViewerHooks.useEffect(...args);",
            "export const useRef = (...args) => globalThis.__structureViewerHooks.useRef(...args);",
            "export const useState = (...args) => globalThis.__structureViewerHooks.useState(...args);",
          ].join("\n");
        }
        if (id === "\0structure-viewer-jsx-runtime") {
          return "export const jsxDEV = (type, props) => ({ type, props });";
        }
        return null;
      },
    }],
    server: { middlewareMode: true, hmr: false },
  });
  ({ StructureViewer } = await vite.ssrLoadModule(
    "/app/workbench/components/structure-viewer.tsx",
  ));
});

after(async () => {
  delete globalThis.__structureViewerHooks;
  delete globalThis.window;
  delete globalThis.document;
  delete globalThis.fetch;
  await vite?.close();
  await rm(cacheDir, { recursive: true, force: true });
});

function createStructureScenario() {
  const events = [];
  const requests = new Map();
  const harness = createHookHarness({});
  globalThis.__structureViewerHooks = harness;
  globalThis.window = {
    $3Dmol: {
      SurfaceType: { VDW: "vdw" },
      createViewer() {
        return {
          addModel(data, format) { events.push(`add:${data}:${format}`); },
          addSurface() {},
          clear() { events.push("clear"); },
          render() { events.push("render"); },
          setStyle(_selection, style) { events.push(`style:${Object.keys(style)[0]}`); },
          zoomTo() {},
        };
      },
    },
  };
  globalThis.document = {};
  globalThis.fetch = (url) => {
    events.push(`fetch:${url}`);
    const request = deferred();
    requests.set(url, request);
    return request.promise;
  };
  return { events, harness, requests };
}

async function switchArtifact(scenario, artifact) {
  scenario.harness.render(() => StructureViewer({ artifact }));
  scenario.harness.flushEffect();
  await settle();
}

async function resolveArtifact(scenario, link, coordinates) {
  scenario.requests.get(link).resolve({
    ok: true,
    headers: { get: () => "chemical/x-pdb" },
    text: async () => coordinates,
  });
  await settle();
}

function viewArtifact(scenario, artifact) {
  return scenario.harness.render(() => StructureViewer({ artifact }));
}

test("switching linked structure identity clears stale content and resets representation", async () => {
  const scenario = createStructureScenario();
  const { events } = scenario;

  const artifactA = linkedArtifact("artifact-A", "/content/A");
  const artifactB = linkedArtifact("artifact-B", "/content/B");
  await switchArtifact(scenario, artifactA);
  await resolveArtifact(scenario, "/content/A", "MODEL A");
  const readyA = viewArtifact(scenario, artifactA);
  findButton(readyA, "sticks").props.onClick();
  assert.equal(findButton(viewArtifact(scenario, artifactA), "sticks").props["aria-pressed"], true);

  events.length = 0;
  await switchArtifact(scenario, artifactB);
  assert.deepEqual(events.slice(0, 3), ["clear", "render", "fetch:/content/B"]);
  const loadingB = viewArtifact(scenario, artifactB);
  assert.match(nodeText(loadingB), /Loading returned artifact content/);
  assert.equal(findButton(loadingB, "sticks"), null);

  await resolveArtifact(scenario, "/content/B", "MODEL B");
  const readyB = viewArtifact(scenario, artifactB);
  assert.equal(findButton(readyB, "cartoon").props["aria-pressed"], true);
  assert.equal(findButton(readyB, "sticks").props["aria-pressed"], false);
  assert.match(events.join("|"), /add:MODEL B:pdb\|style:cartoon/);

  await switchArtifact(scenario, artifactA);
  await resolveArtifact(scenario, "/content/A", "MODEL A RETURNED");
  const returnedA = viewArtifact(scenario, artifactA);
  assert.equal(findButton(returnedA, "cartoon").props["aria-pressed"], true);
  assert.equal(findButton(returnedA, "sticks").props["aria-pressed"], false);
});

test("a failed replacement load leaves the cleared viewer unavailable", async () => {
  const scenario = createStructureScenario();
  const artifactA = linkedArtifact("artifact-A", "/content/A");
  const artifactB = linkedArtifact("artifact-B", "/content/B");
  await switchArtifact(scenario, artifactA);
  await resolveArtifact(scenario, "/content/A", "MODEL A");
  scenario.events.length = 0;
  await switchArtifact(scenario, artifactB);
  scenario.requests.get("/content/B").reject(new Error("replacement failed"));
  await settle();

  assert.deepEqual(scenario.events.slice(0, 3), ["clear", "render", "fetch:/content/B"]);
  const failedB = viewArtifact(scenario, artifactB);
  assert.match(nodeText(failedB), /replacement failed/);
  assert.equal(findButton(failedB, "cartoon"), null);
});
