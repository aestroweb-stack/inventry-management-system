/**
 * MYRAA — path & secret resolution.
 *
 * Separates read-only *code/asset* locations (shipped with the app) from the
 * writable *data* location (per-user, survives reinstalls). When Electron
 * launches the backend it sets MYRAA_DATA_DIR to a
 * writable folder under %APPDATA%\MYRAA, because the install directory
 * (Program Files) is read-only.
 *
 * The Gemini API key is NOT shipped with the app. Each user supplies their own
 * on first run; it is stored here in the per-user data dir (never returned to
 * the frontend).
 */

import fs from "fs";
import path from "path";
import os from "os";
import { randomBytes } from "crypto";

/** Writable per-user data directory. MYRAA_DATA_DIR can override for tests. */
const defaultDataDir = process.env.APPDATA
  ? path.join(process.env.APPDATA, "MYRAA")
  : process.env.XDG_DATA_HOME
    ? path.join(process.env.XDG_DATA_HOME, "myraa")
    : path.join(os.homedir(), ".local", "share", "myraa");
export const DATA_DIR: string = process.env.MYRAA_DATA_DIR || defaultDataDir;

try {
  fs.mkdirSync(DATA_DIR, { recursive: true });
} catch {
  /* already exists / best-effort */
}

/** Absolute path to a file inside the writable data directory. */
export function dataFile(name: string): string {
  return path.join(DATA_DIR, name);
}

// ---------------------------------------------------------------------------
// Gemini API key store (secrets.json in the data dir).
// ---------------------------------------------------------------------------
const SECRETS_FILE = dataFile("secrets.json");

interface Secrets {
  geminiApiKey?: string;
  inventoryMcpApiKey?: string;
  desktopAgentToken?: string;
}

function readSecrets(): Secrets {
  try {
    if (fs.existsSync(SECRETS_FILE)) {
      return JSON.parse(fs.readFileSync(SECRETS_FILE, "utf-8")) as Secrets;
    }
  } catch {
    /* corrupt — treat as empty */
  }
  return {};
}

function writeSecrets(value: Secrets): void {
  fs.writeFileSync(SECRETS_FILE, JSON.stringify(value, null, 2), "utf-8");
  try {
    fs.chmodSync(SECRETS_FILE, 0o600);
  } catch {
    /* Windows ACLs differ; best-effort. */
  }
}

/**
 * Resolve the active Gemini API key.
 * Priority: user-entered key (secrets.json) → environment (.env, dev only).
 */
export function getGeminiApiKey(): string | undefined {
  const stored = readSecrets().geminiApiKey?.trim();
  if (stored) return stored;
  const env = process.env.GEMINI_API_KEY?.trim();
  return env || undefined;
}

/** Whether any usable key is configured (without revealing it). */
export function hasGeminiApiKey(): boolean {
  return Boolean(getGeminiApiKey());
}

/** Persist a user-supplied key to the per-user secrets file. */
export function setGeminiApiKey(key: string): void {
  const trimmed = (key || "").trim();
  if (!trimmed) throw new Error("API key must not be empty.");
  const current = readSecrets();
  current.geminiApiKey = trimmed;
  writeSecrets(current);
}

/** Resolve the inventory MCP key without ever exposing it to the renderer. */
export function getInventoryMcpApiKey(): string | undefined {
  const stored = readSecrets().inventoryMcpApiKey?.trim();
  if (stored) return stored;
  const env = process.env.INVENTORY_MCP_API_KEY?.trim() || process.env.DISHA_MCP_API_KEY?.trim();
  return env || undefined;
}

export function setInventoryMcpApiKey(key: string): void {
  const trimmed = (key || "").trim();
  if (!trimmed) throw new Error("Inventory MCP API key must not be empty.");
  const current = readSecrets();
  current.inventoryMcpApiKey = trimmed;
  writeSecrets(current);
}

export function clearInventoryMcpApiKey(): void {
  const current = readSecrets();
  delete current.inventoryMcpApiKey;
  writeSecrets(current);
}

/** Shared random credential for the local Node-to-Python desktop bridge. */
export function getDesktopAgentToken(): string {
  const current = readSecrets();
  if (!current.desktopAgentToken) {
    current.desktopAgentToken = randomBytes(32).toString("hex");
    writeSecrets(current);
  }
  return current.desktopAgentToken;
}

/** Remove the stored key (used by "reset"/sign-out flows). */
export function clearGeminiApiKey(): void {
  const current = readSecrets();
  delete current.geminiApiKey;
  try {
    writeSecrets(current);
  } catch {
    /* best-effort */
  }
}
