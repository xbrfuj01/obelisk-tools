const form = document.getElementById("download-form");
const statusBox = document.getElementById("status-box");
const modeRadios = document.querySelectorAll('input[name="mode"]');
const qualityWrap = document.getElementById("quality-wrap");
const containerWrap = document.getElementById("container-wrap");
const containerSelect = document.getElementById("container-select");
const urlInput = document.getElementById("url-input");
const urlStatus = document.getElementById("url-status");
const urlClearBtn = document.getElementById("url-clear-btn");
const qualitySelect = document.getElementById("quality-select");
const qualityHint = document.getElementById("quality-hint");
const premiereCompatWrap = document.getElementById("premiere-compat-wrap");
const subtitlesWrap = document.getElementById("subtitles-wrap");
const subtitleSelect = document.getElementById("subtitle-select");
const subtitleHint = document.getElementById("subtitle-hint");
const clipStartInput = document.getElementById("clip-start-input");
const clipEndInput = document.getElementById("clip-end-input");
const clipHint = document.getElementById("clip-hint");
const premiereCompatInput = document.getElementById("premiere-compat-input");

const CLIP_START_DEFAULT = "00:00:00";
const CLIP_END_DEFAULT = "99:99:99";
const CLIP_HINT_DEFAULT = clipHint ? clipHint.textContent : "";
const CLIP_HINT_ERROR = 'Некоректний таймкод — хвилини та секунди мають бути від 0 до 59 (напр. 00:14:48)';

// Only the leftmost (hours) part is unbounded — minutes/seconds must be < 60,
// mirroring the server-side check in parse_timecode() so bad input like
// "00:61:67" gets flagged immediately instead of only failing on submit.
function isClipFieldValid(value) {
  const parts = value.split(":");
  if (parts.length > 3) return false;
  const nums = parts.map(Number);
  if (parts.some((p) => p.trim() === "") || nums.some((n) => Number.isNaN(n) || n < 0)) return false;
  return nums.slice(1).every((n) => n < 60);
}

function validateClipField(input, defaultValue) {
  const v = input.value.trim();
  const ok = v === "" || v === defaultValue || isClipFieldValid(v);
  input.classList.toggle("field-invalid", !ok);
  return ok;
}

function updateClipValidity() {
  const startOk = validateClipField(clipStartInput, CLIP_START_DEFAULT);
  const endOk = validateClipField(clipEndInput, CLIP_END_DEFAULT);
  const allOk = startOk && endOk;
  if (clipHint) {
    clipHint.textContent = allOk ? CLIP_HINT_DEFAULT : CLIP_HINT_ERROR;
    clipHint.classList.toggle("hint-error", !allOk);
  }
  return allOk;
}

const CLIP_PASTE_INPUT_TYPES = new Set(["insertFromPaste", "insertFromPasteAsQuotation", "insertFromDrop"]);

// While the user types digits by hand, auto-insert a colon after every pair
// (0014 -> 00:14 -> 00:14:4...) so they never have to hit ":" themselves.
// Pasted/dropped text is left as-is — it's usually already formatted, and
// reformatting it would just fight the user.
function autoFormatClipInput(e) {
  const input = e.target;
  if (CLIP_PASTE_INPUT_TYPES.has(e.inputType)) {
    updateClipValidity();
    return;
  }
  const digits = input.value.replace(/\D/g, "").slice(0, 6);
  const groups = [];
  for (let i = 0; i < digits.length; i += 2) groups.push(digits.slice(i, i + 2));
  input.value = groups.join(":");
  updateClipValidity();
}

if (clipStartInput && clipEndInput) {
  clipStartInput.addEventListener("input", autoFormatClipInput);
  clipEndInput.addEventListener("input", autoFormatClipInput);
}

