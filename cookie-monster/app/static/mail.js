// Mailbox letter view: the "Respond" draft preview/approval flow, plus a
// fast (~0.8s), never-blocking send animation. Progressive enhancement,
// same philosophy as dashboard.js: every action is a plain HTML form/link
// first, JS only makes it feel better - a fetch failure always falls back
// to a real native submit, so a failure can never look like nothing
// happened and never silently loses the user's approval click.

(function () {
  "use strict";

  const draftPanel = document.getElementById("letter-respond-draft");
  if (!draftPanel) return;

  const companyId = draftPanel.dataset.companyId;
  const toEl = document.getElementById("respond-to");
  const subjectEl = document.getElementById("respond-subject");
  const bodyEl = document.getElementById("respond-body");
  const sendBtn = document.getElementById("respond-send-btn");
  const kindInput = document.getElementById("respond-kind-input");
  const form = document.getElementById("respond-form");
  const choicesEl = document.getElementById("account-warning-choices");

  function openDraft(kind) {
    kindInput.value = kind;
    draftPanel.hidden = false;
    draftPanel.classList.remove("letter-sent", "letter-sending");
    toEl.textContent = "Loading…";
    subjectEl.textContent = "";
    bodyEl.textContent = "";
    sendBtn.disabled = true;
    sendBtn.textContent = "Send letter";

    fetch(`/mail/${companyId}/respond/preview?kind=${encodeURIComponent(kind)}`)
      .then((resp) => (resp.ok ? resp.json() : Promise.reject(new Error("preview failed"))))
      .then((plan) => {
        toEl.textContent = plan.to;
        subjectEl.textContent = plan.subject;
        bodyEl.textContent = plan.body;
        if (plan.send_enabled) {
          sendBtn.disabled = false;
          sendBtn.textContent = "Send letter";
        } else {
          sendBtn.disabled = true;
          sendBtn.textContent = "Automatic sending isn't enabled yet";
        }
      })
      .catch(() => {
        toEl.textContent = "Couldn't load this draft — please try again.";
      });
  }

  document.querySelectorAll("[data-respond-kind]").forEach((btn) => {
    btn.addEventListener("click", () => openDraft(btn.dataset.respondKind));
  });

  const cancelBtn = document.getElementById("respond-cancel");
  if (cancelBtn) {
    cancelBtn.addEventListener("click", () => {
      draftPanel.hidden = true;
    });
  }

  const decideLaterBtn = document.getElementById("letter-decide-later");
  if (decideLaterBtn) {
    decideLaterBtn.addEventListener("click", () => {
      draftPanel.hidden = true;
      if (choicesEl) choicesEl.hidden = true;
    });
  }

  if (form) {
    form.addEventListener("submit", (event) => {
      if (form.dataset.forceNative) return; // native-fallback resubmit - let it through untouched
      event.preventDefault();
      sendBtn.disabled = true;
      sendBtn.textContent = "Sending…";

      fetch(form.action, { method: "POST", body: new FormData(form), credentials: "same-origin" })
        .then((resp) => {
          if (!resp.ok) throw new Error("send failed: " + resp.status);
          // The request already fully succeeded server-side by this point -
          // the animation below only ever delays the NAVIGATION, never the
          // actual send.
          draftPanel.classList.add("letter-sending");
          window.setTimeout(() => {
            window.location.href = `/mail/${companyId}?sent=1`;
          }, 800);
        })
        .catch(() => {
          // Never swallow a failure silently - resubmit as a real native
          // POST, the exact pre-AJAX behavior.
          form.dataset.forceNative = "1";
          if (typeof form.requestSubmit === "function") {
            form.requestSubmit();
          } else {
            form.submit();
          }
        });
    });
  }
})();
