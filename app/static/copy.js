// Copy buttons: <button data-copy="text">. The clipboard API needs HTTPS or 127.0.0.1; elsewhere, fall back
// to selecting the text next to the button so the person can copy it themselves.
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const text = btn.dataset.copy;
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch {
    const code = btn.previousElementSibling;
    if (code) {
      const range = document.createRange();
      range.selectNodeContents(code);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      try { ok = document.execCommand("copy"); } catch { ok = false; }
    }
  }
  const label = btn.textContent;
  btn.textContent = ok ? "Copied" : "Selected: press Ctrl+C or Cmd+C";
  setTimeout(() => { btn.textContent = label; }, 1800);
});
