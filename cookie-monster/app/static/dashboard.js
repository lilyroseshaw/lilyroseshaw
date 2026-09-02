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
// attributes (execution_capability/reason/consequences - all computed
// server-side by deletion_engine.classify_execution_capability, the SAME
// function the execute endpoint itself uses, so this modal can never
// promise something execution won't actually do). The actual POST only
// happens if the user clicks the approval button inside the modal.
(function () {
  const modal = document.getElementById("deletion-modal");
  if (!modal) return;

  const titleEl = document.getElementById("deletion-modal-title");
  const actionEl = document.getElementById("deletion-modal-action");
  const detailsEl = document.getElementById("deletion-modal-details");
  const consequencesEl = document.getElementById("deletion-modal-consequences");
  const consequencesTextEl = document.getElementById("deletion-modal-consequences-text");
  const userStepEl = document.getElementById("deletion-modal-user-step");
  const userStepReasonEl = document.getElementById("deletion-modal-user-step-reason");
  const emailPreviewEl = document.getElementById("deletion-modal-email");
  const emailToEl = document.getElementById("deletion-modal-email-to");
  const emailSubjectEl = document.getElementById("deletion-modal-email-subject");
  const emailBodyEl = document.getElementById("deletion-modal-email-body");
  const form = document.getElementById("deletion-modal-form");
  const cancelBtn = document.getElementById("deletion-modal-cancel");
  const submitBtn = document.getElementById("deletion-modal-submit");

  const SUBMIT_LABEL = {
    AUTO_EXECUTABLE: "Send this email",
    USER_STEP_REQUIRED: "Continue - I'll finish this myself",
    MANUAL_HANDOFF: "Open the verified page",
  };

  function applyCapability(capability, reason) {
    submitBtn.textContent = SUBMIT_LABEL[capability] || "Continue with deletion";
    const showUserStep = capability && capability !== "AUTO_EXECUTABLE" && reason;
    userStepReasonEl.textContent = reason || "";
    userStepEl.hidden = !showUserStep;
  }

  function openModal(btn) {
    const name = btn.dataset.name || "this company";
    titleEl.textContent = "Delete my data — " + name;
    actionEl.textContent = btn.dataset.action || "";
    detailsEl.textContent = btn.dataset.details || "";
    detailsEl.hidden = !btn.dataset.details;
    consequencesTextEl.textContent = btn.dataset.consequences || "";
    consequencesEl.hidden = !btn.dataset.consequences;
    applyCapability(btn.dataset.capability, btn.dataset.capabilityReason);
    form.action = "/api/companies/" + btn.dataset.id + "/deletion/execute";
    submitBtn.disabled = false;
    emailPreviewEl.hidden = true;
    modal.hidden = false;

    // Re-fetches the full execution plan (not just the email fields) so
    // the modal reflects the CURRENT state even if it's changed since this
    // page loaded (e.g. automatic sending was just enabled in another
    // tab) - this is the same classify_execution_capability() call the
    // execute endpoint itself uses, so it can never disagree with what
    // actually happens on approval.
    fetch("/api/companies/" + btn.dataset.id + "/deletion/preview")
      .then((resp) => (resp.ok ? resp.json() : null))
      .then((plan) => {
        if (!plan || modal.hidden) return;
        if (plan.capability) applyCapability(plan.capability, plan.reason);
        if (plan.consequences) {
          consequencesTextEl.textContent = plan.consequences;
          consequencesEl.hidden = false;
        }
        if (btn.dataset.method === "EMAIL_REQUEST" && plan.to) {
          emailToEl.textContent = plan.to || "";
          emailSubjectEl.textContent = plan.subject || "";
          emailBodyEl.textContent = plan.body || "";
          emailPreviewEl.hidden = false;
        }
      })
      .catch(() => {});
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
  // Belt-and-suspenders against a double-click sending two POSTs from the
  // SAME browser tab before the page navigates away - the authoritative
  // guard is server-side (deletion_engine's per-company in-flight lock),
  // this just avoids the wasted extra request in the common case.
  form.addEventListener("submit", () => {
    submitBtn.disabled = true;
  });
})();
