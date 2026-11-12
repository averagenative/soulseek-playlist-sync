// A My music playlist: select songs to take them out. The checkboxes sit in the live rows,
// so the bar is worked out again whenever they refresh.
(function () {
  const bar = document.getElementById("pl-bar");
  if (!bar) return;
  const boxes = () => Array.from(document.querySelectorAll('input[name="key"][form="pl-pick"]'));

  function update() {
    const n = boxes().filter(b => b.checked).length;
    document.getElementById("pl-n").textContent = n;
    bar.hidden = n === 0;
    const all = document.getElementById("pl-all");
    if (all) {
      all.checked = n > 0 && n === boxes().length;
      all.indeterminate = n > 0 && n < boxes().length;
    }
  }
  document.addEventListener("change", (e) => {
    if (e.target.id === "pl-all") boxes().forEach(b => { b.checked = e.target.checked; });
    if (e.target.id === "pl-all" || e.target.matches('input[name="key"][form="pl-pick"]')) update();
  });
  document.getElementById("pl-clear").addEventListener("click", () => {
    boxes().forEach(b => { b.checked = false; });
    update();
  });
  document.addEventListener("live:updated", update);
  update();
})();
