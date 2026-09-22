/* Auto-refresh: ask the server every few seconds whether anything on this
   page changed, and reload only when it has — never while someone is typing. */
(function () {
  var body = document.body;
  var url = body.dataset.fingerprintUrl;
  var current = body.dataset.fingerprint;
  var seconds = parseInt(body.dataset.refreshSeconds || "0", 10);
  var note = document.getElementById("refresh-note");
  var scrollKey = "pft-scroll:" + location.pathname;

  // A reload should land where you were, not at the top.
  try {
    var saved = sessionStorage.getItem(scrollKey);
    if (saved !== null) {
      sessionStorage.removeItem(scrollKey);
      window.scrollTo(0, parseInt(saved, 10) || 0);
    }
  } catch (e) { /* storage unavailable: start at the top */ }

  // One-off messages ("Tracking KL1705 …") live in the URL; drop them so a
  // refresh, automatic or manual, doesn't show them again.
  if (location.search.indexOf("msg=") !== -1 && window.history.replaceState) {
    var params = new URLSearchParams(location.search);
    params.delete("msg");
    params.delete("level");
    var rest = params.toString();
    history.replaceState(null, "", location.pathname + (rest ? "?" + rest : "") + location.hash);
  }

  if (!url || !current || !(seconds > 0)) return;

  function setNote(text) { if (note) note.textContent = text; }
  function clock() {
    var d = new Date();
    return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
  }

  function isTyping() {
    var active = document.activeElement;
    if (active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName)) return true;
    // Unsubmitted text in any form also counts, even after clicking away.
    var fields = document.querySelectorAll("form input, form textarea");
    for (var i = 0; i < fields.length; i++) {
      var f = fields[i];
      if (f.type === "hidden" || f.type === "submit") continue;
      if (f.value !== f.defaultValue) return true;
    }
    return false;
  }

  var pending = false;

  function reload() {
    try { sessionStorage.setItem(scrollKey, String(window.scrollY || 0)); } catch (e) {}
    location.reload();
  }

  function check() {
    if (document.hidden) return;
    fetch(url, { cache: "no-store", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !data.fingerprint) return;
        if (data.fingerprint === "gone") { location.href = "/"; return; }
        if (data.fingerprint === current && !pending) {
          setNote("Up to date · checked " + clock());
          return;
        }
        pending = true;
        if (isTyping()) {
          setNote("New updates — waiting until you finish typing");
          return;
        }
        reload();
      })
      .catch(function () { setNote("Can't reach the tracker — will retry"); });
  }

  // If an update arrived while you were typing, apply it once you stop.
  document.addEventListener("focusout", function () {
    if (pending) setTimeout(function () { if (!isTyping()) reload(); }, 400);
  });
  document.addEventListener("submit", function () { pending = false; });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) check(); });

  setNote("Auto-refresh on");
  setInterval(check, seconds * 1000);
})();
