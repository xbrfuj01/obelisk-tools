const statusBox = document.getElementById("status-box");

const STATUS_LABELS = {
  queued: "У черзі",
  recording: "Запис скролу",
  encoding: "Кодування відео",
};

const DOWNLOAD_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>';
const CANCEL_ICON = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

function triggerAutoDownload(id) {
  const a = document.createElement("a");
  a.href = `/api/scroll-recorder/jobs/${id}/file`;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function cancelBtn(id) {
  return `<button type="button" class="status-cancel-corner" data-cancel-id="${id}" title="Скасувати" aria-label="Скасувати">${CANCEL_ICON}</button>`;
}

function showError(message) {
  statusBox.innerHTML = `<div class="card status-card"><p class="error">${escapeHtml(message)}</p></div>`;
}

function pollStatus(id) {
  const interval = setInterval(async () => {
    try {
      const res = await fetch(`/api/scroll-recorder/jobs/${id}`);
      const job = await res.json();
      if (!res.ok || job.error) {
        showError(job.error || "невідома помилка");
        clearInterval(interval);
        return;
      }
      if (job.status === "finished") {
        triggerAutoDownload(id);
        statusBox.innerHTML = `<div class="card status-card">
          <p class="success">✓ Готово</p>
          <a class="btn-download" href="/api/scroll-recorder/jobs/${id}/file">${DOWNLOAD_ICON} Завантажити ще раз</a>
        </div>`;
        clearInterval(interval);
      } else if (job.status === "error") {
        showError(`Помилка запису: ${job.error || "невідома помилка"}`);
        clearInterval(interval);
      } else if (job.status === "cancelled") {
        statusBox.innerHTML = `<div class="card status-card"><p>Запис скасовано.</p></div>`;
        clearInterval(interval);
      } else {
        const progress = job.progress || 0;
        const indeterminate = job.status === "queued" || job.status === "encoding";
        statusBox.innerHTML = `<div class="card status-card">
          ${cancelBtn(id)}
          <p>Статус: ${STATUS_LABELS[job.status] || job.status}${indeterminate ? "..." : ` (${Math.round(progress)}%)`}</p>
          <div class="progress"><div class="progress-bar${indeterminate ? " indeterminate" : ""}" style="width:${indeterminate ? "" : progress + "%"}"></div></div>
        </div>`;
      }
    } catch (err) {
      clearInterval(interval);
    }
  }, 1200);
}

document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".status-cancel-corner");
  if (!btn) return;
  btn.disabled = true;
  try {
    await fetch(`/api/scroll-recorder/jobs/${btn.dataset.cancelId}`, { method: "DELETE" });
  } catch (err) {
    // ignore — the next poll tick will just show whatever state actually stuck
  }
});

// --- Full-screen preview editor ---

const urlInput = document.getElementById("sr-url");
const previewBtn = document.getElementById("sr-preview-btn");

const overlay = document.getElementById("editor-overlay");
const stage = document.getElementById("editor-stage");
const screenshotImg = document.getElementById("editor-screenshot");
const loadingEl = document.getElementById("editor-loading");
const lineStart = document.getElementById("line-start");
const lineEnd = document.getElementById("line-end");
const rangeTrack = document.getElementById("range-track");
const rangeFill = document.getElementById("range-track-fill");
const handleStart = document.getElementById("handle-start");
const handleEnd = document.getElementById("handle-end");
const panel = document.getElementById("editor-panel");
const panelHeader = document.getElementById("editor-panel-header");
const closeBtn = document.getElementById("editor-close");
const deviceSegmented = document.getElementById("device-segmented");
const aspectRatioSegmented = document.getElementById("aspect-ratio-segmented");
const removeToggleBtn = document.getElementById("ed-remove-toggle");
const removeHeaderBtn = document.getElementById("ed-remove-header");
const undoBtn = document.getElementById("ed-undo");
const durationInput = document.getElementById("ed-duration");
const framerateSegmented = document.getElementById("framerate-segmented");
const blockAdsBtn = document.getElementById("ed-block-ads");
const useProxyBtn = document.getElementById("ed-use-proxy");
const resetRangeBtn = document.getElementById("ed-reset-range");
const recordBtn = document.getElementById("ed-record");

let sessionId = null;
let currentY = 0;
let currentH = 1;
let viewportWidth = 1;
let viewportHeight = 1;
let startFraction = 0.0;
let endFraction = 1.0;
let removeArmed = false;
let deviceValue = "mobile";
let aspectRatioValue = "16:9";
let framerateValue = 30;
let blockAdsArmed = false;
let useProxyArmed = false;

