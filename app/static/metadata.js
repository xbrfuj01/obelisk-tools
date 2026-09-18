const form = document.getElementById("metadata-form");
const statusBox = document.getElementById("status-box");
const resultBox = document.getElementById("result-box");
const fileInput = document.getElementById("file-input");
const dropZone = document.getElementById("file-drop-zone");
const fileDropText = document.getElementById("file-drop-text");
const fileClearBtn = document.getElementById("file-clear-btn");
const DEFAULT_DROP_TEXT = "Натисніть щоб завантажити або перетягніть сюди";

dropZone.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    fileInput.click();
  }
});
fileClearBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  fileInput.value = "";
  fileInput.dispatchEvent(new Event("change"));
});
fileInput.addEventListener("change", () => {
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

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

const DOWNLOAD_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>';

const STATUS_ORDER = { removable: 0, protected_ai: 1, protected: 2, absent: 3 };
const STATUS_CLASS = { removable: "removable", protected_ai: "protected-ai", protected: "protected", absent: "empty" };

// Some tags (QuickTime:MediaData and similar raw dumps) can carry
// megabytes of text in a single value, which would otherwise blow the
// table row up to the same height - collapse anything past this length
// behind a toggle instead.
const LONG_VALUE_THRESHOLD = 300;

function renderValueCell(value) {
  const text = escapeHtml(value);
  if (String(value).length <= LONG_VALUE_THRESHOLD) return text;
  return `<div class="metadata-value-wrap">
    <div class="metadata-value-collapsed">${text}</div>
    <button type="button" class="metadata-value-toggle">Показати більше</button>
  </div>`;
}

function renderResult(data) {
  const entries = Object.entries(data.metadata || {}).map(([key, field]) => {
    const [group, ...rest] = key.split(":");
    const tag = rest.length ? rest.join(":") : group;
    const category = rest.length ? group : "";
    return { category, tag, value: field.value, status: field.status };
  });
  entries.sort(
    (a, b) =>
      STATUS_ORDER[a.status] - STATUS_ORDER[b.status] ||
      a.category.localeCompare(b.category) ||
      a.tag.localeCompare(b.tag)
  );

  const rows = entries
    .map((e) => {
      const display = e.status === "absent" ? "—" : renderValueCell(e.value);
      return `<tr><td>${escapeHtml(e.category)}</td><td>${escapeHtml(e.tag)}</td><td class="${STATUS_CLASS[e.status]}">${display}</td></tr>`;
    })
    .join("");

  const table = entries.length
    ? `<div class="table-wrap card">
        <table class="table metadata-table">
          <thead><tr><th>Категорія</th><th>Тег</th><th>Значення</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <p class="hint">Червоним — дані, які буде видалено. Жовтим — мітки від AI-інструментів (C2PA тощо), які видалити неможливо. Зеленим — інші дані, які видалити неможливо. Сірим — поле відсутнє у файлі.</p>`
    : `<p class="hint">Метаданих не знайдено — файл і так чистий.</p>`;

  const unverifiedWarning = data.verified
    ? ""
    : `<p class="error">⚠ Не вдалося перевірити результат очищення файлу — кольори нижче можуть бути неточними.</p>`;

  resultBox.innerHTML = `
    <div class="card status-card">
      <p class="success">✓ Знайдено ${data.found_count} значень. ${data.removable_count} значень готові до видалення.</p>
      ${unverifiedWarning}
      <a class="btn-download" id="metadata-download-btn" href="/api/metadata/download/${data.token}">${DOWNLOAD_ICON} Завантажити файл без метаданих</a>
      <p class="hint">Файл зберігається на сервері лише до першого завантаження.</p>
    </div>
    ${table}
  `;

  // The server deletes the cleaned file right after it's served once (no
  // history is kept for this tool) - disable the link after the first
  // click so a second click doesn't just hit a 404.
  const btn = document.getElementById("metadata-download-btn");
  if (btn) {
    btn.addEventListener("click", () => {
      setTimeout(() => {
        btn.style.pointerEvents = "none";
        btn.style.opacity = "0.6";
      }, 50);
    });
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (!fileInput.files[0]) return;

  resultBox.innerHTML = "";
  const fd = new FormData(form);
  statusBox.innerHTML = `<div class="card status-card">
    <p id="upload-label">Завантаження файлу на сервер... (0%)</p>
    <div class="progress"><div class="progress-bar" id="upload-bar" style="width:0%"></div></div>
  </div>`;

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/metadata/process");
  xhr.upload.addEventListener("progress", (ev) => {
    if (!ev.lengthComputable) return;
    const pct = Math.round((ev.loaded / ev.total) * 100);
    const bar = document.getElementById("upload-bar");
    const label = document.getElementById("upload-label");
    if (bar) bar.style.width = pct + "%";
    if (label) {
      label.textContent =
        pct < 100
          ? `Завантаження файлу на сервер... (${pct}%)`
          : "Читаємо метадані...";
    }
  });
  xhr.onload = () => {
    let data = null;
    try {
      data = JSON.parse(xhr.responseText);
    } catch (err) {
      data = null;
    }
    if (xhr.status >= 400 || !data || data.error) {
      statusBox.innerHTML = `<div class="card status-card"><p class="error">${escapeHtml((data && data.error) || "Помилка обробки файлу")}</p></div>`;
      return;
    }
    statusBox.innerHTML = "";
    renderResult(data);
  };
  xhr.onerror = () => {
    statusBox.innerHTML = `<div class="card status-card"><p class="error">Помилка з'єднання</p></div>`;
  };
  xhr.send(fd);
});

document.addEventListener("click", (e) => {
  const toggle = e.target.closest(".metadata-value-toggle");
  if (!toggle) return;
  const collapsed = toggle.previousElementSibling;
  const expanded = collapsed.classList.toggle("expanded");
  toggle.textContent = expanded ? "Згорнути" : "Показати більше";
});
