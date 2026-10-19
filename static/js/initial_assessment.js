/* ============================================================
   Cams HMS — Initial Assessment (OPD) workstation
   ------------------------------------------------------------
   The page is a two column workstation: the assessment form on
   the left and a persistent patient search / list on the right.
   Everything the server sends over is read from the DOM, so this
   file stays a plain static asset (no template syntax here).
   ============================================================ */

(function () {
  "use strict";

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  /* ── Server payload ─────────────────────────────────────────── */
  var ASSESSMENT = {};
  var assessmentEl = document.getElementById("iaAssessment");
  if (assessmentEl) {
    try { ASSESSMENT = JSON.parse(assessmentEl.textContent || "{}") || {}; }
    catch (e) { ASSESSMENT = {}; }
  }

  var form = document.getElementById("assessmentForm");
  var PATIENT_ID = form ? ($("#patientIdInput") ? $("#patientIdInput").value : "") : "";
  var SAVE_URL = form ? form.getAttribute("data-save-url") : "";
  var AI_URL = form ? form.getAttribute("data-ai-url") : "";
  var BASE_URL = form ? form.getAttribute("data-base-url") : "";

  /* ───────────────────── Toast ─────────────────────────────── */
  var toastEl = document.getElementById("iaToast");
  var toastTimer = null;

  function showToast(message, kind) {
    if (!toastEl) { return; }
    var icon = kind === "error"
      ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>'
      : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 12.5l2.5 2.5L16 9.5"/></svg>';
    toastEl.innerHTML = icon + message;
    toastEl.classList.toggle("is-error", kind === "error");
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.hidden = true; }, 3200);
  }

  /* ───────────────────── Helpers ────────────────────────────── */
  function val(id) {
    var el = document.getElementById(id);
    return el ? String(el.value || "").trim() : "";
  }

  function numVal(id) {
    var raw = val(id);
    if (raw === "") { return null; }
    var parsed = parseFloat(raw);
    return isNaN(parsed) ? null : parsed;
  }

  function escapeHtml(text) {
    return String(text === null || text === undefined ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /* ───────────────────── Tabs ──────────────────────────────── */
  function initTabs() {
    var tabs = $$(".ia-tab");
    var panels = $$(".ia-panel");
    if (!tabs.length) { return; }

    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        tabs.forEach(function (t) {
          t.classList.remove("active");
          t.setAttribute("aria-selected", "false");
        });
        panels.forEach(function (p) {
          p.hidden = true;
          p.classList.remove("active");
        });
        tab.classList.add("active");
        tab.setAttribute("aria-selected", "true");
        var panel = $('.ia-panel[data-panel="' + tab.getAttribute("data-tab") + '"]');
        if (panel) {
          panel.hidden = false;
          panel.classList.add("active");
        }
        if (tab.getAttribute("data-tab") === "summary") { updateSummary(); }
      });
    });
  }

  /* ───────────────────── BMI ───────────────────────────────── */
  function calcBMI() {
    var height = numVal("height");
    var weight = numVal("weight");
    var bmi = document.getElementById("bmi");
    if (!bmi) { return; }

    if (height && weight && height > 0) {
      var value = (weight / Math.pow(height / 100, 2)).toFixed(1);
      bmi.value = value;
      var parsed = parseFloat(value);
      bmi.classList.toggle("is-alert", parsed > 30 || parsed < 18.5);
    } else if (height || weight) {
      // One of the two was entered — the reading can no longer be trusted.
      bmi.value = "";
      bmi.classList.remove("is-alert");
    }
    // When neither is present the stored BMI (if any) is left untouched.
  }

  /* ───────────────────── Vitals validation ─────────────────── */
  function validateVitals() {
    var issues = [];
    var temp = numVal("temp");
    var pulse = numVal("pulse");
    var rr = numVal("rr");
    var bpSys = numVal("bpSys");
    var bpDia = numVal("bpDia");
    var spo2 = numVal("spo2");
    var rbs = numVal("rbs");
    var pain = numVal("pain");

    if (temp !== null && (temp < 35 || temp > 41)) { issues.push("Temp " + temp + "°C"); }
    if (pulse !== null && (pulse < 40 || pulse > 130)) { issues.push("Pulse " + pulse + "/min"); }
    if (rr !== null && (rr < 12 || rr > 30)) { issues.push("RR " + rr + "/min"); }
    if (bpSys !== null && bpSys > 140) { issues.push("Systolic " + bpSys + " mmHg"); }
    if (bpDia !== null && bpDia > 90) { issues.push("Diastolic " + bpDia + " mmHg"); }
    if (spo2 !== null && spo2 < 95) { issues.push("SpO₂ " + spo2 + "%"); }
    if (rbs !== null && rbs > 250) { issues.push("RBS " + rbs + " mg/dL"); }
    if (pain !== null && pain > 8) { issues.push("Pain " + pain + "/10"); }

    return issues;
  }

  function renderVitalsWarnings() {
    var box = document.getElementById("vitalsWarnings");
    if (!box) { return; }
    var issues = validateVitals();
    if (!issues.length) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    box.hidden = false;
    box.innerHTML = "<strong>Check these readings:</strong> " + escapeHtml(issues.join(", "));
  }

  /* ───────────────────── Complaints ────────────────────────── */
  function addComplaintRow(data) {
    var container = document.getElementById("complaintsContainer");
    var template = document.getElementById("complaintTemplate");
    if (!container || !template) { return null; }

    var row = template.content.querySelector(".ia-complaint-row");
    if (!row) { return null; }
    var textarea = row.querySelector(".ia-complaint-text");
    var duration = row.querySelector(".ia-complaint-duration");
    var removeBtn = row.querySelector(".ia-complaint-remove");

    if (data) {
      textarea.value = data.text || "";
      if (data.duration) { duration.value = data.duration; }
    }

    removeBtn.addEventListener("click", function () {
      row.remove();
      updateSummary();
    });

    container.appendChild(row);
    return row;
  }

  function initComplaints() {
    var container = document.getElementById("complaintsContainer");
    if (!container) { return; }
    if (!container.children.length) { addComplaintRow(); }

    var addBtn = document.getElementById("addComplaintBtn");
    if (addBtn) {
      addBtn.addEventListener("click", function () {
        var row = addComplaintRow();
        if (row) {
          var input = row.querySelector(".ia-complaint-text");
          if (input) { input.focus(); }
        }
      });
    }

    container.addEventListener("input", function (e) {
      if (e.target && e.target.classList.contains("ia-complaint-text")) { updateSummary(); }
    });
  }

  function collectComplaints() {
    return $$(".ia-complaint-text").map(function (textarea) {
      var row = textarea.closest(".ia-complaint-row");
      var duration = row ? row.querySelector(".ia-complaint-duration") : null;
      return {
        text: String(textarea.value || "").trim(),
        duration: duration ? String(duration.value || "").trim() : ""
      };
    }).filter(function (row) { return row.text !== ""; });
  }

  /* ───────────────────── Chronic conditions ────────────────── */
  function initConditions() {
    $$(".ia-cond-check").forEach(function (check) {
      var card = check.closest(".ia-condition");
      var sync = function () { if (card) { card.classList.toggle("is-checked", check.checked); } };
      check.addEventListener("change", sync);
      sync();
    });

    var addBtn = document.getElementById("addOtherConditionBtn");
    var input = document.getElementById("otherConditionInput");
    var list = document.getElementById("otherConditionsList");

    function addCustom(name) {
      if (!list) { return; }
      var tag = document.createElement("span");
      tag.className = "ia-tag ia-tag-soft";
      tag.setAttribute("data-custom-condition", name);
      tag.innerHTML = escapeHtml(name) +
        ' <button type="button" class="ia-tag-x" aria-label="Remove">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>' +
        "</button>";
      list.appendChild(tag);
    }

    if (addBtn && input) {
      addBtn.addEventListener("click", function () {
        var name = String(input.value || "").trim();
        if (!name) { return; }
        addCustom(name);
        input.value = "";
        input.focus();
      });
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); addBtn.click(); }
      });
    }

    if (list) {
      list.addEventListener("click", function (e) {
        var x = e.target.closest(".ia-tag-x");
        if (!x) { return; }
        var tag = x.closest(".ia-tag");
        if (tag) { tag.remove(); }
      });
    }
  }

  function collectConditions() {
    var out = [];
    $$(".ia-condition").forEach(function (card) {
      var check = card.querySelector(".ia-cond-check");
      if (!check || !check.checked) { return; }
      var duration = card.querySelector(".ia-cond-duration");
      out.push({
        name: check.getAttribute("data-label") || "",
        since: duration ? String(duration.value || "").trim() : ""
      });
    });
    $$("[data-custom-condition]").forEach(function (tag) {
      var name = tag.getAttribute("data-custom-condition");
      if (name) { out.push({ name: name, since: "" }); }
    });
    return out;
  }

  /* ───────────────────── Allergies ─────────────────────────── */
  function initAllergies() {
    var list = document.getElementById("allergyTags");
    var input = document.getElementById("allergyInput");
    var addBtn = document.getElementById("addAllergyBtn");

    function addAllergy(name) {
      if (!list) { return; }
      var exists = $$("[data-allergy]").some(function (tag) {
        return (tag.getAttribute("data-allergy") || "").toLowerCase() === name.toLowerCase();
      });
      if (exists) { return; }
      var tag = document.createElement("span");
      tag.className = "ia-tag ia-tag-danger";
      tag.setAttribute("data-allergy", name);
      tag.innerHTML = escapeHtml(name) +
        ' <button type="button" class="ia-tag-x" aria-label="Remove">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>' +
        "</button>";
      list.appendChild(tag);
    }

    if (addBtn && input) {
      addBtn.addEventListener("click", function () {
        var name = String(input.value || "").trim();
        if (!name) { return; }
        addAllergy(name);
        input.value = "";
        input.focus();
        updateSummary();
      });
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); addBtn.click(); }
      });
    }

    if (list) {
      list.addEventListener("click", function (e) {
        var x = e.target.closest(".ia-tag-x");
        if (!x) { return; }
        var tag = x.closest(".ia-tag");
        if (tag) { tag.remove(); }
        updateSummary();
      });
    }
  }

  function collectAllergies() {
    return $$("[data-allergy]").map(function (tag) {
      return tag.getAttribute("data-allergy") || "";
    }).filter(Boolean);
  }

  /* ───────────────────── Medications ───────────────────────── */
  function initMedications() {
    var body = document.getElementById("medTableBody");
    var template = document.getElementById("medRowTemplate");
    var addBtn = document.getElementById("addMedicineBtn");

    if (!body) { return; }

    if (addBtn && template) {
      addBtn.addEventListener("click", function () {
        var row = template.content.querySelector("tr");
        if (!row) { return; }
        var input = row.querySelector(".ia-med-name");
        body.appendChild(row);
        if (input) { input.focus(); }
      });
    }

    body.addEventListener("click", function (e) {
      var btn = e.target.closest(".ia-med-remove");
      if (!btn) { return; }
      var row = btn.closest("tr");
      if (row) { row.remove(); }
      updateSummary();
    });
  }

  function collectMedications() {
    return $$("#medTableBody tr").map(function (row) {
      var name = row.querySelector(".ia-med-name");
      var dose = row.querySelector(".ia-med-dose");
      var freq = row.querySelector(".ia-med-freq");
      return {
        name: name ? String(name.value || "").trim() : "",
        dose: dose ? String(dose.value || "").trim() : "",
        frequency: freq ? String(freq.value || "").trim() : ""
      };
    }).filter(function (med) { return med.name !== ""; });
  }

  /* ───────────────────── Social history ────────────────────── */
  function initSocialHistory() {
    var input = document.getElementById("socialOtherInput");
    var addBtn = document.getElementById("addSocialOtherBtn");
    var list = document.getElementById("socialOtherTags");

    function addOther(name) {
      if (!list) { return; }
      var tag = document.createElement("span");
      tag.className = "ia-tag ia-tag-soft";
      tag.setAttribute("data-social-other", name);
      tag.innerHTML = escapeHtml(name) +
        ' <button type="button" class="ia-tag-x" aria-label="Remove">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>' +
        "</button>";
      list.appendChild(tag);
    }

    if (addBtn && input) {
      addBtn.addEventListener("click", function () {
        var name = String(input.value || "").trim();
        if (!name) { return; }
        addOther(name);
        input.value = "";
        input.focus();
      });
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); addBtn.click(); }
      });
    }

    if (list) {
      list.addEventListener("click", function (e) {
        var x = e.target.closest(".ia-tag-x");
        if (!x) { return; }
        var tag = x.closest(".ia-tag");
        if (tag) { tag.remove(); }
      });
    }
  }

  function collectSocialHistory() {
    var out = [];
    $$(".ia-social-check").forEach(function (check) {
      if (check.checked) { out.push(check.value); }
    });
    $$("[data-social-other]").forEach(function (tag) {
      var name = tag.getAttribute("data-social-other");
      if (name) { out.push(name); }
    });
    return out;
  }

  /* ───────────────────── Examination ───────────────────────── */
  function collectExamination() {
    return {
      general: val("examGeneral"),
      cvs: val("examCvs"),
      respiratory: val("examResp"),
      abdomen: val("examAbd"),
      cns: val("examCns")
    };
  }

  /* ───────────────────── Orders & services ─────────────────── */
  function initOrders() {
    var tabs = $$(".ia-order-tab");
    var sections = $$(".ia-order-section");

    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        var target = tab.getAttribute("data-order-tab");
        tabs.forEach(function (t) {
          t.classList.remove("active");
          t.setAttribute("aria-selected", "false");
        });
        sections.forEach(function (section) {
          section.classList.toggle("active", section.getAttribute("data-order-section") === target);
        });
        tab.classList.add("active");
        tab.setAttribute("aria-selected", "true");
      });
    });

    function addOrder(kind, name) {
      var list = document.querySelector('[data-order-list="' + kind + '"]');
      if (!list || !name) { return; }
      var item = document.createElement("li");
      item.setAttribute("data-order-item", name);
      item.innerHTML = "<span>" + escapeHtml(name) + "</span>" +
        ' <button type="button" class="ia-icon-btn ia-order-remove" aria-label="Remove order">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>' +
        "</button>";
      list.appendChild(item);
    }

    $$(".ia-order-add").forEach(function (btn) {
      var kind = btn.getAttribute("data-order-add");
      var input = document.querySelector('[data-order-input="' + kind + '"]');
      var commit = function () {
        var name = input ? String(input.value || "").trim() : "";
        if (!name) { return; }
        addOrder(kind, name);
        input.value = "";
        input.focus();
      };
      btn.addEventListener("click", commit);
      if (input) {
        input.addEventListener("keydown", function (e) {
          if (e.key === "Enter") { e.preventDefault(); commit(); }
        });
      }
    });

    $$(".ia-order-list").forEach(function (list) {
      list.addEventListener("click", function (e) {
        var btn = e.target.closest(".ia-order-remove");
        if (!btn) { return; }
        var item = btn.closest("li");
        if (item) { item.remove(); }
      });
    });
  }

  function collectOrders() {
    var orders = { laboratory: [], radiology: [], other: [] };
    $$(".ia-order-list").forEach(function (list) {
      var kind = list.getAttribute("data-order-list");
      if (!orders.hasOwnProperty(kind)) { return; }
      $$("li", list).forEach(function (item) {
        var name = item.getAttribute("data-order-item");
        if (name) { orders[kind].push(name); }
      });
    });
    return orders;
  }

  /* ───────────────────── Summary preview ───────────────────── */
  function updateSummary() {
    var vitalsEl = document.getElementById("summaryVitals");
    if (!vitalsEl) { return; }

    var parts = [];
    if (val("temp")) { parts.push("Temp " + val("temp") + "°C"); }
    if (val("pulse")) { parts.push("Pulse " + val("pulse")); }
    if (val("bpSys") && val("bpDia")) { parts.push("BP " + val("bpSys") + "/" + val("bpDia")); }
    if (val("spo2")) { parts.push("SpO₂ " + val("spo2") + "%"); }
    if (val("bmi")) { parts.push("BMI " + val("bmi")); }
    vitalsEl.textContent = parts.length ? parts.join(" · ") : "—";

    var complaintsEl = document.getElementById("summaryComplaints");
    if (complaintsEl) {
      var complaints = collectComplaints();
      complaintsEl.textContent = complaints.length
        ? complaints.map(function (c) { return c.text + (c.duration ? " (" + c.duration + ")" : ""); }).join("; ")
        : "—";
    }

    var allergyEl = document.getElementById("summaryAllergies");
    if (allergyEl) {
      var allergies = collectAllergies();
      allergyEl.textContent = allergies.length ? allergies.join(", ") : "None recorded";
    }

    var medEl = document.getElementById("summaryMedications");
    if (medEl) {
      var meds = collectMedications();
      medEl.textContent = meds.length
        ? meds.map(function (m) { return m.name + (m.dose ? " " + m.dose : ""); }).join(", ")
        : "—";
    }
  }

  /* ───────────────────── AI assistant ──────────────────────── */
  var AI_DRAFT = { summary: "", differential: "", tests: "" };
  var aiTab = "summary";

  function renderMarkdown(text) {
    var safe = escapeHtml(text || "").replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    var lines = safe.split(/\r?\n/);
    var html = [];
    var listType = null;

    function closeList() {
      if (listType) { html.push("</" + listType + ">"); listType = null; }
    }

    lines.forEach(function (line) {
      var trimmed = line.trim();
      if (!trimmed) { closeList(); return; }
      if (/^[-*•]\s+/.test(trimmed)) {
        if (listType !== "ul") { closeList(); html.push("<ul>"); listType = "ul"; }
        html.push("<li>" + trimmed.replace(/^[-*•]\s+/, "") + "</li>");
        return;
      }
      if (/^\d+[.)]\s+/.test(trimmed)) {
        if (listType !== "ol") { closeList(); html.push("<ol>"); listType = "ol"; }
        html.push("<li>" + trimmed.replace(/^\d+[.)]\s+/, "") + "</li>");
        return;
      }
      closeList();
      html.push("<p>" + trimmed + "</p>");
    });
    closeList();
    return html.join("");
  }

  function renderAI() {
    var box = document.getElementById("aiContent");
    if (!box) { return; }
    var text = AI_DRAFT[aiTab] || "";
    if (!text) {
      box.innerHTML = '<p class="ia-ai-empty">Nothing generated for this section yet. Click <strong>Regenerate draft</strong> to build the AI summary.</p>';
      return;
    }
    box.innerHTML = renderMarkdown(text);
  }

  function initAI() {
    var tabs = $$(".ia-ai-tab");
    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        aiTab = tab.getAttribute("data-ai-tab");
        tabs.forEach(function (t) {
          t.classList.remove("active");
          t.setAttribute("aria-selected", "false");
        });
        tab.classList.add("active");
        tab.setAttribute("aria-selected", "true");
        renderAI();
      });
    });

    var generateBtn = document.getElementById("aiGenerateBtn");
    if (generateBtn) {
      generateBtn.addEventListener("click", function () {
        if (!AI_URL) { showToast("AI assistant is not configured.", "error"); return; }
        var box = document.getElementById("aiContent");
        generateBtn.disabled = true;
        if (box) { box.classList.add("is-loading"); box.textContent = "Generating draft…"; }

        var complaints = collectComplaints().map(function (c) {
          return c.text + (c.duration ? " (" + c.duration + ")" : "");
        }).join(", ");

        var payload = {
          vitals: {
            temp: val("temp"), pulse: val("pulse"), rr: val("rr"),
            bpSys: val("bpSys"), bpDia: val("bpDia"), spo2: val("spo2"),
            height: val("height"), weight: val("weight"),
            bmi: val("bmi"), rbs: val("rbs")
          },
          complaints: complaints,
          allergies: collectAllergies(),
          patientName: ""
        };

        fetch(AI_URL, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
          body: JSON.stringify(payload)
        }).then(function (response) {
          if (!response.ok) { throw new Error("HTTP " + response.status); }
          return response.json();
        }).then(function (data) {
          AI_DRAFT = {
            summary: data.summary || "",
            differential: data.differential || "",
            tests: data.tests || ""
          };
          if (box) { box.classList.remove("is-loading"); }
          renderAI();
          showToast("AI draft ready — review it before saving.");
        }).catch(function (err) {
          if (box) { box.classList.remove("is-loading"); }
          renderAI();
          showToast("Could not generate the AI draft (" + err.message + ")", "error");
        }).then(function () {
          generateBtn.disabled = false;
        });
      });
    }

    var acceptBtn = document.getElementById("aiAcceptBtn");
    if (acceptBtn) {
      acceptBtn.addEventListener("click", function () {
        var text = AI_DRAFT[aiTab] || "";
        if (!text) { showToast("Generate a draft first.", "error"); return; }
        var note = document.getElementById("clinicalNote");
        if (!note) { return; }
        var plain = text.replace(/\*\*/g, "").replace(/^[-*•]\s+/gm, "• ").trim();
        note.value = (note.value ? note.value.replace(/\s+$/, "") + "\n\n" : "") + plain;
        note.focus();
        showToast("Draft added to the triage note.");
      });
    }
  }

  /* ───────────────────── Save / edit ───────────────────────── */
  function csrfToken() {
    var input = document.querySelector('[name="csrfmiddlewaretoken"]');
    if (input && input.value) { return input.value; }
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]*)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function collectPayload() {
    return {
      patient_id: PATIENT_ID,
      temperature: val("temp"),
      pulse: val("pulse"),
      respiratory_rate: val("rr"),
      bp_systolic: val("bpSys"),
      bp_diastolic: val("bpDia"),
      spo2: val("spo2"),
      height: val("height"),
      weight: val("weight"),
      bmi: val("bmi"),
      rbs: val("rbs"),
      pain_score: val("pain"),
      complaints: collectComplaints(),
      hpi: val("hpi"),
      summary_note: val("clinicalNote"),
      surgeries: val("surgeries"),
      chronic_conditions: collectConditions(),
      allergies: collectAllergies(),
      medications: collectMedications(),
      social_history: collectSocialHistory(),
      examination: collectExamination(),
      orders: collectOrders()
    };
  }

  var isLocked = false;

  function setLocked(locked) {
    isLocked = locked;
    if (!form) { return; }

    $$("input, textarea, select", form).forEach(function (field) {
      if (field.type === "hidden") { return; }
      field.disabled = locked;
    });

    // Buttons inside the form are locked too — except the tab strip, which
    // must stay usable so the record can be reviewed.
    $$("button", form).forEach(function (btn) {
      if (btn.classList.contains("ia-tab") || btn.classList.contains("ia-ai-tab")) { return; }
      btn.disabled = locked;
    });

    // The orders panel lives outside the form but belongs to the same record.
    $$(".ia-orders .ia-order-input, .ia-orders .ia-order-add, .ia-orders .ia-order-remove").forEach(function (el) {
      el.disabled = locked;
    });

    var notice = document.getElementById("iaLockedNotice");
    if (notice) { notice.hidden = !locked; }

    var editBtn = document.getElementById("btnEditAssessment");
    if (editBtn) { editBtn.disabled = !locked; }

    var saveBtn = document.getElementById("btnSaveAssessment");
    if (saveBtn) { saveBtn.disabled = locked; }

    ["saveAssessmentBtn", "saveDraftBtn", "markUrgentBtn"].forEach(function (id) {
      var btn = document.getElementById(id);
      if (btn) { btn.disabled = locked; }
    });
  }

  function saveAssessment(options) {
    options = options || {};
    if (!PATIENT_ID) { showToast("Select a patient before saving.", "error"); return; }
    if (!SAVE_URL) { showToast("Save endpoint missing.", "error"); return; }

    var payload = collectPayload();
    if (options.urgent) { payload.urgent = true; }

    var buttons = $$("#btnSaveAssessment, #saveAssessmentBtn, #saveDraftBtn, #markUrgentBtn");
    buttons.forEach(function (btn) { btn.disabled = true; });

    fetch(SAVE_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
      body: JSON.stringify(payload)
    }).then(function (response) {
      if (!response.ok) { throw new Error("HTTP " + response.status); }
      return response.json();
    }).then(function (data) {
      if (!data || !data.ok) { throw new Error((data && data.error) || "Save failed"); }
      showToast(options.urgent ? "Assessment saved and marked urgent." : "Assessment saved.");
      if (options.redirect && BASE_URL) {
        window.setTimeout(function () { window.location.href = BASE_URL; }, 700);
      }
    }).catch(function (err) {
      showToast("Could not save the assessment (" + err.message + ")", "error");
    }).then(function () {
      buttons.forEach(function (btn) { btn.disabled = isLocked; });
    });
  }

  function initSaveActions() {
    var saveBtn = document.getElementById("btnSaveAssessment");
    if (saveBtn) { saveBtn.addEventListener("click", function () { saveAssessment({}); }); }

    var saveAndQueue = document.getElementById("saveAssessmentBtn");
    if (saveAndQueue) { saveAndQueue.addEventListener("click", function () { saveAssessment({ redirect: true }); }); }

    var draftBtn = document.getElementById("saveDraftBtn");
    if (draftBtn) { draftBtn.addEventListener("click", function () { saveAssessment({}); }); }

    var urgentBtn = document.getElementById("markUrgentBtn");
    if (urgentBtn) { urgentBtn.addEventListener("click", function () { saveAssessment({ urgent: true }); }); }

    var editBtn = document.getElementById("btnEditAssessment");
    if (editBtn) {
      editBtn.addEventListener("click", function () {
        setLocked(false);
        showToast("Edit mode — update the assessment and save.");
        var first = form ? form.querySelector("input:not([type=hidden]), textarea, select") : null;
        if (first) { first.focus(); }
      });
    }

    var cancelBtn = document.getElementById("cancelAssessmentBtn");
    if (cancelBtn) {
      cancelBtn.addEventListener("click", function () {
        window.location.href = BASE_URL || window.location.pathname;
      });
    }
  }

  /* ───────────────────── Patient pickers ───────────────────── */
  function patientRow(item) {
    var row = document.createElement("a");
    row.className = "ia-patient";
    row.href = item.url;
    row.setAttribute("data-name", item.search || "");
    row.setAttribute("data-id", item.id);
    row.innerHTML =
      '<span class="ia-patient-avatar">' + escapeHtml(item.initials || "PT") + "</span>" +
      '<span class="ia-patient-info">' +
        '<span class="ia-patient-name">' + escapeHtml(item.name || "—") +
          (item.emergency ? '<span class="ia-chip ia-chip-urgent ia-chip-xs">Emergency</span>' : "") +
        "</span>" +
        '<span class="ia-patient-meta">' + escapeHtml(item.meta || "") + "</span>" +
      "</span>" +
      (item.has_assessment ? '<span class="ia-patient-done" title="Initial assessment recorded">✓</span>' : "");
    return row;
  }

  function filterPatientList(list, term, countEl, emptyEl) {
    var query = String(term || "").trim().toLowerCase();
    var visible = 0;
    $$(".ia-patient", list).forEach(function (item) {
      var haystack = item.getAttribute("data-name") || "";
      var match = !query || haystack.indexOf(query) !== -1;
      item.hidden = !match;
      if (match) { visible += 1; }
    });
    if (countEl) { countEl.textContent = visible; }
    if (emptyEl) { emptyEl.hidden = visible !== 0; }
    return visible;
  }

  function initPatientPickers() {
    $$("[data-patient-search-input]").forEach(function (input) {
      // The search box, the result list and the counter all live inside the
      // same picker container (the rail card or the search popup).
      var scope = input.closest("[data-patient-picker]") || document;
      var list = scope.querySelector("[data-patient-list]");
      if (!list) { return; }
      var countEl = scope.querySelector("[data-patient-count]");
      var emptyEl = scope.querySelector("[data-patient-empty]");
      var apiUrl = input.getAttribute("data-patient-api");
      var live = input.getAttribute("data-live-search") === "1" && !!apiUrl;
      var initialHtml = list.innerHTML;
      var timer = null;

      if (live) {
        var search = function () {
          var term = String(input.value || "").trim();
          if (!term) {
            list.innerHTML = initialHtml;
            filterPatientList(list, "", countEl, emptyEl);
            return;
          }
          fetch(apiUrl + "?q=" + encodeURIComponent(term), {
            credentials: "same-origin",
            headers: { "Accept": "application/json" }
          }).then(function (response) {
            if (!response.ok) { throw new Error("HTTP " + response.status); }
            return response.json();
          }).then(function (data) {
            var results = (data && data.results) || [];
            list.innerHTML = "";
            results.forEach(function (item) { list.appendChild(patientRow(item)); });
            if (countEl) { countEl.textContent = results.length; }
            if (emptyEl) { emptyEl.hidden = results.length !== 0; }
          }).catch(function () {
            showToast("Patient search is unavailable right now.", "error");
          });
        };

        input.addEventListener("input", function () {
          clearTimeout(timer);
          timer = setTimeout(search, 250);
        });
        var btn = scope.querySelector("[data-patient-search-btn]");
        if (btn) { btn.addEventListener("click", search); }
      } else {
        input.addEventListener("input", function () {
          filterPatientList(list, input.value, countEl, emptyEl);
        });
        var formEl = scope.querySelector("form");
        if (formEl) {
          formEl.addEventListener("submit", function () {
            if (!String(input.value || "").trim()) { filterPatientList(list, "", countEl, emptyEl); }
          });
        }
      }
    });
  }

  /* ───────────────────── Patient search popup ───────────────────── */
  function initPatientModal() {
    var modal = document.getElementById("iaPatientModal");
    if (!modal) { return; }
    var input = document.getElementById("iaModalSearchInput");
    var lastFocus = null;

    function open() {
      lastFocus = document.activeElement;
      modal.hidden = false;
      document.body.classList.add("ia-modal-open");
      if (input) {
        input.value = "";
        input.focus();
      }
    }

    function close() {
      modal.hidden = true;
      document.body.classList.remove("ia-modal-open");
      if (lastFocus && lastFocus.focus) { lastFocus.focus(); }
    }

    $$("[data-open-patient-modal]").forEach(function (btn) {
      btn.addEventListener("click", open);
    });

    var closeBtn = document.getElementById("iaPatientModalClose");
    if (closeBtn) { closeBtn.addEventListener("click", close); }

    var cancelBtn = document.getElementById("iaPatientModalCancel");
    if (cancelBtn) { cancelBtn.addEventListener("click", close); }

    modal.addEventListener("click", function (e) {
      if (e.target === modal) { close(); }
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !modal.hidden) { close(); }
    });
  }

  /* ───────────────────── Boot ──────────────────────────────── */
  function init() {
    initTabs();
    initComplaints();
    initConditions();
    initAllergies();
    initMedications();
    initSocialHistory();
    initOrders();
    initAI();
    initSaveActions();
    initPatientPickers();
    initPatientModal();

    ["height", "weight"].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) { el.addEventListener("input", calcBMI); }
    });

    var formEl = document.getElementById("assessmentForm");
    if (formEl) {
      formEl.addEventListener("input", function () {
        renderVitalsWarnings();
        updateSummary();
      });
    }

    calcBMI();
    renderVitalsWarnings();
    updateSummary();
    setLocked(ASSESSMENT.saved === true);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
