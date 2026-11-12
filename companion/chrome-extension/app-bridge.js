const MYRAA_BROWSER_ACTIONS = new Set([
  "browserOpen", "browserSearch", "browserReadPage", "browserClick",
  "browserType", "browserFillForm", "browserScroll", "browserGoBack",
  "browserMediaControl", "browserTabAction",
]);

window.addEventListener("message", async (event) => {
  const iframe = document.querySelector("iframe.assistant-voice-frame");
  const message = event.data;
  if (event.source !== iframe?.contentWindow || event.origin !== "http://127.0.0.1:3000"
      || message?.source !== "myraa-assistant" || message.type !== "browserCommand") return;

  const requestId = String(message.requestId || "").slice(0, 120);
  const action = String(message.action || "");
  let result;
  if (!requestId || !MYRAA_BROWSER_ACTIONS.has(action)) {
    result = { error: "This browser command is invalid." };
  } else {
    try {
      const response = await chrome.runtime.sendMessage({
        type: "MYRAA_BROWSER_ACTION",
        action,
        args: message.args && typeof message.args === "object" ? message.args : {},
      });
      result = response?.ok ? response.result : { error: response?.error || "The browser helper could not complete that action." };
    } catch (error) {
      result = { error: "Browser helper is not responding. Reload MYRAA in chrome://extensions and refresh the Inventory app." };
    }
  }
  const iframeReply = {
    source: "inventory-myraa-parent",
    type: "browserCommandResult",
    requestId,
    result,
  };
  try {
    iframe?.contentWindow?.postMessage(iframeReply, "http://127.0.0.1:3000");
  } catch {
    // Keep the parent-page bridge below as a fallback for the deployed app bundle.
  }
  window.postMessage({
    source: "inventory-browser-extension",
    type: "browserCommandResult",
    requestId,
    result,
  }, window.location.origin);
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "MYRAA_SHARE_SELECTED_WHATSAPP_TEXT") return false;

  const text = String(message.text || "").trim().slice(0, 350);
  if (!text) {
    sendResponse({ ok: false, reason: "Select a WhatsApp message first." });
    return false;
  }

  const input = document.getElementById("assistant-chat-input");
  if (!input) {
    sendResponse({ ok: false, reason: "Open the Inventory app and MYRAA assistant first." });
    return false;
  }
  if (input.value.trim()) {
    sendResponse({ ok: false, reason: "Send or clear the current MYRAA draft first." });
    return false;
  }

  window.postMessage({
    source: "inventory-whatsapp-companion",
    type: "selected-text",
    text
  }, window.location.origin);
  sendResponse({ ok: true });
  return false;
});
