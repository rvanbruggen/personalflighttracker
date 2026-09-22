/* Show stored UTC moments in the viewer's own time zone and date format.
   The server renders <time class="local-time" datetime="...Z">13:49 UTC</time>
   so the page still reads correctly without JavaScript. */
(function () {
  var zone = "";
  try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) {}

  var format;
  try {
    format = new Intl.DateTimeFormat(undefined, {
      day: "numeric", month: "short", hour: "numeric", minute: "2-digit",
    });
  } catch (e) { return; }  // very old browser: keep the UTC fallback text

  var nodes = document.querySelectorAll("time.local-time[datetime]");
  for (var i = 0; i < nodes.length; i++) {
    var el = nodes[i];
    var when = new Date(el.getAttribute("datetime"));
    if (isNaN(when.getTime())) continue;
    var utcText = el.textContent;
    el.textContent = format.format(when);
    el.title = (zone ? zone + " · " : "") + utcText;
  }

  var note = document.getElementById("tz-note");
  if (note && zone && nodes.length) note.textContent = "Times in " + zone;
})();
