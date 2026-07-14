const MENU_ID = "add-to-tv-queue";
const QUEUE_URL = "http://127.0.0.1:8765/queue";
const BADGE_DURATION_MS = 3000;

let badgeTimer;

function createContextMenu() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_ID,
      title: "Add to TV queue",
      contexts: ["link", "page"],
    });
  });
}

function showBadge(text, color, title) {
  clearTimeout(badgeTimer);
  chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setBadgeText({ text });
  chrome.action.setTitle({ title });
  badgeTimer = setTimeout(() => {
    chrome.action.setBadgeText({ text: "" });
    chrome.action.setTitle({ title: "TV queue" });
  }, BADGE_DURATION_MS);
}

chrome.runtime.onInstalled.addListener(createContextMenu);
chrome.runtime.onStartup.addListener(createContextMenu);

chrome.contextMenus.onClicked.addListener(async (info) => {
  if (info.menuItemId !== MENU_ID) return;

  const url = info.linkUrl || info.pageUrl;

  try {
    const response = await fetch(QUEUE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });

    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    showBadge("OK", "#16803c", "Added to TV queue");
  } catch (error) {
    showBadge("!", "#b42318", `TV queue failed: ${error.message}`);
  }
});
