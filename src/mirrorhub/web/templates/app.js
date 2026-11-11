// No inline scripts are allowed by the CSP; keep global behavior here.
document.addEventListener("htmx:responseError", function (event) {
  var status = event.detail.xhr.status;
  if (status === 401 || status === 403) {
    window.location.reload();
  }
});
