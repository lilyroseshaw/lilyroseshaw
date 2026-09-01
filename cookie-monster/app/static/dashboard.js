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

// "Delete my data" confirmation modal. The button never submits anything by
// itself - it only opens this modal, populated from the button's own data-*
// attributes. The actual POST only happens if the user clicks "Continue
// with deletion" inside the modal.
(function () {
  const modal = document.getElementById("deletion-modal");
  if (!modal) return;

  const titleEl = document.getElementById("deletion-modal-title");
  const actionEl = document.getElementById("deletion-modal-action");
  const detailsEl = document.getElementById("deletion-modal-details");
  const form = document.getElementById("deletion-modal-form");
  const cancelBtn = document.getElementById("deletion-modal-cancel");

  function openModal(btn) {
    const name = btn.dataset.name || "this company";
    titleEl.textContent = "Delete my data — " + name;
    actionEl.textContent = btn.dataset.action || "";
    detailsEl.textContent = btn.dataset.details || "";
    detailsEl.hidden = !btn.dataset.details;
    form.action = "/api/companies/" + btn.dataset.id + "/deletion/execute";
    modal.hidden = false;
  }

  function closeModal() {
    modal.hidden = true;
  }

  document.querySelectorAll(".delete-my-data-btn").forEach((btn) => {
    btn.addEventListener("click", () => openModal(btn));
  });

  cancelBtn.addEventListener("click", closeModal);
  modal.addEventListener("click", (event) => {
    if (event.target === modal) closeModal();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !modal.hidden) closeModal();
  });
})();
