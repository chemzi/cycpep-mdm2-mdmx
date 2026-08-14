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
  let pendingEffects = [];

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
      if (changed) pendingEffects.push({ index, effect, dependencies });
    },
    flushEffect() {
      assert.ok(pendingEffects.length, "the component scheduled an artifact effect");
      const next = pendingEffects;
      pendingEffects = [];
      for (const entry of next) {
        effects[entry.index]?.cleanup?.();
        effects[entry.index] = {
          dependencies: entry.dependencies,
          cleanup: entry.effect(),
        };
      }
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
  if (node.type === "button"
    && (node.props?.children === label || node.props?.["aria-label"] === label)) return node;
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

function createSamplingContext(canvas) {
  return {
    drawImage() {},
    getImageData() {
      const size = canvas.width * canvas.height;
      const data = new Uint8ClampedArray(size * 4);
      const painted = Math.max(0, Math.min(canvas.nonBg, size));
      for (let index = 0; index < size * 4; index += 4) {
        data[index] = 7;
        data[index + 1] = 17;
        data[index + 2] = 15;
        data[index + 3] = 255;
      }
      for (let index = 0; index < painted; index += 1) {
        data[index * 4] = 190;
        data[index * 4 + 1] = 190;
        data[index * 4 + 2] = 190;
        data[index * 4 + 3] = 255;
      }
      return { data };
    },
  };
}

function createStructureScenario() {
  const events = [];
  const requests = new Map();
  const canvas = {
    width: 100,
    height: 100,
    nonBg: 5000,
    getContext(kind) {
      return kind === "2d" ? createSamplingContext(canvas) : null;
    },
  };
  const container = {
    querySelector(selector) {
      return selector === "canvas" ? canvas : null;
    },
  };
  const harness = createHookHarness(container);
  globalThis.__structureViewerHooks = harness;
  globalThis.window = {
    requestAnimationFrame(callback) {
      callback();
      return 0;
    },
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
  globalThis.document = {
    createElement(tag) {
      if (tag !== "canvas") return {};
      return {
        width: 0,
        height: 0,
        getContext: (kind) => (kind === "2d" ? createSamplingContext(canvas) : null),
      };
    },
  };
  globalThis.fetch = (url) => {
    events.push(`fetch:${url}`);
    const request = deferred();
    requests.set(url, request);
    return request.promise;
  };
  return { events, harness, requests, canvas };
}

async function switchArtifact(scenario, artifact) {
  scenario.harness.render(() => StructureViewer({ artifact }));
  scenario.harness.flushEffect();
  await settle();
}

async function requestPreview(scenario, artifact) {
  const node = scenario.harness.render(() => StructureViewer({ artifact }));
  findButton(node, "Load structure preview").props.onClick();
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

test("structure preview stays idle until an explicit load action", async () => {
  const scenario = createStructureScenario();
  const artifactA = linkedArtifact("artifact-A", "/content/A");
  await switchArtifact(scenario, artifactA);

  assert.deepEqual(scenario.events, [], "no fetch happens before the preview is requested");
  const idleA = viewArtifact(scenario, artifactA);
  assert.match(nodeText(idleA), /preview loads on demand/);
  assert.ok(
    findButton(idleA, "Load structure preview"),
    "idle state offers an explicit load action",
  );
  assert.equal(findButton(idleA, "cartoon"), null, "no representation controls before loading");

  await requestPreview(scenario, artifactA);
  assert.deepEqual(scenario.events, ["fetch:/content/A"]);

  await resolveArtifact(scenario, "/content/A", "MODEL A");
  const readyA = viewArtifact(scenario, artifactA);
  assert.equal(findButton(readyA, "cartoon").props["aria-pressed"], true);
  assert.equal(findButton(readyA, "sticks").props["aria-pressed"], false);
  assert.match(scenario.events.join("|"), /add:MODEL A:pdb\|style:cartoon/);
});

test("switching linked structure identity resets to idle without fetching", async () => {
  const scenario = createStructureScenario();
  const artifactA = linkedArtifact("artifact-A", "/content/A");
  const artifactB = linkedArtifact("artifact-B", "/content/B");

  await switchArtifact(scenario, artifactA);
  await requestPreview(scenario, artifactA);
  await resolveArtifact(scenario, "/content/A", "MODEL A");
  const readyA = viewArtifact(scenario, artifactA);
  findButton(readyA, "sticks").props.onClick();
  assert.equal(
    findButton(viewArtifact(scenario, artifactA), "sticks").props["aria-pressed"],
    true,
  );

  scenario.events.length = 0;
  await switchArtifact(scenario, artifactB);
  assert.deepEqual(
    scenario.events,
    ["clear", "render"],
    "switching identity clears the viewer without fetching",
  );
  const idleB = viewArtifact(scenario, artifactB);
  assert.match(nodeText(idleB), /preview loads on demand/);
  assert.ok(findButton(idleB, "Load structure preview"));
  assert.equal(findButton(idleB, "sticks"), null);

  await requestPreview(scenario, artifactB);
  assert.deepEqual(scenario.events, ["clear", "render", "fetch:/content/B"]);
  await resolveArtifact(scenario, "/content/B", "MODEL B");
  const readyB = viewArtifact(scenario, artifactB);
  assert.equal(findButton(readyB, "cartoon").props["aria-pressed"], true);
  assert.equal(findButton(readyB, "sticks").props["aria-pressed"], false);
  assert.match(scenario.events.join("|"), /add:MODEL B:pdb\|style:cartoon/);

  scenario.events.length = 0;
  await switchArtifact(scenario, artifactA);
  assert.deepEqual(
    scenario.events,
    ["clear", "render"],
    "returning to an identity requires a fresh explicit request",
  );
  const idleA = viewArtifact(scenario, artifactA);
  assert.ok(findButton(idleA, "Load structure preview"));
});

test("a failed preview load reports the error and keeps controls hidden", async () => {
  const scenario = createStructureScenario();
  const artifactB = linkedArtifact("artifact-B", "/content/B");
  await switchArtifact(scenario, artifactB);
  await requestPreview(scenario, artifactB);
  scenario.requests.get("/content/B").reject(new Error("preview load failed"));
  await settle();

  const failedB = viewArtifact(scenario, artifactB);
  assert.match(nodeText(failedB), /preview load failed/);
  assert.equal(findButton(failedB, "cartoon"), null);
});

test("an artifact without a published content link remains unavailable", async () => {
  const scenario = createStructureScenario();
  const artifact = linkedArtifact("artifact-C", null);
  await switchArtifact(scenario, artifact);
  const unavailable = viewArtifact(scenario, artifact);
  assert.match(nodeText(unavailable), /browser preview was not published/);
  assert.equal(findButton(unavailable, "Load structure preview"), null);
  assert.deepEqual(scenario.events, [], "no fetch is attempted without a content link");
});

test("a visible cartoon keeps the cartoon representation after verification", async () => {
  const scenario = createStructureScenario();
  const artifactA = linkedArtifact("artifact-A", "/content/A");
  await switchArtifact(scenario, artifactA);
  await requestPreview(scenario, artifactA);
  await resolveArtifact(scenario, "/content/A", "MODEL A");

  const readyA = viewArtifact(scenario, artifactA);
  assert.equal(findButton(readyA, "cartoon").props["aria-pressed"], true);
  assert.equal(findButton(readyA, "sticks").props["aria-pressed"], false);
  assert.doesNotMatch(nodeText(readyA), /Cartoon did not render/);
  assert.doesNotMatch(scenario.events.join("|"), /style:stick/);
});

test("a cartoon that renders nothing falls back to sticks with a note", async () => {
  const scenario = createStructureScenario();
  scenario.canvas.nonBg = 0;
  const artifactA = linkedArtifact("artifact-A", "/content/A");
  await switchArtifact(scenario, artifactA);
  await requestPreview(scenario, artifactA);
  await resolveArtifact(scenario, "/content/A", "MODEL A");

  const readyA = viewArtifact(scenario, artifactA);
  assert.equal(findButton(readyA, "sticks").props["aria-pressed"], true);
  assert.equal(findButton(readyA, "cartoon").props["aria-pressed"], false);
  assert.match(nodeText(readyA), /Cartoon did not render/);
  assert.match(scenario.events.join("|"), /style:cartoon.*style:stick/);
});
