// Bottom player for the playlist page. Click a song's cover to play your downloaded copy
// (then it moves on to the next downloaded song); "Original" plays the streaming
// service's 30-second clip so you can compare.
(function () {
  const bar = document.getElementById("player");
  if (!bar) return;
  const audio = document.getElementById("pl-audio");
  const $ = (id) => document.getElementById(id);
  let current = null;   // {key, kind}

  function mark() {
    document.querySelectorAll("tr.playing").forEach(tr => tr.classList.remove("playing"));
    if (!current) return;
    const row = document.getElementById("song-" + current.key);
    if (row) row.classList.add("playing");
    document.querySelectorAll("[data-play]").forEach(b =>
      b.classList.toggle("is-playing", !!current && b.dataset.key === current.key && b.dataset.kind === current.kind && !audio.paused));
  }

  function play(btn) {
    const d = btn.dataset;
    if (current && current.key === d.key && current.kind === d.kind) {
      audio.paused ? audio.play() : audio.pause();
      return;
    }
    current = {key: d.key, kind: d.kind};
    audio.src = d.src;
    $("pl-title").textContent = d.title;
    $("pl-artist").textContent = d.artist;
    $("pl-kind").textContent = d.kind === "file" ? "Your copy" : "Original, 30 s";
    $("pl-art").src = d.art || "/static/icons/icon-192.png";
    bar.hidden = false;
    document.body.classList.add("has-player");
    audio.play().catch(() => {});
    mark();
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-play]");
    if (btn) { e.preventDefault(); play(btn); }
  });
  audio.addEventListener("play", mark);
  audio.addEventListener("pause", mark);
  audio.addEventListener("ended", () => {
    if (!current || current.kind !== "file") { mark(); return; }
    const files = Array.from(document.querySelectorAll('[data-play][data-kind="file"]'));
    const i = files.findIndex(b => b.dataset.key === current.key);
    if (i >= 0 && i + 1 < files.length) play(files[i + 1]); else mark();
  });
  $("pl-close").addEventListener("click", () => {
    audio.pause(); audio.removeAttribute("src"); current = null;
    bar.hidden = true; document.body.classList.remove("has-player"); mark();
  });
  document.addEventListener("live:updated", mark);   // the song list was refreshed
})();
