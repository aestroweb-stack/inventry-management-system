const INVENTORY_APP_HOST = "inventry-management-system-production.up.railway.app";
const PAGE_TEXT_LIMIT = 9000;
const ALLOWED_ACTIONS = new Set([
  "browserOpen", "browserSearch", "browserReadPage", "browserClick",
  "browserType", "browserFillForm", "browserScroll", "browserGoBack",
  "browserMediaControl", "browserTabAction",
]);

function normalizeUrl(value) {
  let url;
  try {
    url = new URL(String(value || "").trim());
  } catch {
    url = new URL(`https://${String(value || "").trim()}`);
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new Error("For safety, MYRAA can open only http and https websites.");
  }
  return url.href;
}

function requireAppBridge(sender) {
  const senderUrl = new URL(sender?.url || "about:blank");
  if (senderUrl.hostname !== INVENTORY_APP_HOST || !senderUrl.pathname.startsWith("/app/")) {
    throw new Error("Browser commands are accepted only from the Inventory app assistant.");
  }
  if (!sender.tab?.id || sender.tab.windowId < 0) {
    throw new Error("The Inventory app tab is no longer available.");
  }
  return sender.tab;
}

async function activeTabInWindow(windowId) {
  const [tab] = await chrome.tabs.query({ active: true, windowId });
  if (!tab?.id) throw new Error("There is no active tab in the Inventory app's Chrome window.");
  return tab;
}

async function pageInfo(tab, maxChars = PAGE_TEXT_LIMIT) {
  if (!tab?.id) throw new Error("The requested browser tab is unavailable.");
  if (!/^https?:\/\//i.test(String(tab.url || ""))) {
    throw new Error("Chrome internal pages such as chrome:// settings cannot be read or controlled.");
  }
  const limit = Math.max(500, Math.min(PAGE_TEXT_LIMIT, Number(maxChars) || PAGE_TEXT_LIMIT));
  const [injection] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    args: [limit],
    func: (maxText) => ({
      title: document.title || "Untitled page",
      url: location.href,
      text: String(document.body?.innerText || "").replace(/\n{3,}/g, "\n\n").slice(0, maxText),
    }),
  });
  if (!injection?.result) throw new Error("Could not read visible page content. Check Chrome's page permission.");
  return { ...injection.result, tabId: tab.id };
}

function waitForPage(tabId, timeoutMs = 20000) {
  return new Promise((resolve) => {
    let finished = false;
    const finish = (tab) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(onUpdated);
      resolve(tab || null);
    };
    const onUpdated = (updatedId, changeInfo, tab) => {
      if (updatedId === tabId && changeInfo.status === "complete") finish(tab);
    };
    const timer = setTimeout(() => {
      chrome.tabs.get(tabId).then(finish).catch(() => finish(null));
    }, timeoutMs);
    chrome.tabs.onUpdated.addListener(onUpdated);
    chrome.tabs.get(tabId).then((tab) => {
      if (tab.status === "complete") finish(tab);
    }).catch(() => finish(null));
  });
}

async function openInSameWindow(sourceTab, url) {
  const opened = await chrome.tabs.create({ windowId: sourceTab.windowId, url, active: true });
  if (!opened?.id) throw new Error("Chrome could not open the requested page.");
  const loaded = await waitForPage(opened.id);
  const tab = loaded || await chrome.tabs.get(opened.id);
  return pageInfo(tab);
}