// Persists across page reloads and URL changes (unlike the clip/subtitle
// fields, which are tied to one specific video) - it's a standing user
// preference, not per-video state.
const PREMIERE_COMPAT_STORAGE_KEY = "obelisk_premiere_compat";
if (premiereCompatInput) {
  try {
    const stored = localStorage.getItem(PREMIERE_COMPAT_STORAGE_KEY);
    // No stored preference yet (first visit) defaults to checked; once the
    // user picks a value, that exact choice is honored from then on.
    premiereCompatInput.checked = stored === null ? true : stored === "1";
  } catch (err) {
    // ignore — localStorage unavailable, just falls back to checked (the default)
    premiereCompatInput.checked = true;
  }
  premiereCompatInput.addEventListener("change", () => {
    try {
      localStorage.setItem(PREMIERE_COMPAT_STORAGE_KEY, premiereCompatInput.checked ? "1" : "0");
    } catch (err) {
      // ignore
    }
  });
}

const VIDEO_FORMAT_OPTIONS = [
  { value: "mp4", label: "MP4" },
  { value: "webm", label: "WebM" },
  { value: "mkv", label: "MKV" },
];
const AUDIO_FORMAT_OPTIONS = [
  { value: "mp3", label: "MP3" },
  { value: "m4a", label: "M4A" },
  { value: "opus", label: "Opus" },
  { value: "wav", label: "WAV" },
];

function formatSize(bytes) {
  if (!bytes) return null;
  const mb = bytes / 1048576;
  if (mb >= 1024) return (mb / 1024).toFixed(1) + " ГБ";
  if (mb < 1) return mb.toFixed(1) + " МБ"; // avoid rounding a real, small file down to "0 МБ"
  return Math.round(mb) + " МБ";
}

function formatEta(seconds) {
  if (seconds == null || seconds < 0) return null;
  seconds = Math.round(seconds);
  if (seconds < 60) return "≈" + seconds + " с";
  const totalMinutes = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (totalMinutes < 60) return "≈" + totalMinutes + " хв" + (s ? " " + s + " с" : "");
  const h = Math.floor(totalMinutes / 60);
  const m = totalMinutes % 60;
  return "≈" + h + " год" + (m ? " " + m + " хв" : "");
}

function currentMode() {
  const checked = document.querySelector('input[name="mode"]:checked');
  return checked ? checked.value : "video";
}

let lastQualities = [];

function qualityBytes(q, mode) {
  if (mode === "video_only") return q.video_bytes;
  if (q.video_bytes && q.audio_bytes) return q.video_bytes + q.audio_bytes;
  return q.video_bytes;
}

function estimatedBytesForSelection() {
  const mode = currentMode();
  if (mode === "audio") return null;
  const q = lastQualities.find((q) => q.value === qualitySelect.value);
  return q ? qualityBytes(q, mode) : null;
}

function renderQualityOptions() {
  if (!lastQualities.length) return; // stay on just "Найкраща доступна" until a probe succeeds
  const mode = currentMode();
  const current = qualitySelect.value;
  qualitySelect.innerHTML = "";

  const bestOpt = document.createElement("option");
  bestOpt.value = "best";
  bestOpt.textContent = "Найкраща доступна";
  qualitySelect.appendChild(bestOpt);

  lastQualities.forEach((q) => {
    const opt = document.createElement("option");
    opt.value = q.value;
    let label = q.label;
    const size = formatSize(qualityBytes(q, mode));
    if (size) label += ` — ~${size}`;
    opt.textContent = label;
    qualitySelect.appendChild(opt);
  });

  const values = Array.from(qualitySelect.options).map((o) => o.value);
  qualitySelect.value = values.includes(current) ? current : "best";
}

let lastSubtitles = [];

function renderSubtitleOptions() {
  const current = subtitleSelect.value;
  subtitleSelect.innerHTML = "";

  const noneOpt = document.createElement("option");
  noneOpt.value = "";
  noneOpt.textContent = "Без субтитрів";
  subtitleSelect.appendChild(noneOpt);

  lastSubtitles.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s.code;
    opt.textContent = s.label + (s.auto ? " (авто)" : " (оригінал)");
    subtitleSelect.appendChild(opt);
  });

  const values = Array.from(subtitleSelect.options).map((o) => o.value);
  subtitleSelect.value = values.includes(current) ? current : "";
}

