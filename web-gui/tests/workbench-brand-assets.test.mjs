import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

const appUrl = new URL("../app/", import.meta.url);
const publicUrl = new URL("../public/", import.meta.url);

test("the workbench ships its scientific and utility fonts locally with notices", async () => {
  const fontPaths = [
    "fonts/stix-two-text/STIXTwoText-Regular.woff2",
    "fonts/stix-two-text/STIXTwoText-SemiBold.woff2",
    "fonts/ibm-plex-sans/IBMPlexSans-Regular.woff2",
    "fonts/ibm-plex-sans/IBMPlexSans-SemiBold.woff2",
    "fonts/ibm-plex-mono/IBMPlexMono-Regular.woff2",
  ];

  await Promise.all(fontPaths.map((path) => access(new URL(path, publicUrl))));

  const notice = await readFile(new URL("fonts/NOTICE.md", publicUrl), "utf8");
  assert.match(notice, /STIX Two Text/i);
  assert.match(notice, /IBM Plex Sans/i);
  assert.match(notice, /IBM Plex Mono/i);
  assert.match(notice, /SIL Open Font License, Version 1\.1/i);
  assert.match(notice, /github\.com\/stipub\/stixfonts/i);
  assert.match(notice, /github\.com\/IBM\/plex/i);
});

test("layout wires local font roles and consistent workbench metadata", async () => {
  const layout = await readFile(new URL("layout.tsx", appUrl), "utf8");
  const styles = await readFile(new URL("globals.css", appUrl), "utf8");

  assert.match(layout, /STIXTwoText-Regular\.woff2/);
  assert.match(layout, /IBMPlexSans-Regular\.woff2/);
  assert.match(layout, /IBMPlexMono-Regular\.woff2/);
  assert.match(styles, /--font-scientific:\s*"STIX Two Text"/);
  assert.match(styles, /--font-interface:\s*"IBM Plex Sans"/);
  assert.match(styles, /--font-data:\s*"IBM Plex Mono"/);
  assert.match(styles, /font-family:\s*var\(--font-interface\)/);
  assert.match(styles, /font-family:\s*var\(--font-scientific\)/);
  assert.match(styles, /font-family:\s*var\(--font-data\)/);
  assert.match(layout, /font-display:\s*swap/);
  assert.doesNotMatch(layout, /fonts\.(googleapis|gstatic)\.com/i);

  assert.match(layout, /title:\s*"CycPep Workbench"/);
  assert.match(layout, /icons:\s*\{\s*icon:\s*"\/favicon\.svg"/s);
  assert.doesNotMatch(layout, /Frontend V2 Workbench|observability contract|architecture slogan/i);
});

test("the workbench title uses the NovaPeptide team mark", async () => {
  const component = await readFile(
    new URL("workbench/components/brand-mark.tsx", appUrl),
    "utf8",
  );
  const styles = await readFile(new URL("globals.css", appUrl), "utf8");
  const logo = await readFile(new URL("nova-peptide-logo.png", publicUrl));
  const favicon = await readFile(new URL("favicon.svg", publicUrl), "utf8");

  assert.match(component, /aria-hidden="true"/);
  assert.match(styles, /url\("\/nova-peptide-logo\.png"\)/);
  assert.match(styles, /border-radius:\s*50%/);
  assert.deepEqual([...logo.subarray(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10]);

  assert.match(favicon, /<path/);
  assert.match(favicon, /<line/);
  assert.doesNotMatch(favicon, /gradient|filter|glow|<circle/gi);
  assert.doesNotMatch(favicon, /#68C4FF|#0C79D8|#2E9EFF/i);
});
