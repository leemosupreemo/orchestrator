"use strict";
// Figma sends the person back here with ?code=… once they click Allow.
// We hand it to the app (same origin, using sessionStorage) and return to Connections, which exchanges and saves it.
(function () {
  var params = new URLSearchParams(location.search);
  var code = params.get("code");
  var error = params.get("error") || params.get("error_description");
  var state = params.get("state");
  if (error) {
    document.getElementById("status").textContent = "Figma authorization was canceled or failed: " + error + ". Go back and try again.";
    return;
  }
  if (!code) {
    document.getElementById("status").textContent = "Figma didn't send an authorization code. Go back and try again.";
    return;
  }
  var pkce = null;
  try {
    var raw = sessionStorage.getItem("orchestrator_figma_pkce");
    if (raw) pkce = JSON.parse(raw);
  } catch (e) { /* storage blocked */ }
  var redirectUri = new URL("figma-auth.html", location.href).href;
  try {
    sessionStorage.setItem("orchestrator_figma_auth", JSON.stringify({
      code: code,
      state: state,
      verifier: pkce ? pkce.verifier : "",
      clientId: pkce ? pkce.clientId : "",
      redirectUri: redirectUri
    }));
  } catch (e) { /* storage blocked */ }
  location.replace("./#/connections");
})();
