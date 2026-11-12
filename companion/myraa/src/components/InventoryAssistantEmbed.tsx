import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Check, CircleAlert, Loader2, Mic, MicOff, Monitor, Send, Settings, Square, X } from "lucide-react";
import { MyraaAudioSession, type LiveState } from "../lib/audio";

const INVENTORY_MCP_DEFAULT = "https://inventry-management-system-production.up.railway.app/api/mcp/";
const INVENTORY_PARENT_ORIGIN = "https://inventry-management-system-production.up.railway.app";
const CHROME_BROWSER_TOOLS = new Set([
  "browserOpen", "browserSearch", "browserReadPage", "browserClick",
  "browserType", "browserFillForm", "browserScroll", "browserGoBack",
  "browserMediaControl", "browserTabAction",
]);

type ChatMessage = { id?: string; role: "user" | "assistant"; text: string };
type McpConfig = { endpoint: string; hasApiKey: boolean };
type LiveTranscriptDraft = { id: string; text: string };

function normalizeTranscript(text: string): string {
  return text.toLocaleLowerCase().replace(/[\s.,!?;:'"`~()[\]{}_-]+/g, "");
}

function localTimeLabel(state: LiveState): string {
  if (state === "connecting") return "Connecting…";
  if (state === "chatting") return "Chat connected · microphone off";
  if (state === "listening") return "Listening · Hindi, English and Hinglish";
  if (state === "speaking") return "MYRAA is speaking";
  return "Voice is off";
}

export function InventoryAssistantEmbed() {
  const [state, setState] = useState<LiveState>("disconnected");
  const [sharingScreen, setSharingScreen] = useState(false);
  const [screenShareError, setScreenShareError] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([
    { role: "assistant", text: "Namaste! Main MYRAA hoon. Inventory, orders, ya roz ke kisi bhi sawaal mein madad karungi." },
  ]);
  const [input, setInput] = useState("");
  const [error, setError] = useState("");
  const [mcpStatus, setMcpStatus] = useState("Checking inventory connection…");
  const [mcpConfig, setMcpConfig] = useState<McpConfig>({ endpoint: INVENTORY_MCP_DEFAULT, hasApiKey: false });
  const [apiKey, setApiKey] = useState("");
  const [geminiApiKey, setGeminiApiKey] = useState("");
  const [hasGeminiKey, setHasGeminiKey] = useState(false);
  const hasGeminiKeyRef = useRef(false);
  const [toolCount, setToolCount] = useState(0);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [pendingPreview, setPendingPreview] = useState<any>(null);
  const sessionRef = useRef<MyraaAudioSession | null>(null);
  const approvalRef = useRef<((result: any) => void) | null>(null);
  const browserCallbacksRef = useRef(new Map<string, { complete: (result: any) => void; timer: number }>());
  const endRef = useRef<HTMLDivElement>(null);
  const typedTurnsRef = useRef<string[]>([]);
  const pendingTypedTurnsRef = useRef<string[]>([]);
  const liveUserTranscriptRef = useRef<LiveTranscriptDraft | null>(null);
  const liveAssistantTranscriptRef = useRef<LiveTranscriptDraft | null>(null);
  const screenStreamRef = useRef<MediaStream | null>(null);
  const screenVideoRef = useRef<HTMLVideoElement | null>(null);
  const screenCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const screenIntervalRef = useRef<number | null>(null);

  const embedded = new URLSearchParams(window.location.search).get("embed") === "inventory";
  const notifyParent = useCallback((data: Record<string, unknown>) => {
    if (embedded && window.parent !== window) {
      window.parent.postMessage({ source: "myraa-assistant", ...data }, INVENTORY_PARENT_ORIGIN);
    }
  }, [embedded]);

  const stopScreenShare = useCallback(() => {
    if (screenIntervalRef.current !== null) {
      window.clearInterval(screenIntervalRef.current);
      screenIntervalRef.current = null;
    }
    screenStreamRef.current?.getTracks().forEach((track) => track.stop());
    screenStreamRef.current = null;
    screenVideoRef.current?.pause();
    screenVideoRef.current = null;
    setSharingScreen(false);
  }, []);

  const captureScreenFrame = useCallback(() => {
    const video = screenVideoRef.current;
    if (!video || video.videoWidth < 1 || video.videoHeight < 1) return;
    const canvas = screenCanvasRef.current || document.createElement("canvas");
    screenCanvasRef.current = canvas;
    const scale = Math.min(1, 960 / Math.max(video.videoWidth, video.videoHeight));
    canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
    canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
    const context = canvas.getContext("2d");
    if (!context) return;
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    const base64 = canvas.toDataURL("image/jpeg", 0.55).split(",")[1];
    if (base64) sessionRef.current?.sendVideoFrame(base64);
  }, []);

  const startScreenShare = useCallback(async () => {
    setScreenShareError("");
    const liveState = sessionRef.current?.getState();
    if (!hasGeminiKeyRef.current) {
      setScreenShareError("Screen samajhne ke liye pehle Gemini key save karein.");
      setSettingsOpen(true);
      notifyParent({ type: "resize", height: 460 });
      return;
    }
    if (!navigator.mediaDevices?.getDisplayMedia) {
      setScreenShareError("Is browser mein screen sharing available nahi hai. Updated Chrome ya Edge use karein.");
      return;
    }
    let stream: MediaStream | null = null;
    try {
      // Keep this call directly inside the button flow so the browser can show
      // its native screen/window chooser and permission prompt.
      stream = await navigator.mediaDevices.getDisplayMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 5 } },
        audio: false,
      });
      const video = document.createElement("video");
      video.srcObject = stream;
      video.muted = true;
      video.playsInline = true;
      await video.play();
      screenStreamRef.current = stream;
      screenVideoRef.current = video;
      const track = stream.getVideoTracks()[0];
      if (track) track.onended = stopScreenShare;
      screenIntervalRef.current = window.setInterval(captureScreenFrame, 2000);
      setSharingScreen(true);
      if (liveState === "disconnected") {
        setMcpStatus("Screen selected · connecting text session");
        await sessionRef.current?.connect({ microphone: false, surface: "inventory" });
      }
      window.setTimeout(captureScreenFrame, 500);
    } catch (cause: any) {
      stream?.getTracks().forEach((track) => track.stop());
      setScreenShareError(cause?.name === "NotAllowedError"
        ? "Screen share cancel hua. Share screen dabakar display chunein aur browser prompt allow karein."
        : `Screen share start nahi hua: ${cause?.message || "browser capture unavailable"}`);
    }
  }, [captureScreenFrame, notifyParent, stopScreenShare]);

  useEffect(() => () => stopScreenShare(), [stopScreenShare]);
  useEffect(() => {
    if (state === "disconnected" && sharingScreen) stopScreenShare();
  }, [state, sharingScreen, stopScreenShare]);

  useEffect(() => { hasGeminiKeyRef.current = hasGeminiKey; }, [hasGeminiKey]);

  useEffect(() => {
    let disposed = false;
    const session = new MyraaAudioSession({
      onStateChange: (next) => {
        setState(next);
        notifyParent({ type: "status", text: localTimeLabel(next) });
        if (next === "chatting" || next === "listening") {
          const active = sessionRef.current;
          while (active && pendingTypedTurnsRef.current.length) {
            if (!active.sendTextInput(pendingTypedTurnsRef.current[0])) break;
            pendingTypedTurnsRef.current.shift();
          }
        }
      },
      onTranscription: (role, text, finished = false) => {
        const chunk = String(text || "");
        if (chunk.length > 0) {
          if (role === "user") {
            const previous = liveUserTranscriptRef.current;
            const fullText = `${previous?.text || ""}${chunk}`;
            const normalized = normalizeTranscript(fullText);
            const typedIndex = typedTurnsRef.current.findIndex((turn) => {
              const typed = normalizeTranscript(turn);
              return Boolean(normalized && typed.startsWith(normalized));
            });
            if (typedIndex >= 0) {
              if (finished) typedTurnsRef.current.splice(typedIndex, 1);
              liveUserTranscriptRef.current = finished ? null : { id: previous?.id || "", text: fullText };
            } else if (fullText.trim()) {
              const id = previous?.id || `user-${Date.now()}-${Math.random()}`;
              liveUserTranscriptRef.current = finished ? null : { id, text: fullText };
              setMessages((current) => {
                const index = current.findIndex((message) => message.id === id);
                const next = [...current];
                const message = { id, role: "user" as const, text: fullText };
                if (index >= 0) next[index] = message;
                else next.push(message);
                return next.slice(-80);
              });
            }
          } else {
            const previous = liveAssistantTranscriptRef.current;
            const id = previous?.id || `assistant-${Date.now()}-${Math.random()}`;
            const fullText = `${previous?.text || ""}${chunk}`;
            liveAssistantTranscriptRef.current = finished ? null : { id, text: fullText };
            if (fullText.trim()) setMessages((current) => {
              const index = current.findIndex((message) => message.id === id);
              const next = [...current];
              const message = { id, role: "assistant" as const, text: fullText };
              if (index >= 0) next[index] = message;
              else next.push(message);
              return next.slice(-80);
            });
          }
          notifyParent({ type: "transcription", role, text: chunk, finished });
        } else if (finished && role === "user") {
          liveUserTranscriptRef.current = null;
        } else if (finished) {
          liveAssistantTranscriptRef.current = null;
        }
      },
      onTurnComplete: () => {
        liveUserTranscriptRef.current = null;
        liveAssistantTranscriptRef.current = null;
        notifyParent({ type: "turnComplete" });
      },
      onToolCall: (name, args, callback) => {
        if (name === "__inventory_action_approval__") {
          approvalRef.current = callback;
          setPendingPreview(args?.preview || {});
          return;
        }
        if (CHROME_BROWSER_TOOLS.has(name)) {
          const requestId = `browser-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
          const timer = window.setTimeout(() => {
            const pending = browserCallbacksRef.current.get(requestId);
            if (!pending) return;
            browserCallbacksRef.current.delete(requestId);
            pending.complete({ error: "The MYRAA Chrome extension did not respond. Reload it at chrome://extensions, enable its browser access permission, then refresh the Inventory app." });
          }, 35000);
          browserCallbacksRef.current.set(requestId, { complete: callback, timer });
          notifyParent({ type: "browserCommand", requestId, action: name, args: args || {} });
          return;
        }
        callback({ error: `MYRAA cannot run ${name} from the inventory panel.` });
      },
      onError: (message) => {
        pendingTypedTurnsRef.current = [];
        setError(message);
        notifyParent({ type: "error", message });
      },
      onInventoryStatus: (result) => {
        if (result.status === "connected") {
          setToolCount(Number(result.toolCount) || 0);
          setMcpStatus(`Inventory connected · ${Number(result.toolCount) || 0} tools`);
          notifyParent({ type: "status", text: `Inventory connected · ${Number(result.toolCount) || 0} tools` });
        } else if (result.status === "not_configured") {
          setMcpStatus("Inventory access not connected · add Railway MCP key in settings");
          notifyParent({ type: "status", text: "Inventory access not connected · add the Railway MCP key in MYRAA settings" });
        } else {
          setMcpStatus(result.message || "Inventory MCP connection failed");
        }
      },
    });
    sessionRef.current = session;

    const loadConfig = async () => {
      try {
        const [response, geminiResponse] = await Promise.all([
          fetch("/api/inventory-mcp/config", { cache: "no-store" }),
          fetch("/api/config", { cache: "no-store" }),
        ]);
        const data = await response.json();
        const gemini = await geminiResponse.json();
        if (!disposed && response.ok) {
          setMcpConfig({ endpoint: data.endpoint || INVENTORY_MCP_DEFAULT, hasApiKey: Boolean(data.hasApiKey) });
          setHasGeminiKey(Boolean(gemini.hasApiKey));
          const status = data.hasApiKey ? "Inventory key saved · connect voice to load tools" : "Inventory access not connected · add Railway MCP key in settings";
          setMcpStatus(status);
          if (!data.hasApiKey) notifyParent({ type: "status", text: "Inventory access not connected · add the Railway MCP key in MYRAA settings" });
        }
      } catch {
        if (!disposed) setMcpStatus("Local MYRAA server is not reachable");
      }
    };
    void loadConfig();

    const onParentMessage = (event: MessageEvent) => {
      if (!embedded || event.source !== window.parent || event.origin !== INVENTORY_PARENT_ORIGIN
          || event.data?.source !== "inventory-myraa-parent") return;
      if (event.data.type === "browserCommandResult") {
        const requestId = String(event.data.requestId || "");
        const pending = browserCallbacksRef.current.get(requestId);
        if (!pending) return;
        window.clearTimeout(pending.timer);
        browserCallbacksRef.current.delete(requestId);
        pending.complete(event.data.result || { error: "The Chrome helper returned an empty result." });
      } else if (event.data.type === "chat") {
        const text = String(event.data.text || "").trim().slice(0, 2000);
        if (!text) return;
        if (!hasGeminiKeyRef.current) {
          setSettingsOpen(true);
          notifyParent({ type: "resize", height: 460 });
          notifyParent({ type: "chatError", requestId: event.data.requestId, message: "Gemini key MYRAA settings mein add karein." });
          return;
        }
        const active = sessionRef.current;
        if (!active) return;
        if (!active.sendTextInput(text)) {
          const current = active.getState();
          if (current === "disconnected" || current === "connecting") {
            pendingTypedTurnsRef.current.push(text);
            if (current === "disconnected") void active.connect({ microphone: false, surface: "inventory" });
          } else {
            notifyParent({ type: "chatError", requestId: event.data.requestId, message: "MYRAA text session is reconnecting. Please try Send again." });
            return;
          }
        }
        typedTurnsRef.current.push(text);
        setMessages((current) => [...current, { role: "user", text }].slice(-80));
      } else if (event.data.type === "stop") {
        pendingTypedTurnsRef.current = [];
        sessionRef.current?.disconnect();
      } else if (event.data.type === "newConversation") {
        pendingTypedTurnsRef.current = [];
        sessionRef.current?.disconnect();
        typedTurnsRef.current = [];
        liveUserTranscriptRef.current = null;
        liveAssistantTranscriptRef.current = null;
        setMessages([{ role: "assistant", text: "New conversation started. Main MYRAA hoon—kaise madad karun?" }]);
      }
    };
    window.addEventListener("message", onParentMessage);
    if (embedded) notifyParent({ type: "ready" });

    return () => {
      disposed = true;
      pendingTypedTurnsRef.current = [];
      window.removeEventListener("message", onParentMessage);
      for (const pending of browserCallbacksRef.current.values()) {
        window.clearTimeout(pending.timer);
        pending.complete({ error: "The assistant session ended before the browser action completed." });
      }
      browserCallbacksRef.current.clear();
      session.disconnect();
      sessionRef.current = null;
    };
  }, [embedded, notifyParent]);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [messages]);

  const toggleVoice = async () => {
    setError("");
    if (!hasGeminiKey) {
      setSettingsOpen(true);
      notifyParent({ type: "resize", height: 460 });
      setError("Gemini API key add karne ke liye settings kholen.");
      return;
    }
    if (state === "disconnected") await sessionRef.current?.connect({ microphone: true, surface: "inventory" });
    else if (state === "chatting") await sessionRef.current?.enableMicrophone();
    else {
      pendingTypedTurnsRef.current = [];
      sessionRef.current?.disconnect();
    }
  };

  const sendText = (event: FormEvent) => {
    event.preventDefault();
    const text = input.trim().slice(0, 2000);
    if (!text) return;
    if (!hasGeminiKeyRef.current) {
      setSettingsOpen(true);
      setError("Gemini API key add karne ke liye settings kholen.");
      return;
    }
    const active = sessionRef.current;
    if (!active) return;
    if (!active.sendTextInput(text)) {
      const current = active.getState();
      if (current === "disconnected" || current === "connecting") {
        pendingTypedTurnsRef.current.push(text);
        if (current === "disconnected") void active.connect({ microphone: false, surface: "inventory" });
      } else {
        setError("MYRAA text session reconnect ho raha hai. Dobara Send dabayein.");
        return;
      }
    }
    typedTurnsRef.current.push(text);
    setMessages((current) => [...current, { role: "user", text }].slice(-80));
    setInput("");
  };

  const saveInventoryConfig = async (event: FormEvent) => {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    setError("");
    try {
      if (geminiApiKey.trim()) {
        const geminiResponse = await fetch("/api/config/apikey", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ apiKey: geminiApiKey.trim() }),
        });
        const geminiResult = await geminiResponse.json();
        if (!geminiResponse.ok) throw new Error(geminiResult.error || "Gemini API key was rejected.");
        setHasGeminiKey(true);
        setGeminiApiKey("");
      }
      const response = await fetch("/api/inventory-mcp/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ endpoint: mcpConfig.endpoint.trim(), apiKey: apiKey.trim() }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Could not save inventory connection.");
      setMcpConfig({ endpoint: result.endpoint, hasApiKey: Boolean(result.hasApiKey) });
      setApiKey("");
      setMcpStatus(result.hasApiKey ? "Saved · reconnect MYRAA to load inventory tools" : "Gemini chat is available · inventory key is still required for app access");
      if (state !== "disconnected") sessionRef.current?.disconnect();
      if (result.hasApiKey) {
        const test = await fetch("/api/inventory-mcp/test", { method: "POST" });
        const testResult = await test.json();
        if (!test.ok) throw new Error(testResult.error || "Inventory MCP test failed.");
        setToolCount(Number(testResult.toolCount) || 0);
        setMcpStatus(`Connection verified · ${Number(testResult.toolCount) || 0} inventory tools`);
        notifyParent({ type: "status", text: `Inventory connected · ${Number(testResult.toolCount) || 0} tools · reconnect MYRAA` });
      } else {
        setMcpStatus("Gemini chat works · add the Railway MCP key for inventory access");
        notifyParent({ type: "status", text: "Gemini chat is ready · inventory access needs the Railway MCP API key in MYRAA settings" });
      }
      setSettingsOpen(false);
      notifyParent({ type: "resize", height: 150 });
    } catch (cause: any) {
      const message = cause?.message || "Inventory MCP setup failed.";
      setError(message);
      notifyParent({ type: "error", message });
    } finally {
      setSaving(false);
    }
  };

  const answerApproval = (approved: boolean) => {
    approvalRef.current?.({ approved });
    approvalRef.current = null;
    setPendingPreview(null);
  };

  const displayDetails = (value: unknown) => {
    try { return JSON.stringify(value ?? {}, null, 2); } catch { return String(value ?? ""); }
  };

  return (
    <main className="min-h-screen bg-[#0a0b13] text-slate-100 flex flex-col font-sans">
      <header className="px-4 py-3 flex items-center justify-between border-b border-white/10 bg-[#10121d]">
        <div className="min-w-0">
          <div className="font-semibold tracking-wide">MYRAA <span className="text-cyan-300">· Inventory Assistant</span></div>
          <div className="text-[11px] text-slate-400 truncate">{localTimeLabel(state)} · {mcpStatus}</div>
        </div>
        <button type="button" onClick={() => { setSettingsOpen(true); notifyParent({ type: "resize", height: 460 }); }} title="Connection settings" className="p-2 rounded-lg hover:bg-white/10 text-slate-300"><Settings size={18}/></button>
      </header>

      {!embedded && <section className="flex-1 overflow-auto px-3 py-3 space-y-3" aria-live="polite">
        {!mcpConfig.hasApiKey && <div className="rounded-xl border border-amber-400/25 bg-amber-400/10 p-3 text-xs text-amber-100">Connect the inventory MCP key in settings to use live stock, orders, dispatch, CRUD, and analytics.</div>}
        {messages.map((message, index) => (
          <div key={`${index}-${message.role}`} className={`max-w-[92%] rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap break-words ${message.role === "user" ? "ml-auto bg-cyan-500/20 border border-cyan-300/20" : "mr-auto bg-white/8 border border-white/10"}`}>
            {message.text}
          </div>
        ))}
        <div ref={endRef}/>
      </section>}

      {error && !embedded && <div className="mx-3 mb-2 rounded-xl border border-rose-400/30 bg-rose-950/40 p-3 text-xs text-rose-100 flex gap-2"><CircleAlert size={16} className="shrink-0"/><span>{error}</span><button onClick={() => setError("")} aria-label="Dismiss" className="ml-auto"><X size={14}/></button></div>}

      <div className="px-3 py-2 border-t border-white/10 flex items-center gap-2">
        <button type="button" onClick={toggleVoice} disabled={state === "connecting"} className={`h-10 px-3 rounded-xl flex items-center gap-2 text-sm font-medium ${state === "disconnected" ? "bg-cyan-500 hover:bg-cyan-400 text-slate-950" : "bg-rose-500/15 border border-rose-400/30 text-rose-200"}`}>
          {state === "connecting" ? <Loader2 size={16} className="animate-spin"/> : state === "disconnected" ? <Mic size={16}/> : <MicOff size={16}/>}
          {state === "disconnected" ? "Start voice" : state === "connecting" ? "Connecting" : state === "chatting" ? "Enable voice" : "End voice"}
        </button>
        <button type="button" onClick={() => { if (sharingScreen) stopScreenShare(); else void startScreenShare(); }} aria-label={sharingScreen ? "Stop screen sharing" : "Share screen with MYRAA"} title={sharingScreen ? "Stop screen sharing" : "Share screen with MYRAA"} className={`h-10 px-2.5 rounded-xl flex items-center gap-1.5 text-xs font-medium whitespace-nowrap ${sharingScreen ? "bg-rose-500/15 border border-rose-400/30 text-rose-200" : "bg-white/10 hover:bg-white/15 text-slate-100"}`}>
          {sharingScreen ? <Square size={14}/> : <Monitor size={15}/>}
          {sharingScreen ? "Stop screen" : "Share screen"}
        </button>
        {!embedded && <form onSubmit={sendText} className="flex flex-1 gap-2 min-w-0">
          <input value={input} onChange={(event) => setInput(event.target.value)} maxLength={2000} placeholder="Hindi, English, Hinglish mein likhein…" className="min-w-0 flex-1 rounded-xl bg-white/5 border border-white/10 px-3 text-sm outline-none focus:border-cyan-300/50" aria-label="Message MYRAA"/>
          <button type="submit" title="Send message" className="w-10 rounded-xl bg-white/10 hover:bg-white/15 grid place-items-center"><Send size={16}/></button>
        </form>}
      </div>
      {screenShareError && <div role="alert" className="mx-3 mb-2 rounded-lg border border-rose-400/25 bg-rose-950/30 px-3 py-2 text-[11px] text-rose-100">{screenShareError}</div>}
      {sharingScreen && <div role="status" className="px-3 pb-2 text-[11px] text-emerald-200">Screen is shared with the active MYRAA session. Stop it any time.</div>}

      {pendingPreview && (
        <div className="fixed inset-0 z-50 bg-black/75 p-4 flex items-center justify-center">
          <section role="dialog" aria-modal="true" aria-labelledby="inventory-approval-title" className="w-full max-w-lg max-h-[90vh] flex flex-col rounded-2xl border border-amber-300/25 bg-[#131622] shadow-2xl">
            <header className="p-4 border-b border-white/10 flex justify-between items-center"><div><h2 id="inventory-approval-title" className="font-semibold">Confirm inventory change</h2><p className="text-xs text-amber-200 mt-1">Nothing changes until you confirm here.</p></div><button onClick={() => answerApproval(false)} aria-label="Cancel" className="p-2 rounded-lg hover:bg-white/10"><X size={18}/></button></header>
            <div className="p-4 overflow-auto"><div className="text-sm font-medium text-cyan-200 mb-2">{String(pendingPreview.action || "Inventory action")}</div><pre className="text-xs leading-relaxed whitespace-pre-wrap break-words rounded-xl bg-black/25 p-3">{displayDetails(pendingPreview.details)}</pre></div>
            <footer className="p-4 pt-0 flex justify-end gap-2"><button onClick={() => answerApproval(false)} className="px-4 py-2 rounded-xl border border-white/15 hover:bg-white/5">Cancel</button><button onClick={() => answerApproval(true)} className="px-4 py-2 rounded-xl bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold flex items-center gap-2"><Check size={16}/> Confirm change</button></footer>
          </section>
        </div>
      )}

      {settingsOpen && (
        <div className="fixed inset-0 z-40 bg-black/75 p-4 flex items-center justify-center">
          <form onSubmit={saveInventoryConfig} className="w-full max-w-md rounded-2xl border border-white/10 bg-[#131622] p-5 shadow-2xl space-y-4">
            <header className="flex items-start justify-between"><div><h2 className="font-semibold">MYRAA connections</h2><p className="text-xs text-slate-400 mt-1">Keys stay in this PC’s private MYRAA data folder.</p></div><button type="button" onClick={() => { setSettingsOpen(false); notifyParent({ type: "resize", height: 150 }); }} aria-label="Close settings" className="p-2 rounded-lg hover:bg-white/10"><X size={17}/></button></header>
            <label className="block text-xs text-slate-300 space-y-1"><span>Gemini API key {hasGeminiKey && <em className="text-emerald-300 not-italic">· saved</em>}</span><input value={geminiApiKey} onChange={(event) => setGeminiApiKey(event.target.value)} className="w-full rounded-lg bg-black/25 border border-white/10 p-2.5 text-sm" type="password" autoComplete="new-password" placeholder={hasGeminiKey ? "Leave blank to keep the saved key" : "Paste your Gemini API key"}/></label>
            <label className="block text-xs text-slate-300 space-y-1"><span>MCP endpoint</span><input value={mcpConfig.endpoint} onChange={(event) => setMcpConfig((current) => ({ ...current, endpoint: event.target.value }))} className="w-full rounded-lg bg-black/25 border border-white/10 p-2.5 text-sm" required type="url"/></label>
            <label className="block text-xs text-slate-300 space-y-1"><span>Inventory MCP API key {mcpConfig.hasApiKey && <em className="text-emerald-300 not-italic">· saved</em>}</span><input value={apiKey} onChange={(event) => setApiKey(event.target.value)} className="w-full rounded-lg bg-black/25 border border-white/10 p-2.5 text-sm" type="password" autoComplete="new-password" placeholder={mcpConfig.hasApiKey ? "Leave blank to keep the saved key" : "Paste Railway INVENTORY_MCP_API_KEY / DISHA_MCP_API_KEY"}/></label>
            <p className="text-[11px] text-slate-400">Gemini key powers conversation; it does not grant inventory access. Paste the same private key configured on Railway as `INVENTORY_MCP_API_KEY` or `DISHA_MCP_API_KEY`. MYRAA stores it only in this PC’s private data folder. Save &amp; test should report the available tools; then reconnect voice. Share screen can start a text session without turning on your microphone.</p>
            <footer className="flex justify-end gap-2"><button type="button" onClick={() => { setSettingsOpen(false); notifyParent({ type: "resize", height: 150 }); }} className="px-3 py-2 rounded-lg border border-white/10 text-sm">Close</button><button type="submit" disabled={saving} className="px-3 py-2 rounded-lg bg-cyan-400 text-slate-950 font-semibold text-sm flex items-center gap-2">{saving && <Loader2 size={14} className="animate-spin"/>}Save &amp; test</button></footer>
          </form>
        </div>
      )}
    </main>
  );
}
