// Shows the account's push notifications (sent by the control plane as data only) and opens the right computer and
// page when one is tapped. Registered only by the hosted app; see enablePush() in app.js.
"use strict";

// Before Firebase's own handler, which only acts on notifications it displayed itself.
self.addEventListener("notificationclick", (event) => {
  const link = event.notification.data && event.notification.data.link;
  if (!link) return;
  event.notification.close();
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
    const open = windows.find((w) => new URL(w.url).origin === self.location.origin && "navigate" in w);
    return open ? open.focus().then(() => open.navigate(link)) : self.clients.openWindow(link);
  }));
});

importScripts(
  "https://www.gstatic.com/firebasejs/10.13.2/firebase-app-compat.js",
  "https://www.gstatic.com/firebasejs/10.13.2/firebase-messaging-compat.js",
);

firebase.initializeApp({
  apiKey: "AIzaSyBg8h8yiC8OCoezFLEq6mQLhlc260b8CcI",
  authDomain: "swift-orch-web-20260923.firebaseapp.com",
  projectId: "swift-orch-web-20260923",
  messagingSenderId: "1093947686212",
  appId: "1:1093947686212:web:95cd975ed0d626ec196b11",
});

firebase.messaging().onBackgroundMessage((payload) => {
  const data = payload.data || {};
  // The tag matches the alert an open tab shows for the same event, so one replaces the other.
  return self.registration.showNotification(data.title || "Orchestrator", {
    body: data.body || "",
    tag: data.tag || "orchestrator",
    icon: "icon-192.png",
    data: { link: data.link || "/" },
  });
});