// Shared behavior for the two-option toggle groups (device, framerate):
// clicking a button arms it and disarms its sibling, then reports the
// chosen value via onChange.
function setupSegmented(containerEl, onChange) {
  const buttons = containerEl.querySelectorAll(".segmented-btn");
  containerEl.addEventListener("click", (e) => {
    const btn = e.target.closest(".segmented-btn");
    if (!btn) return;
    buttons.forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
    onChange(btn.dataset.value);
  });
}

function setLoading(loading) {
  loadingEl.hidden = !loading;
}

function showScreenshot(b64) {
  screenshotImg.src = `data:image/png;base64,${b64}`;
}

function totalScrollable() {
  return Math.max(1, currentH - viewportHeight);
}

function positionLineIfVisible(lineEl, absolutePx) {
  const offset = absolutePx - currentY;
  if (offset < 0 || offset > viewportHeight) {
    lineEl.hidden = true;
    return;
  }
  lineEl.style.top = `${(offset / viewportHeight) * 100}%`;
  lineEl.hidden = false;
}

function updateOverlays() {
  const trackHeight = rangeTrack.clientHeight;
  const startTop = startFraction * trackHeight;
  const endTop = endFraction * trackHeight;
  handleStart.style.top = `${startTop}px`;
  handleEnd.style.top = `${endTop}px`;
  rangeFill.style.top = `${Math.min(startTop, endTop)}px`;
  rangeFill.style.height = `${Math.abs(endTop - startTop)}px`;

  const total = totalScrollable();
  positionLineIfVisible(lineStart, startFraction * total);
  positionLineIfVisible(lineEnd, endFraction * total);
}

async function closeSession() {
  if (!sessionId) return;
  const id = sessionId;
  sessionId = null;
  try {
    await fetch(`/api/scroll-recorder/preview/${id}`, { method: "DELETE" });
  } catch (err) {
    // ignore — an idle-cleanup sweep on the sidecar will eventually close it anyway
  }
}

async function openSession() {
  setLoading(true);
  screenshotImg.removeAttribute("src");
  lineStart.hidden = true;
  lineEnd.hidden = true;
  try {
    const res = await fetch("/api/scroll-recorder/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: urlInput.value.trim(),
        aspect_ratio: aspectRatioValue,
        device: deviceValue,
        block_ads: blockAdsArmed,
        use_proxy: useProxyArmed,
      }),
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      overlay.hidden = true;
      showError(data.error || data.detail || "Не вдалося відкрити сторінку");
      return;
    }
    sessionId = data.session_id;
    viewportWidth = data.width;
    viewportHeight = data.height;
    currentY = data.y;
    currentH = data.page_height;
    showScreenshot(data.screenshot);
    updateOverlays();
  } catch (err) {
    overlay.hidden = true;
    showError("Помилка з'єднання");
  } finally {
    setLoading(false);
  }
}

async function doScroll(deltaY) {
  if (!sessionId || Math.abs(deltaY) < 1) return;
  setLoading(true);
  try {
    const res = await fetch(`/api/scroll-recorder/preview/${sessionId}/scroll`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ delta_y: deltaY }),
    });
    const data = await res.json();
    if (res.ok && data.screenshot) {
      showScreenshot(data.screenshot);
      currentY = data.y;
      currentH = data.h;
      updateOverlays();
    }
  } catch (err) {
    // ignore — the view just won't update this tick, user can scroll again
  } finally {
    setLoading(false);
  }
}

previewBtn.addEventListener("click", async () => {
  const url = urlInput.value.trim();
  if (!url) return;
  overlay.hidden = false;
  startFraction = 0.0;
  endFraction = 1.0;
  removeArmed = false;
  removeToggleBtn.setAttribute("aria-pressed", "false");
  screenshotImg.classList.remove("remove-armed");
  await openSession();
});

closeBtn.addEventListener("click", async () => {
  overlay.hidden = true;
  await closeSession();
});

function syncAspectRatioAvailability() {
  const disabled = deviceValue === "mobile";
  aspectRatioSegmented.querySelectorAll(".segmented-btn").forEach((b) => {
    b.disabled = disabled;
  });
}
syncAspectRatioAvailability();

