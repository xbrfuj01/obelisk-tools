(function () {
  var wrap = document.getElementById("processes-wrap");
  var btn = document.getElementById("processes-btn");
  var badge = document.getElementById("processes-badge");
  var panel = document.getElementById("processes-panel");
  var list = document.getElementById("processes-list");
  if (!wrap || !btn || !panel || !list) return;

  var ACTIVE_STATUSES = { queued: true, downloading: true, converting: true };
  var POLL_OPEN_MS = 2000;
  var POLL_CLOSED_MS = 8000;
  var pollTimer = null;

  var DOWNLOAD_ICON = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>';
  var CANCEL_ICON = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';
  // Same icon set as admin.html's status_badge macro / STATUS_ICONS, so a
  // given status looks identical whether it's rendered by the server
  // (admin) or here on the client.
  var STATUS_ICON_FINISHED = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
  var STATUS_ICON_ERROR = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';
  var STATUS_ICON_EXPIRED = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2h4"/><path d="M12 14v-4"/><circle cx="12" cy="14" r="8"/></svg>';
  var STATUS_ICON_CANCELLED = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m8 8 8 8"/></svg>';

  // Shared with app.js/converter.js: whichever poller (this global tray, or
  // the originating tool page's own poller) notices a job finish first
  // claims it, so the browser download doesn't fire twice for one job.
  var AUTO_DOWNLOAD_STORAGE_KEY = "obelisk_auto_downloaded";
  function claimAutoDownload(key) {
    try {
      var done = JSON.parse(localStorage.getItem(AUTO_DOWNLOAD_STORAGE_KEY) || "[]");
      if (done.indexOf(key) !== -1) return false;
      done.push(key);
      if (done.length > 200) done.splice(0, done.length - 200);
      localStorage.setItem(AUTO_DOWNLOAD_STORAGE_KEY, JSON.stringify(done));
      return true;
    } catch (err) {
      return true;
    }
  }

  function triggerAutoDownload(url) {
    var a = document.createElement("a");
    a.href = url;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // This is the point of the whole tray: a job started on the downloader/
  // converter page keeps running server-side even after you navigate away
  // (or the page reloads) - without this, nothing would ever pick the
  // finished file back up and send it to your computer.
  function autoDownloadFinished(items) {
    items.forEach(function (item) {
      if (item.status !== "finished") return;
      if (item.kind === "download") {
        if (item.auto_convert_id) return; // the real deliverable is the conversion below, not this raw file
        if (claimAutoDownload("download:" + item.id)) triggerAutoDownload("/api/file/" + item.id);
      } else {
        if (claimAutoDownload("conversion:" + item.id)) triggerAutoDownload("/api/convert/file/" + item.id);
      }
    });
  }

  function escapeHtml(str) {
    var div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
  }

  function formatSize(bytes) {
    if (!bytes) return "";
    var mb = bytes / 1048576;
    if (mb >= 1024) return (mb / 1024).toFixed(1) + " ГБ";
    if (mb < 1) return mb.toFixed(1) + " МБ";
    return Math.round(mb) + " МБ";
  }

  function formatEta(seconds) {
    if (seconds == null || seconds < 0) return "";
    seconds = Math.round(seconds);
    if (seconds < 60) return "≈" + seconds + " с";
    var totalMinutes = Math.floor(seconds / 60);
    var s = seconds % 60;
    if (totalMinutes < 60) return "≈" + totalMinutes + " хв" + (s ? " " + s + " с" : "");
    var h = Math.floor(totalMinutes / 60);
    var m = totalMinutes % 60;
    return "≈" + h + " год" + (m ? " " + m + " хв" : "");
  }

  function statusSymbol(status) {
    if (status === "finished") return STATUS_ICON_FINISHED;
    if (status === "error") return STATUS_ICON_ERROR;
    if (status === "expired") return STATUS_ICON_EXPIRED;
    if (status === "cancelled") return STATUS_ICON_CANCELLED;
    if (status === "downloading" || status === "converting") return '<span class="spinner"></span>';
    if (status === "queued") return "⏳";
    return "–";
  }

  function renderRow(item) {
    var isActive = !!ACTIVE_STATUSES[item.status];
    var isAutoConverting = item.kind === "download" && !!item.auto_convert_id;
    var fileUrl = item.kind === "download" ? "/api/file/" + item.id : "/api/convert/file/" + item.id;
    var metaBits = [];
    if (item.status === "queued") metaBits.push("У черзі");
    else if (isActive && item.eta_seconds != null) metaBits.push(formatEta(item.eta_seconds) + " до завершення");
    if (item.status === "finished" && isAutoConverting) metaBits.push("Конвертується для сумісності з відеоредакторами");
    else if (item.status === "finished" && item.filesize) metaBits.push(formatSize(item.filesize));
    if (item.status === "error") metaBits.push("Помилка");
    if (item.status === "cancelled") metaBits.push("Скасовано");

    var action = "";
    if (item.status === "finished" && !isAutoConverting) {
      action = '<a href="' + fileUrl + '" class="processes-row-link" title="Завантажити файл" aria-label="Завантажити файл">' + DOWNLOAD_ICON + '</a>';
    } else if (isActive) {
      action = '<button type="button" class="processes-row-link cancel" data-cancel-kind="' + item.kind + '" data-cancel-id="' + item.id + '" title="Скасувати" aria-label="Скасувати">' + CANCEL_ICON + '</button>';
    }
    var progressBar = isActive
      ? '<div class="progress"><div class="progress-bar' + (item.progress ? '' : ' indeterminate') + '" style="width:' + (item.progress || 0) + '%"></div></div>'
      : "";

    return '<div class="processes-row">' +
      '<div class="processes-row-top">' +
        '<span class="status-icon status-' + item.status + '">' + statusSymbol(item.status) + '</span>' +
        '<span class="processes-row-title" title="' + escapeHtml(item.title) + '">' + escapeHtml(item.title) + '</span>' +
        action +
      '</div>' +
      (metaBits.length ? '<span class="processes-row-meta">' + escapeHtml(metaBits.join(" · ")) + '</span>' : "") +
      progressBar +
      '</div>';
  }

  function render(items) {
    if (!items.length) {
      list.innerHTML = '<p class="empty-hint">Немає процесів</p>';
    } else {
      list.innerHTML = items.map(renderRow).join("");
    }
    var activeCount = items.filter(function (it) { return ACTIVE_STATUSES[it.status]; }).length;
    if (activeCount > 0) {
      badge.textContent = activeCount > 99 ? "99+" : String(activeCount);
      badge.hidden = false;
    } else {
      badge.hidden = true;
    }
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(refresh, panel.hidden ? POLL_CLOSED_MS : POLL_OPEN_MS);
  }

  async function refresh() {
    try {
      const res = await fetch("/api/processes");
      if (res.status === 401 || res.status === 403) {
        wrap.hidden = true;
        return;
      }
      if (!res.ok) {
        schedulePoll();
        return;
      }
      wrap.hidden = false;
      const items = await res.json();
      autoDownloadFinished(items);
      render(items);
    } catch (err) {
      // ignore — keep showing the last known state, try again next tick
    } finally {
      schedulePoll();
    }
  }

  function closePanel() {
    panel.hidden = true;
  }

  btn.addEventListener("click", function () {
    var willOpen = panel.hidden;
    panel.hidden = !willOpen;
    if (willOpen) {
      refresh();
    }
  });

  document.addEventListener("click", function (e) {
    if (!panel.hidden && !wrap.contains(e.target)) closePanel();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !panel.hidden) closePanel();
  });

  // Fired by app.js/converter.js right when a job is created AND right
  // when one of their own active polls notices it reach a terminal state
  // (finished/error/cancelled) - so the badge count updates the instant
  // a task starts or ends on this tab, instead of waiting for the next
  // scheduled poll tick here (which can be several seconds away if the
  // panel is closed).
  document.addEventListener("obelisk:jobs-changed", refresh);

  // Browsers throttle background-tab timers (sometimes to once a minute or
  // less), so a job that finishes while the tab isn't focused can leave the
  // badge stuck on a stale count for a while - refreshing immediately on
  // return fixes that without waiting for reload.
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refresh();
  });
  window.addEventListener("focus", refresh);

  list.addEventListener("click", async function (e) {
    var cancelBtn = e.target.closest(".processes-row-link.cancel");
    if (!cancelBtn) return;
    e.stopPropagation();
    cancelBtn.disabled = true;
    var url = cancelBtn.dataset.cancelKind === "download"
      ? "/api/cancel/" + cancelBtn.dataset.cancelId
      : "/api/convert/cancel/" + cancelBtn.dataset.cancelId;
    try {
      await fetch(url, { method: "POST" });
    } catch (err) {
      // ignore — refresh() below will just show whatever state actually stuck
    }
    refresh();
  });

  refresh();
})();

