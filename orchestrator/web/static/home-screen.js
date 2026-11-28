(function (root) {
  "use strict";

  // How to put Orchestrator on an iPhone or iPad Home Screen, written for the browser and device actually in use.
  // iOS only allows web push for Home Screen apps, and each browser (and the iPad) puts the Share button somewhere else.
  // Steps use **bold** for the words to look for on screen.

  const IN_APP = /FBAN|FBAV|FB_IAB|Instagram|Line\/|MicroMessenger|Snapchat|TikTok|Twitter|LinkedInApp|GSA\//;

  function detect(nav = {}) {
    const ua = String(nav.userAgent || "");
    const touch = Number(nav.maxTouchPoints) || 0;
    // iPadOS asks for desktop sites by default, so it reports itself as a Mac with a touch screen.
    const ipad = /iPad/.test(ua) || (nav.platform === "MacIntel" && touch > 1);
    const iphone = /iPhone|iPod/.test(ua);
    if (!ipad && !iphone) return null;
    const browser = IN_APP.test(ua) ? "in-app" : /CriOS/.test(ua) ? "chrome" : /FxiOS/.test(ua) ? "firefox" : /EdgiOS/.test(ua) ? "edge"
      : /OPiOS|OPT\//.test(ua) ? "opera" : "safari";
    const version = ua.match(/OS (\d+)[_.](\d+)/) || ua.match(/Version\/(\d+)\.(\d+)/);
    return { device: ipad ? "iPad" : "iPhone", browser, version: version ? [Number(version[1]), Number(version[2])] : null };
  }

  const BROWSER_NAME = { chrome: "Chrome", firefox: "Firefox", edge: "Edge", opera: "Opera", safari: "Safari" };

  function shareStep({ device, browser }) {
    if (browser === "safari") {
      return device === "iPad"
        ? "Tap the **Share** button (a square with an arrow) in the toolbar at the top of the screen, beside the address bar."
        : "Tap the **Share** button (a square with an arrow) in the toolbar at the bottom of the screen.";
    }
    if (browser === "chrome") return "Tap the **Share** button (a square with an arrow) at the right end of the address bar.";
    return `Tap the **⋯** menu in ${BROWSER_NAME[browser]}, then **Share**. If you don't see Add to Home Screen, open this page in Safari instead.`;
  }

  // null when this isn't an iPhone or iPad (nothing to explain); otherwise { title, intro, steps, note }.
  function guide(nav) {
    const d = detect(nav);
    if (!d) return null;
    if (d.browser === "in-app") {
      return {
        title: "Open Orchestrator in Safari first",
        intro: "This in-app browser can't add Orchestrator to the Home Screen, and iPhone only allows alerts for apps on the Home Screen.",
        steps: ["Open this page in **Safari** (look for **Open in Safari** in this browser's menu, or copy the address into Safari).",
                shareStep({ device: d.device, browser: "safari" }), "Choose **Add to Home Screen**, then tap **Add**.",
                "Open **Orchestrator** from your Home Screen, sign in, and tap **Get alerts** again."],
        note: "",
      };
    }
    const old = d.version && (d.version[0] < 16 || (d.version[0] === 16 && d.version[1] < 4));
    return {
      title: "Add Orchestrator to your Home Screen",
      intro: `${d.device} only allows alerts for apps on the Home Screen.`,
      steps: [shareStep(d), "Choose **Add to Home Screen** (scroll the list if you don't see it), then tap **Add**.",
              "Open **Orchestrator** from your Home Screen, sign in, and tap **Get alerts** again."],
      note: old ? `Alerts need ${d.device === "iPad" ? "iPadOS" : "iOS"} 16.4 or newer. This one is ${d.version[0]}.${d.version[1]}, so update it first (Settings, General, Software Update).` : "",
    };
  }

  const api = { detect, guide };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.HomeScreen = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
