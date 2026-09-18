(function () {
  var modal = document.getElementById("notification-modal");
  var messageEl = document.getElementById("notification-modal-message");
  var okBtn = document.getElementById("notification-modal-ok");
  var closeBtn = document.getElementById("notification-modal-close");
  if (!modal || !messageEl || !okBtn || !closeBtn) return;

  // Rare, admin-triggered event - a modest poll interval is plenty
  // responsive without adding meaningful background traffic.
  var POLL_MS = 20000;
  var currentId = null;
  var pending = false;

  function escapeHtml(str) {
    var div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
  }

  async function checkForNotification() {
    if (pending || !modal.hidden) return; // one at a time - don't stack fetches or overwrite a shown message
    try {
      const res = await fetch("/api/notifications/next");
      if (!res.ok) return; // 401/403 (not logged in) or a transient error - just try again next tick
      const data = await res.json();
      if (data && data.id) {
        currentId = data.id;
        messageEl.innerHTML = escapeHtml(data.message).replace(/\n/g, "<br>");
        modal.hidden = false;
      }
    } catch (err) {
      // ignore - try again next tick
    }
  }

  async function dismiss() {
    if (modal.hidden || pending) return;
    modal.hidden = true;
    var id = currentId;
    currentId = null;
    if (!id) return;
    pending = true;
    try {
      await fetch("/api/notifications/" + id + "/dismiss", { method: "POST" });
    } catch (err) {
      // ignore - worst case the same message shows again next tick
    } finally {
      pending = false;
      checkForNotification(); // another one may already be queued
    }
  }

  okBtn.addEventListener("click", dismiss);
  closeBtn.addEventListener("click", dismiss);
  modal.addEventListener("click", function (e) {
    if (e.target === modal) dismiss();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !modal.hidden) dismiss();
  });

  checkForNotification();
  setInterval(checkForNotification, POLL_MS);
})();
