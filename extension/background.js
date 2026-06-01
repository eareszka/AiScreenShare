// Service worker: its only job is to make clicking the toolbar icon open the
// side panel. All the real work (capture, change detection, AI calls) happens
// in sidepanel.js, which has the same extension privileges but a persistent UI.
chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel
    .setPanelBehavior({ openPanelOnActionClick: true })
    .catch((err) => console.error("setPanelBehavior failed:", err));
});