async function runBrowserAction(action, args, sourceTab) {
  const hasPageAccess = await chrome.permissions.contains({ origins: ["<all_urls>"] });
  if (!hasPageAccess) {
    throw new Error("Enable browser access from the MYRAA extension popup first, then allow Chrome's permission prompt.");
  }
  const active = () => activeTabInWindow(sourceTab.windowId);
  if (action === "browserOpen") {
    return openInSameWindow(sourceTab, normalizeUrl(args.url));
  }
  if (action === "browserSearch") {
    const query = String(args.query || "").trim().slice(0, 500);
    if (!query) throw new Error("Enter a search query first.");
    return openInSameWindow(sourceTab, `https://www.google.com/search?q=${encodeURIComponent(query)}`);
  }
  if (action === "browserReadPage") return pageInfo(await active(), args.max_chars);

  if (action === "browserClick") {
    const tab = await active();
    const result = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      args: [String(args.selector || "").slice(0, 300), String(args.text || args.description || "").slice(0, 200)],
      func: (selector, wantedText) => {
        let element = null;
        if (selector) {
          try { element = document.querySelector(selector); } catch { /* invalid selector: try visible text */ }
        }
        if (!element && wantedText) {
          const wanted = wantedText.trim().toLocaleLowerCase();
          const candidates = [...document.querySelectorAll("a,button,[role=button],input[type=submit],summary")];
          element = candidates.find((item) => {
            const label = String(item.innerText || item.getAttribute("aria-label") || item.getAttribute("title") || item.value || "").trim().toLocaleLowerCase();
            return label && (label === wanted || label.includes(wanted));
          });
        }
        if (!element) return { ok: false, error: "Could not find a matching visible link or button." };
        const label = String(element.innerText || element.getAttribute("aria-label") || element.getAttribute("title") || "").trim().slice(0, 180);
        element.scrollIntoView({ block: "center", behavior: "instant" });
        element.click();
        return { ok: true, clicked: label || element.tagName };
      },
    });
    const clicked = result?.[0]?.result;
    if (!clicked?.ok) throw new Error(clicked?.error || "The page did not accept the click.");
    await new Promise((resolve) => setTimeout(resolve, 750));
    let updatedTab = await active();
    if (updatedTab.status !== "complete") updatedTab = (await waitForPage(updatedTab.id, 7000)) || updatedTab;
    return { action: "clicked", label: clicked.clicked, ...(await pageInfo(updatedTab)) };
  }

  if (action === "browserType") {
    const text = String(args.text || "").slice(0, 2000);
    if (!text) throw new Error("There is no text to type.");
    const tab = await active();
    const [injection] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      args: [text, String(args.selector || "").slice(0, 300), args.clear !== false],
      func: (value, selector, clear) => {
        const field = selector ? document.querySelector(selector) : document.activeElement;
        if (!field) return { ok: false, error: "No active input field was found." };
        const editable = field.isContentEditable;
        if (!editable && !(field instanceof HTMLInputElement) && !(field instanceof HTMLTextAreaElement)) {
          return { ok: false, error: "The selected element is not an editable text field." };
        }
        if (editable) {
          if (clear) field.textContent = "";
          field.textContent = `${field.textContent || ""}${value}`;
        } else {
          const proto = field instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
          const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
          const nextValue = `${clear ? "" : field.value}${value}`;
          if (setter) setter.call(field, nextValue); else field.value = nextValue;
        }
        field.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
        field.dispatchEvent(new Event("change", { bubbles: true }));
        return { ok: true, field: field.getAttribute("aria-label") || field.getAttribute("placeholder") || field.tagName };
      },
    });
    if (!injection?.result?.ok) throw new Error(injection?.result?.error || "Could not type into that field.");
    return { action: "typed", field: injection.result.field, ...(await pageInfo(tab)) };
  }

  if (action === "browserFillForm") {
    const fields = Array.isArray(args.fields)
      ? args.fields.slice(0, 20).map((field) => [field?.selector, field?.value])
      : (args.fields && typeof args.fields === "object" ? Object.entries(args.fields).slice(0, 20) : []);
    if (!fields.length) throw new Error("No form fields were provided.");
    const entries = fields.map(([selector, value]) => [String(selector).slice(0, 300), String(value ?? "").slice(0, 2000)]);
    const submitSelector = String(args.submit || "").slice(0, 300);
    const tab = await active();
    const [injection] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      args: [entries, submitSelector],
      func: (values, submit) => {
        const filled = [];
        for (const [selector, value] of values) {
          let field;
          try { field = document.querySelector(selector); } catch { field = null; }
          if (!field) return { ok: false, error: `Could not find form field: ${selector}` };
          if (field.isContentEditable) field.textContent = value;
          else {
            const proto = field instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
            if (setter) setter.call(field, value); else field.value = value;
          }
          field.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
          field.dispatchEvent(new Event("change", { bubbles: true }));
          filled.push(selector);
        }
        if (submit) {
          let button;
          try { button = document.querySelector(submit); } catch { button = null; }
          if (!button) return { ok: false, error: `Could not find submit button: ${submit}`, filled };
          button.click();
        }
        return { ok: true, filled, submitted: Boolean(submit) };
      },
    });
    if (!injection?.result?.ok) throw new Error(injection?.result?.error || "The form could not be filled.");
    await new Promise((resolve) => setTimeout(resolve, 600));
    let updated = await active();
    if (updated.status !== "complete") updated = (await waitForPage(updated.id, 7000)) || updated;
    return { ...injection.result, ...(await pageInfo(updated)) };
  }

  if (action === "browserScroll") {
    const tab = await active();
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      args: [args.direction === "up" ? -1 : 1, Math.max(100, Math.min(1600, Number(args.amount) || 500))],
      func: (direction, distance) => window.scrollBy({ top: direction * distance, behavior: "smooth" }),
    });
    await new Promise((resolve) => setTimeout(resolve, 400));
    return { action: "scrolled", ...(await pageInfo(await active())) };
  }

  if (action === "browserGoBack") {
    const tab = await active();
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: () => history.back() });
    await new Promise((resolve) => setTimeout(resolve, 600));
    const updated = await active();
    return { action: "went back", ...(await pageInfo(updated)) };
  }

  if (action === "browserMediaControl") {
    const tab = await active();
    const [injection] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      args: [String(args.action || ""), Math.max(0, Math.min(100, Number(args.value) || 0))],
      func: async (command, volume) => {
        const media = [...document.querySelectorAll("video,audio")].find((item) => item.readyState > 0) || document.querySelector("video,audio");
        if (!media) return { ok: false, error: "No audio or video is playing on this tab." };
        try {
          if (command === "play") await media.play();
          else if (command === "pause") media.pause();
          else if (command === "mute") media.muted = true;
          else if (command === "unmute") media.muted = false;
          else if (command === "volume") media.volume = volume / 100;
          else if (command === "skip") media.currentTime = Math.min(media.duration || Infinity, media.currentTime + 10);
          else if (command === "fullscreen") await (media.requestFullscreen ? media.requestFullscreen() : Promise.reject(new Error("Fullscreen is unavailable.")));
          else if (command === "exit_fullscreen") await document.exitFullscreen?.();
          else return { ok: false, error: "Unsupported media action." };
          return { ok: true, action: command, paused: media.paused, muted: media.muted, volume: media.volume };
        } catch (error) { return { ok: false, error: error?.message || "The browser blocked this media action." }; }
      },
    });
    if (!injection?.result?.ok) throw new Error(injection?.result?.error || "Media control failed.");
    return { ...injection.result, ...(await pageInfo(tab)) };
  }

  if (action === "browserTabAction") {
    if (args.action === "new") return openInSameWindow(sourceTab, normalizeUrl(args.url || "https://www.google.com"));
    if (args.action === "switch") {
      const tabId = Number(args.tabId);
      if (!Number.isInteger(tabId)) throw new Error("To switch tabs, MYRAA needs the tab ID shown by a previous page read.");
      const target = await chrome.tabs.get(tabId);
      if (target.windowId !== sourceTab.windowId) throw new Error("MYRAA can switch only within this Chrome window.");
      await chrome.tabs.update(tabId, { active: true });
      return pageInfo(await chrome.tabs.get(tabId));
    }
    if (args.action === "close") {
      const tabId = args.tabId ? Number(args.tabId) : (await active()).id;
      if (!Number.isInteger(tabId)) throw new Error("The tab ID is invalid.");
      if (tabId === sourceTab.id) throw new Error("I am keeping the Inventory assistant tab open so our voice session continues.");
      const target = await chrome.tabs.get(tabId);
      if (target.windowId !== sourceTab.windowId) throw new Error("MYRAA can close only tabs in this Chrome window.");
      await chrome.tabs.remove(tabId);
      return { action: "closed tab", tabId };
    }
    throw new Error("Tab action must be new, switch, or close.");
  }
  throw new Error("This browser action is not enabled.");
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type !== "MYRAA_BROWSER_ACTION") return false;
  (async () => {
    const sourceTab = requireAppBridge(sender);
    const action = String(message.action || "");
    if (!ALLOWED_ACTIONS.has(action)) throw new Error("This browser action is not enabled.");
    return runBrowserAction(action, message.args && typeof message.args === "object" ? message.args : {}, sourceTab);
  })().then((result) => sendResponse({ ok: true, result })).catch((error) => {
    sendResponse({ ok: false, error: String(error?.message || "Browser action failed.").slice(0, 500) });
  });
  return true;
});