setupSegmented(deviceSegmented, async (value) => {
  deviceValue = value;
  syncAspectRatioAvailability();
  if (overlay.hidden) return;
  await closeSession();
  startFraction = 0.0;
  endFraction = 1.0;
  await openSession();
});

setupSegmented(aspectRatioSegmented, async (value) => {
  aspectRatioValue = value;
  if (overlay.hidden || deviceValue === "mobile") return;
  await closeSession();
  startFraction = 0.0;
  endFraction = 1.0;
  await openSession();
});

setupSegmented(framerateSegmented, (value) => {
  framerateValue = Number(value);
});

// Wheel-driven navigation is the primary way to move around the live
// remote page - accumulated and debounced, since each tick is a real
// network round-trip (scroll + screenshot) through the sidecar.
let wheelAccum = 0;
let wheelTimer = null;
stage.addEventListener(
  "wheel",
  (e) => {
    e.preventDefault();
    wheelAccum += e.deltaY;
    if (wheelTimer) return;
    wheelTimer = setTimeout(() => {
      const amount = wheelAccum;
      wheelAccum = 0;
      wheelTimer = null;
      doScroll(amount);
    }, 120);
  },
  { passive: false }
);

removeToggleBtn.addEventListener("click", () => {
  removeArmed = !removeArmed;
  removeToggleBtn.setAttribute("aria-pressed", String(removeArmed));
  screenshotImg.classList.toggle("remove-armed", removeArmed);
});

blockAdsBtn.addEventListener("click", () => {
  blockAdsArmed = !blockAdsArmed;
  blockAdsBtn.setAttribute("aria-pressed", String(blockAdsArmed));
});

useProxyBtn.addEventListener("click", () => {
  useProxyArmed = !useProxyArmed;
  useProxyBtn.setAttribute("aria-pressed", String(useProxyArmed));
});

removeHeaderBtn.addEventListener("click", async () => {
  if (!sessionId) return;
  setLoading(true);
  try {
    const res = await fetch(`/api/scroll-recorder/preview/${sessionId}/remove-header`, {
      method: "POST",
    });
    const data = await res.json();
    if (res.ok && data.screenshot) showScreenshot(data.screenshot);
  } catch (err) {
    // ignore — the screenshot just won't update this click, user can retry
  } finally {
    setLoading(false);
  }
});

screenshotImg.addEventListener("click", async (e) => {
  if (!removeArmed || !sessionId) return;
  const rect = screenshotImg.getBoundingClientRect();
  // Scale against the browser viewport's own CSS pixel size (viewportWidth/
  // viewportHeight), not the screenshot's natural/physical pixel size -
  // they only match when deviceScaleFactor is 1 (desktop mode). Mobile
  // mode's Pixel 7 emulation uses deviceScaleFactor 2.625, so the
  // screenshot is ~2.6x denser than the CSS viewport elementFromPoint
  // expects - using naturalWidth/naturalHeight there sent wildly
  // out-of-viewport coordinates and silently missed every element.
  const scaleX = viewportWidth / rect.width;
  const scaleY = viewportHeight / rect.height;
  const x = (e.clientX - rect.left) * scaleX;
  const y = (e.clientY - rect.top) * scaleY;

  setLoading(true);
  try {
    const res = await fetch(`/api/scroll-recorder/preview/${sessionId}/remove`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ x, y }),
    });
    const data = await res.json();
    if (res.ok && data.screenshot) showScreenshot(data.screenshot);
  } catch (err) {
    // ignore — the screenshot just won't update this click, user can retry
  } finally {
    setLoading(false);
  }
});

undoBtn.addEventListener("click", async () => {
  if (!sessionId) return;
  setLoading(true);
  try {
    const res = await fetch(`/api/scroll-recorder/preview/${sessionId}/undo`, { method: "POST" });
    const data = await res.json();
    if (res.ok && data.screenshot) showScreenshot(data.screenshot);
  } catch (err) {
    // ignore
  } finally {
    setLoading(false);
  }
});

resetRangeBtn.addEventListener("click", () => {
  startFraction = 0.0;
  endFraction = 1.0;
  updateOverlays();
});

recordBtn.addEventListener("click", async () => {
  if (!sessionId) return;
  const id = sessionId;
  sessionId = null; // the sidecar session is consumed by /record either way
  overlay.hidden = true;

  statusBox.innerHTML = `<div class="card status-card">
    <p>Надсилаємо запит...</p>
    <div class="progress"><div class="progress-bar indeterminate"></div></div>
  </div>`;

  try {
    const res = await fetch(`/api/scroll-recorder/preview/${id}/record`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        duration_seconds: Number(durationInput.value),
        framerate: framerateValue,
        start_fraction: startFraction,
        end_fraction: endFraction,
      }),
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      showError(data.error || data.detail || "Помилка");
      return;
    }
    pollStatus(data.job_id);
  } catch (err) {
    showError("Помилка з'єднання");
  }
});

