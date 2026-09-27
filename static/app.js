// Countdown timers — server provides UTC ISO end times in data-ends.
function pad(n) { return String(n).padStart(2, "0"); }

function tick() {
  const now = Date.now();
  document.querySelectorAll("[data-countdown]").forEach(el => {
    const ends = Date.parse(el.dataset.ends);
    if (isNaN(ends)) { el.textContent = ""; return; }
    let diff = Math.max(0, Math.floor((ends - now) / 1000));
    const d = Math.floor(diff / 86400);
    const h = Math.floor((diff % 86400) / 3600);
    const m = Math.floor((diff % 3600) / 60);
    const s = diff % 60;
    let text;
    if (d > 0) text = `${d}d ${pad(h)}h ${pad(m)}m`;
    else if (h > 0) text = `${pad(h)}:${pad(m)}:${pad(s)}`;
    else text = `${pad(m)}:${pad(s)}`;
    if (diff === 0) {
      el.textContent = "Ended";
    } else {
      // Keep any prefix the template put before the timer text.
      el.textContent = (el.dataset.prefix || "") + text;
    }
  });
}

// data-prefix lets templates write e.g. "Ends in " without JS clobbering it.
document.querySelectorAll("[data-countdown]").forEach(el => {
  const raw = el.textContent.trim();
  if (raw && raw !== "…" && !/^\d/.test(raw)) el.dataset.prefix = raw + " ";
});

tick();
setInterval(tick, 1000);
