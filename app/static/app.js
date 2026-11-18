"use strict";

// Keep the screen on while cooking; the lock is dropped whenever the tab is hidden, so re-request on return.
if (document.querySelector("[data-wakelock]")) {
  const lock = () => navigator.wakeLock?.request("screen").catch(() => {});
  lock();
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") lock(); });
}

const toastEl = document.getElementById("toast");
let toastTimer;
function toast(text) {
  toastEl.replaceChildren(text);
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.hidden = true; }, 8000);
}

async function post(url, body) {
  const init = body instanceof FormData ? {method: "POST", body}
    : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  const r = await fetch(url, init);
  if (r.status === 503) throw new Error("Can't reach Grocy. What you just did may not have finished — check before trying again.");
  const data = await r.json().catch(() => null);
  if (!data || (!r.ok && !data.message)) throw new Error(`Something went wrong (${r.status}).`);
  return data;
}

// Phone photos are several MB; Claude only needs ~1.5k px.
async function shrink(file) {
  const img = await createImageBitmap(file);
  const k = Math.min(1, 1568 / Math.max(img.width, img.height));
  const c = document.createElement("canvas");
  c.width = Math.round(img.width * k);
  c.height = Math.round(img.height * k);
  c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
  return new Promise((done) => c.toBlob(done, "image/jpeg", 0.85));
}

const video = document.getElementById("scanner");
if (video) {
  const status = document.getElementById("scan-status");
  const unknown = document.getElementById("unknown");
  const photo = document.getElementById("photo");
  const dlg = document.getElementById("fix");
  const sheet = document.getElementById("ask");
  const result = document.getElementById("scan-result");
  const idle = "Point the camera at a barcode.";
  const action = () => document.querySelector("[name=scan-action]:checked").value;
  let busy = false, last = "", lastAt = 0, unknownCode = "", shown = null;

  // A number from the in-page sheet, or null when skipped.
  function ask(text, value, [no, yes]) {
    document.getElementById("ask-text").textContent = text;
    const input = document.getElementById("ask-amount");
    input.value = value ?? "";
    const [noBtn, yesBtn] = sheet.querySelectorAll("button");
    noBtn.textContent = no;
    yesBtn.textContent = yes;
    sheet.returnValue = "";  // Esc keeps the last value
    sheet.showModal();
    input.focus();
    return new Promise((done) => {
      sheet.onclose = () => {
        const n = Number(input.value);
        done(sheet.returnValue === "ok" && n > 0 && Number.isFinite(n) ? n : null);
      };
    });
  }

  function report(title, note) {
    document.getElementById("result-title").textContent = title;
    document.getElementById("result-note").textContent = note;
    result.hidden = false;
  }

  document.getElementById("result-fix").addEventListener("click", () => {
    const d = shown;
    document.getElementById("fix-name").textContent = d.product;
    const input = dlg.querySelector("[name=generic]");
    input.value = d.generic;
    dlg.onclose = async () => {
      if (dlg.returnValue !== "ok") return;
      const name = input.value.trim();
      try {
        const r = await post("/api/relink", {product_id: d.product_id, generic_name: name});
        if (r.status !== "ok") return toast(r.message);
        d.generic = name;
        document.getElementById("result-note").textContent = `Counts as ${name}.`;
      } catch (e) { toast(e.message); }
    };
    dlg.returnValue = "";
    dlg.showModal();
  });

  async function show(d, code) {
    if (d.status === "need_min") {  // a new supply: how many to keep before it goes on Running low, then the add
      const n = await ask(`Keep at least how many? (${d.product}, ${d.unit})`, d.default, ["Skip", "Save"]);
      if (n) await post(`/api/products/${d.product_id}/min`, {amount: n});
      return show(await post("/api/scan", {code, action: d.action}), code);
    }
    if (d.status === "need_amount") {  // package size unknown: ask, then send the same scan with the amount
      const n = await ask(`How much is in one package of ${d.product}? (${d.unit}) — we'll remember it.`, null,
        ["Cancel", d.action === "add" ? "Add" : "Use"]);
      if (n) return show(await post("/api/scan", {code, action: d.action, amount: n}), code);
      toast(`Nothing changed for ${d.product}.`);
    } else if (d.status === "unknown") {
      unknownCode = code;
      document.getElementById("unknown-code").textContent = code;
      unknown.hidden = false;
    } else if (d.status !== "ok") {
      toast(d.message || "Something went wrong.");
    } else {
      shown = d;
      const title = d.action === "add" ? `Added ${d.product}` : d.amount > 0 ? `Used ${d.product}` : `No ${d.product} in stock`;
      report(title, `Counts as ${d.generic}.`);
    }
  }

  async function run(label, work) {
    busy = true;
    status.textContent = label;
    try { await work(); } catch (e) { toast(e.message); }
    busy = false;
    status.textContent = idle;
  }

  photo.addEventListener("change", () => {
    const file = photo.files[0];
    photo.value = "";
    if (!file) return;
    unknown.hidden = true;
    run("Reading the photo…", async () => {
      const fd = new FormData();
      fd.append("code", unknownCode);
      fd.append("action", action());
      fd.append("image", await shrink(file), "photo.jpg");
      await show(await post("/api/scan/photo", fd), unknownCode);
    });
  });

  new ZXingBrowser.BrowserMultiFormatReader().decodeFromVideoDevice(undefined, video, (result) => {
    if (!result) return;
    const code = result.getText(), now = Date.now();
    const repeat = code === last && now - lastAt < 2000;  // same package still in front of the camera
    if (code === last) lastAt = now;
    if (busy || repeat) return;
    last = code;
    lastAt = now;
    unknown.hidden = true;
    result.hidden = true;
    run(`Looking up ${code}…`, async () => show(await post("/api/scan", {code, action: action()}), code));
  }).catch((e) => { status.textContent = `Camera unavailable: ${e.message}`; });
}

// Chore form: show only the fields for the chosen "Repeats" option (all stay visible without JS).
const schedule = document.querySelector("select[name=schedule]");
if (schedule) {
  const sync = () => document.querySelectorAll("[data-for]").forEach((el) => { el.hidden = el.dataset.for !== schedule.value; });
  schedule.addEventListener("change", sync);
  sync();
}
