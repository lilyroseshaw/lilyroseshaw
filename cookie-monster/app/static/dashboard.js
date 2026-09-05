// Dashboard interactivity. Three concerns, in order below:
//   1. The two-checkbox merge picker.
//   2. Generic progressive-enhancement AJAX for card-level actions
//      (Research, Confirm/Reject/Reset, Check for responses, Save
//      correction, Mark completed, attach-thread, and the "Delete my
//      data" modal's own submit) - every one of these is a plain HTML
//      <form> first; JS only intercepts it to avoid a full-page reload.
//   3. The "Delete my data" confirmation modal itself.
//
// Every server route this page talks to ALREADY redirects (or renders)
// back to the exact same company's card - see main.py's
// _redirect_to_company_card(). The AJAX layer below never invents a
// result: it fetches that same server-rendered response and swaps in
// exactly the one <div id="company-{id}"> from it, verbatim. If anything
// goes wrong (network failure, a non-2xx response, any JS exception), the
// form is resubmitted as a real native POST - full page reload, the exact
// pre-AJAX behavior - so a failure can never look like nothing happened.

(function () {
  "use strict";

  // ---- 1. Merge picker ----

  function refreshMergePicker() {
    const checkboxes = Array.from(document.querySelectorAll(".merge-pick"));
    const keepInput = document.getElementById("merge-keep-id");
    const otherInput = document.getElementById("merge-other-id");
    const submitBtn = document.getElementById("merge-submit");
    if (!keepInput || !otherInput || !submitBtn) return;

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

  function wireMergeCheckboxes(scopeEl) {
    scopeEl.querySelectorAll(".merge-pick").forEach((cb) => {
      if (cb.dataset.wired) return;
      cb.dataset.wired = "1";
      cb.addEventListener("change", refreshMergePicker);
    });
  }

  // ---- 2. Generic card-level AJAX ----

  function findCard(el) {
    return el.closest(".company-card");
  }

  function swapCard(cardId, html) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    const newCard = doc.getElementById("company-" + cardId);
    const oldCard = document.getElementById("company-" + cardId);
    if (!newCard || !oldCard) return false;
    oldCard.replaceWith(newCard);
    wireCard(newCard);
    return true;
  }

  // Submits `form` via fetch instead of a real navigation. `cardId` is the
  // company whose card should be refreshed once the response comes back -
  // normally form.closest('.company-card'), but the "Delete my data"
  // modal's form lives OUTSIDE any card, so it passes its own tracked id
  // (see part 3 below) instead of relying on DOM position.
  function submitFormAjax(form, cardId, button, onSwapped) {
    const loadingText = form.dataset.loadingText || (button && button.dataset.loadingText);
    if (button) {
      button.disabled = true;
      if (loadingText) button.textContent = loadingText;
    }

    fetch(form.action, { method: "POST", body: new FormData(form), credentials: "same-origin" })
      .then((resp) => {
        if (!resp.ok) throw new Error("check-response/execute failed: " + resp.status);
        return resp.text();
      })
      .then((html) => {
        if (!cardId || !swapCard(cardId, html)) {
          window.location.reload();
          return;
        }
        if (onSwapped) onSwapped();
      })
      .catch(() => {
        // Never swallow a failure silently - fall back to a real submit,
        // which reproduces the exact pre-AJAX (server-authoritative)
        // behavior, including any error page.
        form.dataset.forceNative = "1";
        if (typeof form.requestSubmit === "function") {
          form.requestSubmit();
        } else {
          form.submit();
        }
      });
  }

  function wireCardForms(scopeEl) {
    scopeEl.querySelectorAll("form").forEach((form) => {
      if (form.method.toLowerCase() !== "post") return;
      if (form.classList.contains("check-response-form")) return; // handled by its own state machine, part 4 below - must never fall back to a native reload
      if (form.dataset.ajaxWired) return;
      form.dataset.ajaxWired = "1";
      form.addEventListener("submit", (event) => {
        if (form.dataset.forceNative) return; // the native-fallback resubmit above - let it through untouched
        const card = findCard(form);
        if (!card) return; // not a card-scoped form (merge/delete-all) - leave it as a normal submit
        event.preventDefault();
        const button = event.submitter || form.querySelector('button[type="submit"]');
        submitFormAjax(form, card.dataset.id, button);
      });
    });
  }

  function wireCard(cardEl) {
    wireCardForms(cardEl);
    wireMergeCheckboxes(cardEl);
    wireDeleteButtons(cardEl);
    wireCheckResponseForms(cardEl);
  }

  // ---- 3. "Delete my data" confirmation modal ----
  // The button never submits anything by itself - it only opens this
  // modal, populated from the button's own data-* attributes (execution
  // capability/reason/consequences - all computed server-side by
  // deletion_engine.classify_execution_capability, the SAME function the
  // execute endpoint itself uses, so this modal can never promise
  // something execution won't actually do). The actual POST only happens
  // if the user clicks the approval button inside the modal.

  const modal = document.getElementById("deletion-modal");
  let modalCompanyId = null;

  const SUBMIT_LABEL = {
    AUTO_EXECUTABLE: "Send this email",
    USER_STEP_REQUIRED: "Continue - I'll finish this myself",
    MANUAL_HANDOFF: "Open the verified page",
  };
  const IN_FLIGHT_LABEL = {
    AUTO_EXECUTABLE: "Sending…",
    USER_STEP_REQUIRED: "Continuing…",
    MANUAL_HANDOFF: "Opening…",
  };

  function applyCapability(capability, reason) {
    const submitBtn = document.getElementById("deletion-modal-submit");
    const userStepEl = document.getElementById("deletion-modal-user-step");
    const userStepReasonEl = document.getElementById("deletion-modal-user-step-reason");
    submitBtn.textContent = SUBMIT_LABEL[capability] || "Continue with deletion";
    submitBtn.dataset.loadingText = IN_FLIGHT_LABEL[capability] || "Working…";
    const showUserStep = capability && capability !== "AUTO_EXECUTABLE" && reason;
    userStepReasonEl.textContent = reason || "";
    userStepEl.hidden = !showUserStep;
  }

  function openModal(btn) {
    if (!modal) return;
    const titleEl = document.getElementById("deletion-modal-title");
    const actionEl = document.getElementById("deletion-modal-action");
    const detailsEl = document.getElementById("deletion-modal-details");
    const consequencesEl = document.getElementById("deletion-modal-consequences");
    const consequencesTextEl = document.getElementById("deletion-modal-consequences-text");
    const emailPreviewEl = document.getElementById("deletion-modal-email");
    const emailToEl = document.getElementById("deletion-modal-email-to");
    const emailSubjectEl = document.getElementById("deletion-modal-email-subject");
    const emailBodyEl = document.getElementById("deletion-modal-email-body");
    const form = document.getElementById("deletion-modal-form");
    const submitBtn = document.getElementById("deletion-modal-submit");
    const chooseRecipeEl = document.getElementById("deletion-modal-choose-recipe");
    const confirmEl = document.getElementById("deletion-modal-confirm");
    const recipeExplanationEl = document.getElementById("deletion-modal-recipe-explanation");
    const recipeSummaryEl = document.getElementById("deletion-modal-recipe-summary");
    const trackingNoteEl = document.getElementById("deletion-modal-tracking-note");
    const recipeSubmitBtn = document.getElementById("deletion-modal-recipe-submit");

    modalCompanyId = btn.dataset.id;
    const name = btn.dataset.name || "this company";
    titleEl.textContent = "Delete my data — " + name;
    modal.hidden = false;

    // Full Clean gate (see main.py's preview_deletion_email/
    // execute_company_deletion): until this company's PrivacyCase has
    // FULL_CLEAN on file, the modal only ever shows the recipe-choice
    // step - never the consequences/email preview/execute form, and never
    // fetches deletion/preview (which the server refuses anyway without a
    // recipe selected). Choosing Full Clean records intent only; the real
    // preview -> execute flow below runs unchanged the NEXT time this
    // modal opens, once selected.
    const fullCleanSelected = btn.dataset.fullCleanSelected === "true";
    chooseRecipeEl.hidden = fullCleanSelected;
    confirmEl.hidden = !fullCleanSelected;
    submitBtn.hidden = !fullCleanSelected;
    recipeSubmitBtn.hidden = fullCleanSelected;
    if (!fullCleanSelected) {
      recipeExplanationEl.textContent = btn.dataset.recipeExplanation || "";
      recipeSubmitBtn.disabled = false;
      recipeSubmitBtn.textContent = "Choose Full Clean";
      return;
    }

    recipeSummaryEl.textContent = btn.dataset.recipeSummary || "";
    trackingNoteEl.textContent = btn.dataset.recipeTracking || "";
    actionEl.textContent = btn.dataset.action || "";
    detailsEl.textContent = btn.dataset.details || "";
    detailsEl.hidden = !btn.dataset.details;
    consequencesTextEl.textContent = btn.dataset.consequences || "";
    consequencesEl.hidden = !btn.dataset.consequences;
    applyCapability(btn.dataset.capability, btn.dataset.capabilityReason);
    form.action = "/api/companies/" + btn.dataset.id + "/deletion/execute";
    submitBtn.disabled = false;
    emailPreviewEl.hidden = true;

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
    if (modal) modal.hidden = true;
    modalCompanyId = null;
  }

  function wireDeleteButtons(scopeEl) {
    scopeEl.querySelectorAll(".delete-my-data-btn").forEach((btn) => {
      if (btn.dataset.ajaxWired) return;
      btn.dataset.ajaxWired = "1";
      btn.addEventListener("click", () => openModal(btn));
    });
  }

  if (modal) {
    const cancelBtn = document.getElementById("deletion-modal-cancel");
    const form = document.getElementById("deletion-modal-form");
    const submitBtn = document.getElementById("deletion-modal-submit");
    const recipeSubmitBtn = document.getElementById("deletion-modal-recipe-submit");

    cancelBtn.addEventListener("click", closeModal);
    modal.addEventListener("click", (event) => {
      if (event.target === modal) closeModal();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !modal.hidden) closeModal();
    });
    form.addEventListener("submit", (event) => {
      if (form.dataset.forceNative) return;
      event.preventDefault();
      const cardId = modalCompanyId;
      // Stays open (button disabled, showing its in-flight label) until
      // the response is back, so approving isn't followed by a moment of
      // nothing happening - THEN closes once the swap (or fallback) runs.
      submitFormAjax(form, cardId, submitBtn, closeModal);
    });

    // "Choose Full Clean" - records intent only (see main.py's
    // select_company_recipe). Never posts to deletion/execute, never a
    // form submit - a separate, explicit click, entirely distinct from
    // the confirm/execute button above.
    if (recipeSubmitBtn) {
      recipeSubmitBtn.addEventListener("click", () => {
        const cardId = modalCompanyId;
        if (!cardId) return;
        recipeSubmitBtn.disabled = true;
        recipeSubmitBtn.textContent = "Choosing…";
        fetch("/api/companies/" + cardId + "/privacy-case/recipe", {
          method: "POST",
          body: new URLSearchParams({ recipe: "FULL_CLEAN" }),
          credentials: "same-origin",
        })
          .then((resp) => {
            if (!resp.ok) throw new Error("recipe selection failed: " + resp.status);
            return resp.text();
          })
          .then((html) => {
            swapCard(cardId, html);
            closeModal();
          })
          .catch(() => {
            window.location.reload();
          });
      });
    }
  }

  // ---- 4. Dedicated "Check for reply" state machine ----
  // Locked UX requirement: DEFAULT -> CHECKING (disabled, no double-click)
  // -> NEW REPLY / NO NEW REPLY / ERROR, all shown in place on the card.
  // Unlike every other card form, the ERROR state must NEVER fall back to
  // a native full-page submit/reload - a failed check has to look like a
  // failed check, not like nothing happened and not like a random reload.
  // So this intentionally does NOT go through wireCardForms/submitFormAjax.

  function findOrCreateResultEl(form) {
    const card = findCard(form);
    if (!card) return null;
    let el = card.querySelector(".check-response-result");
    if (!el) {
      el = document.createElement("p");
      el.className = "check-response-result";
      el.setAttribute("role", "status");
      form.insertAdjacentElement("beforebegin", el);
    }
    return el;
  }

  function setCheckResult(form, kind, text) {
    const el = findOrCreateResultEl(form);
    if (!el) return;
    el.dataset.checkResult = kind;
    el.textContent = text;
  }

  function wireCheckResponseForms(scopeEl) {
    scopeEl.querySelectorAll(".check-response-form").forEach((form) => {
      if (form.dataset.checkWired) return;
      form.dataset.checkWired = "1";
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const button = form.querySelector('button[type="submit"]');
        if (button.disabled) return; // already checking - no double-click
        const originalLabel = button.textContent;
        button.disabled = true;
        button.textContent = "Checking…";
        setCheckResult(form, "checking", "Checking…");

        fetch(form.action, { method: "POST", body: new FormData(form), credentials: "same-origin" })
          .then((resp) => {
            if (!resp.ok) throw new Error("check-response failed: " + resp.status);
            return resp.text();
          })
          .then((html) => {
            const cardId = form.dataset.companyId;
            if (!cardId || !swapCard(cardId, html)) {
              // Card couldn't be located in the response - stay in place
              // and report it as a failure rather than reloading the page.
              throw new Error("check-response: card not found in response");
            }
          })
          .catch(() => {
            button.disabled = false;
            button.textContent = originalLabel;
            setCheckResult(form, "check_failed", "Couldn't check right now. Try again.");
          });
      });
    });
  }

  // ---- "You've got mail" banner dismiss ----
  // Client-side only, for this page view - the persistent mailbox nav
  // badge (base.html) stays visibly indicated regardless, so nothing is
  // lost by not persisting the dismissal server-side.
  const mailBannerDismiss = document.getElementById("mail-banner-dismiss");
  if (mailBannerDismiss) {
    mailBannerDismiss.addEventListener("click", () => {
      const banner = document.getElementById("mail-banner");
      if (banner) banner.hidden = true;
    });
  }

  // ---- Init ----
  document.querySelectorAll(".company-card").forEach(wireCard);
  refreshMergePicker();
})();
