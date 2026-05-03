/**
 * Passkey enrollment + step-up helpers for tbdtask (Phase 8a).
 *
 * Exposes window.tbdPasskey with:
 *   - register({optionsJson, formId})    — runs navigator.credentials.create()
 *   - verify({optionsJson, formId})      — runs navigator.credentials.get()
 *
 * Each helper:
 *   1. Decodes the server-issued options JSON and base64url fields.
 *   2. Calls the WebAuthn API with userVerification = "required".
 *   3. Probes for PRF support during register (optional extension).
 *   4. Encodes the response as JSON, drops it into a hidden form field
 *      called ``response_json``, and submits the form.
 *
 * The PRF *output* — when present — is read for the side-effect of
 * detection, then discarded. No PRF bytes ever leave the browser tab
 * (8a captures only the boolean fact of support; 8b will derive the
 * KEK in-tab without persisting). This rule is load-bearing; do not
 * console.log or transmit prf.results.first.
 *
 * The strict CSP (no inline script/style) requires this file to be
 * loaded by URL with SRI; templates pin the bundle hash.
 */
(function () {
  "use strict";

  // ---------- base64url helpers ----------

  function b64uToBytes(s) {
    const pad = "=".repeat((4 - (s.length % 4)) % 4);
    const b64 = (s + pad).replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(b64);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out.buffer;
  }

  function bytesToB64u(buf) {
    const bytes = new Uint8Array(buf);
    let s = "";
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  // The server returns options as a JSON blob shaped like the
  // PublicKeyCredentialCreationOptions spec, with all binary fields
  // already encoded as base64url. Re-hydrate those fields to ArrayBuffers.

  function decodeRegistrationOptions(opts) {
    opts.challenge = b64uToBytes(opts.challenge);
    if (opts.user && typeof opts.user.id === "string") {
      opts.user.id = b64uToBytes(opts.user.id);
    }
    if (Array.isArray(opts.excludeCredentials)) {
      opts.excludeCredentials.forEach((c) => {
        if (typeof c.id === "string") c.id = b64uToBytes(c.id);
      });
    }
    // PRF probe: the server sets `extensions.prf.eval.first` to a
    // 32-byte zero string we ignore the bytes of. Decode so the
    // browser hands the request to the authenticator correctly.
    if (opts.extensions && opts.extensions.prf && opts.extensions.prf.eval) {
      const ev = opts.extensions.prf.eval;
      if (typeof ev.first === "string") ev.first = b64uToBytes(ev.first);
      if (typeof ev.second === "string") ev.second = b64uToBytes(ev.second);
    }
    return opts;
  }

  function decodeAssertionOptions(opts) {
    opts.challenge = b64uToBytes(opts.challenge);
    if (Array.isArray(opts.allowCredentials)) {
      opts.allowCredentials.forEach((c) => {
        if (typeof c.id === "string") c.id = b64uToBytes(c.id);
      });
    }
    return opts;
  }

  // ---------- response encoding ----------

  function encodeRegistrationResponse(cred) {
    const r = cred.response;
    const ext = cred.getClientExtensionResults
      ? cred.getClientExtensionResults()
      : {};
    // Determine PRF support from extension results. Two shapes are
    // possible: { prf: { results: { first: ... } } } means the
    // authenticator returned an output, and { prf: { enabled: true } }
    // means it acknowledged the extension on registration. Treat
    // either as "supported".
    let prfEnabled = false;
    if (ext && ext.prf) {
      if (ext.prf.enabled === true) prfEnabled = true;
      if (ext.prf.results && ext.prf.results.first) prfEnabled = true;
    }
    return {
      id: cred.id,
      rawId: bytesToB64u(cred.rawId),
      type: cred.type,
      response: {
        clientDataJSON: bytesToB64u(r.clientDataJSON),
        attestationObject: bytesToB64u(r.attestationObject),
        transports:
          typeof r.getTransports === "function" ? r.getTransports() : [],
      },
      // We send back ONLY the boolean PRF flag — never the bytes.
      clientExtensionResults: { prf: { enabled: prfEnabled } },
    };
  }

  function encodeAssertionResponse(cred) {
    const r = cred.response;
    return {
      id: cred.id,
      rawId: bytesToB64u(cred.rawId),
      type: cred.type,
      response: {
        clientDataJSON: bytesToB64u(r.clientDataJSON),
        authenticatorData: bytesToB64u(r.authenticatorData),
        signature: bytesToB64u(r.signature),
        userHandle: r.userHandle ? bytesToB64u(r.userHandle) : null,
      },
    };
  }

  // ---------- public API ----------

  async function register(formEl, optionsJson) {
    const opts = decodeRegistrationOptions(JSON.parse(optionsJson));
    let cred;
    try {
      cred = await navigator.credentials.create({ publicKey: opts });
    } catch (err) {
      _renderError(formEl, "Could not register the passkey: " + err.message);
      return;
    }
    if (!cred) {
      _renderError(formEl, "No credential returned from authenticator.");
      return;
    }
    const payload = encodeRegistrationResponse(cred);
    _injectResponseAndSubmit(formEl, payload);
  }

  async function verify(formEl, optionsJson) {
    const opts = decodeAssertionOptions(JSON.parse(optionsJson));
    let cred;
    try {
      cred = await navigator.credentials.get({ publicKey: opts });
    } catch (err) {
      _renderError(formEl, "Could not verify the passkey: " + err.message);
      return;
    }
    if (!cred) {
      _renderError(formEl, "No credential returned from authenticator.");
      return;
    }
    const payload = encodeAssertionResponse(cred);
    _injectResponseAndSubmit(formEl, payload);
  }

  function _injectResponseAndSubmit(formEl, payload) {
    let input = formEl.querySelector('input[name="response_json"]');
    if (!input) {
      input = document.createElement("input");
      input.type = "hidden";
      input.name = "response_json";
      formEl.appendChild(input);
    }
    input.value = JSON.stringify(payload);
    formEl.submit();
  }

  function _renderError(formEl, msg) {
    let box = formEl.querySelector("[data-passkey-error]");
    if (!box) {
      box = document.createElement("div");
      box.setAttribute("data-passkey-error", "");
      box.className = "auth-empty";
      formEl.prepend(box);
    }
    box.textContent = msg;
  }

  // ---------- DOM wire-up ----------

  function bind() {
    document.querySelectorAll("[data-passkey-register]").forEach((btn) => {
      const formEl = btn.closest("form");
      if (!formEl) return;
      const optionsJson = formEl.getAttribute("data-options-json") || "{}";
      btn.addEventListener("click", function (ev) {
        ev.preventDefault();
        register(formEl, optionsJson);
      });
    });
    document.querySelectorAll("[data-passkey-verify]").forEach((btn) => {
      const formEl = btn.closest("form");
      if (!formEl) return;
      const optionsJson = formEl.getAttribute("data-options-json") || "{}";
      btn.addEventListener("click", function (ev) {
        ev.preventDefault();
        verify(formEl, optionsJson);
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }

  // Expose for tests / programmatic use.
  window.tbdPasskey = { register: register, verify: verify };
})();
