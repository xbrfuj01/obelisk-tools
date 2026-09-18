const form = document.getElementById("convert-form");
const statusBox = document.getElementById("status-box");
const fileInput = document.getElementById("file-input");
const dropZone = document.getElementById("file-drop-zone");
const fileDropText = document.getElementById("file-drop-text");
const fileClearBtn = document.getElementById("file-clear-btn");
const DEFAULT_DROP_TEXT = "Натисніть щоб завантажити або перетягніть сюди";
const advancedToggle = document.getElementById("advanced-toggle");
const advancedWrap = document.getElementById("advanced-wrap");

if (advancedToggle) {
  advancedToggle.addEventListener("change", () => {
    advancedWrap.hidden = !advancedToggle.checked;
  });
}

dropZone.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    fileInput.click();
  }
});

// Arriving via "Конвертувати" on an already-downloaded file: skip the
// picker entirely and pre-fill the file field with that download instead
// of a real browser File — the server just copies its own file, no upload.
const _params = new URLSearchParams(window.location.search);
let sourceDownloadId = _params.get("from_download");
const sourceFilename = _params.get("filename");
if (sourceDownloadId) {
  fileInput.required = false;
  fileDropText.textContent = sourceFilename ? `${sourceFilename} (із завантажень)` : "Файл із завантажень";
  fileClearBtn.hidden = false;
  dropZone.classList.add("has-file");
}

fileClearBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  sourceDownloadId = null;
  fileInput.required = true;
  fileInput.value = "";
  fileInput.dispatchEvent(new Event("change"));
});

fileInput.addEventListener("change", () => {
  sourceDownloadId = null; // picking a file manually overrides the download source
  const file = fileInput.files[0];
  fileDropText.textContent = file ? file.name : DEFAULT_DROP_TEXT;
  fileClearBtn.hidden = !file;
  dropZone.classList.toggle("has-file", !!file);
});

// Prevent the browser's default "navigate to the dropped file" behavior
// for drops that land outside the drop zone.
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());

["dragenter", "dragover"].forEach((evt) =>
  dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropZone.classList.add("dragover");
  })
);
["dragleave", "dragend", "drop"].forEach((evt) =>
  dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropZone.classList.remove("dragover");
  })
);
dropZone.addEventListener("drop", (e) => {
  const file = e.dataTransfer.files && e.dataTransfer.files[0];
  if (!file) return;
  const dt = new DataTransfer();
  dt.items.add(file);
  fileInput.files = dt.files;
  fileInput.dispatchEvent(new Event("change"));
});

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

const STATUS_LABELS = {
  queued: "У черзі",
  converting: "Конвертація",
};

const DOWNLOAD_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>';
const STATUS_CANCEL_ICON = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';
// Same icon set as admin.html's status_badge macro / STATUS_ICONS, so a
// given status looks identical whether it's rendered by the server (admin)
// or here on the client.
const STATUS_ICON_FINISHED = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
const STATUS_ICON_ERROR = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';
const STATUS_ICON_EXPIRED = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2h4"/><path d="M12 14v-4"/><circle cx="12" cy="14" r="8"/></svg>';
const STATUS_ICON_CANCELLED = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m8 8 8 8"/></svg>';

function statusCancelBtn(id) {
  return `<button type="button" class="status-cancel-corner" data-cancel-id="${id}" title="Скасувати" aria-label="Скасувати">${STATUS_CANCEL_ICON}</button>`;
}

function triggerAutoDownload(id) {
  const a = document.createElement("a");
  a.href = `/api/convert/file/${id}`;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// Shared with app.js/processes.js: whichever poller (this page's own, or
// the global processes tray on any page) notices a job finish first claims
// it, so the same file doesn't get auto-downloaded twice.
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
    return true;
  }
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

