/* ============================================================
   Cams HMS — Initial Assessment (OPD) workstation interactions
   ============================================================
   Notes for future LLM integration
   ─────────────────────────────────────────────────────────────
   The MedGemma AI assistant below is intentionally structured so a real
   model backend can be dropped in without touching the UI:

   • MedGemmaAssistant.isLoading   — loading state (disables/animates UI)
   • MedGemmaAssistant.draftData   — { summary, differential, tests }
   • MedGemmaAssistant.onGenerateDraft() — build/refresh the draft
       (replace the body with `await fetch("/api/medgemma/draft", …)` )
   • MedGemmaAssistant.onAcceptDraft()   — insert the active tab's text
       into the Assessment Summary note
   ============================================================ */
(function () {
  "use strict";

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  /* ── Config from server (allergies list) ── */
  var configEl = document.getElementById("iaConfig");
  var ALLERGIES = [];
  try { ALLERGIES = JSON.parse(configEl.textContent || "[]"); } catch (e) { ALLERGIES = []; }

  /* ───────────────────────── Toast ───────────────────────── */
  var toast = $("#toast");
  var toastTimer = null;
  function showToast(message) {
    toast.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 12.5l2.5 2.5L16 9.5"/></svg>' + message;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.hidden = true; }, 2600);
  }

  /* ───────────────────── Workflow tabs ───────────────────── */
  var tabs = $$(".tab");
  var panels = $$(".panel");
  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      tabs.forEach(function (t) { t.classList.remove("active"); t.setAttribute("aria-selected", "false"); });
      panels.forEach(function (p) { p.hidden = true; p.classList.remove("active"); });
      tab.classList.add("active");
      tab.setAttribute("aria-selected", "true");
      var panel = $('.panel[data-panel="' + tab.dataset.tab + '"]');
      if (panel) { panel.hidden = false; panel.classList.add("active"); }
      if (tab.dataset.tab === "summary") refreshSummary();
    });
  });

  /* ───────────────────── Vitals: BMI + alerts ───────────────────── */
  function num(id) {
    var el = document.getElementById(id);
    var v = parseFloat(el && el.value);
    return isNaN(v) ? null : v;
  }

  function calcBMI() {
    var h = num("height");
    var w = num("weight");
    var bmi = $("#bmi");
    if (h && w && h > 0) {
      bmi.value = (w / Math.pow(h / 100, 2)).toFixed(1);
    } else {
      bmi.value = "—";
    }
    return bmi.value;
  }

  function vitalsSnapshot() {
    return {
      temp: num("temp"), pulse: num("pulse"), rr: num("rr"),
      bpSys: num("bpSys"), bpDia: num("bpDia"), spo2: num("spo2"),
      height: num("height"), weight: num("weight"), bmi: calcBMI(),
      rbs: num("rbs"), pain: $("#pain") ? $("#pain").value : "0"
    };
  }

  function complaintsText() {
    var rows = $$(".complaint-text").map(function (t) { return t.value.trim(); }).filter(Boolean);
    return rows.length ? rows.join("; ") : "Not recorded";
  }

  /* ───────────────────── Active clinical alerts ───────────────────── */
  function renderAlerts() {
    var v = vitalsSnapshot();
    var list = $("#alertsList");
    var alerts = [];

    // Verified patient allergies → red
    (ALLERGIES.length ? ALLERGIES : ["Penicillin"]).forEach(function (a) {
      alerts.push({ level: "red", title: a + " allergy", text: "Confirmed in patient record — verify before prescribing." });
    });

    // Out-of-range vitals
    if (v.temp !== null && v.temp >= 38.0) {
      alerts.push({ level: "red", title: "Fever — " + v.temp + "°C", text: "Temperature above normal range (≥ 38.0°C)." });
    } else if (v.temp !== null && v.temp < 36.0) {
      alerts.push({ level: "amber", title: "Low temperature — " + v.temp + "°C", text: "Below normal range (< 36.0°C)." });
    }
    if (v.spo2 !== null && v.spo2 < 95) {
      alerts.push({ level: "red", title: "Low SpO₂ — " + v.spo2 + "%", text: "Below 95% — consider oxygen support." });
    }
    if (v.pulse !== null && v.pulse > 100) {
      alerts.push({ level: "amber", title: "Tachycardia — " + v.pulse + "/min", text: "Pulse above normal range (> 100/min)." });
    }
    if (v.rr !== null && v.rr > 20) {
      alerts.push({ level: "amber", title: "Tachypnoea — " + v.rr + "/min", text: "Respiratory rate above 20/min." });
    }
    if (v.bpSys !== null && v.bpDia !== null && (v.bpSys >= 180 || v.bpDia >= 110)) {
      alerts.push({ level: "red", title: "Hypertensive — " + v.bpSys + "/" + v.bpDia, text: "Severely elevated blood pressure." });
    }
    if (v.rbs !== null && v.rbs >= 200) {
      alerts.push({ level: "amber", title: "Hyperglycaemia — " + v.rbs + " mg/dL", text: "Random blood sugar elevated." });
    }

    $("#alertCount").textContent = alerts.length;
    var pillCount = $("#pillAlertCount");
    if (pillCount) pillCount.textContent = alerts.length;
    list.innerHTML = alerts.map(function (a) {
      var icon = a.level === "red"
        ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2 20h20L12 3Z"/><path d="M12 9v5"/><circle cx="12" cy="17.2" r="1.1" fill="currentColor" stroke="none"/></svg>'
        : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>';
      return '<div class="alert-card ' + a.level + '">' + icon +
        '<div class="alert-body"><b>' + a.title + '</b><span>' + a.text + '</span></div></div>';
    }).join("");
  }

  /* Wire vitals inputs → recompute BMI + alerts + summary */
  ["temp", "pulse", "rr", "bpSys", "bpDia", "spo2", "height", "weight", "rbs"].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener("input", function () { calcBMI(); renderAlerts(); refreshSummary(); });
  });
  var painEl = $("#pain");
  if (painEl) painEl.addEventListener("change", function () { renderAlerts(); refreshSummary(); });

  /* ───────────────────── Complaints: add/remove ───────────────────── */
  $("#addComplaint").addEventListener("click", function () {
    var tpl = $("#complaintTemplate");
    var node = tpl.content.cloneNode(true);
    $("#complaintsWrap").appendChild(node);
    wireComplaintRemove($("#complaintsWrap").lastElementChild);
  });

  function wireComplaintRemove(row) {
    var btn = $(".complaint-remove", row);
    if (btn) {
      btn.addEventListener("click", function () {
        if ($$(".complaint-row").length <= 1) { showToast("At least one complaint is required."); return; }
        row.remove();
      });
    }
  }
  $$(".complaint-row").forEach(wireComplaintRemove);

  /* ───────────────────── Allergies: tags + modal ───────────────────── */
  var allergyModal = $("#allergyModal");
  var allergyInput = $("#allergyInput");

  function addAllergyTag(name) {
    name = (name || "").trim();
    if (!name) return;
    var tag = document.createElement("span");
    tag.className = "tag tag-danger";
    tag.innerHTML = '<span class="tag-text"></span><button type="button" class="tag-x" aria-label="Remove ' + name + '"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg></button>';
    tag.querySelector(".tag-text").textContent = name;
    tag.querySelector(".tag-x").addEventListener("click", function () { tag.remove(); refreshAllergySource(); renderAlerts(); });
    $("#allergyTags").appendChild(tag);
    refreshAllergySource();
    renderAlerts();
  }

  // Keep ALLERGIES in sync with the visible tags for alert generation.
  function refreshAllergySource() {
    ALLERGIES = $$("#allergyTags .tag-text").map(function (t) { return t.textContent; });
  }

  function openModal() { allergyModal.hidden = false; allergyInput.value = ""; allergyInput.focus(); }
  function closeModal() { allergyModal.hidden = true; }

  $("#addAllergy").addEventListener("click", openModal);
  $("#allergyCancel").addEventListener("click", closeModal);
  $("#allergyDismiss").addEventListener("click", closeModal);
  $("#allergyConfirm").addEventListener("click", function () {
    addAllergyTag(allergyInput.value);
    closeModal();
  });
  allergyInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { addAllergyTag(allergyInput.value); closeModal(); }
  });
  allergyModal.addEventListener("click", function (e) { if (e.target === allergyModal) closeModal(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeModal(); });

  /* ───────────────────── Alerts popup ───────────────────── */
  var alertsModal = $("#alertsModal");
  function openAlerts() { if (alertsModal) alertsModal.hidden = false; }
  function closeAlerts() { if (alertsModal) alertsModal.hidden = true; }
  var alertsPill = $("#openAlertsPill");
  if (alertsPill) alertsPill.addEventListener("click", openAlerts);
  var alertsClose = $("#alertsClose");
  if (alertsClose) alertsClose.addEventListener("click", closeAlerts);
  if (alertsModal) alertsModal.addEventListener("click", function (e) { if (e.target === alertsModal) closeAlerts(); });

  /* ───────────────────── Actions popup ───────────────────── */
  var actionsModal = $("#actionsModal");
  function openActions() { if (actionsModal) actionsModal.hidden = false; }
  function closeActions() { if (actionsModal) actionsModal.hidden = true; }
  var actionsPill = $("#openActionsPill");
  if (actionsPill) actionsPill.addEventListener("click", openActions);
  var actionsClose = $("#actionsClose");
  if (actionsClose) actionsClose.addEventListener("click", closeActions);
  if (actionsModal) actionsModal.addEventListener("click", function (e) { if (e.target === actionsModal) closeActions(); });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    if (alertsModal && !alertsModal.hidden) closeAlerts();
    if (actionsModal && !actionsModal.hidden) closeActions();
  });

  $$("#allergyTags .tag-x").forEach(function (btn) {
    btn.addEventListener("click", function () {
      btn.closest(".tag").remove();
      refreshAllergySource();
      renderAlerts();
    });
  });

  $$("#allergyQuickPicks .pick").forEach(function (pick) {
    pick.addEventListener("click", function () { allergyInput.value = pick.dataset.pick; });
  });

  /* ───────────────────── Medications: add/remove ───────────────────── */
  function wireRowDelete(row) {
    var btn = $(".row-del", row);
    if (btn) btn.addEventListener("click", function () { row.remove(); });
  }
  $$("#medTableBody tr").forEach(wireRowDelete);
  $("#addMedicine").addEventListener("click", function () {
    var node = $("#medRowTemplate").content.cloneNode(true);
    $("#medTableBody").appendChild(node);
    var rows = $$("#medTableBody tr");
    wireRowDelete(rows[rows.length - 1]);
    $(".med-name", rows[rows.length - 1]).focus();
  });

  /* ───────────────────── Social history "Others" ───────────────────── */
  var othersCheck = $("#socialOthersCheck");
  var othersInput = $("#socialOthers");
  if (othersCheck) {
    othersCheck.addEventListener("change", function () {
      othersInput.disabled = !othersCheck.checked;
      if (othersCheck.checked) othersInput.focus();
    });
  }

  /* ───────────────────── Assessment summary (live) ───────────────────── */
  function refreshSummary() {
    var v = vitalsSnapshot();
    var sVitals = $("#summaryVitals");
    if (sVitals) {
      var bmi = (v.bmi && v.bmi !== "—") ? v.bmi : "—";
      sVitals.textContent = "Temp " + (v.temp ?? "—") + "°C · Pulse " + (v.pulse ?? "—") + "/min · RR " +
        (v.rr ?? "—") + "/min · BP " + (v.bpSys ?? "—") + "/" + (v.bpDia ?? "—") +
        " · SpO₂ " + (v.spo2 ?? "—") + "% · BMI " + bmi;
    }
    var sComp = $("#summaryComplaints");
    if (sComp) sComp.textContent = complaintsText();
  }

  /* ───────────────────── MedGemma AI assistant ───────────────────── */
  var AI_TAB = "summary"; // summary | differential | tests
  var aiContent = $("#aiContent");

  // Markdown-lite renderer (bold, h4, bullet lists).
  function renderMarkdown(md) {
    var html = String(md)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    var lines = html.split("\n");
    var out = [], inList = false;
    lines.forEach(function (line) {
      var bullet = line.match(/^[-•]\s+(.*)/);
      if (bullet) {
        if (!inList) { out.push("<ul>"); inList = true; }
        out.push("<li>" + bullet[1] + "</li>");
      } else {
        if (inList) { out.push("</ul>"); inList = false; }
        if (line.trim() === "") { return; }
        out.push("<p>" + line + "</p>");
      }
    });
    if (inList) out.push("</ul>");
    return out.join("");
  }

  var MedGemmaAssistant = {
    isLoading: false,
    draftData: { summary: "", differential: "", tests: "" },

    // Local fallback (used when no provider is configured or the call fails).
    mockGenerate: function () {
      var v = vitalsSnapshot();
      var comp = complaintsText();
      var bmi = (v.bmi && v.bmi !== "—") ? v.bmi : "—";

      this.draftData.summary =
        "**Presenting complaint:** " + comp + ".\n" +
        "**Vitals:** Temp " + (v.temp ?? "—") + "°C, Pulse " + (v.pulse ?? "—") + "/min, RR " + (v.rr ?? "—") + "/min, BP " +
        (v.bpSys ?? "—") + "/" + (v.bpDia ?? "—") + " mmHg, SpO₂ " + (v.spo2 ?? "—") + "%, BMI " + bmi + ".\n" +
        "**Relevant history:** Type 2 diabetes and hypertension on treatment; allergic to " +
        (ALLERGIES.length ? ALLERGIES.join(", ") : "—") + ".\n" +
        "**Assessment:** Acute febrile illness with hyperglycaemia — review before prescribing.";

      this.draftData.differential =
        "1. **Viral fever** — most likely; supportive care advised.\n" +
        "2. **Acute bacterial upper respiratory infection** — consider if symptoms persist.\n" +
        "3. **Dengue / tropical infection** — evaluate platelet trend given geography.\n" +
        "4. **Uncontrolled diabetes** — RBS " + (v.rbs ?? "—") + " mg/dL warrants HbA1c and dose review.";

      this.draftData.tests =
        "- CBC with differential and platelet count\n" +
        "- Rapid malarial antigen / dengue NS1 (as clinically indicated)\n" +
        "- HbA1c and fasting lipid profile\n" +
        "- Urine routine and microscopy\n" +
        "- Chest X-ray if respiratory signs develop";

      return this.draftData;
    },

    // Real integration: calls the Django endpoint, which tries a configured
    // provider (OpenAI-compatible / Gemini) and falls back to the mock.
    onGenerateDraft: async function () {
      if (this.isLoading) return this.draftData;
      this.isLoading = true;
      this.renderLoading();

      var v = vitalsSnapshot();
      var comp = complaintsText();
      var csrfInput = document.querySelector('input[name=csrfmiddlewaretoken]');
      var csrf = csrfInput ? csrfInput.value : "";
      var payload = {
        vitals: v,
        complaints: comp,
        allergies: ALLERGIES.slice(),
        patientName: (document.querySelector(".banner-identity h2") || {}).textContent || ""
      };

      try {
        var res = await fetch("/api/medgemma/draft/", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
          body: JSON.stringify(payload),
        });
        if (!res.ok) throw new Error("HTTP " + res.status);
        var data = await res.json();
        if (data && data.summary && data.differential && data.tests) {
          this.draftData = { summary: data.summary, differential: data.differential, tests: data.tests };
          this.isLoading = false;
          this.render();
          showToast(data._fallback ? "AI unavailable — showing fallback draft." : "Draft generated by AI.");
          return this.draftData;
        }
        throw new Error("Invalid AI response");
      } catch (err) {
        this.mockGenerate();
        this.isLoading = false;
        this.render();
        showToast("Using local draft (AI endpoint unavailable).");
        return this.draftData;
      }
    },

    // Insert the active tab's text into the Assessment Summary note.
    onAcceptDraft: function () {
      var note = $("#summaryNote");
      if (!note) return;
      var text = this.draftData[AI_TAB] || this.draftData.summary;
      var current = note.value.trim();
      note.value = current ? current + "\n\n" + text : text;
      // Jump to the summary tab so the nurse can review the inserted note.
      var summaryTab = $('.tab[data-tab="summary"]');
      if (summaryTab) summaryTab.click();
      showToast("AI draft inserted into the assessment note.");
    },

    render: function () {
      var content = this.draftData[AI_TAB] || "";
      aiContent.classList.remove("is-loading");
      aiContent.innerHTML = renderMarkdown(content);
    },

    renderLoading: function () {
      aiContent.classList.add("is-loading");
      aiContent.innerHTML = '<span class="spinner"></span> Synthesising draft…';
    }
  };

  // Initial seed (also gives the static page useful default content).
  MedGemmaAssistant.onGenerateDraft();

  $$(".ai-tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      $$(".ai-tab").forEach(function (t) { t.classList.remove("active"); });
      tab.classList.add("active");
      AI_TAB = tab.dataset.ai;
      MedGemmaAssistant.render();
    });
  });

  $("#aiGenerate").addEventListener("click", function () {
    if (MedGemmaAssistant.isLoading) return;
    MedGemmaAssistant.onGenerateDraft();
  });

  $("#aiAccept").addEventListener("click", function () {
    MedGemmaAssistant.onAcceptDraft();
  });

  /* ───────────────────── Next action buttons ───────────────────── */
  $("#btnSendToQueue").addEventListener("click", function () {
    showToast("Patient sent to the Doctor Queue.");
  });
  $("#btnSavePool").addEventListener("click", function () {
    showToast("Assessment saved — patient kept in pool.");
  });
  $("#btnUrgent").addEventListener("click", function () {
    showToast("Patient marked as Urgent.");
  });
  $("#btnReschedule").addEventListener("click", function () {
    showToast("Reschedule appointment — workflow opened.");
  });
  $("#btnCancel").addEventListener("click", function () {
    showToast("Visit cancelled.");
  });

  /* ── Initial render ── */
  calcBMI();
  renderAlerts();
  refreshSummary();
})();
