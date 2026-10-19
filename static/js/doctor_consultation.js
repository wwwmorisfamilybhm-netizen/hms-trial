/* ============================================================
   Cams Hospital HMS — Doctor Consultation JavaScript Engine
   ============================================================ */

document.addEventListener("DOMContentLoaded", function () {
  const csrfToken = getCsrfToken();
  const patientId = document.getElementById("dcPatientId") ? document.getElementById("dcPatientId").value : null;
  const consultationId = document.getElementById("dcConsultationId") ? document.getElementById("dcConsultationId").value : null;

  // ── Tab Switching ──
  const tabButtons = document.querySelectorAll(".dc-tab-btn");
  const tabContents = document.querySelectorAll(".dc-tab-content");
  let currentTabIndex = 1;

  function switchTab(tabIndex) {
    currentTabIndex = parseInt(tabIndex);
    tabButtons.forEach(btn => {
      btn.classList.toggle("active", parseInt(btn.dataset.tab) === currentTabIndex);
    });
    tabContents.forEach(content => {
      content.classList.toggle("active", parseInt(content.dataset.tab) === currentTabIndex);
    });

    // Update forward button label
    const forwardBtn = document.getElementById("btnSaveContinue");
    if (forwardBtn) {
      if (currentTabIndex >= 7) {
        forwardBtn.innerHTML = "Complete Consultation & Save →";
        forwardBtn.dataset.action = "complete";
      } else {
        forwardBtn.innerHTML = "Save & Continue →";
        forwardBtn.dataset.action = "continue";
      }
    }
  }

  tabButtons.forEach(btn => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  // ── Forward Button / Lifecycle ──
  const btnSaveContinue = document.getElementById("btnSaveContinue");
  if (btnSaveContinue) {
    btnSaveContinue.addEventListener("click", function () {
      if (this.dataset.action === "complete") {
        saveConsultation("complete", true);
      } else {
        saveConsultation("save_draft", false);
        if (currentTabIndex < 7) {
          switchTab(currentTabIndex + 1);
        }
      }
    });
  }

  const btnSaveDraft = document.getElementById("btnSaveDraft");
  if (btnSaveDraft) {
    btnSaveDraft.addEventListener("click", () => saveConsultation("save_draft", false));
  }

  const btnEndConsultation = document.getElementById("btnEndConsultation");
  if (btnEndConsultation) {
    btnEndConsultation.addEventListener("click", function () {
      if (confirm("Are you sure you want to end and complete this consultation?")) {
        saveConsultation("complete", true);
      }
    });
  }

  // ── Prescription State Management ──
  let prescriptions = [];
  const prescriptionTableBody = document.querySelector("#currentPrescriptionTable tbody");

  // Load existing prescriptions from initial page data
  if (window.INITIAL_PRESCRIPTIONS && Array.isArray(window.INITIAL_PRESCRIPTIONS)) {
    prescriptions = [...window.INITIAL_PRESCRIPTIONS];
    renderPrescriptions();
  }

  function renderPrescriptions() {
    if (!prescriptionTableBody) return;
    prescriptionTableBody.innerHTML = "";

    if (prescriptions.length === 0) {
      prescriptionTableBody.innerHTML = `<tr><td colspan="8" class="dc-empty">No medications prescribed yet. Search formulary or load a preset.</td></tr>`;
      updatePrescriptionPreview();
      checkSafety();
      return;
    }

    prescriptions.forEach((item, idx) => {
      const row = document.createElement("tr");
      row.innerHTML = `
        <td>${idx + 1}</td>
        <td><strong>${escapeHtml(item.medicine_name)}</strong></td>
        <td>${escapeHtml(item.dose || "—")}</td>
        <td>${escapeHtml(item.route || "Oral")}</td>
        <td><span class="dc-pill dc-pill-neutral">${escapeHtml(item.frequency || "OD")}</span></td>
        <td>${escapeHtml(item.duration || "5 days")}</td>
        <td><span style="font-size:12px;color:var(--dc-muted);">${escapeHtml(item.instructions || "—")}</span></td>
        <td style="text-align:right;">
          <button type="button" class="btn-del-rx" data-index="${idx}" style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:14px;" title="Remove medicine">✕</button>
        </td>
      `;
      prescriptionTableBody.appendChild(row);
    });

    // Bind delete buttons
    document.querySelectorAll(".btn-del-rx").forEach(btn => {
      btn.addEventListener("click", function () {
        const index = parseInt(this.dataset.index);
        prescriptions.splice(index, 1);
        renderPrescriptions();
      });
    });

    updatePrescriptionPreview();
    checkSafety();
  }

  function updatePrescriptionPreview() {
    const previewContainer = document.getElementById("prescriptionPreviewList");
    if (!previewContainer) return;
    if (prescriptions.length === 0) {
      previewContainer.innerHTML = `<p style="color:var(--dc-muted);font-size:13px;margin:0;">No items on prescription.</p>`;
      return;
    }

    let html = `<ol style="margin:0;padding-left:20px;font-size:13px;line-height:1.6;">`;
    prescriptions.forEach(p => {
      html += `<li><strong>${escapeHtml(p.medicine_name)}</strong> ${escapeHtml(p.dose || "")} — ${escapeHtml(p.route || "Oral")}, ${escapeHtml(p.frequency || "OD")}, for ${escapeHtml(p.duration || "5 days")}. <em style="color:var(--dc-muted);">(${escapeHtml(p.instructions || "As directed")})</em></li>`;
    });
    html += `</ol>`;
    previewContainer.innerHTML = html;
  }

  // ── Drug Formulary Search & Add ──
  const drugSearchInput = document.getElementById("drugSearchInput");
  const drugCategoryPills = document.querySelectorAll(".dc-cat-pill");
  const drugResultsList = document.getElementById("drugResultsList");
  let activeDrugCategory = "Common";

  if (drugSearchInput) {
    drugSearchInput.addEventListener("input", debounce(fetchFormularyDrugs, 200));
  }

  drugCategoryPills.forEach(pill => {
    pill.addEventListener("click", function () {
      drugCategoryPills.forEach(p => p.classList.remove("active"));
      this.classList.add("active");
      activeDrugCategory = this.dataset.cat;
      fetchFormularyDrugs();
    });
  });

  function fetchFormularyDrugs() {
    if (!drugResultsList) return;
    const q = drugSearchInput ? drugSearchInput.value.trim() : "";
    fetch(`/api/consultation/drug-search/?q=${encodeURIComponent(q)}&cat=${encodeURIComponent(activeDrugCategory)}`)
      .then(r => r.json())
      .then(data => {
        if (!data.ok || !data.drugs) return;
        renderDrugList(data.drugs);
      });
  }

  function renderDrugList(drugs) {
    if (!drugResultsList) return;
    drugResultsList.innerHTML = "";
    if (drugs.length === 0) {
      drugResultsList.innerHTML = `<div style="padding:12px;text-align:center;color:var(--dc-muted);font-size:12px;">No matching medicines found.</div>`;
      return;
    }

    drugs.forEach(d => {
      const row = document.createElement("div");
      row.className = "dc-drug-row";
      row.innerHTML = `
        <div>
          <div style="font-weight:700;color:var(--dc-ink);">${escapeHtml(d.name)}</div>
          <div style="font-size:11.5px;color:var(--dc-muted);">${escapeHtml(d.generic)} · ${escapeHtml(d.dose)}</div>
        </div>
        <button type="button" class="dc-btn dc-btn-outline dc-btn-sm btn-quick-add-drug" data-drug='${JSON.stringify(d).replace(/'/g, "&apos;")}'>+ Add</button>
      `;
      drugResultsList.appendChild(row);
    });

    document.querySelectorAll(".btn-quick-add-drug").forEach(btn => {
      btn.addEventListener("click", function () {
        const drugData = JSON.parse(this.dataset.drug);
        prescriptions.push({
          medicine_name: drugData.name,
          dose: drugData.dose || "",
          route: drugData.route || "Oral",
          frequency: drugData.default_freq || "OD",
          duration: drugData.default_dur || "5 days",
          instructions: drugData.instructions || "",
        });
        renderPrescriptions();
      });
    });
  }

  // ── "Copy from Previous Visit" ──
  const btnCopyPreviousRx = document.getElementById("btnCopyPreviousRx");
  if (btnCopyPreviousRx) {
    btnCopyPreviousRx.addEventListener("click", function () {
      if (window.PREVIOUS_PRESCRIPTIONS && Array.isArray(window.PREVIOUS_PRESCRIPTIONS) && window.PREVIOUS_PRESCRIPTIONS.length > 0) {
        prescriptions = [...prescriptions, ...window.PREVIOUS_PRESCRIPTIONS];
        renderPrescriptions();
        alert("Copied medications from previous encounter.");
      } else {
        alert("No previous prescription items on record for this patient.");
      }
    });
  }

  // ── Clinical Safety Check ──
  function checkSafety() {
    const alertBox = document.getElementById("safetyAlertCard");
    if (!alertBox || !patientId) return;

    fetch("/api/consultation/safety-check/", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
      body: JSON.stringify({
        patient_id: patientId,
        medicines: prescriptions.map(p => p.medicine_name),
      }),
    })
      .then(r => r.json())
      .then(data => {
        if (!data.ok) return;
        if (data.allergy_alerts && data.allergy_alerts.length > 0) {
          alertBox.style.display = "block";
          alertBox.className = "dc-alert-box";
          alertBox.style.background = "#fef2f2";
          alertBox.style.borderColor = "#fecaca";
          alertBox.innerHTML = `
            <div class="dc-alert-title" style="color:#dc2626;">⚠️ Contraindication / Allergy Alert</div>
            ${data.allergy_alerts.map(a => `<div style="font-size:12.5px;color:#991b1b;margin-bottom:4px;">${escapeHtml(a)}</div>`).join("")}
          `;
        } else {
          alertBox.style.display = "block";
          alertBox.style.background = "#ecfdf5";
          alertBox.style.borderColor = "#a7f3d0";
          alertBox.innerHTML = `
            <div style="color:#059669;font-weight:700;display:flex;align-items:center;gap:6px;font-size:12.5px;">
              ✓ No major drug-drug or drug-allergy interactions detected.
            </div>
          `;
        }
      });
  }

  // ── ICD-10 Search / Select ──
  const primaryIcdInput = document.getElementById("primaryIcdInput");
  const primaryDiagnosisInput = document.getElementById("primaryDiagnosisInput");
  const icdDropdown = document.getElementById("icdDropdownResults");

  if (primaryDiagnosisInput) {
    primaryDiagnosisInput.addEventListener("input", debounce(function () {
      const q = this.value.trim();
      if (!q || q.length < 2) {
        if (icdDropdown) icdDropdown.style.display = "none";
        return;
      }
      fetch(`/api/consultation/icd10-search/?q=${encodeURIComponent(q)}`)
        .then(r => r.json())
        .then(data => {
          if (!data.ok || !data.results || !icdDropdown) return;
          icdDropdown.innerHTML = "";
          data.results.forEach(item => {
            const opt = document.createElement("div");
            opt.style.padding = "8px 12px";
            opt.style.cursor = "pointer";
            opt.style.fontSize = "12.5px";
            opt.style.borderBottom = "1px solid #f1f5f9";
            opt.innerHTML = `<strong>${item.code}</strong> — ${item.desc}`;
            opt.addEventListener("click", () => {
              primaryDiagnosisInput.value = item.desc;
              if (primaryIcdInput) primaryIcdInput.value = item.code;
              icdDropdown.style.display = "none";
            });
            icdDropdown.appendChild(opt);
          });
          icdDropdown.style.display = "block";
        });
    }, 250));
  }

  // ── PACS / Image Viewer Controls ──
  let currentZoom = 1;
  let currentRotation = 0;
  let isInverted = false;
  const radiologyImg = document.getElementById("radiologyMainImg");

  const btnZoomIn = document.getElementById("btnViewerZoomIn");
  const btnZoomOut = document.getElementById("btnViewerZoomOut");
  const btnRotate = document.getElementById("btnViewerRotate");
  const btnInvert = document.getElementById("btnViewerInvert");
  const btnReset = document.getElementById("btnViewerReset");

  function applyImageTransforms() {
    if (!radiologyImg) return;
    radiologyImg.style.transform = `scale(${currentZoom}) rotate(${currentRotation}deg)`;
    radiologyImg.style.filter = isInverted ? "invert(100%)" : "none";
  }

  if (btnZoomIn) {
    btnZoomIn.addEventListener("click", () => { currentZoom = Math.min(currentZoom + 0.2, 3); applyImageTransforms(); });
  }
  if (btnZoomOut) {
    btnZoomOut.addEventListener("click", () => { currentZoom = Math.max(currentZoom - 0.2, 0.5); applyImageTransforms(); });
  }
  if (btnRotate) {
    btnRotate.addEventListener("click", () => { currentRotation = (currentRotation + 90) % 360; applyImageTransforms(); });
  }
  if (btnInvert) {
    btnInvert.addEventListener("click", () => { isInverted = !isInverted; applyImageTransforms(); });
  }
  if (btnReset) {
    btnReset.addEventListener("click", () => { currentZoom = 1; currentRotation = 0; isInverted = false; applyImageTransforms(); });
  }

  // ── Print Prescription ──
  const btnPrintPrescription = document.getElementById("btnPrintPrescription");
  if (btnPrintPrescription) {
    btnPrintPrescription.addEventListener("click", () => window.print());
  }

  // ── Master Save Consultation ──
  function saveConsultation(action = "save_draft", redirectOnComplete = false) {
    if (!patientId) return;

    const payload = {
      patient_id: patientId,
      consultation_id: consultationId,
      action: action,
      visit_type: document.querySelector("input[name='visit_type']:checked") ? document.querySelector("input[name='visit_type']:checked").value : "new_problem",
      clinical_note: document.getElementById("clinicalNoteText") ? document.getElementById("clinicalNoteText").value : "",
      diagnosis_primary: primaryDiagnosisInput ? primaryDiagnosisInput.value.trim() : "",
      diagnosis_primary_icd: primaryIcdInput ? primaryIcdInput.value.trim() : "",
      treatment_plan: document.getElementById("treatmentPlanText") ? document.getElementById("treatmentPlanText").value : "",
      special_advice: document.getElementById("specialAdviceText") ? document.getElementById("specialAdviceText").value : "",
      mark_follow_up: document.getElementById("markFollowUpCheck") ? document.getElementById("markFollowUpCheck").checked : false,
      follow_up_after: document.getElementById("followUpDurationSelect") ? document.getElementById("followUpDurationSelect").value : "",
      prescriptions: prescriptions,
      orders: gatherActiveOrders(),
    };

    fetch("/api/consultation/save/", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
      body: JSON.stringify(payload),
    })
      .then(r => r.json())
      .then(data => {
        if (data.ok) {
          showToast(action === "complete" ? "Consultation completed and saved." : "Consultation draft saved.");
          if (redirectOnComplete) {
            setTimeout(() => {
              window.location.href = "/doctor/patient-queue/";
            }, 800);
          }
        } else {
          alert("Error saving consultation: " + (data.error || "Unknown error"));
        }
      })
      .catch(err => {
        console.error(err);
        alert("Network error while saving.");
      });
  }

  function gatherActiveOrders() {
    const orders = [];
    document.querySelectorAll(".order-checkbox:checked").forEach(cb => {
      orders.push({
        order_type: cb.dataset.type || "laboratory",
        name: cb.dataset.name,
        status: "Ordered",
      });
    });
    return orders;
  }

  // Helper utilities
  function getCsrfToken() {
    const match = document.cookie.match(/csrftoken=([^;]+)/);
    return match ? match[1] : "";
  }

  function debounce(func, wait) {
    let timeout;
    return function (...args) {
      clearTimeout(timeout);
      timeout = setTimeout(() => func.apply(this, args), wait);
    };
  }

  function escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function showToast(msg) {
    let toast = document.getElementById("dcToast");
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "dcToast";
      toast.style.position = "fixed";
      toast.style.bottom = "80px";
      toast.style.right = "24px";
      toast.style.background = "#0f172a";
      toast.style.color = "#fff";
      toast.style.padding = "10px 18px";
      toast.style.borderRadius = "8px";
      toast.style.fontSize = "13px";
      toast.style.fontWeight = "600";
      toast.style.zIndex = "1000";
      toast.style.boxShadow = "0 4px 12px rgba(0,0,0,0.15)";
      document.body.appendChild(toast);
    }
    toast.textContent = msg;
    toast.style.display = "block";
    setTimeout(() => { toast.style.display = "none"; }, 2500);
  }
});