function updateSubtitleAvailability() {
  if (!subtitleSelect) return;
  const embeddable = containerSelect.value === "mp4" || containerSelect.value === "mkv";
  subtitleSelect.disabled = !embeddable;
  if (!embeddable) {
    subtitleSelect.value = "";
    if (subtitleHint) subtitleHint.textContent = "Субтитри вшиваються лише для MP4/MKV — оберіть інший формат файлу.";
  } else if (lastSubtitles.length) {
    if (subtitleHint) subtitleHint.textContent = `Знайдено ${lastSubtitles.length} мов(и) субтитрів для цього відео.`;
  } else if (subtitleHint) {
    subtitleHint.textContent = "";
  }
}
containerSelect.addEventListener("change", updateSubtitleAvailability);

function updateVisibility() {
  const mode = currentMode();
  qualityWrap.style.display = mode === "audio" ? "none" : "";
  if (premiereCompatWrap) premiereCompatWrap.style.display = mode === "audio" ? "none" : "";
  if (subtitlesWrap) subtitlesWrap.style.display = mode === "audio" ? "none" : "";
  renderQualityOptions();

  const options = mode === "audio" ? AUDIO_FORMAT_OPTIONS : VIDEO_FORMAT_OPTIONS;
  const current = containerSelect.value;
  containerSelect.innerHTML = "";
  options.forEach((o) => {
    const opt = document.createElement("option");
    opt.value = o.value;
    opt.textContent = o.label;
    containerSelect.appendChild(opt);
  });
  const values = options.map((o) => o.value);
  containerSelect.value = values.includes(current) ? current : options[0].value;

  updateSubtitleAvailability();
}
modeRadios.forEach((r) => r.addEventListener("change", updateVisibility));
updateVisibility();

let lastProbedUrl = "";
let probeTimer = null;

// Resets everything that only makes sense for the *previous* video - a
// clip range or subtitle language from one video is meaningless (and the
// clip range could even be invalid, e.g. longer than the new video) once
// the URL points somewhere else. "Сумісність з відеоредакторами" is a
// standing preference (persisted separately, see above), not per-video
// state, so it's deliberately left alone here.
function resetPerVideoOptions() {
  clipStartInput.value = "";
  clipEndInput.value = "";
  updateClipValidity();

  lastSubtitles = [];
  renderSubtitleOptions();
  updateSubtitleAvailability();
}

async function probeQualities() {
  const url = urlInput.value.trim();
  if (!url || !url.startsWith("http") || url === lastProbedUrl) return;
  lastProbedUrl = url;
  resetPerVideoOptions();

  urlStatus.innerHTML = '<span class="spinner"></span>';
  urlStatus.className = "url-status loading";
  qualityHint.textContent = "Отримуємо список доступних роздільних здатностей...";

  try {
    const res = await fetch(`/api/formats?url=${encodeURIComponent(url)}`);
    const data = await res.json();

    if (data.error) {
      // A real, specific reason (rate limit, module disabled, banned URL...)
      // - shown as-is instead of being flattened into the generic message
      // below, which used to make e.g. a rate limit look identical to
      // "this video just has no detectable qualities".
      urlStatus.innerHTML = "";
      qualityHint.textContent = data.error;
      return;
    }
    if (!data.qualities || !data.qualities.length) {
      urlStatus.innerHTML = "";
      qualityHint.textContent = "Не вдалося визначити якості для цього посилання — буде використано найкращу доступну.";
      return;
    }

    lastQualities = data.qualities;
    lastSubtitles = data.subtitles || [];
    renderQualityOptions();
    renderSubtitleOptions();
    updateSubtitleAvailability();

    urlStatus.innerHTML = "✓";
    urlStatus.className = "url-status ok";
    qualityHint.textContent = `Знайдено ${data.qualities.length} варіант(ів) якості для цього відео.`;
  } catch (err) {
    urlStatus.innerHTML = "";
    qualityHint.textContent = "Не вдалося зв'язатись із сервером для перевірки якостей.";
  }
}

urlInput.addEventListener("blur", probeQualities);
urlInput.addEventListener("paste", () => {
  clearTimeout(probeTimer);
  probeTimer = setTimeout(probeQualities, 200);
});
urlInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    probeQualities();
  }
});

function updateUrlClearButton() {
  if (urlClearBtn) urlClearBtn.hidden = !urlInput.value;
}
urlInput.addEventListener("input", updateUrlClearButton);
updateUrlClearButton();

