// Small progressive-enhancement script for the two-checkbox merge picker.
// Everything else on this page works via plain HTML forms (no JS required).
(function () {
  const checkboxes = Array.from(document.querySelectorAll(".merge-pick"));
  const keepInput = document.getElementById("merge-keep-id");
  const otherInput = document.getElementById("merge-other-id");
  const submitBtn = document.getElementById("merge-submit");

  function refresh() {
    const checked = checkboxes.filter((cb) => cb.checked);
    checkboxes.forEach((cb) => {
      cb.disabled = checked.length >= 2 && !cb.checked;
    });
    if (checked.length === 2) {
      keepInput.value = checked[0].value;
      otherInput.value = checked[1].value;
      submitBtn.disabled = false;
    } else {
      submitBtn.disabled = true;
    }
  }

  checkboxes.forEach((cb) => cb.addEventListener("change", refresh));
  refresh();
})();
