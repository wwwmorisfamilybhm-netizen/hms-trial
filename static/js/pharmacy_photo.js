(function () {
  const input = document.getElementById("photoFile");
  const zone = document.getElementById("photoPasteZone");
  const preview = document.getElementById("localPhotoPreview");
  const image = document.getElementById("localPhotoImage");
  const status = document.getElementById("photoInputStatus");
  const button = document.getElementById("photoUploadButton");
  const link = document.getElementById("photoLink");
  if (!input || !zone) return;
  let imageUrl = "";

  function showSelection() {
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    preview.hidden = true;
    image.onload = null;
    image.onerror = null;
    image.removeAttribute("src");
    const file = input.files[0];
    button.value = "preview_upload";
    button.textContent = "Preview photo";
    button.disabled = false;
    if (!file) {
      status.textContent = "";
      return;
    }
    if (file.size > 5 * 1024 * 1024 || !["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
      status.textContent = "Choose a JPEG, PNG or WebP image up to 5 MB.";
      button.disabled = true;
      return;
    }
    imageUrl = URL.createObjectURL(file);
    image.onload = function () {
      preview.hidden = false;
      button.value = "upload";
      button.textContent = "Use this photo";
      status.textContent = "Check this photo against the medicine details, then approve it.";
    };
    image.onerror = function () {
      status.textContent = "This image could not be previewed. Choose another photo.";
      button.disabled = true;
    };
    image.src = imageUrl;
  }

  function acceptImage(file) {
    try {
      const transfer = new DataTransfer();
      transfer.items.add(file);
      input.files = transfer.files;
      showSelection();
    } catch {
      status.textContent = "Use Choose a photo to attach this image.";
    }
  }

  zone.addEventListener("paste", function (event) {
    const files = Array.from(event.clipboardData?.files || []);
    if (!files.length) {
      files.push(...Array.from(event.clipboardData?.items || []).map(function (item) {
        return item.kind === "file" ? item.getAsFile() : null;
      }).filter(Boolean));
    }
    const file = files.find(function (item) { return item.type.startsWith("image/"); });
    if (file) {
      event.preventDefault();
      acceptImage(file);
      return;
    }
    const text = event.clipboardData?.getData("text/plain").trim();
    if (text && text.startsWith("https://")) {
      event.preventDefault();
      link.value = text;
      link.focus();
    } else {
      status.textContent = "Copy the image itself, or paste an HTTPS product link.";
    }
  });
  zone.addEventListener("dragover", function (event) {
    event.preventDefault();
    zone.classList.add("is-dragging");
  });
  zone.addEventListener("dragleave", function () { zone.classList.remove("is-dragging"); });
  zone.addEventListener("drop", function (event) {
    event.preventDefault();
    zone.classList.remove("is-dragging");
    const file = event.dataTransfer?.files[0];
    if (file) {
      acceptImage(file);
    } else {
      const text = event.dataTransfer?.getData("text/plain").trim();
      if (text && text.startsWith("https://")) {
        link.value = text;
        link.focus();
      } else {
        status.textContent = "Drop an image file or paste an HTTPS product link.";
      }
    }
  });
  input.addEventListener("change", showSelection);
})();
