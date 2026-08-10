import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("the stylesheet confines 1920x1080 to an internally scrolling workspace", async () => {
  const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");

  assert.match(
    css,
    /\.workbench-frame\s*\{[^}]*height:\s*100dvh[^}]*overflow:\s*hidden[^}]*\}/s,
    "the frame must be viewport-height and prevent page-level desktop scrolling",
  );
});

test("the 1440x900 policy yields auxiliary space before the scientific workspace", async () => {
  const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");

  assert.match(
    css,
    /@media\s*\(max-width:\s*1500px\)[\s\S]*\.workbench-frame/s,
    "1440x900 must have an explicit compact desktop policy",
  );
  assert.match(
    css,
    /@media\s*\(max-width:\s*1500px\)[\s\S]*(?:inspector|history)[\s\S]*(?:collapsed|display:\s*none|grid-template)/s,
    "the compact policy must sacrifice an auxiliary pane before the primary workspace",
  );
  assert.doesNotMatch(
    css,
    /@media\s*\(max-width:\s*1500px\)[\s\S]*\.primary-workspace\s*\{[^}]*display:\s*none/s,
  );

  const compactDesktop = css.slice(
    css.indexOf("@media (max-width: 1500px)"),
    css.indexOf("@media (max-width: 1100px)"),
  );
  assert.doesNotMatch(
    compactDesktop,
    /\.workbench-attention\s*\{[^}]*display:\s*none/s,
    "1440px must keep blocker and stale status visible",
  );
});

test("lifecycle and compact blocker DOM contracts have matching CSS selectors", async () => {
  const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");

  for (const status of ["COMMITTED", "FAILED", "ROLLED_BACK"]) {
    assert.match(css, new RegExp(`\\[data-lifecycle-status=["']${status}["']\\]`));
  }
  assert.match(css, /\.workbench-blockers\.is-compact\b/);
  assert.doesNotMatch(css, /\.workbench-blockers\.compact\b/);
});
