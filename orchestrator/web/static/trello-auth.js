"use strict";
// Trello sends the person back here with #token=… once they click Allow. Hand it to the app (same site, so the
// same session storage) and go back to Connections, which saves it. The token never appears in a server log.
(function () {
  var token = new URLSearchParams(location.hash.replace(/^#/, "")).get("token");
  if (!token) {
    document.getElementById("status").textContent = "Trello didn't send a token. Go back and try again.";
    return;
  }
  try { sessionStorage.setItem("orchestrator_trello_token", token); } catch (e) { /* storage blocked */ }
  location.replace("./#/connections");
})();
