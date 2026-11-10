// No inline scripts are allowed by the CSP; keep global behavior here.
document.addEventListener("htmx:responseError", function (event) {
  var status = event.detail.xhr.status;
  if (status === 401 || status === 403) {
    window.location.reload();
  }
});

document.addEventListener("DOMContentLoaded", function () {
  var toolSelect = document.getElementById("tool-select");
  var methodSelect = document.getElementById("method-select");
  var methodField = document.getElementById("method-field");
  if (!toolSelect || !methodSelect || !methodField) {
    return;
  }

  function updateMethod() {
    if (toolSelect.value === "rsync") {
      methodSelect.value = "rsync";
      methodField.hidden = true;
    } else {
      methodField.hidden = false;
      if (methodSelect.value === "rsync") {
        methodSelect.value = "https";
      }
    }
  }

  function updateToolSections() {
    var tool = toolSelect.value;
    var sections = document.querySelectorAll("[data-tool-section]");
    for (var index = 0; index < sections.length; index += 1) {
      var section = sections[index];
      var active = section.dataset.toolSection === tool;
      section.hidden = !active;
      var fields = section.querySelectorAll("input, select, textarea");
      for (var fieldIndex = 0; fieldIndex < fields.length; fieldIndex += 1) {
        fields[fieldIndex].disabled = !active;
      }
    }
    updateMethod();
  }

  toolSelect.addEventListener("change", updateToolSections);
  updateToolSections();
});