async function submitFromDownload(downloadId) {
  const fd = new FormData(form);
  statusBox.innerHTML = `<div class="card status-card">
    <p>Готуємо файл із завантажень...</p>
    <div class="progress"><div class="progress-bar indeterminate"></div></div>
  </div>`;
  try {
    const res = await fetch(`/api/convert/from-download/${downloadId}`, { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok || data.error) {
      statusBox.innerHTML = `<div class="card status-card"><p class="error">${data.error || "Помилка"}</p></div>`;
      return;
    }
    pollConvertStatus(data.id, data.duration_seconds, data.input_summary);
    document.dispatchEvent(new CustomEvent("obelisk:jobs-changed"));
  } catch (err) {
    statusBox.innerHTML = `<div class="card status-card"><p class="error">Помилка з'єднання</p></div>`;
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (sourceDownloadId) {
    submitFromDownload(sourceDownloadId);
    return;
  }
  if (!fileInput.files[0]) return;

  const fd = new FormData(form);
  statusBox.innerHTML = `<div class="card status-card">
    <p id="upload-label">Завантаження файлу на сервер... (0%)</p>
    <div class="progress"><div class="progress-bar" id="upload-bar" style="width:0%"></div></div>
  </div>`;

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/convert");
  xhr.upload.addEventListener("progress", (ev) => {
    if (!ev.lengthComputable) return;
    const pct = Math.round((ev.loaded / ev.total) * 100);
    const bar = document.getElementById("upload-bar");
    const label = document.getElementById("upload-label");
    if (bar) bar.style.width = pct + "%";
    if (label) label.textContent = `Завантаження файлу на сервер... (${pct}%)`;
  });
  xhr.onload = () => {
    let data = null;
    try {
      data = JSON.parse(xhr.responseText);
    } catch (err) {
      data = null;
    }
    if (xhr.status >= 400 || !data || data.error) {
      statusBox.innerHTML = `<div class="card status-card"><p class="error">${(data && data.error) || "Помилка завантаження файлу"}</p></div>`;
      return;
    }
    pollConvertStatus(data.id, data.duration_seconds, data.input_summary);
    document.dispatchEvent(new CustomEvent("obelisk:jobs-changed"));
  };
  xhr.onerror = () => {
    statusBox.innerHTML = `<div class="card status-card"><p class="error">Помилка з'єднання</p></div>`;
  };
  xhr.send(fd);
});

function pollConvertStatus(id, durationSeconds, inputSummary) {
  const isIndeterminate = !durationSeconds;
  const summaryLine = inputSummary ? `<p class="hint">${escapeHtml(inputSummary)}</p>` : "";
  const interval = setInterval(async () => {
    try {
      const res = await fetch(`/api/convert/status/${id}`);
      const job = await res.json();
      if (job.status === "finished") {
        const size = formatSize(job.filesize);
        if (claimAutoDownload("conversion:" + id)) triggerAutoDownload(id);
        statusBox.innerHTML = `<div class="card status-card">
          <p class="success">✓ Готово${size ? ` (${size})` : ""}</p>
          ${summaryLine}
          <a class="btn-download" href="/api/convert/file/${id}">${DOWNLOAD_ICON} Завантажити ще раз</a>
        </div>`;
        clearInterval(interval);
        refreshRecent();
        document.dispatchEvent(new CustomEvent("obelisk:jobs-changed"));
      } else if (job.status === "error") {
        statusBox.innerHTML = `<div class="card status-card"><p class="error">Помилка конвертації: ${escapeHtml(job.error || "невідома помилка")}</p></div>`;
        clearInterval(interval);
        refreshRecent();
        document.dispatchEvent(new CustomEvent("obelisk:jobs-changed"));
      } else if (job.status === "cancelled") {
        statusBox.innerHTML = `<div class="card status-card"><p>Конвертацію скасовано.</p></div>`;
        clearInterval(interval);
        refreshRecent();
        document.dispatchEvent(new CustomEvent("obelisk:jobs-changed"));
      } else if (isIndeterminate) {
        statusBox.innerHTML = `<div class="card status-card">
          ${statusCancelBtn(id)}
          <p>Статус: ${STATUS_LABELS[job.status] || job.status}...</p>
          ${summaryLine}
          <div class="progress"><div class="progress-bar indeterminate"></div></div>
        </div>`;
      } else {
        const progress = job.progress || 0;
        const eta = formatEta(job.eta_seconds);
        statusBox.innerHTML = `<div class="card status-card">
          ${statusCancelBtn(id)}
          <p>Статус: ${STATUS_LABELS[job.status] || job.status} (${progress}%)${eta ? ` — ${eta} до завершення` : ""}</p>
          ${summaryLine}
          <div class="progress"><div class="progress-bar" style="width:${progress}%"></div></div>
        </div>`;
      }
    } catch (err) {
      clearInterval(interval);
    }
  }, 1200);
}

const CANCELLABLE_STATUSES = { queued: true, converting: true };

function statusIcon(status) {
  if (status === "finished") return STATUS_ICON_FINISHED;
  if (status === "error") return STATUS_ICON_ERROR;
  if (status === "expired") return STATUS_ICON_EXPIRED;
  if (status === "cancelled") return STATUS_ICON_CANCELLED;
  if (status === "converting") return '<span class="spinner"></span>';
  if (status === "queued") return "⏳";
  return "–";
}

const CANCEL_ICON = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';

function renderRow(r) {
  const btn =
    r.status === "finished"
      ? `<a class="dl-download-btn" href="/api/convert/file/${r.id}" title="Завантажити">${DOWNLOAD_ICON}</a>`
      : `<span class="dl-download-btn disabled" title="Ще не готово">${DOWNLOAD_ICON}</span>`;
  const size = formatSize(r.filesize);
  const title = size ? `${r.title} (${size})` : r.title;
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
    const res = await fetch(`/api/convert/recent?page=${recentPage}`);
    const data = await res.json();
    const list = document.getElementById("recent-list");
    const paginationBox = document.getElementById("recent-pagination");
    if (!list) return;
    recentPage = data.page;
    if (!data.items.length) {
      list.innerHTML = `<p class="empty-hint">Ще немає конвертацій</p>`;
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
    await fetch(`/api/convert/cancel/${cancelBtn.dataset.cancelId}`, { method: "POST" });
  } catch (err) {
    // ignore — the row will just keep showing its old state until the next poll
  }
  refreshRecent();
});

document.addEventListener("click", async (e) => {
  const cancelBtn = e.target.closest(".status-cancel-corner");
  if (!cancelBtn) return;
  cancelBtn.disabled = true;
  try {
    await fetch(`/api/convert/cancel/${cancelBtn.dataset.cancelId}`, { method: "POST" });
  } catch (err) {
    // ignore — the next poll tick will just show whatever state actually stuck
  }
});
