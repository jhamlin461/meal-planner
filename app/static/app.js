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
  if (r.status === 502) throw new Error("Grocy turned that change down, so nothing was saved.");
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

// Scanner (Scan & Go): a read stops decoding and holds the frame; the sheet looks the code up and nothing changes
// until it's confirmed. Closing the sheet any way (Cancel, Esc, a confirm) starts scanning again.
const video = document.getElementById("scanner");
if (video) {
  const $ = (id) => document.getElementById(id);
  const sheet = $("sheet"), found = $("found"), manual = $("manual"), photo = $("photo"), fix = $("fix");
  const stage = document.querySelector(".scan-stage"), status = $("scan-status"), tripBtn = $("trip");
  const reader = new ZXingBrowser.BrowserMultiFormatReader();
  const mode = () => document.querySelector("[name=scan-action]:checked");
  let scanning = null, last = "", lastAt = 0, code = "", item = null, action = "add", turn = 0, retry = null;

  // This trip: kept for the tab's session; without storage it still works, it just isn't kept across reloads.
  let trip = [];
  try { trip = JSON.parse(sessionStorage.getItem("scan-trip")) || []; } catch { trip = []; }
  function saveTrip() {
    try { sessionStorage.setItem("scan-trip", JSON.stringify(trip)); } catch { /* not kept, still shown */ }
    drawTrip();
  }
  function drawTrip() {
    const added = trip.filter((t) => t.action === "add").length, used = trip.length - added;
    tripBtn.textContent = [added && `${added} added`, used && `${used} used`].filter(Boolean).join(", ");
    tripBtn.hidden = !trip.length;
    $("trip-list").replaceChildren(...trip.map((t) => {
      const li = document.createElement("li"), text = document.createElement("span");
      const undo = document.createElement("button"), err = document.createElement("p");
      li.className = "trip-line";
      text.className = "grow breakable";
      text.textContent = t.text;
      undo.type = "button";
      undo.className = "btn";
      undo.textContent = "Undo";
      undo.setAttribute("aria-label", `Undo ${t.text}`);
      err.className = "error";
      err.setAttribute("role", "alert");
      err.hidden = true;
      undo.addEventListener("click", async () => {
        undo.disabled = true;
        try {
          const r = await post("/api/scan/undo", {transaction_id: t.id, action: t.action});
          if (r.status !== "ok") throw new Error(r.message);
          trip = trip.filter((x) => x !== t);
          saveTrip();
          if (trip.length) sheet.querySelector("[data-state=trip] [data-focus]").focus();
          else sheet.close();
        } catch (e) {
          err.textContent = e.message;
          err.hidden = false;
          undo.disabled = false;
        }
      });
      li.append(text, undo, err);
      return li;
    }));
  }
  drawTrip();

  function cameraError(e) {
    status.textContent = `Camera unavailable: ${e?.message || "the camera didn't start"}`;
    $("elsewhere").hidden = false;
  }

  // Decode until a read. The camera stays on between reads; only the decoding stops.
  function scan() {
    if (scanning || !video.srcObject) return;
    stage.classList.remove("locked");
    lastAt = Date.now();  // the package just handled may still be in front of the camera
    scanning = reader.decodeFromVideoElement(video, (result, _error, controls) => {
      if (!result) return;
      const c = result.getText(), now = Date.now();
      const repeat = c === last && now - lastAt < 2000;
      if (c === last) lastAt = now;
      if (repeat) return;
      controls.stop();
      scanning = null;
      video.pause();  // hold the frame that was read
      stage.classList.add("locked");
      navigator.vibrate?.(60);
      last = c;
      code = c;
      action = mode().value;
      lookup(() => post("/api/scan/lookup", {code}));
    }).catch(cameraError);
  }
  function hold() {
    scanning?.then((controls) => controls?.stop());
    scanning = null;
  }
  Promise.resolve().then(() => navigator.mediaDevices.getUserMedia({video: {facingMode: "environment"}}))
    .then((stream) => { video.srcObject = stream; scan(); }, cameraError);

  function show(state) {
    for (const s of sheet.querySelectorAll("[data-state]")) s.hidden = s.dataset.state !== state;
    sheet.setAttribute("aria-labelledby", `h-${state}`);
    if (!sheet.open) {
      hold();
      sheet.showModal();
    }
    sheet.querySelector(`[data-state=${state}] [data-focus]`)?.focus();
  }
  sheet.addEventListener("close", () => {
    turn++;  // a lookup still on its way is dropped
    mode().focus();
    scan();
  });
  sheet.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) sheet.close(); });

  // Look something up with the sheet in its loading state; `again` is what Try again does.
  async function lookup(request, label = "Looking up…", again = null) {
    const my = ++turn;
    retry = again || (() => lookup(request, label, again));
    $("h-loading").textContent = label;
    show("loading");
    let d;
    try { d = await request(); } catch (e) { d = {status: "error", message: e.message}; }
    if (my !== turn || !sheet.open) return;
    if (d.status === "found") showFound(d);
    else if (d.status === "manual") openManual(d.name, true);
    else if (d.status === "unknown") {
      $("unknown-code").textContent = code;
      show("unknown");
    } else {
      $("error-text").textContent = d.message || "Something went wrong.";
      show("error");
    }
  }
  $("retry").addEventListener("click", () => retry());

  const qty = (n, unit) => (unit && unit !== "piece" ? `${n} ${unit}` : `${n}`);
  const byAmount = () => !$("amount-field").hidden;

  function showFound(d) {
    item = d;
    const use = action === "use", f = found.elements;
    const amountMode = d.package == null && !(use && d.kind === "supply");  // supplies are used one at a time
    const keep = d.need_min && !use;
    $("h-found").textContent = d.product;
    $("found-generic").textContent = `Counts as ${d.generic}`;
    const left = +Number(d.in_stock || 0).toFixed(2);
    $("found-left").textContent = `${qty(left, d.unit)} left`;
    $("found-photo").hidden = !d.photo;
    if (d.photo) $("found-photo").src = d.photo;
    f.place.replaceChildren(...d.places.map((p) => new Option(p, p, p === d.place, p === d.place)));
    $("place-field").hidden = use;
    $("packs-field").hidden = amountMode;
    f.packs.value = 1;
    $("amount-field").hidden = !amountMode;
    f.amount.value = "";
    f.amount.required = amountMode;
    const unit = d.unit === "piece" ? "pieces" : d.unit;
    $("amount-label").textContent = use ? `How much did you use? (${unit})` : `How much is in it? (${unit})`;
    $("amount-hint").hidden = use;
    $("min-field").hidden = !keep;
    $("min-label").textContent = `Keep at least (${unit})`;
    f.min.value = keep ? d.package ?? 1 : "";
    $("found-error").hidden = true;
    label();
    show("found");
    if (amountMode) f.amount.focus();
  }

  function label() {
    const f = found.elements;
    $("found-go").textContent = action === "add" ? `Add to ${f.place.value}` : byAmount() ? "Use" : `Use ${f.packs.value || 1}`;
  }
  found.addEventListener("input", label);
  for (const b of found.querySelectorAll("[data-step]")) {
    b.addEventListener("click", () => {
      const packs = found.elements.packs;
      packs.value = Math.max(1, Math.round(Number(packs.value) || 1) + Number(b.dataset.step));
      label();
    });
  }

  function fail(text) {
    $("found-error").textContent = text;
    $("found-error").hidden = false;
  }

  found.addEventListener("submit", async (e) => {
    e.preventDefault();
    const d = item, f = found.elements, go = $("found-go");
    const packs = Number(f.packs.value), amount = byAmount() ? Number(f.amount.value) : null;
    const min = $("min-field").hidden ? null : Number(f.min.value) || null;
    if (amount !== null && !(amount > 0)) return fail("Type how much.");
    if (amount === null && !(Number.isInteger(packs) && packs >= 1)) return fail("Pick a whole number, 1 or more.");
    if (min !== null && !(min > 0)) return fail("Keep at least needs a number above 0.");
    const place = action === "add" ? f.place.value : null;
    go.disabled = true;
    $("found-error").hidden = true;
    try {
      const r = await post("/api/scan/confirm", {product_id: d.product_id, action, packs: amount ? 1 : packs, amount,
        place, min});
      if (r.status !== "ok") throw new Error(r.message);
      const n = amount ? qty(amount, d.unit) : packs;
      const asked = amount || (d.kind === "supply" ? packs : packs * (d.package || 0));
      let text;
      if (action === "add") text = `Added ${n} ${d.product} to ${place}`;
      else if (r.amount <= 0) text = `No ${d.product} in stock, so nothing was used`;
      else if (r.amount < asked) text = `Used the last ${d.product}`;
      else text = `Used ${n} ${d.product}`;
      if (r.transaction_id) {
        trip.unshift({id: r.transaction_id, action, text});
        saveTrip();
      }
      toast(text);
      if (sheet.open) sheet.close();
    } catch (err) {
      fail(err.message);
    }
    go.disabled = false;
  });

  // "Counts as": the relink dialog opens over the sheet.
  $("found-change").addEventListener("click", () => {
    const d = item, input = fix.querySelector("[name=generic]");
    $("fix-name").textContent = d.product;
    input.value = d.generic;
    fix.onclose = async () => {
      if (fix.returnValue !== "ok") return;
      const name = input.value.trim();
      try {
        const r = await post("/api/relink", {product_id: d.product_id, generic_name: name});
        if (r.status !== "ok") throw new Error(r.message);
        d.generic = name;
        $("found-generic").textContent = `Counts as ${name}`;
      } catch (err) { fail(err.message); }
    };
    fix.returnValue = "";
    fix.showModal();
  });

  $("take-photo").addEventListener("click", () => photo.click());
  photo.addEventListener("change", () => {
    const file = photo.files[0];
    photo.value = "";
    if (!file) return;
    lookup(async () => {
      const fd = new FormData();
      fd.append("code", code);
      fd.append("image", await shrink(file), "photo.jpg");
      return post("/api/scan/photo", fd);
    }, "Reading the photo…", () => show("unknown"));
  });

  function openManual(name, paused) {
    manual.reset();
    manual.elements.namedItem("name").value = name || "";
    $("manual-note").textContent = paused ? `AI is paused until the 1st, so tell me about barcode ${code}.`
      : `Barcode ${code}.`;
    show("manual");
  }
  $("to-manual").addEventListener("click", () => openManual("", false));
  manual.addEventListener("submit", (e) => {
    e.preventDefault();
    const fd = new FormData(manual);
    fd.append("code", code);
    lookup(() => post("/api/scan/manual", fd), "Saving…", () => show("manual"));
  });

  tripBtn.addEventListener("click", () => show("trip"));
}

