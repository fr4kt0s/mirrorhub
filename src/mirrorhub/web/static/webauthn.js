// Security keys (FIDO2/WebAuthn): register in Settings, log in with or after the password.
// No inline scripts (CSP); the server sends WebAuthn options as JSON with base64url fields.
(function () {
  "use strict";

  function toBytes(value) {
    var base64 = value.replace(/-/g, "+").replace(/_/g, "/");
    while (base64.length % 4) {
      base64 += "=";
    }
    var binary = window.atob(base64);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes.buffer;
  }

  function toText(buffer) {
    var bytes = new Uint8Array(buffer);
    var binary = "";
    for (var i = 0; i < bytes.length; i += 1) {
      binary += String.fromCharCode(bytes[i]);
    }
    return window.btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function csrfToken() {
    try {
      return JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRF-Token"] || "";
    } catch (error) {
      return "";
    }
  }

  function post(url, body) {
    return window.fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().catch(function () {
        return { error: "Unexpected answer from the server (HTTP " + response.status + ")" };
      }).then(function (data) {
        if (!response.ok) {
          throw new Error(data.error || "HTTP " + response.status);
        }
        return data;
      });
    });
  }

  function creationOptions(options) {
    options.challenge = toBytes(options.challenge);
    options.user.id = toBytes(options.user.id);
    (options.excludeCredentials || []).forEach(function (item) {
      item.id = toBytes(item.id);
    });
    return options;
  }

  function requestOptions(options) {
    options.challenge = toBytes(options.challenge);
    (options.allowCredentials || []).forEach(function (item) {
      item.id = toBytes(item.id);
    });
    return options;
  }

  function credentialJson(credential) {
    var response = credential.response;
    var out = {
      id: credential.id,
      rawId: toText(credential.rawId),
      type: credential.type,
      response: { clientDataJSON: toText(response.clientDataJSON) },
      clientExtensionResults: credential.getClientExtensionResults
        ? credential.getClientExtensionResults()
        : {}
    };
    if (credential.authenticatorAttachment) {
      out.authenticatorAttachment = credential.authenticatorAttachment;
    }
    if (response.attestationObject) {
      out.response.attestationObject = toText(response.attestationObject);
      if (response.getTransports) {
        out.response.transports = response.getTransports();
      }
    } else {
      out.response.authenticatorData = toText(response.authenticatorData);
      out.response.signature = toText(response.signature);
      out.response.userHandle = response.userHandle ? toText(response.userHandle) : null;
    }
    return out;
  }

  function status(scope, message) {
    var element = scope.querySelector("[data-webauthn-status]") ||
      document.querySelector("[data-webauthn-status]");
    if (!element) {
      return;
    }
    element.textContent = message || "";
    element.hidden = !message;
  }

  function explain(error) {
    if (error && error.name === "NotAllowedError") {
      return "Cancelled or timed out (no touch, wrong PIN, or the key is not registered).";
    }
    if (error && error.name === "SecurityError") {
      return "The browser refused: open MirrorHub by its host name over HTTPS with a trusted certificate.";
    }
    if (error && error.name === "InvalidStateError") {
      return "This key is already registered.";
    }
    return (error && error.message) || String(error);
  }

  function supported(scope) {
    if (window.PublicKeyCredential && navigator.credentials) {
      return true;
    }
    status(scope, "This browser or connection does not support security keys (HTTPS with a trusted certificate is required).");
    return false;
  }

  function finishLogin(scope, options, button) {
    status(scope, "");
    if (button) {
      button.disabled = true;
    }
    return navigator.credentials.get({ publicKey: requestOptions(options) })
      .then(function (credential) {
        return post("/login/key", { credential: credentialJson(credential) });
      })
      .then(function (data) {
        window.location.assign(data.redirect || "/");
      })
      .catch(function (error) {
        status(scope, explain(error));
        if (button) {
          button.disabled = false;
        }
      });
  }

  function setupLogin(button) {
    var scope = button.parentElement || document;
    button.addEventListener("click", function () {
      if (!supported(scope)) {
        return;
      }
      button.disabled = true;
      post("/login/key/options", {})
        .then(function (data) {
          return finishLogin(scope, data.options, button);
        })
        .catch(function (error) {
          status(scope, explain(error));
          button.disabled = false;
        });
    });
  }

  function setupSecond(element) {
    var button = element.querySelector("[data-webauthn-second-start]");
    var scope = element.parentElement || document;
    var used = false;
    function start() {
      if (used || !supported(scope)) {
        return;
      }
      used = true;  // the challenge is single-use; after a failure log in again
      var options = JSON.parse(element.getAttribute("data-options"));
      finishLogin(scope, options, button).then(function () {
        if (button) {
          button.disabled = true;
        }
      });
    }
    if (button) {
      button.addEventListener("click", start);
    }
  }

  function setupRegister(form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!supported(form)) {
        return;
      }
      var button = form.querySelector("button[type=submit]");
      var name = form.querySelector("input[name=name]").value;
      var password = form.querySelector("input[name=password]");
      status(form, "");
      button.disabled = true;
      post("/settings/security-keys/options", { name: name, password: password.value })
        .then(function (data) {
          password.value = "";
          return navigator.credentials.create({ publicKey: creationOptions(data.options) });
        })
        .then(function (credential) {
          return post("/settings/security-keys", { credential: credentialJson(credential) });
        })
        .then(function (data) {
          window.location.assign(data.redirect || "/settings");
          window.location.reload();
        })
        .catch(function (error) {
          status(form, explain(error));
          button.disabled = false;
        });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-webauthn-login]").forEach(setupLogin);
    document.querySelectorAll("[data-webauthn-second]").forEach(setupSecond);
    document.querySelectorAll("form[data-webauthn-register]").forEach(setupRegister);
  });
})();
