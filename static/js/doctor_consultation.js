/* ============================================================
   Cams HMS — Doctor Consultation workstation
   Vanilla JS, no dependencies. Wires the consultation form to the
   save / ICD / formulary / safety / AI endpoints.
   ============================================================ */

(function () {
  "use strict";

  const $ = (selector, root) => (root || document).querySelector(selector);
  const $$ = (selector, root) => Array.prototype.slice.call((root || document).querySelectorAll(selector));

  const form = document.getElementById("consultationForm");
  if (!form) { return; }
  form.addEventListener("submit", function (event) { event.preventDefault(); });

  const patientId = form.dataset.patientId || "";
  const saveUrl = form.dataset.saveUrl || "/api/consultation/save/";
  const aiUrl = form.dataset.aiUrl || "/api/medgemma/draft/";
  const listUrl = form.dataset.listUrl || "/doctor/consultation/";
  let consultationId = form.dataset.consultationId || "";

  /* ───────────────────── Data from the server ───────────────────── */
  function readJsonScript(id, fallback) {
    const el = document.getElementById(id);
    if (!el) { return fallback; }
    try {
      const parsed = JSON.parse(el.textContent || "null");
      return parsed === null ? fallback : parsed;
    } catch (error) {
      return fallback;
    }
  }

  const assessment = readJsonScript("dcAssessment", {});
  const previousPrescriptions = readJsonScript("dcPreviousPrescriptions", []);
  let prescriptions = readJsonScript("dcPrescriptions", []);
  if (!Array.isArray(prescriptions)) { prescriptions = []; }
  if (!Array.isArray(previousPrescriptions)) { /* readJsonScript already returns [] */ }

  /* ───────────────────── Utilities ───────────────────── */
  function escapeHtml(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function nl2br(value) {
    return escapeHtml(value).replace(/\r?\n/g, "<br>");
  }

  function csrfToken() {
    const input = $('[name="csrfmiddlewaretoken"]', form);
    if (input && input.value) { return input.value; }
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function debounce(fn, wait) {
    let timer = null;
    return function () {
      const args = arguments;
      const context = this;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(context, args); }, wait);
    };
  }

  function value(id) {
    const el = document.getElementById(id);
    return el ? String(el.value || "").trim() : "";
  }

  let toastTimer = null;
  function toast(message, kind) {
    const el = document.getElementById("iaToast");
    if (!el) { return; }
    const icon = kind === "error"
      ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>'
      : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="m8 12.5 2.5 2.5L16 9.5"/></svg>';
    el.innerHTML = icon + "<span>" + escapeHtml(message) + "</span>";
    el.classList.toggle("is-error", kind === "error");
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, 3400);
  }

  /* ───────────────────── Dirty state ───────────────────── */
  const saveState = document.getElementById("saveState");
  let dirty = false;

  function markDirty() {
    dirty = true;
    if (saveState) {
      saveState.textContent = "Unsaved changes";
      saveState.className = "dc-save-state is-dirty";
    }
  }

  function markSaved(text) {
    dirty = false;
    if (saveState) {
      saveState.textContent = text || "Saved just now";
      saveState.className = "dc-save-state is-saved";
    }
  }

  form.addEventListener("input", function (event) {
    if (event.target.matches("[data-rx-field]")) { return; }
    markDirty();
    updateChecklist();
  });
  form.addEventListener("change", function (event) {
    markDirty();
    updateChecklist();
    if (event.target.matches(".dc-order-check")) {
      const label = event.target.closest(".dc-order-option");
      if (label) { label.classList.toggle("is-checked", event.target.checked); }
      updateOrderSummary();
    }
    if (event.target.id === "markFollowUpCheck") { updateFollowUp(); }
    if (event.target.id === "followUpDurationSelect") { updateFollowUp(); }
  });

  /* ───────────────────── Steps ───────────────────── */
  const tabs = $$(".dc-tabs .ia-tab");
  const panels = $$(".ia-panel[data-panel]");
  const STEP_ORDER = ["assessment", "examination", "diagnosis", "orders", "prescription", "advice"];

  function updateStepFooters() {
    const current = $(".dc-tabs .ia-tab.active");
    const currentIndex = current ? STEP_ORDER.indexOf(current.dataset.tab) : 0;
    $$(".dc-step-footer").forEach(function (footer) {
      const panel = footer.closest(".ia-panel");
      const panelIndex = panel ? STEP_ORDER.indexOf(panel.dataset.panel) : 0;
      const prev = footer.querySelector("[data-step-prev]");
      const next = footer.querySelector("[data-step-next]");
      if (prev) { prev.hidden = panelIndex <= 0; }
      if (next) { next.hidden = panelIndex >= STEP_ORDER.length - 1; }
    });
    // Keep the active step visible when the pill bar scrolls horizontally.
    const activeTab = tabs[currentIndex];
    if (activeTab && activeTab.scrollIntoView) {
      activeTab.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }

  function switchTab(name) {
    tabs.forEach(function (tab) {
      const active = tab.dataset.tab === name;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    panels.forEach(function (panel) {
      const active = panel.dataset.panel === name;
      panel.hidden = !active;
      panel.classList.toggle("active", active);
    });
    updateStepFooters();
  }

  function goToStep(name) {
    switchTab(name);
    const formCard = document.querySelector(".dc-form-card");
    if (formCard) {
      formCard.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  function stepBy(offset) {
    const current = $(".dc-tabs .ia-tab.active");
    const index = current ? STEP_ORDER.indexOf(current.dataset.tab) : 0;
    const next = index + offset;
    if (next >= 0 && next < STEP_ORDER.length) { goToStep(STEP_ORDER[next]); }
  }

  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () { goToStep(tab.dataset.tab); });
  });
  $$("[data-goto-tab]").forEach(function (button) {
    button.addEventListener("click", function () { goToStep(button.dataset.gotoTab); });
  });
  $$("[data-step-next]").forEach(function (button) {
    button.addEventListener("click", function () { stepBy(1); });
  });
  $$("[data-step-prev]").forEach(function (button) {
    button.addEventListener("click", function () { stepBy(-1); });
  });

  /* ───────────────────── Clinical note helpers ───────────────────── */
  const clinicalNote = document.getElementById("clinicalNoteText");

  function insertIntoNote(text) {
    if (!clinicalNote) { return; }
    const start = typeof clinicalNote.selectionStart === "number" ? clinicalNote.selectionStart : clinicalNote.value.length;
    const end = typeof clinicalNote.selectionEnd === "number" ? clinicalNote.selectionEnd : clinicalNote.value.length;
    const before = clinicalNote.value.slice(0, start);
    const after = clinicalNote.value.slice(end);
    const spacer = before && !/\s$/.test(before) ? " " : "";
    clinicalNote.value = before + spacer + text + (after && !/^\s/.test(after) ? " " : "") + after;
    const caret = (before + spacer + text).length;
    clinicalNote.focus();
    clinicalNote.setSelectionRange(caret, caret);
    markDirty();
    updateChecklist();
  }

  $$("[data-insert-note]").forEach(function (button) {
    button.addEventListener("click", function () { insertIntoNote(button.dataset.insertNote); });
  });

  const nurseNoteButton = document.getElementById("useNurseNote");
  if (nurseNoteButton && assessment.summary_note) {
    nurseNoteButton.addEventListener("click", function () { insertIntoNote(assessment.summary_note); });
  }

  /* ───────────────────── AI draft ───────────────────── */
  const AI_DRAFT = { summary: "", differential: "", tests: "" };
  let aiTab = "summary";

  function renderAiMarkdown(text) {
    const safe = escapeHtml(text);
    const lines = safe.split(/\r?\n/);
    const html = [];
    let list = null;
    function closeList() {
      if (list) { html.push("</" + list + ">"); list = null; }
    }
    lines.forEach(function (line) {
      const trimmed = line.trim();
      if (!trimmed) { closeList(); return; }
      if (/^[-*•]\s+/.test(trimmed)) {
        if (list !== "ul") { closeList(); html.push("<ul>"); list = "ul"; }
        html.push("<li>" + trimmed.replace(/^[-*•]\s+/, "") + "</li>");
        return;
      }
      if (/^\d+[.)]\s+/.test(trimmed)) {
        if (list !== "ol") { closeList(); html.push("<ol>"); list = "ol"; }
        html.push("<li>" + trimmed.replace(/^\d+[.)]\s+/, "") + "</li>");
        return;
      }
      closeList();
      html.push("<p>" + trimmed + "</p>");
    });
    closeList();
    return html.join("").replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  }

  function renderAi() {
    const box = document.getElementById("aiContent");
    if (!box) { return; }
    const text = AI_DRAFT[aiTab] || "";
    if (!text) {
      box.innerHTML = '<p class="dc-ai-empty">Click <strong>Regenerate draft</strong> to build a clinical summary from the recorded vitals and complaints.</p>';
      return;
    }
    box.innerHTML = renderAiMarkdown(text);
  }

  $$(".dc-ai-tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      aiTab = tab.dataset.aiTab;
      $$(".dc-ai-tab").forEach(function (item) {
        item.classList.toggle("active", item === tab);
        item.setAttribute("aria-selected", item === tab ? "true" : "false");
      });
      renderAi();
    });
  });

  const aiGenerateBtn = document.getElementById("aiGenerateBtn");
  if (aiGenerateBtn) {
    aiGenerateBtn.addEventListener("click", function () {
      const box = document.getElementById("aiContent");
      const vitals = {
        temp: assessment.temperature || "",
        temperature_unit: "F",
        pulse: assessment.pulse || "",
        rr: assessment.respiratory_rate || "",
        bpSys: assessment.bp_systolic || "",
        bpDia: assessment.bp_diastolic || "",
        spo2: assessment.spo2 || "",
        height: assessment.height || "",
        weight: assessment.weight || "",
        bmi: assessment.bmi || "",
        rbs: assessment.rbs || "",
      };
      const complaints = (assessment.complaint_items || []).map(function (item) {
        return item.text + (item.duration ? " (" + item.duration + ")" : "");
      }).join(", ") || assessment.complaints || "";
      const allergies = (assessment.allergies || []).map(function (item) {
        return typeof item === "string" ? item : (item.allergen || item.name || "");
      }).filter(Boolean);

      aiGenerateBtn.disabled = true;
      if (box) { box.classList.add("is-loading"); box.textContent = "Generating draft…"; }

      fetch(aiUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
        body: JSON.stringify({
          vitals: vitals,
          complaints: complaints,
          allergies: allergies,
          patientName: "",
        }),
      })
        .then(function (response) {
          if (!response.ok) { throw new Error("HTTP " + response.status); }
          return response.json();
        })
        .then(function (data) {
          AI_DRAFT.summary = data.summary || "";
          AI_DRAFT.differential = data.differential || "";
          AI_DRAFT.tests = data.tests || "";
          if (box) { box.classList.remove("is-loading"); }
          renderAi();
          if (data._provider === "mock" && data._fallback) {
            toast("Gemini is unavailable; showing a sample draft. Check server configuration and logs.", "error");
          } else {
            toast("AI draft ready — review it before using it.");
          }
        })
        .catch(function (error) {
          if (box) { box.classList.remove("is-loading"); }
          renderAi();
          toast("Could not generate the AI draft (" + error.message + ")", "error");
        })
        .then(function () { aiGenerateBtn.disabled = false; });
    });
  }

  const aiAcceptBtn = document.getElementById("aiAcceptBtn");
  if (aiAcceptBtn) {
    aiAcceptBtn.addEventListener("click", function () {
      const text = AI_DRAFT[aiTab] || "";
      if (!text) { toast("Generate a draft first.", "error"); return; }
      const plain = text.replace(/\*\*/g, "").replace(/^[-*•]\s+/gm, "• ").trim();
      insertIntoNote(plain);
      toast("Draft added to the clinical note.");
    });
  }

  /* ───────────────────── Examination ───────────────────── */
  const normalExam = {
    general: "Patient is conscious, oriented and haemodynamically stable. No pallor, icterus, cyanosis, clubbing or pedal edema.",
    cvs: "S1 and S2 heard, normal in intensity. No murmurs, rubs or gallops.",
    respiratory: "Bilateral equal air entry, vesicular breath sounds. No added sounds.",
    abdomen: "Soft, non-tender, non-distended. No organomegaly. Bowel sounds normal.",
    cns: "Conscious, oriented to time, place and person. No focal neurological deficit.",
    other: "No skin rash, wounds or significant ENT findings.",
  };

  const applyNormalExam = document.getElementById("applyNormalExam");
  if (applyNormalExam) {
    applyNormalExam.addEventListener("click", function () {
      $$("[data-exam]").forEach(function (input) {
        input.value = normalExam[input.dataset.exam] || "";
      });
      $$(".dc-gpe-check").forEach(function (box) { box.checked = false; });
      markDirty();
      updateChecklist();
      toast("Normal examination findings applied.");
    });
  }

  function collectExamination() {
    const result = {};
    $$("[data-exam]").forEach(function (input) { result[input.dataset.exam] = input.value.trim(); });
    result.gpe = $$(".dc-gpe-check:checked").map(function (box) { return box.value; });
    return result;
  }

  /* ───────────────────── ICD-10 search ───────────────────── */
  const diagnosisInput = document.getElementById("primaryDiagnosisInput");
  const icdInput = document.getElementById("primaryIcdInput");
  const icdResults = document.getElementById("icdDropdownResults");
  let icdActiveIndex = -1;

  function closeIcd() {
    if (!icdResults) { return; }
    icdResults.hidden = true;
    icdResults.innerHTML = "";
    icdActiveIndex = -1;
  }

  function icdItems() {
    return icdResults ? $$(".dc-icd-item", icdResults) : [];
  }

  function renderIcdResults(results) {
    if (!icdResults) { return; }
    icdResults.innerHTML = "";
    if (!results.length) {
      icdResults.innerHTML = '<div class="dc-icd-empty">No matching ICD-10 code.</div>';
      icdResults.hidden = false;
      return;
    }
    results.forEach(function (item) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "dc-icd-item";
      button.innerHTML = "<strong>" + escapeHtml(item.code) + "</strong><span>" + escapeHtml(item.desc) + "</span>";
      button.addEventListener("click", function () {
        if (diagnosisInput) { diagnosisInput.value = item.desc; }
        if (icdInput) { icdInput.value = item.code; }
        closeIcd();
        markDirty();
        updateChecklist();
        if (diagnosisInput) { diagnosisInput.focus(); }
      });
      icdResults.appendChild(button);
    });
    icdResults.hidden = false;
    icdActiveIndex = -1;
  }

  const searchIcd = debounce(function () {
    const query = diagnosisInput ? diagnosisInput.value.trim() : "";
    if (!icdResults || query.length < 2) { closeIcd(); return; }
    fetch("/api/consultation/icd10-search/?q=" + encodeURIComponent(query), { credentials: "same-origin" })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (!data.ok || !Array.isArray(data.results)) { return; }
        renderIcdResults(data.results);
      })
      .catch(function () { closeIcd(); });
  }, 220);

  if (diagnosisInput) {
    diagnosisInput.addEventListener("input", searchIcd);
    diagnosisInput.addEventListener("focus", function () {
      if (diagnosisInput.value.trim().length >= 2) { searchIcd(); }
    });
    diagnosisInput.addEventListener("keydown", function (event) {
      const items = icdItems();
      if (event.key === "Escape") { closeIcd(); return; }
      if (!items.length) { return; }
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        icdActiveIndex = event.key === "ArrowDown"
          ? Math.min(icdActiveIndex + 1, items.length - 1)
          : Math.max(icdActiveIndex - 1, 0);
        items.forEach(function (item, index) { item.classList.toggle("is-active", index === icdActiveIndex); });
      }
      if (event.key === "Enter" && icdActiveIndex >= 0) {
        event.preventDefault();
        items[icdActiveIndex].click();
      }
    });
  }

  document.addEventListener("click", function (event) {
    if (!icdResults) { return; }
    if (!icdResults.contains(event.target) && event.target !== diagnosisInput) { closeIcd(); }
  });

  $$(".dc-common-dx-chip").forEach(function (chip) {
    chip.addEventListener("click", function () {
      if (diagnosisInput) { diagnosisInput.value = chip.dataset.icdDesc || ""; }
      if (icdInput) { icdInput.value = chip.dataset.icdCode || ""; }
      markDirty();
      updateChecklist();
      if (diagnosisInput) { diagnosisInput.focus(); }
    });
  });

  /* ───────────────────── Orders ───────────────────── */
  const orderSummaryList = document.getElementById("orderSummaryList");
  const orderCount = document.getElementById("orderCount");
  const extraOrderList = document.getElementById("extraOrderList");
  const extraOrdersBlock = document.getElementById("extraOrdersBlock");

  function checkedOrders() {
    const seen = new Set();
    return $$(".dc-order-check:checked").map(function (box) {
      const label = box.closest(".dc-order-option");
      const statusEl = label ? label.querySelector(".dc-order-status") : null;
      return {
        order_type: box.dataset.type || "laboratory",
        name: box.dataset.name,
        price: box.dataset.price || 0,
        status: statusEl ? statusEl.textContent.trim() : "Ordered",
      };
    }).filter(function (order) { const key = order.order_type + ":" + order.name.toLowerCase(); if (seen.has(key)) return false; seen.add(key); return true; });
  }

  function updateOrderSummary() {
    const orders = checkedOrders();
    if (orderCount) { orderCount.textContent = String(orders.length); }
    if (orderSummaryList) {
      orderSummaryList.innerHTML = "";
      if (!orders.length) {
        const li = document.createElement("li");
        li.className = "dc-order-summary-empty";
        li.textContent = "No orders selected yet.";
        orderSummaryList.appendChild(li);
      } else {
        orders.forEach(function (order) {
          const li = document.createElement("li");
          const name = document.createElement("span");
          name.textContent = order.name;
          const status = document.createElement("em");
          status.textContent = order.status || "Ordered";
          li.appendChild(name);
          li.appendChild(status);
          orderSummaryList.appendChild(li);
        });
      }
    }
    updateChecklist();
  }

  const serviceCatalogueSelect = document.getElementById('serviceCatalogueSelect');
  if (serviceCatalogueSelect) serviceCatalogueSelect.addEventListener('change', function () {
    const selected = serviceCatalogueSelect.selectedOptions[0];
    if (!selected || !selected.value) return;
    const name = document.getElementById('customOrderName');
    const type = document.getElementById('customOrderType');
    const price = document.getElementById('customOrderPrice');
    if (name) name.value = selected.dataset.name;
    if (type) type.value = selected.dataset.type;
    if (price) price.value = selected.dataset.price || "";
  });
  const addOrderBtn = document.getElementById("addOrderBtn");
  if (addOrderBtn) {
    addOrderBtn.addEventListener("click", function () {
      const nameInput = document.getElementById("customOrderName");
      const typeInput = document.getElementById("customOrderType");
      const priceInput = document.getElementById("customOrderPrice");
      const name = nameInput ? nameInput.value.trim() : "";
      const type = typeInput ? typeInput.value : "laboratory";
      const price = priceInput ? String(priceInput.value || "").trim() : "";
      if (!name) { toast("Enter a test or service name first.", "error"); if (nameInput) { nameInput.focus(); } return; }

      const existing = $$(".dc-order-check").find(function (box) {
        return (box.dataset.type || "laboratory") === type && box.dataset.name.toLowerCase() === name.toLowerCase();
      });
      if (existing) {
        existing.checked = true;
        if (price) { existing.dataset.price = price; }
        const label = existing.closest(".dc-order-option");
        if (label) { label.classList.add("is-checked"); }
        updateOrderSummary();
        toast("That order is already on the list.");
      } else {
        if (extraOrdersBlock) { extraOrdersBlock.hidden = false; }
        if (extraOrderList) {
          const label = document.createElement("label");
          label.className = "dc-order-option is-checked";
          const box = document.createElement("input");
          box.type = "checkbox";
          box.className = "dc-order-check";
          box.dataset.type = type;
          box.dataset.name = name;
          if (price) { box.dataset.price = price; }
          box.checked = true;
          const span = document.createElement("span");
          span.textContent = name;
          label.appendChild(box);
          label.appendChild(span);
          if (price) {
            const priceTag = document.createElement("em");
            priceTag.className = "dc-order-price";
            priceTag.textContent = "₹" + price;
            label.appendChild(priceTag);
          }
          extraOrderList.appendChild(label);
        }
        updateOrderSummary();
        toast("Order added.");
      }
      if (nameInput) { nameInput.value = ""; }
      if (priceInput) { priceInput.value = ""; }
    });
  }

  const customOrderName = document.getElementById("customOrderName");
  if (customOrderName) {
    customOrderName.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        if (addOrderBtn) { addOrderBtn.click(); }
      }
    });
  }

  /* ───────────────────── Prescription state ───────────────────── */
  const rxTableBody = $("#currentPrescriptionTable tbody");
  const routeOptions = ["Oral", "IV", "IM", "SC", "Inhalation", "Topical", "Nasal", "Rectal", "Other"];
  const frequencyOptions = ["OD", "BD", "TDS", "QID", "HS", "SOS", "STAT", "Weekly"];

  function optionMarkup(options, selected) {
    const list = options.slice();
    if (selected && list.indexOf(selected) === -1) { list.unshift(selected); }
    return list.map(function (option) {
      return '<option value="' + escapeHtml(option) + '"' + (option === selected ? " selected" : "") + ">" + escapeHtml(option) + "</option>";
    }).join("");
  }

  function cleanPrescriptionsForSave() {
    return prescriptions
      .map(function (item) {
        return {
          id: item.id || null,
          medicine_id: item.medicine_id || null,
          prescribed_quantity: item.prescribed_quantity || null,
          medicine_name: String(item.medicine_name || "").trim(),
          dose: String(item.dose || "").trim(),
          route: String(item.route || "Oral").trim() || "Oral",
          frequency: String(item.frequency || "OD").trim() || "OD",
          duration: String(item.duration || "").trim(),
          instructions: String(item.instructions || "").trim(),
        };
      })
      .filter(function (item) { return item.medicine_name; });
  }

  function renderPrescriptions() {
    if (!rxTableBody) { return; }
    rxTableBody.innerHTML = "";

    if (!prescriptions.length) {
      rxTableBody.innerHTML = '<tr><td colspan="9" class="dc-rx-empty">No medications prescribed yet. Search the formulary or add a custom medicine.</td></tr>';
    } else {
      prescriptions.forEach(function (item, index) {
        const row = document.createElement("tr");
        row.dataset.index = String(index);
        row.innerHTML =
          '<td class="dc-rx-index">' + (index + 1) + "</td>" +
          '<td><input type="text" class="dc-rx-medicine" data-rx-field="medicine_name" value="' + escapeHtml(item.medicine_name || "") + '" placeholder="Medicine name"></td>' +
          '<td><input type="text" data-rx-field="dose" value="' + escapeHtml(item.dose || "") + '" placeholder="Dose"></td>' +
          '<td><select data-rx-field="route">' + optionMarkup(routeOptions, item.route || "Oral") + "</select></td>" +
          '<td><select data-rx-field="frequency">' + optionMarkup(frequencyOptions, item.frequency || "OD") + "</select></td>" +
          '<td><input type="text" data-rx-field="duration" value="' + escapeHtml(item.duration || "") + '" placeholder="e.g. 5 days"></td>' +
          '<td><input type="number" min="1" max="9999" aria-label="Total dispensing quantity" data-rx-field="prescribed_quantity" value="' + escapeHtml(item.prescribed_quantity || '') + '" placeholder="Units"></td>' +
          '<td><input type="text" class="dc-rx-instructions" data-rx-field="instructions" value="' + escapeHtml(item.instructions || "") + '" placeholder="Instructions"></td>' +
          '<td><button type="button" class="dc-rx-remove" aria-label="Remove medicine"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg></button></td>';
        if (item.released_at || item.legacy_dispensed_quantity || item.dispense_status === "cancelled") {
          row.querySelectorAll('input,select').forEach(function (input) { input.disabled = true; });
          row.lastElementChild.innerHTML = item.dispense_status === 'cancelled' ? '<span>Cancelled</span>' : '<span class="dc-helper-text">Released</span><button type="button" class="dc-rx-cancel">Cancel balance</button>';
        }
        rxTableBody.appendChild(row);
      });
    }

    updatePrescriptionPreview();
    updateChecklist();
    scheduleSafetyCheck();
  }

  function updatePrescriptionPreview() {
    const preview = document.getElementById("prescriptionPreviewList");
    if (!preview) { return; }
    const items = prescriptions.filter(function (item) { return String(item.medicine_name || "").trim(); });
    if (!items.length) {
      preview.innerHTML = '<p class="dc-summary-empty">No items on this prescription.</p>';
      return;
    }
    const rows = items.map(function (item) {
      return "<li><strong>" + escapeHtml(item.medicine_name) + "</strong> " +
        escapeHtml(item.dose || "") +
        " — " + escapeHtml(item.route || "Oral") + ", " + escapeHtml(item.frequency || "OD") +
        ", for " + escapeHtml(item.duration || "as advised") + ". " +
        '<em>(' + escapeHtml(item.instructions || "As directed") + ")</em></li>";
    }).join("");
    preview.innerHTML = "<ol>" + rows + "</ol>";
  }

  if (rxTableBody) {
    rxTableBody.addEventListener("input", function (event) {
      const input = event.target.closest("[data-rx-field]");
      if (!input) { return; }
      const row = input.closest("tr[data-index]");
      const index = row ? parseInt(row.dataset.index, 10) : -1;
      if (isNaN(index) || !prescriptions[index]) { return; }
      if (prescriptions[index].released_at) return;
      prescriptions[index][input.dataset.rxField] = input.value;
      if (input.dataset.rxField === "medicine_name") prescriptions[index].medicine_id = null;
      markDirty();
      updatePrescriptionPreview();
      updateChecklist();
      scheduleSafetyCheck();
    });

    rxTableBody.addEventListener("change", function (event) {
      const input = event.target.closest("[data-rx-field]");
      if (!input) { return; }
      const row = input.closest("tr[data-index]");
      const index = row ? parseInt(row.dataset.index, 10) : -1;
      if (isNaN(index) || !prescriptions[index]) { return; }
      if (prescriptions[index].released_at) return;
      prescriptions[index][input.dataset.rxField] = input.value;
      if (input.dataset.rxField === "medicine_name") prescriptions[index].medicine_id = null;
      markDirty();
      updatePrescriptionPreview();
      scheduleSafetyCheck();
    });

    rxTableBody.addEventListener("click", function (event) {
      const cancel = event.target.closest('.dc-rx-cancel');
      if (cancel) {
        const item = prescriptions[Number(cancel.closest('tr').dataset.index)];
        fetch('/doctor/prescriptions/' + item.id + '/cancel/', {method: 'POST', headers: {'X-CSRFToken': csrfToken(), 'Accept': 'application/json'}})
          .then(function (response) { return response.json(); })
          .then(function (data) { if (!data.ok) throw new Error(data.error); item.dispense_status = 'cancelled'; renderPrescriptions(); toast('Unissued balance cancelled.'); })
          .catch(function (error) { toast(error.message || 'Could not cancel the balance.', 'error'); });
        return;
      }
      const button = event.target.closest(".dc-rx-remove");
      if (!button) { return; }
      const row = button.closest("tr[data-index]");
      const index = row ? parseInt(row.dataset.index, 10) : -1;
      if (isNaN(index)) { return; }
      prescriptions.splice(index, 1);
      renderPrescriptions();
      markDirty();
    });
  }

  const addBlankRx = document.getElementById("btnAddBlankRx");
  if (addBlankRx) {
    addBlankRx.addEventListener("click", function () {
      prescriptions.push({ medicine_name: "", dose: "", route: "Oral", frequency: "OD", duration: "", instructions: "" });
      renderPrescriptions();
      const last = rxTableBody ? rxTableBody.querySelector("tr[data-index]:last-child .dc-rx-medicine") : null;
      if (last) { last.focus(); }
      markDirty();
    });
  }

  const copyPreviousRx = document.getElementById("btnCopyPreviousRx");
  if (copyPreviousRx) {
    copyPreviousRx.addEventListener("click", function () {
      if (!previousPrescriptions.length) {
        toast("No previous prescription items on record for this patient.", "error");
        return;
      }
      previousPrescriptions.forEach(function (item) {
        prescriptions.push({
          medicine_id: item.medicine_id || null,
          prescribed_quantity: item.prescribed_quantity || null,
          medicine_name: item.medicine_name || "",
          dose: item.dose || "",
          route: item.route || "Oral",
          frequency: item.frequency || "OD",
          duration: item.duration || "",
          instructions: item.instructions || "",
        });
      });
      renderPrescriptions();
      markDirty();
      toast("Copied " + previousPrescriptions.length + " medicine(s) from the previous visit.");
    });
  }

  function addPrescriptionDrug(data) {
    prescriptions.push({
      medicine_id: data.id || data.medicine_id || null,
      medicine_name: data.name || data.medicine_name || "",
      dose: data.dose || "",
      route: data.route || "Oral",
      frequency: data.freq || data.frequency || "OD",
      duration: data.dur || data.duration || "",
      instructions: data.instructions || "",
    });
    renderPrescriptions();
    markDirty();
    toast("Medicine added to the prescription.");
  }

  /* ───────────────────── Formulary search ───────────────────── */
  const drugSearchInput = document.getElementById("drugSearchInput");
  const drugResultsList = document.getElementById("drugResultsList");
  let activeDrugCategory = "";

  function renderDrugList(drugs) {
    if (!drugResultsList) { return; }
    drugResultsList.innerHTML = "";
    if (!drugs.length) {
      drugResultsList.innerHTML = '<div class="dc-drug-empty">No matching medicines found.</div>';
      return;
    }
    drugs.forEach(function (drug) {
      const row = document.createElement("div");
      row.className = "dc-drug-row";

      const info = document.createElement("div");
      info.className = "dc-drug-info";
      const name = document.createElement("strong");
      name.textContent = drug.name || "";
      const meta = document.createElement("span");
      meta.textContent = [drug.generic, drug.dose].filter(Boolean).join(" · ");
      info.appendChild(name);
      info.appendChild(meta);

      const button = document.createElement("button");
      button.type = "button";
      button.className = "dc-drug-add";
      button.textContent = "+ Add";
      button.addEventListener("click", function () { addPrescriptionDrug(drug); });

      row.appendChild(info);
      row.appendChild(button);
      drugResultsList.appendChild(row);
    });
  }

  function fetchFormulary() {
    if (!drugResultsList) { return; }
    const query = drugSearchInput ? drugSearchInput.value.trim() : "";
    fetch("/api/consultation/drug-search/?q=" + encodeURIComponent(query) + "&cat=" + encodeURIComponent(activeDrugCategory), { credentials: "same-origin" })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (data && data.ok && Array.isArray(data.drugs)) { renderDrugList(data.drugs); }
      })
      .catch(function () {
        drugResultsList.innerHTML = '<div class="dc-drug-empty">Could not load the formulary.</div>';
      });
  }

  if (drugSearchInput) {
    drugSearchInput.addEventListener("input", debounce(fetchFormulary, 220));
  }
  $$(".dc-cat-pill").forEach(function (pill) {
    pill.addEventListener("click", function () {
      $$(".dc-cat-pill").forEach(function (item) { item.classList.toggle("active", item === pill); });
      activeDrugCategory = pill.dataset.cat || "";
      fetchFormulary();
    });
  });

  if (drugResultsList) {
    drugResultsList.addEventListener("click", function (event) {
      const button = event.target.closest(".dc-drug-add");
      if (!button) { return; }
      addPrescriptionDrug({
        id: button.dataset.id || null,
        name: button.dataset.name,
        dose: button.dataset.dose,
        route: button.dataset.route,
        freq: button.dataset.freq,
        dur: button.dataset.dur,
        instructions: button.dataset.instructions,
      });
    });
  }

  /* ───────────────────── Safety check ───────────────────── */
  const safetyBox = document.getElementById("safetyAlertCard");
  let safetyRequest = 0;

  function runSafetyCheck() {
    if (!safetyBox || !patientId) { return; }
    const medicines = cleanPrescriptionsForSave().map(function (item) { return item.medicine_name; });
    if (!medicines.length) {
      safetyBox.hidden = true;
      safetyBox.innerHTML = "";
      return;
    }
    const requestId = ++safetyRequest;
    fetch("/api/consultation/safety-check/", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
      body: JSON.stringify({ patient_id: patientId, medicines: medicines }),
    })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (requestId !== safetyRequest || !data.ok) { return; }
        const alerts = []
          .concat(data.allergy_alerts || [])
          .concat(data.interaction_alerts || []);
        safetyBox.hidden = false;
        if (alerts.length) {
          safetyBox.className = "dc-safety-box is-alert";
          safetyBox.innerHTML = '<div class="dc-safety-title">⚠ Allergy / interaction alert</div>' +
            alerts.map(function (alert) { return "<div>" + escapeHtml(alert) + "</div>"; }).join("");
        } else {
          safetyBox.className = "dc-safety-box is-safe";
          safetyBox.innerHTML = '<div class="dc-safety-title">✓ No major interactions detected</div><div>No drug-allergy or drug-drug interaction flags for the current prescription.</div>';
        }
      })
      .catch(function () { /* fail quietly — saving must remain possible */ });
  }

  const scheduleSafetyCheck = debounce(runSafetyCheck, 650);

  /* ───────────────────── Follow-up ───────────────────── */
  const followUpCheck = document.getElementById("markFollowUpCheck");
  const followUpSelect = document.getElementById("followUpDurationSelect");
  const followUpHint = document.getElementById("followUpDateHint");

  function updateFollowUp() {
    if (!followUpCheck || !followUpSelect || !followUpHint) { return; }
    followUpSelect.disabled = !followUpCheck.checked;
    if (!followUpCheck.checked) {
      followUpHint.textContent = "Pick a review interval to see the expected date.";
      followUpHint.classList.remove("is-on");
      return;
    }
    const days = { "3 days": 3, "5 days": 5, "1 week": 7, "2 weeks": 14, "1 month": 30 }[followUpSelect.value];
    if (!days) {
      followUpHint.textContent = "Choose a review interval.";
      followUpHint.classList.remove("is-on");
      return;
    }
    const date = new Date();
    date.setDate(date.getDate() + days);
    followUpHint.textContent = "Expected review: " + date.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" }) + ".";
    followUpHint.classList.add("is-on");
  }

  /* ───────────────────── Checklist / progress ───────────────────── */
  function hasExamination() {
    const exam = collectExamination();
    if (exam.gpe && exam.gpe.length) { return true; }
    return ["general", "cvs", "respiratory", "abdomen", "cns", "other"].some(function (key) {
      return String(exam[key] || "").trim().length > 0;
    });
  }

  function updateChecklist() {
    const checklist = document.getElementById("encounterChecklist");
    if (!checklist) { return; }
    const states = {
      note: Boolean(clinicalNote && clinicalNote.value.trim()),
      exam: hasExamination(),
      diagnosis: Boolean(value("primaryDiagnosisInput")),
      plan: Boolean(value("treatmentPlanText")),
      orders: checkedOrders().length > 0,
      rx: cleanPrescriptionsForSave().length > 0,
    };
    let done = 0;
    Object.keys(states).forEach(function (key) {
      const item = checklist.querySelector('[data-check="' + key + '"]');
      if (item) { item.classList.toggle("is-done", states[key]); }
      if (states[key]) { done += 1; }
    });
    const total = Object.keys(states).length;
    const count = document.getElementById("checklistCount");
    if (count) { count.textContent = done + "/" + total; }

    // Step pills show a tick once their core content is present.
    const stepState = {
      assessment: states.note,
      examination: states.exam,
      diagnosis: states.diagnosis || states.plan,
      orders: states.orders,
      prescription: states.rx,
      advice: Boolean(value("specialAdviceText"))
        || $$(".dc-education-check:checked").length > 0
        || Boolean(followUpCheck && followUpCheck.checked),
    };
    tabs.forEach(function (tab) {
      tab.classList.toggle("is-done", Boolean(stepState[tab.dataset.tab]));
    });

    const percent = Math.round((done / total) * 100);
    const progressValue = document.getElementById("progressValue");
    const progressBar = document.getElementById("progressBar");
    const progressBand = document.getElementById("progressBand");
    const progressGuidance = document.getElementById("progressGuidance");
    if (progressValue) { progressValue.textContent = percent + "%"; }
    if (progressBar) { progressBar.style.width = percent + "%"; }
    if (progressBand) {
      progressBand.textContent = percent === 100
        ? "Ready to close"
        : percent >= 50 ? "Nearly there" : "In progress";
    }
    if (progressGuidance) {
      progressGuidance.textContent = !states.diagnosis
        ? "A primary diagnosis is required before completing the visit."
        : !states.note
          ? "Add the clinical note to complete the assessment."
          : !states.plan
            ? "Document the treatment plan for this visit."
            : "Diagnosis and plan are set. Orders and prescription are optional.";
    }
  }

  /* ───────────────────── Save ───────────────────── */
  function collectPayload(action) {
    const visitType = $('input[name="visit_type"]:checked');
    return {
      patient_id: patientId,
      consultation_id: consultationId,
      action: action,
      visit_type: visitType ? visitType.value : "new_problem",
      clinical_note: value("clinicalNoteText"),
      examination: collectExamination(),
      diagnosis_primary: value("primaryDiagnosisInput"),
      diagnosis_primary_icd: value("primaryIcdInput"),
      diagnosis_secondary: value("secondaryDiagnosesInput")
        .split(",")
        .map(function (item) { return item.trim(); })
        .filter(Boolean),
      treatment_plan: value("treatmentPlanText"),
      special_advice: value("specialAdviceText"),
      patient_education: $$(".dc-education-check:checked").map(function (box) { return box.value; }),
      mark_follow_up: Boolean(followUpCheck && followUpCheck.checked),
      follow_up_after: followUpSelect ? followUpSelect.value : "",
      prescriptions: cleanPrescriptionsForSave(),
      orders: checkedOrders(),
    };
  }

  let isSaving = false;

  function setSaving(saving) {
    isSaving = saving;
    $$('[data-action]').forEach(function (button) {
      if (saving) {
        button.dataset.originalHtml = button.innerHTML;
        button.disabled = true;
        button.innerHTML = "Saving…";
      } else {
        button.disabled = false;
        if (button.dataset.originalHtml) {
          button.innerHTML = button.dataset.originalHtml;
          delete button.dataset.originalHtml;
        }
      }
    });
    if (saveState && saving) {
      saveState.textContent = "Saving…";
      saveState.className = "dc-save-state";
    }
  }

  function saveConsultation(action) {
    if (isSaving) { return; }
    if (action === "complete") {
      if (!value("primaryDiagnosisInput")) {
        toast("A primary diagnosis is required before completing the consultation.", "error");
        switchTab("diagnosis");
        const diagnosis = document.getElementById("primaryDiagnosisInput");
        if (diagnosis) { diagnosis.focus(); }
        return;
      }
      if (!window.confirm("Complete this consultation? The visit will be closed. Use Send to Pharmacy to release prescription items.")) {
        return;
      }
    }

    setSaving(true);
    fetch(saveUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
      body: JSON.stringify(collectPayload(action)),
    })
      .then(function (response) {
        return response.text().then(function (text) {
          let data = {};
          try { data = text ? JSON.parse(text) : {}; } catch (error) { throw new Error(text || ("HTTP " + response.status)); }
          if (!response.ok || !data.ok) { throw new Error(data.error || "Could not save the consultation."); }
          return data;
        });
      })
      .then(function (data) {
        if (data.id) {
          consultationId = String(data.id);
          form.dataset.consultationId = consultationId;
        }
        if (Array.isArray(data.prescriptions)) { prescriptions = data.prescriptions; renderPrescriptions(); }
        if (action === "complete") {
          markSaved("Consultation completed");
          toast("Consultation completed and saved.");
          setTimeout(function () { window.location.href = listUrl; }, 950);
        } else {
          markSaved("Draft saved just now");
          toast(action === "send_pharmacy" ? "Prescription released to pharmacy." : "Consultation draft saved.");
        }
      })
      .catch(function (error) {
        toast(error.message || "Could not save the consultation.", "error");
      })
      .then(function () { setSaving(false); });
  }

  $$("[data-action]").forEach(function (button) {
    button.addEventListener("click", function () {
      const action = button.dataset.action;
      saveConsultation(action === "send_pharmacy" ? "send_pharmacy" : action === "complete" ? "complete" : "save_draft");
    });
  });

  document.addEventListener("keydown", function (event) {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      if (!isSaving) { saveConsultation("save_draft"); }
    }
  });

  window.addEventListener("beforeunload", function (event) {
    if (!dirty) { return; }
    event.preventDefault();
    event.returnValue = "";
  });

  /* ───────────────────── Print ───────────────────── */
  function patientName() {
    return form.dataset.patientName || "Patient";
  }

  function printSection(title, body) {
    if (!body) { return ""; }
    return '<section class="dc-print-section"><h4>' + escapeHtml(title) + "</h4>" + body + "</section>";
  }

  function buildPrintDocument(mode) {
    const printDoc = document.getElementById("dcPrintDoc");
    if (!printDoc) { return; }
    const isPrescription = mode === "prescription";
    const meds = cleanPrescriptionsForSave();
    const orders = checkedOrders();
    const diagnosis = value("primaryDiagnosisInput");
    const icd = value("primaryIcdInput");
    const secondary = value("secondaryDiagnosesInput");

    const patientRows = [
      ["Name", form.dataset.patientName || patientName()],
      ["UHID", form.dataset.patientUhid || ""],
      ["Age / Gender", form.dataset.patientAgeGender || ""],
      ["Visit token", form.dataset.patientToken || ""],
      ["Department", form.dataset.patientDepartment || ""],
      ["Consultant", form.dataset.patientConsultant || ""],
    ];

    const patientHtml = patientRows.map(function (row) {
      return "<div><span>" + escapeHtml(row[0]) + "</span><strong>" + escapeHtml(row[1] || "—") + "</strong></div>";
    }).join("");

    let html =
      '<div class="dc-print-head">' +
        '<div class="dc-print-brand"><strong>Cams HMS</strong><span>Medical Center</span></div>' +
        '<div class="dc-print-title"><strong>' + (isPrescription ? "Prescription" : "Consultation Summary") + "</strong><span>" + new Date().toLocaleString() + "</span></div>" +
      "</div>" +
      '<div class="dc-print-patient">' + patientHtml + "</div>";

    if (!isPrescription && assessment && (assessment.temperature || assessment.pulse)) {
      const vitals = [
        ["Temperature", assessment.temperature ? assessment.temperature + " °F" : "—"],
        ["Pulse", assessment.pulse ? assessment.pulse + " /min" : "—"],
        ["BP", assessment.bp_systolic ? assessment.bp_systolic + "/" + assessment.bp_diastolic + " mmHg" : "—"],
        ["SpO₂", assessment.spo2 ? assessment.spo2 + " %" : "—"],
        ["Resp. rate", assessment.respiratory_rate ? assessment.respiratory_rate + " /min" : "—"],
        ["RBS", assessment.rbs ? assessment.rbs + " mg/dL" : "—"],
        ["BMI", assessment.bmi || "—"],
        ["Pain", assessment.pain_score !== "" && assessment.pain_score !== undefined ? assessment.pain_score + " /10" : "—"],
      ];
      html += printSection("Vitals recorded at triage",
        '<div class="dc-print-vitals">' +
          vitals.map(function (vital) { return "<div><span>" + escapeHtml(vital[0]) + "</span><strong>" + escapeHtml(vital[1]) + "</strong></div>"; }).join("") +
        "</div>");
    }

    if (!isPrescription) {
      const complaintText = (assessment.complaint_items || []).map(function (item) {
        return item.text + (item.duration ? " (" + item.duration + ")" : "");
      }).join("; ") || assessment.complaints || "";
      html += printSection("Chief complaints", complaintText ? "<p>" + escapeHtml(complaintText) + "</p>" : '<p class="dc-print-empty">Not recorded.</p>');
      html += printSection("History of present illness", assessment.hpi ? "<p>" + nl2br(assessment.hpi) + "</p>" : "");
      html += printSection("Nurse handover", assessment.summary_note ? "<p>" + nl2br(assessment.summary_note) + "</p>" : "");
      html += printSection("Doctor's clinical note", clinicalNote && clinicalNote.value.trim() ? "<p>" + nl2br(clinicalNote.value.trim()) + "</p>" : '<p class="dc-print-empty">Not recorded.</p>');

      const exam = collectExamination();
      const examParts = ["general", "cvs", "respiratory", "abdomen", "cns", "other"]
        .filter(function (key) { return (exam[key] || "").trim(); })
        .map(function (key) {
          return "<div><span>" + escapeHtml(key === "cvs" ? "CVS" : key === "cns" ? "CNS" : key.charAt(0).toUpperCase() + key.slice(1)) + "</span><strong>" + escapeHtml(exam[key]) + "</strong></div>";
        }).join("");
      if (exam.gpe && exam.gpe.length) {
        examParts += '<div><span>GPE</span><strong>' + escapeHtml(exam.gpe.join(", ")) + "</strong></div>";
      }
      if (examParts) {
        html += printSection("Examination", '<div class="dc-print-patient" style="border:none;padding:0;margin:0;grid-template-columns:1fr;">' + examParts + "</div>");
      }
    }

    html += printSection("Diagnosis",
      '<p><strong>' + escapeHtml(diagnosis || "Not recorded") + "</strong>" +
      (icd ? " <span>[" + escapeHtml(icd) + "]</span>" : "") +
      (secondary ? "<br><span>" + escapeHtml(secondary) + "</span>" : "") + "</p>");

    if (!isPrescription) {
      html += printSection("Treatment plan", value("treatmentPlanText") ? "<p>" + nl2br(value("treatmentPlanText")) + "</p>" : "");
      if (orders.length) {
        html += printSection("Investigations & services",
          '<table class="dc-print-table"><thead><tr><th>Service / test</th><th>Status</th></tr></thead><tbody>' +
          orders.map(function (order) { return "<tr><td>" + escapeHtml(order.name) + "</td><td>" + escapeHtml(order.status || "Ordered") + "</td></tr>"; }).join("") +
          "</tbody></table>");
      }
    }

    if (meds.length) {
      html += printSection("Prescription",
        '<table class="dc-print-table"><thead><tr><th>#</th><th>Medicine</th><th>Dose</th><th>Route</th><th>Frequency</th><th>Duration</th><th>Instructions</th></tr></thead><tbody>' +
        meds.map(function (item, index) {
          return "<tr><td>" + (index + 1) + "</td><td><strong>" + escapeHtml(item.medicine_name) + "</strong></td><td>" + escapeHtml(item.dose || "—") + "</td><td>" + escapeHtml(item.route) + "</td><td>" + escapeHtml(item.frequency) + "</td><td>" + escapeHtml(item.duration || "—") + "</td><td>" + escapeHtml(item.instructions || "—") + "</td></tr>";
        }).join("") +
        "</tbody></table>");
    } else {
      html += printSection("Prescription", '<p class="dc-print-empty">No medicines prescribed.</p>');
    }

    const advice = value("specialAdviceText");
    const education = $$(".dc-education-check:checked").map(function (box) { return box.value; });
    if (advice || education.length) {
      html += printSection("Advice & patient education",
        (advice ? "<p>" + nl2br(advice) + "</p>" : "") +
        (education.length ? "<ul>" + education.map(function (item) { return "<li>" + escapeHtml(item) + "</li>"; }).join("") + "</ul>" : ""));
    }

    if (followUpCheck && followUpCheck.checked && followUpSelect) {
      html += printSection("Follow-up", "<p>Review after " + escapeHtml(followUpSelect.value) + ".</p>");
    }

    html += '<div class="dc-print-signature"><div>Consultant signature</div></div>' +
      '<p class="dc-print-note">Generated from the Cams HMS doctor consultation workstation. Review the clinical record before filing.</p>';

    printDoc.innerHTML = html;
  }

  $$("[data-print]").forEach(function (button) {
    button.addEventListener("click", function () {
      buildPrintDocument(button.dataset.print);
      window.print();
    });
  });

  /* ───────────────────── Initial paint ───────────────────── */
  renderPrescriptions();
  updateOrderSummary();
  updateFollowUp();
  updateChecklist();
  updateStepFooters();
  renderAi();
  runSafetyCheck();
})();