// Chore form: show only the fields for the chosen "Repeats" option (all stay visible without JS).
const schedule = document.querySelector("select[name=schedule]");
if (schedule) {
  const sync = () => document.querySelectorAll("[data-for]").forEach((el) => { el.hidden = el.dataset.for !== schedule.value; });
  schedule.addEventListener("change", sync);
  sync();
}

// Table editor: a chip another step already ticks is marked "used" and hidden (until "Show all"; CSS). The server
// marks them on render; this keeps the marks current as boxes are ticked and unticked.
document.querySelectorAll(".step-rows").forEach((list) => list.addEventListener("change", () => {
  const ticked = new Set([...list.querySelectorAll(".chip input:checked")].map((b) => b.value));
  list.querySelectorAll(".chip input").forEach((b) => b.parentElement.classList.toggle("used", !b.checked && ticked.has(b.value)));
}));

// Recipe photo: without popover support the thumbnail opens the full photo itself.
if (!HTMLElement.prototype.hasOwnProperty("popover")) {
  document.querySelectorAll(".recipe-thumb[data-href]").forEach((b) => b.addEventListener("click", () => {
    location.href = b.dataset.href;
  }));
}

// Recipe table: tap (or Enter/Space on) a step or an ingredient to cross it off; kept per recipe on this device.
// Storage may be missing (private mode): crossing off still works, it just isn't remembered.
document.querySelectorAll("[data-cross-key]").forEach((wrap) => {
  const key = wrap.dataset.crossKey;
  const cells = wrap.querySelectorAll("[data-cross]");
  let done;
  try { done = new Set(JSON.parse(localStorage.getItem(key) || "[]")); } catch { done = new Set(); }
  const show = () => cells.forEach((c) => c.setAttribute("aria-pressed", done.has(c.dataset.cross)));
  const save = () => {
    try { done.size ? localStorage.setItem(key, JSON.stringify([...done])) : localStorage.removeItem(key); } catch {}
    show();
  };
  const toggle = (c) => { done.delete(c.dataset.cross) || done.add(c.dataset.cross); save(); };
  wrap.addEventListener("click", (e) => { const c = e.target.closest("[data-cross]"); if (c) toggle(c); });
  wrap.addEventListener("keydown", (e) => {
    const c = e.target.closest("[data-cross]");
    if (c && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggle(c); }
  });
  document.getElementById("start-over")?.addEventListener("click", () => { done.clear(); save(); });
  show();
});

// Import drop zone: the input covers the zone; say which files were picked and light up while dragging over.
document.querySelectorAll(".drop").forEach((zone) => {
  const input = zone.querySelector("input"), names = zone.querySelector(".drop-files"), idle = names.textContent;
  input.addEventListener("change", () => {
    const n = input.files.length;
    names.textContent = n === 0 ? idle : n === 1 ? input.files[0].name : `${n} PDFs: ${[...input.files].map((f) => f.name).join(", ")}`;
  });
  input.addEventListener("dragenter", () => zone.classList.add("over"));
  ["dragleave", "drop"].forEach((t) => input.addEventListener(t, () => zone.classList.remove("over")));
});

// Import review: the source PDF is folded away on smaller screens and always open beside the review on wide ones.
const wide = matchMedia("(min-width: 1280px)");
const openWide = () => document.querySelectorAll("[data-open-wide]").forEach((d) => { if (wide.matches) d.open = true; });
wide.addEventListener("change", openWide);
openWide();