if (urlClearBtn) {
  urlClearBtn.addEventListener("click", () => {
    urlInput.value = "";
    lastProbedUrl = "";
    lastQualities = [];
    qualitySelect.innerHTML = '<option value="best">Найкраща доступна</option>';
    resetPerVideoOptions();
    urlStatus.innerHTML = "";
    urlStatus.className = "url-status";
    qualityHint.textContent = "";
    updateUrlClearButton();
    urlInput.focus();
  });
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!updateClipValidity()) {
    statusBox.innerHTML = `<div class="card status-card"><p class="error">${CLIP_HINT_ERROR}</p></div>`;
    return;
  }
  const estimatedBytes = estimatedBytesForSelection();
  const fd = new FormData(form);
  const clipStart = clipStartInput.value.trim();
  const clipEnd = clipEndInput.value.trim();
  fd.set("clip_start", clipStart === CLIP_START_DEFAULT ? "" : clipStart);
  fd.set("clip_end", clipEnd === CLIP_END_DEFAULT ? "" : clipEnd);
  const isClipped = Boolean(fd.get("clip_start")) || Boolean(fd.get("clip_end"));
  statusBox.innerHTML = '<div class="card status-card"><p>Додаємо у чергу...</p></div>';
  try {
    const res = await fetch("/api/download", { method: "POST", body: fd });
    const data = await res.json();
    if (data.error) {
      statusBox.innerHTML = `<div class="card status-card"><p class="error">${data.error}</p></div>`;
      return;
    }
    pollStatus(data.id, estimatedBytes, isClipped);
    document.dispatchEvent(new CustomEvent("obelisk:jobs-changed"));
  } catch (err) {
    statusBox.innerHTML = `<div class="card status-card"><p class="error">Помилка з'єднання</p></div>`;
  }
});

const STATUS_LABELS = {
  queued: "У черзі",
  downloading: "Підготовка",
};

const DOWNLOAD_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>';
const CONVERT_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 7h11l-3.5-3.5"/><path d="M17 17H6l3.5 3.5"/></svg>';
const STATUS_CANCEL_ICON = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';
// Same icon set as admin.html's status_badge macro / STATUS_ICONS, so a
// given status looks identical whether it's rendered by the server (admin)
// or here on the client.
const OPEN_ORIGINAL_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>';
const STATUS_ICON_FINISHED = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
const STATUS_ICON_ERROR = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';
const STATUS_ICON_EXPIRED = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2h4"/><path d="M12 14v-4"/><circle cx="12" cy="14" r="8"/></svg>';
const STATUS_ICON_CANCELLED = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m8 8 8 8"/></svg>';

// Top-right cancel button embedded in the active-status card itself (as
// opposed to the one hidden behind hover in the history rows) - kind is
// "download" or "conversion", matching the two /api/*cancel/{id} routes.
function statusCancelBtn(kind, id) {
  return `<button type="button" class="status-cancel-corner" data-cancel-kind="${kind}" data-cancel-id="${id}" title="Скасувати" aria-label="Скасувати">${STATUS_CANCEL_ICON}</button>`;
}