// --- Draggable settings panel ---

let panelDrag = null;
panelHeader.addEventListener("mousedown", (e) => {
  if (e.target.closest(".modal-close")) return;
  const rect = panel.getBoundingClientRect();
  panel.style.left = `${rect.left}px`;
  panel.style.top = `${rect.top}px`;
  panelDrag = { offsetX: e.clientX - rect.left, offsetY: e.clientY - rect.top };
  document.addEventListener("mousemove", onPanelDrag);
  document.addEventListener("mouseup", endPanelDrag);
});
function onPanelDrag(e) {
  if (!panelDrag) return;
  const x = Math.max(0, Math.min(window.innerWidth - panel.offsetWidth, e.clientX - panelDrag.offsetX));
  const y = Math.max(0, Math.min(window.innerHeight - panel.offsetHeight, e.clientY - panelDrag.offsetY));
  panel.style.left = `${x}px`;
  panel.style.top = `${y}px`;
}
function endPanelDrag() {
  panelDrag = null;
  document.removeEventListener("mousemove", onPanelDrag);
  document.removeEventListener("mouseup", endPanelDrag);
}

// --- Height-range handles (right-edge slider) and stripes (on the image) ---

// The live view follows the handle while dragging, not just once it's
// dropped - each network round-trip (scroll + screenshot) takes real
// time, so rapid drag movement is coalesced: a move updates the target
// scrollY and, if a request is already in flight, just waits for it to
// finish before firing the *latest* target rather than queuing every
// intermediate position.
let scrollFollowBusy = false;
let scrollFollowPending = null;

async function followTargetY(targetY) {
  scrollFollowPending = targetY;
  if (scrollFollowBusy) return;
  scrollFollowBusy = true;
  while (scrollFollowPending !== null) {
    const target = scrollFollowPending;
    scrollFollowPending = null;
    await doScroll(target - currentY);
  }
  scrollFollowBusy = false;
}

// Keeps the stripe comfortably in view while dragging instead of pinned
// to the very top/bottom edge of frame: the start stripe is held at the
// middle of the upper half of the screen, the end stripe at the middle
// of the lower half. Clamped to the page's real scroll bounds - if that
// would need to scroll past the top or bottom, the browser's own scroll
// just stays pinned there (confirmed via the server's real reported y on
// each response) and the stripe naturally keeps moving within whatever's
// still visible instead of forcing the page further.
function attachHandleDrag(handleEl, isStart) {
  const centerRatio = isStart ? 0.25 : 0.75;
  handleEl.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const trackRect = rangeTrack.getBoundingClientRect();
    function onMove(ev) {
      const fraction = Math.max(0, Math.min(1, (ev.clientY - trackRect.top) / trackRect.height));
      if (isStart) startFraction = fraction;
      else endFraction = fraction;
      updateOverlays();
      const total = totalScrollable();
      const targetY = Math.max(0, Math.min(total, fraction * total - centerRatio * viewportHeight));
      followTargetY(targetY);
    }
    function onUp() {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}
attachHandleDrag(handleStart, true);
attachHandleDrag(handleEnd, false);

function attachLineDrag(lineEl, isStart) {
  lineEl.addEventListener("mousedown", (e) => {
    e.preventDefault();
    e.stopPropagation();
    function clampedFraction(ev) {
      const imgRect = screenshotImg.getBoundingClientRect();
      const offsetPx = Math.max(0, Math.min(imgRect.height, ev.clientY - imgRect.top));
      return offsetPx / imgRect.height;
    }
    function onMove(ev) {
      lineEl.style.top = `${clampedFraction(ev) * 100}%`;
    }
    function onUp(ev) {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      const offsetFraction = clampedFraction(ev);
      const absolutePx = currentY + offsetFraction * viewportHeight;
      const fraction = Math.max(0, Math.min(1, absolutePx / totalScrollable()));
      if (isStart) startFraction = fraction;
      else endFraction = fraction;
      updateOverlays();
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}
attachLineDrag(lineStart, true);
attachLineDrag(lineEnd, false);
