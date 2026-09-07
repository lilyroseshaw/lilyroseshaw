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
    if (oldCard.dataset.pantry !== newCard.dataset.pantry) {
      // The company moved between the active company list and the Pantry
      // - two separate sections of the page a single in-place node swap
      // can't relocate between. Treated as a swap "failure" so every
      // existing caller's fallback (a full reload) runs - the Pantry
      // section a reload lands on already reflects the change correctly.
      return false;
    }
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

  // The server (app.privacy_action.just_the_essentials_review) already
  // translates every PrivacyAction's internal status into what should be
  // shown - status_label/explanation/cta_label/scope_note. This map only
  // covers the ONE thing that's still a button label decision made here:
  // whether a "find/look again" research button is offered at all, and
  // what to call it before an in-flight request relabels it "Searching…".
  // NEEDS_RESEARCH/NEEDS_REVIEW are the only two statuses with a button;
  // every other status (USER_ACTION_REQUIRED, and anything beyond it) has
  // its own cta_label/cta_url instead - never both.
  const JTE_RESEARCH_BUTTON_LABEL = {
    NEEDS_RESEARCH: "Find cleanup method",
    NEEDS_REVIEW: "Look again",
  };

  function runJteResearch(companyId, companyName, actionType, button) {
    const originalLabel = button.textContent;
    button.disabled = true;
    button.textContent = "Searching…";
    fetch("/api/companies/" + companyId + "/just-the-essentials/" + actionType + "/research", {
      method: "POST",
      credentials: "same-origin",
    })
      .then((resp) => (resp.ok ? resp.json() : null))
      .then((data) => {
        if (!data) throw new Error("research failed");
        renderJteActions(companyId, companyName, data.actions);
      })
      .catch(() => {
        button.disabled = false;
        button.textContent = originalLabel;
      });
  }

  // Records the USER's attestation that they personally completed the
  // verified privacy control for ONE PrivacyAction - never immediately
  // on the first click (see renderJteActions' confirm sub-block below).
  // USER-ATTESTED ONLY: this never sends Gmail, submits anything
  // externally, executes a deletion, or runs research - see
  // app.privacy_action.attest_user_completed.
  function attestJteAction(companyId, companyName, actionType, button) {
    button.disabled = true;
    button.textContent = "Saving…";
    fetch("/api/companies/" + companyId + "/just-the-essentials/" + actionType + "/attest-completed", {
      method: "POST",
      credentials: "same-origin",
    })
      .then((resp) => (resp.ok ? resp.json() : null))
      .then((data) => {
        if (!data) throw new Error("attest failed");
        renderJteActions(companyId, companyName, data.actions);
      })
      .catch(() => {
        button.disabled = false;
        button.textContent = "Confirm I did this";
      });
  }

  function renderJteActions(companyId, companyName, actions) {
    const listEl = document.getElementById("deletion-modal-jte-actions");
    listEl.textContent = "";
    (actions || []).forEach((action) => {
      const item = document.createElement("li");

      const label = document.createElement("strong");
      label.textContent = action.label;
      item.appendChild(label);

      if (action.status_label) {
        const pill = document.createElement("span");
        pill.className = "jte-status-pill";
        pill.textContent = action.status_label;
        item.appendChild(pill);
      }

      const summary = document.createElement("p");
      summary.className = "deletion-detail";
      summary.textContent = action.summary || "";
      item.appendChild(summary);

      if (action.explanation) {
        const explanation = document.createElement("p");
        explanation.className = "deletion-detail";
        explanation.textContent = action.explanation;
        item.appendChild(explanation);
      }

      if (action.scope_note) {
        const note = document.createElement("p");
        note.className = "deletion-detail jte-scope-note";
        note.textContent = action.scope_note;
        item.appendChild(note);
      }

      const researchLabel = JTE_RESEARCH_BUTTON_LABEL[action.status];
      if (researchLabel) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-secondary btn-small";
        btn.textContent = researchLabel;
        btn.addEventListener("click", () => runJteResearch(companyId, companyName, action.action_type, btn));
        item.appendChild(btn);
      } else if (action.cta_url) {
        const link = document.createElement("a");
        link.href = action.cta_url;
        link.target = "_blank";
        link.rel = "noopener";
        link.className = "btn btn-primary btn-small";
        link.textContent = action.cta_label || "Continue cleanup";
        item.appendChild(link);
      }

      // "I did this" -> a compact, explicit confirm step (never an
      // immediate state change on the first click) - see
      // app.privacy_action.attest_user_completed. Only offered when the
      // server says this action is actually eligible (can_attest), i.e.
      // there's a real, verified mechanism the user could have used.
      if (action.can_attest) {
        const attestBtn = document.createElement("button");
        attestBtn.type = "button";
        attestBtn.className = "btn btn-ghost btn-small";
        attestBtn.textContent = "I did this";

        const confirmBlock = document.createElement("div");
        confirmBlock.className = "jte-attest-confirm";
        confirmBlock.hidden = true;

        const confirmText = document.createElement("p");
        confirmText.className = "deletion-detail";
        confirmText.textContent =
          "You're confirming that you completed " + companyName + "'s privacy control yourself. " +
          "Baker's Dozen will record this action as completed by you. This does not mean " +
          companyName + " confirmed that historical data was deleted or recalled.";
        confirmBlock.appendChild(confirmText);

        const cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "btn btn-ghost btn-small";
        cancelBtn.textContent = "Cancel";
        cancelBtn.addEventListener("click", () => {
          confirmBlock.hidden = true;
          attestBtn.hidden = false;
        });

        const confirmBtn = document.createElement("button");
        confirmBtn.type = "button";
        confirmBtn.className = "btn btn-primary btn-small";
        confirmBtn.textContent = "Confirm I did this";
        confirmBtn.addEventListener("click", () => attestJteAction(companyId, companyName, action.action_type, confirmBtn));
        confirmBlock.appendChild(cancelBtn);
        confirmBlock.appendChild(confirmBtn);

        attestBtn.addEventListener("click", () => {
          attestBtn.hidden = true;
          confirmBlock.hidden = false;
        });

        item.appendChild(attestBtn);
        item.appendChild(confirmBlock);
      }

      listEl.appendChild(item);
    });
  }

  function openModal(btn) {
    if (!modal) return;
    const titleEl = document.getElementById("deletion-modal-title");
    const recipeLabelEl = document.getElementById("deletion-modal-recipe-label");
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
    const jteReviewEl = document.getElementById("deletion-modal-jte-review");
    const recipeExplanationEl = document.getElementById("deletion-modal-recipe-explanation");
    const jteExplanationEl = document.getElementById("deletion-modal-jte-explanation");
    const leaveItBeExplanationEl = document.getElementById("deletion-modal-leave-it-be-explanation");
    const recipeSummaryEl = document.getElementById("deletion-modal-recipe-summary");
    const trackingNoteEl = document.getElementById("deletion-modal-tracking-note");
    const recipeSubmitBtn = document.getElementById("deletion-modal-recipe-submit");
    const jteSubmitBtn = document.getElementById("deletion-modal-jte-submit");
    const leaveItBeSubmitBtn = document.getElementById("deletion-modal-leave-it-be-submit");
    const jteSummaryEl = document.getElementById("deletion-modal-jte-summary");

    modalCompanyId = btn.dataset.id;
    const name = btn.dataset.name || "this company";
    titleEl.textContent = "Delete my data — " + name;
    modal.hidden = false;

    // Recipe gate (see main.py's preview_deletion_email/
    // execute_company_deletion/preview_just_the_essentials): until this
    // company's PrivacyCase has a recipe on file, the modal only ever
    // shows the recipe-choice step - never a preview/execute form, and
    // never fetches a preview endpoint (the server refuses those anyway
    // without a matching recipe selected). Choosing a recipe records
    // intent only; the real flow for that recipe runs the NEXT time this
    // modal opens (in practice, immediately - see the choose-button
    // handlers below, which re-open this same modal on success).
    const selectedRecipe = btn.dataset.selectedRecipe || "";
    chooseRecipeEl.hidden = selectedRecipe !== "";
    confirmEl.hidden = selectedRecipe !== "FULL_CLEAN";
    jteReviewEl.hidden = selectedRecipe !== "JUST_THE_ESSENTIALS";
    submitBtn.hidden = selectedRecipe !== "FULL_CLEAN";
    recipeSubmitBtn.hidden = selectedRecipe !== "";
    jteSubmitBtn.hidden = selectedRecipe !== "";
    leaveItBeSubmitBtn.hidden = selectedRecipe !== "";

    // Reset in case any button is ever visible again later - never carry
    // a stale "Choosing…"/disabled state into a stage that shouldn't show
    // it at all.
    recipeSubmitBtn.disabled = false;
    recipeSubmitBtn.textContent = "Choose Full Clean";
    jteSubmitBtn.disabled = false;
    jteSubmitBtn.textContent = "Choose Just the Essentials";
    leaveItBeSubmitBtn.disabled = false;
    leaveItBeSubmitBtn.textContent = "Choose Leave It Be";

    if (selectedRecipe === "") {
      recipeLabelEl.textContent = "Choose a Cleanup Recipe";
      recipeExplanationEl.textContent = btn.dataset.recipeExplanation || "";
      jteExplanationEl.textContent = btn.dataset.jteExplanation || "";
      leaveItBeExplanationEl.textContent = btn.dataset.leaveItBeExplanation || "";
      return;
    }

    if (selectedRecipe === "LEAVE_IT_BE") {
      // A disposition, not a privacy action - nothing to preview or
      // execute. chooseRecipeEl/confirmEl/jteReviewEl/submitBtn are all
      // already hidden above; Cancel is the only control this stage
      // needs. (Structurally unreachable today - an active card's recipe
      // is never LEAVE_IT_BE, since choosing it moves the company to its
      // own Pantry card instead - but handled explicitly rather than
      // falling through to the Full Clean preview/execute code below.)
      recipeLabelEl.textContent = "Cleanup Recipe: Leave It Be";
      return;
    }

    if (selectedRecipe === "JUST_THE_ESSENTIALS") {
      recipeLabelEl.textContent = "Cleanup Recipe: Just the Essentials";
      jteSummaryEl.textContent = "Just the Essentials for " + name + ":";
      renderJteActions(btn.dataset.id, name, []);
      fetch("/api/companies/" + btn.dataset.id + "/just-the-essentials/preview")
        .then((resp) => (resp.ok ? resp.json() : null))
        .then((data) => {
          if (!data || modal.hidden) return;
          renderJteActions(btn.dataset.id, name, data.actions);
        })
        .catch(() => {});
      return;
    }

    // FULL_CLEAN - unchanged existing preview -> execute flow.
    recipeLabelEl.textContent = "Cleanup Recipe: Full Clean";
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

  // Records a Cleanup Recipe choice - intent only (see main.py's
  // select_company_recipe). Never posts to deletion/execute or any other
  // execution route, and never a form submit - a separate, explicit
  // click, entirely distinct from any confirm/execute button. On
  // success, seamlessly transitions this SAME modal straight into that
  // recipe's own next step (never closes and makes the user click
  // "Delete my data" a second time) by re-running openModal() against
  // the freshly swapped card's own button, whose server-rendered
  // data-selected-recipe now reflects the choice - that's the one and
  // only thing that decides which stage renders, so this is just the
  // normal open path, not a special case.
  function chooseRecipe(recipe, button) {
    const cardId = modalCompanyId;
    if (!cardId) return;
    button.disabled = true;
    button.textContent = "Choosing…";
    fetch("/api/companies/" + cardId + "/privacy-case/recipe", {
      method: "POST",
      body: new URLSearchParams({ recipe: recipe }),
      credentials: "same-origin",
    })
      .then((resp) => {
        if (!resp.ok) throw new Error("recipe selection failed: " + resp.status);
        return resp.text();
      })
      .then((html) => {
        if (!swapCard(cardId, html)) {
          window.location.reload();
          return;
        }
        const newBtn = document.querySelector('#company-' + cardId + ' .delete-my-data-btn');
        if (newBtn) {
          openModal(newBtn);
        } else {
          closeModal();
        }
      })
      .catch(() => {
        window.location.reload();
      });
  }

  if (modal) {
    const cancelBtn = document.getElementById("deletion-modal-cancel");
    const form = document.getElementById("deletion-modal-form");
    const submitBtn = document.getElementById("deletion-modal-submit");
    const recipeSubmitBtn = document.getElementById("deletion-modal-recipe-submit");
    const jteSubmitBtn = document.getElementById("deletion-modal-jte-submit");
    const leaveItBeSubmitBtn = document.getElementById("deletion-modal-leave-it-be-submit");

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

    if (recipeSubmitBtn) {
      recipeSubmitBtn.addEventListener("click", () => chooseRecipe("FULL_CLEAN", recipeSubmitBtn));
    }
    if (jteSubmitBtn) {
      jteSubmitBtn.addEventListener("click", () => chooseRecipe("JUST_THE_ESSENTIALS", jteSubmitBtn));
    }
    if (leaveItBeSubmitBtn) {
      leaveItBeSubmitBtn.addEventListener("click", () => chooseRecipe("LEAVE_IT_BE", leaveItBeSubmitBtn));
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
