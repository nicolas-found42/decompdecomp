// The desktop offers agent decisions for inspection and preserves setup actions.
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const staticPath = path.join(__dirname, "../src/decompnet/app/static");
const read = name => fs.readFileSync(path.join(staticPath, "js", name + ".js"), "utf8").replace(/\r\n/g, "\n");
const elements = new Map();
const get = id => {
  if (!elements.has(id)) elements.set(id, {hidden: false, innerHTML: ""});
  return elements.get(id);
};
const ctx = {$: get, S: {slug: "game", decisionLast: {slug: "game", items: [
  {kind: "compiler", agent: "reviewer", detail: "Exact match checked on routine.", created_at: 1}
]}}, esc: text => String(text).replaceAll("<", "&lt;"), miniMarkdown: String, hex8: String, clock: String,
setHtml: (el, html) => { el.innerHTML = html; }};
vm.createContext(ctx);
const steering = read("steering");
vm.runInContext(steering.slice(steering.indexOf("function renderDecisions"), steering.indexOf("// a disc")), ctx);
ctx.renderDecisions({suggestions: [{text: "Tested new settings", toolchain: "gcc", check: "More evidence needed", agents: ["reviewer"], t: 1}],
  boundaries: [{function: "routine", agent: "reviewer", cut: 4, end: 32, rest: "00000000", reason: "Padding follows"}]});
let rendered = get("suggestList").innerHTML;
assert.match(rendered, /Exact match checked on routine/);
assert.match(rendered, /Still being checked/);
assert.match(rendered, /Padding follows/);
assert.doesNotMatch(rendered, /<button|waiting for you/i);

const sdk = read("sdk-libraries");
vm.runInContext(sdk.slice(sdk.indexOf("const sdkRecentDecisions"), sdk.indexOf("async function runSdkLibraryAction")), ctx);
ctx.renderSdkReview({report: {archives: 4, functions: 20, pending: 6}, reviews: {
  a: {decision: "approved", active: true, original_name: "routine", address: 12, by: "reviewer", reason: "Check <reference>",
    evidence: {name: "known", archive: "lib.a", member: "body.o", relocations: 2, resolved_relocations: 1}}
}});
rendered = get("sdkLibraryReview").innerHTML;
assert.match(rendered, /6 possible names still need evidence/);
assert.match(rendered, /Applied routine → known/);
assert.match(rendered, /Check &lt;reference>/);
assert.doesNotMatch(rendered, /<button|checkbox/);

const html = fs.readFileSync(path.join(staticPath, "index.html"), "utf8");
assert.match(html, /id="projectAdvanced"[\s\S]+?<h2>Advanced<\/h2>[\s\S]+?id="compilerPanel"/);
assert.match(html, /Connect another AI tool with MCP/);
assert.match(html, /id="sdkLibrarySelect"[^>]*>Save and scan/);
assert.doesNotMatch(html, /id="knowAuto"|id="setTcSave"|Approve selected/);