// Kicks off the browser's own download for the finished file without
// navigating away from the page, so the user gets it on their device
// automatically instead of having to click the button themselves.
function triggerAutoDownload(url) {
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// Shared with processes.js: both this page's own poller and the global
// processes tray can notice the same job finish (e.g. the tray polls
// regardless of which page you're on) - this keeps whichever one gets
// there first from triggering the browser download twice.
const AUTO_DOWNLOAD_STORAGE_KEY = "obelisk_auto_downloaded";
function claimAutoDownload(key) {
  try {
    const done = JSON.parse(localStorage.getItem(AUTO_DOWNLOAD_STORAGE_KEY) || "[]");
    if (done.includes(key)) return false;
    done.push(key);
    if (done.length > 200) done.splice(0, done.length - 200);
    localStorage.setItem(AUTO_DOWNLOAD_STORAGE_KEY, JSON.stringify(done));
    return true;
  } catch (err) {
    return true; // no localStorage — just always trigger, better than never
  }
}

function pollStatus(id, estimatedBytes, isClipped) {
  const estimatedSize = formatSize(estimatedBytes);
  const interval = setInterval(async () => {
    try {
      const res = await fetch(`/api/status/${id}`);
      const job = await res.json();
      if (job.status === "finished" && job.auto_convert_id) {
        // Компат-режим виявив несумісний кодек і вже запустив конвертацію —
        // показуємо той самий рядок стану, але вже для процесу конвертації.
        clearInterval(interval);
        refreshRecent();
        document.dispatchEvent(new CustomEvent("obelisk:jobs-changed"));
        pollAutoConvert(job.auto_convert_id, job.title);
      } else if (job.status === "finished") {
        const finalSize = formatSize(job.filesize) || estimatedSize;
        if (claimAutoDownload("download:" + id)) triggerAutoDownload(`/api/file/${id}`);
        // Only offered when premiere_compat was off - if it was on, the file
        // either got auto-converted already (the auto_convert_id branch
        // above) or was already compatible, so a manual convert option here
        // would just be redundant.
        const convertHref = `/converter?from_download=${id}&filename=${encodeURIComponent(job.title || "")}`;
        const convertBtn = job.premiere_compat ? "" : `<a class="btn-convert" href="${convertHref}">${CONVERT_ICON} Конвертувати для Premiere</a>`;
        statusBox.innerHTML = `<div class="card status-card">
          <p class="success">✓ Готово: ${escapeHtml(job.title || "")}${finalSize ? ` (${finalSize})` : ""}</p>
          <div class="status-actions">
            <a class="btn-download" href="/api/file/${id}">${DOWNLOAD_ICON} Завантажити ще раз</a>
            ${convertBtn}
          </div>
        </div>`;
        clearInterval(interval);
        refreshRecent();
        document.dispatchEvent(new CustomEvent("obelisk:jobs-changed"));
      } else if (job.status === "error") {
        statusBox.innerHTML = `<div class="card status-card"><p class="error">Помилка завантаження: ${escapeHtml(job.error || "невідома помилка")}</p></div>`;
        clearInterval(interval);
        refreshRecent();
        document.dispatchEvent(new CustomEvent("obelisk:jobs-changed"));
      } else if (job.status === "cancelled") {
        statusBox.innerHTML = `<div class="card status-card"><p>Завантаження скасовано.</p></div>`;
        clearInterval(interval);
        refreshRecent();
        document.dispatchEvent(new CustomEvent("obelisk:jobs-changed"));
      } else if (isClipped) {
        // yt-dlp never reports incremental progress while cutting a clip —
        // only a single event once it's fully done — so a real percentage
        // would just sit frozen. Show an indeterminate bar instead.
        statusBox.innerHTML = `<div class="card status-card">
          ${statusCancelBtn("download", id)}
          <p>Статус: Підготовка фрагмента... (це може тривати довше за звичайне завантаження)</p>
          <div class="progress"><div class="progress-bar indeterminate"></div></div>
        </div>`;
      } else {
        const progress = job.progress || 0;
        const label = STATUS_LABELS[job.status] || job.status;
        const eta = formatEta(job.eta_seconds);
        statusBox.innerHTML = `<div class="card status-card">
          ${statusCancelBtn("download", id)}
          <p>Статус: ${label} (${progress}%)${eta ? ` — ${eta} до завершення` : estimatedSize ? ` — орієнтовно ~${estimatedSize}` : ""}</p>
          <div class="progress"><div class="progress-bar" style="width:${progress}%"></div></div>
        </div>`;
      }
    } catch (err) {
      clearInterval(interval);
    }
  }, 1500);
}

// Continues the same status card once a download has handed off to an
// auto-triggered "make it Premiere-compatible" conversion, so the user
// sees one continuous process instead of the card just going quiet.
function pollAutoConvert(convertId, title) {
  const interval = setInterval(async () => {
    try {
      const res = await fetch(`/api/convert/status/${convertId}`);
      const job = await res.json();
      if (job.status === "finished") {
        const size = formatSize(job.filesize);
        if (claimAutoDownload("conversion:" + convertId)) triggerAutoDownload(`/api/convert/file/${convertId}`);
        statusBox.innerHTML = `<div class="card status-card">
          <p class="success">✓ Готово: ${escapeHtml(title || "")}${size ? ` (${size})` : ""}</p>
          <p class="hint">Відео автоматично сконвертовано для сумісності з відеоредакторами.</p>
          <div class="status-actions">
            <a class="btn-download" href="/api/convert/file/${convertId}">${DOWNLOAD_ICON} Завантажити ще раз</a>
          </div>
        </div>`;
        clearInterval(interval);
        document.dispatchEvent(new CustomEvent("obelisk:jobs-changed"));
      } else if (job.status === "error") {
        statusBox.innerHTML = `<div class="card status-card"><p class="error">Відео завантажено, але автоконвертація не вдалась: ${escapeHtml(job.error || "невідома помилка")}</p></div>`;
        clearInterval(interval);
        document.dispatchEvent(new CustomEvent("obelisk:jobs-changed"));
      } else if (job.status === "cancelled") {
        statusBox.innerHTML = `<div class="card status-card"><p>Автоконвертацію скасовано.</p></div>`;
        clearInterval(interval);
        document.dispatchEvent(new CustomEvent("obelisk:jobs-changed"));
      } else if (job.status === "queued") {
        const posLine = job.queue_position
          ? `Місце в черзі на конвертацію: ${job.queue_position}${job.queue_total ? ` з ${job.queue_total}` : ""}`
          : "У черзі на конвертацію для сумісності з відеоредакторами...";
        statusBox.innerHTML = `<div class="card status-card">
          ${statusCancelBtn("conversion", convertId)}
          <p>${posLine}</p>
          <div class="progress"><div class="progress-bar indeterminate"></div></div>
        </div>`;
      } else {
        const progress = job.progress || 0;
        const eta = formatEta(job.eta_seconds);
        statusBox.innerHTML = `<div class="card status-card">
          ${statusCancelBtn("conversion", convertId)}
          <p>Статус: Конвертація для сумісності з відеоредакторами (${progress}%)${eta ? ` — ${eta} до завершення` : ""}</p>
          <div class="progress"><div class="progress-bar" style="width:${progress}%"></div></div>
        </div>`;
      }
    } catch (err) {
      clearInterval(interval);
    }
  }, 1500);
}

const CANCELLABLE_STATUSES = { queued: true, downloading: true };

function statusIcon(status) {
  if (status === "finished") return STATUS_ICON_FINISHED;
  if (status === "error") return STATUS_ICON_ERROR;
  if (status === "expired") return STATUS_ICON_EXPIRED;
  if (status === "cancelled") return STATUS_ICON_CANCELLED;
  if (status === "downloading") return '<span class="spinner"></span>';
  if (status === "queued") return "⏳";
  return "–";
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

const CANCEL_ICON = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';

function renderRow(r) {
  let btn;
  if (r.status === "finished") {
    const convertHref = `/converter?from_download=${r.id}&filename=${encodeURIComponent(r.title || "")}`;
    // Same reasoning as pollStatus: only useful when premiere_compat was off.
    const convertBtn = r.premiere_compat ? "" : `<a class="dl-convert-btn" href="${convertHref}" title="Конвертувати для Premiere">${CONVERT_ICON}</a>`;
    btn = `${convertBtn}
      <a class="dl-download-btn" href="/api/file/${r.id}" title="Завантажити">${DOWNLOAD_ICON}</a>`;
  } else {
    btn = `<span class="dl-download-btn disabled" title="Ще не готово">${DOWNLOAD_ICON}</span>`;
  }
  const size = formatSize(r.filesize);
  const title = size ? `${r.title} (${size})` : r.title;
  const sourceLink = r.url
    ? `<a class="dl-source-btn" href="${escapeHtml(r.url)}" target="_blank" rel="noopener noreferrer" title="Відкрити оригінал" aria-label="Відкрити оригінал">${OPEN_ORIGINAL_ICON}</a>`
    : "";
  const statusCell = CANCELLABLE_STATUSES[r.status]
    ? `<span class="status-icon-wrap">
         <span class="status-icon status-${r.status}">${statusIcon(r.status)}</span>
         <button type="button" class="status-cancel-btn" data-cancel-id="${r.id}" title="Скасувати" aria-label="Скасувати">${CANCEL_ICON}</button>
       </span>`
    : `<span class="status-icon status-${r.status}">${statusIcon(r.status)}</span>`;
  return `
    <div class="dl-row" data-id="${r.id}">
      ${statusCell}
      <button type="button" class="dl-title">${escapeHtml(title)}</button>
      ${sourceLink}
      ${btn}
    </div>`;
}

let recentPage = 1;

function renderPagination(page, totalPages) {
  if (totalPages <= 1) return "";
  const atFirst = page <= 1;
  const atLast = page >= totalPages;
  return `<nav class="pagination" data-max="${totalPages}">
    <button type="button" class="page-link" data-page="1"${atFirst ? " disabled" : ""} title="Перша сторінка" aria-label="Перша сторінка">«</button>
    <button type="button" class="page-link" data-page="${page - 1}"${atFirst ? " disabled" : ""} title="Попередня сторінка" aria-label="Попередня сторінка">‹</button>
    <span class="page-jump">
      <input type="number" min="1" max="${totalPages}" value="${page}" class="page-jump-input" title="Введіть номер сторінки і натисніть Enter" aria-label="Номер сторінки">
      <span>з ${totalPages}</span>
    </span>
    <button type="button" class="page-link" data-page="${page + 1}"${atLast ? " disabled" : ""} title="Наступна сторінка" aria-label="Наступна сторінка">›</button>
    <button type="button" class="page-link" data-page="${totalPages}"${atLast ? " disabled" : ""} title="Остання сторінка" aria-label="Остання сторінка">»</button>
  </nav>`;
}

async function refreshRecent() {
  try {
    const res = await fetch(`/api/recent?page=${recentPage}`);
    const data = await res.json();
    const list = document.getElementById("recent-list");
    const paginationBox = document.getElementById("recent-pagination");
    if (!list) return;
    recentPage = data.page;
    if (!data.items.length) {
      list.innerHTML = `<p class="empty-hint">Ще немає завантажень</p>`;
    } else {
      list.innerHTML = data.items.map(renderRow).join("");
    }
    if (paginationBox) paginationBox.innerHTML = renderPagination(data.page, data.total_pages);
  } catch (err) {
    // ignore
  }
}
refreshRecent();
setInterval(refreshRecent, 5000);

document.addEventListener("click", (e) => {
  const pageBtn = e.target.closest("#recent-pagination .page-link");
  if (!pageBtn) return;
  recentPage = parseInt(pageBtn.dataset.page, 10) || 1;
  refreshRecent();
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const input = e.target.closest("#recent-pagination .page-jump-input");
  if (!input) return;
  e.preventDefault();
  const max = parseInt(input.closest(".pagination").dataset.max, 10) || 1;
  recentPage = Math.max(1, Math.min(max, parseInt(input.value, 10) || 1));
  refreshRecent();
});

document.addEventListener("click", (e) => {
  const titleBtn = e.target.closest(".dl-title");
  if (titleBtn) titleBtn.classList.toggle("expanded");
});

document.addEventListener("click", async (e) => {
  const cancelBtn = e.target.closest(".status-cancel-btn");
  if (!cancelBtn) return;
  cancelBtn.disabled = true;
  try {
    await fetch(`/api/cancel/${cancelBtn.dataset.cancelId}`, { method: "POST" });
  } catch (err) {
    // ignore — the row will just keep showing its old state until the next poll
  }
  refreshRecent();
});

document.addEventListener("click", async (e) => {
  const cancelBtn = e.target.closest(".status-cancel-corner");
  if (!cancelBtn) return;
  cancelBtn.disabled = true;
  const url = cancelBtn.dataset.cancelKind === "conversion"
    ? `/api/convert/cancel/${cancelBtn.dataset.cancelId}`
    : `/api/cancel/${cancelBtn.dataset.cancelId}`;
  try {
    await fetch(url, { method: "POST" });
  } catch (err) {
    // ignore — the next poll tick will just show whatever state actually stuck
  }
});
