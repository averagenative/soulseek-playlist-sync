// My music: the bar for adding ticked songs to a playlist.
(function () {
  const form = document.getElementById("pick");
  const bar = document.getElementById("pick-bar");
  if (!form || !bar) return;
  const boxes = () => Array.from(form.querySelectorAll('input[name="key"]'));
  const all = document.getElementById("pick-all");
  const to = document.getElementById("pick-to");
  const name = document.getElementById("pick-name");

  function update() {
    const n = boxes().filter(b => b.checked).length;
    document.getElementById("pick-n").textContent = n;
    bar.hidden = n === 0;
    if (all) {
      all.checked = n > 0 && n === boxes().length;
      all.indeterminate = n > 0 && n < boxes().length;
    }
    name.hidden = !!to.value;
  }
  form.addEventListener("change", (e) => {
    if (e.target === all) boxes().forEach(b => { b.checked = all.checked; });
    update();
  });
  // Enter in the name box adds the ticked songs (the form's first button would make a playlist of everything).
  name.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); bar.querySelector('button[value="selected"]').click(); }
  });
  document.getElementById("pick-clear").addEventListener("click", () => {
    boxes().forEach(b => { b.checked = false; });
    update();
  });
  update();
})();
