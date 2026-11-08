// Runs web/static/app.js against a stub DOM (no browser) and prints the rendered state as JSON.
// Usage: node dashboard_harness.js <app.js> <data.json | "FETCH_FAIL">
"use strict";
const fs = require("fs");
const vm = require("vm");

const [appPath, dataArg] = process.argv.slice(2);
const errors = [];
const els = {};
function el(id) {
  if (!els[id]) {
    const attrs = {};
    els[id] = {
      id, innerHTML: "", textContent: "", hidden: true, className: "", title: "", value: "", disabled: false,
      dataset: {}, offsetHeight: 80, parentElement: { innerHTML: "" },
      setAttribute: (k, v) => { attrs[k] = String(v); }, getAttribute: (k) => attrs[k] ?? null,
      removeAttribute: (k) => { delete attrs[k]; }, addEventListener() {}, querySelector: () => null,
      getBoundingClientRect: () => ({ top: 1000 }), focus() {}, attrs,
    };
  }
  return els[id];
}

const ctx = {
  document: {
    documentElement: {}, title: "", getElementById: el, querySelector: () => ({ offsetHeight: 80 }),
    querySelectorAll: () => [],
  },
  window: { addEventListener() {} },
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  fetch: async () => {
    if (dataArg === "FETCH_FAIL") throw new Error("connection refused");
    const data = JSON.parse(fs.readFileSync(dataArg, "utf8"));
    return { ok: true, status: 200, json: async () => data };
  },
  console: { ...console, error: (...a) => errors.push(a.join(" ")) },
  Intl, Number, Math, String, Object, Set, Date, Array, JSON, isNaN, Promise,
};
vm.createContext(ctx);
process.on("unhandledRejection", (e) => errors.push(String(e && e.stack || e)));
try {
  vm.runInContext(fs.readFileSync(appPath, "utf8"), ctx);
} catch (e) {
  errors.push(String(e.stack || e));
}
setTimeout(() => {
  // Elements the script never touched keep their template state (hidden).
  const pick = (id) => { const e = el(id); return { hidden: e.hidden, html: e.innerHTML, text: e.textContent, disabled: e.disabled }; };
  const ids = ["loading", "content", "empty", "errorState", "errorText", "demoBanner", "sectionNav", "reportSelect",
    "kpis", "videoTable", "videoFilter", "videoCount", "videoMore", "coverage", "viewsByAge", "history", "quality",
    "generated", "formatTable", "notesCard"];
  process.stdout.write(JSON.stringify({ errors, title: ctx.document.title,
    els: Object.fromEntries(ids.map((i) => [i, pick(i)])) }));
}, 100);