// ---------------- Admin: processes of every user ----------------
// Separate button+panel (only rendered in the DOM at all for admins) - a
// site-wide read-only view, not "my" tray, so it skips cancel buttons and
// auto-download entirely and just shows who's running what.
(function () {
  var wrap = document.getElementById("admin-processes-wrap");
  if (!wrap) return;
  var btn = document.getElementById("admin-processes-btn");
  var badge = document.getElementById("admin-processes-badge");
  var panel = document.getElementById("admin-processes-panel");
  var list = document.getElementById("admin-processes-list");
  if (!btn || !badge || !panel || !list) return;

  var ACTIVE_STATUSES = { queued: true, downloading: true, converting: true };
  var POLL_OPEN_MS = 3000;
  var POLL_CLOSED_MS = 15000;
  var pollTimer = null;

  // Same icon set as admin.html's status_badge macro / STATUS_ICONS.
  var STATUS_ICON_FINISHED = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
  var STATUS_ICON_ERROR = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';
  var STATUS_ICON_EXPIRED = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2h4"/><path d="M12 14v-4"/><circle cx="12" cy="14" r="8"/></svg>';
  var STATUS_ICON_CANCELLED = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m8 8 8 8"/></svg>';

  function escapeHtml(str) {
    var div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
  }

  function formatEta(seconds) {
    if (seconds == null || seconds < 0) return "";
    seconds = Math.round(seconds);
    if (seconds < 60) return "≈" + seconds + " с";
    var totalMinutes = Math.floor(seconds / 60);
    var s = seconds % 60;
    if (totalMinutes < 60) return "≈" + totalMinutes + " хв" + (s ? " " + s + " с" : "");
    var h = Math.floor(totalMinutes / 60);
    var m = totalMinutes % 60;
    return "≈" + h + " год" + (m ? " " + m + " хв" : "");
  }

  function statusSymbol(status) {
    if (status === "finished") return STATUS_ICON_FINISHED;
    if (status === "error") return STATUS_ICON_ERROR;
    if (status === "expired") return STATUS_ICON_EXPIRED;
    if (status === "cancelled") return STATUS_ICON_CANCELLED;
    if (status === "downloading" || status === "converting") return '<span class="spinner"></span>';
    if (status === "queued") return "⏳";
    return "–";
  }

  function renderRow(item) {
    var isActive = !!ACTIVE_STATUSES[item.status];
    var kindLabel = item.kind === "download" ? "Завантаження" : "Конвертація";
    var metaBits = [kindLabel];
    if (item.status === "queued") metaBits.push("У черзі");
    else if (isActive && item.eta_seconds != null) metaBits.push(formatEta(item.eta_seconds) + " до завершення");
    if (item.status === "error") metaBits.push("Помилка");
    if (item.status === "cancelled") metaBits.push("Скасовано");
    var progressBar = isActive
      ? '<div class="progress"><div class="progress-bar' + (item.progress ? '' : ' indeterminate') + '" style="width:' + (item.progress || 0) + '%"></div></div>'
      : "";

    return '<div class="processes-row">' +
      '<div class="processes-row-top">' +
        '<span class="status-icon status-' + item.status + '">' + statusSymbol(item.status) + '</span>' +
        '<span class="processes-row-title"><b>' + escapeHtml(item.username) + '</b> — ' + escapeHtml(item.title) + '</span>' +
      '</div>' +
      '<span class="processes-row-meta">' + escapeHtml(metaBits.join(" · ")) + '</span>' +
      progressBar +
      '</div>';
  }

  function render(items) {
    if (!items.length) {
      list.innerHTML = '<p class="empty-hint">Немає процесів</p>';
    } else {
      list.innerHTML = items.map(renderRow).join("");
    }
    var activeCount = items.filter(function (it) { return ACTIVE_STATUSES[it.status]; }).length;
    if (activeCount > 0) {
      badge.textContent = activeCount > 99 ? "99+" : String(activeCount);
      badge.hidden = false;
    } else {
      badge.hidden = true;
    }
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(refresh, panel.hidden ? POLL_CLOSED_MS : POLL_OPEN_MS);
  }

  async function refresh() {
    try {
      const res = await fetch("/admin/api/processes");
      if (res.ok) render(await res.json());
    } catch (err) {
      // ignore — keep showing the last known state, try again next tick
    } finally {
      schedulePoll();
    }
  }

  function closePanel() {
    panel.hidden = true;
  }

  btn.addEventListener("click", function () {
    var willOpen = panel.hidden;
    panel.hidden = !willOpen;
    if (willOpen) refresh();
  });

  document.addEventListener("click", function (e) {
    if (!panel.hidden && !wrap.contains(e.target)) closePanel();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !panel.hidden) closePanel();
  });

  // Fired by app.js/converter.js right when a job is created AND right
  // when one of their own active polls notices it reach a terminal state -
  // an admin watching this site-wide tray (on this same tab) sees both
  // happen immediately instead of waiting for this tray's own poll tick.
  document.addEventListener("obelisk:jobs-changed", refresh);

  // Same reasoning as the personal tray above - don't leave the badge
  // showing a stale count just because the tab was backgrounded.
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refresh();
  });
  window.addEventListener("focus", refresh);

  refresh();
})();
