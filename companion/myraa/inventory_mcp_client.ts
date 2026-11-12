export interface InventoryMcpTool {
  name: string;
  description?: string;
  inputSchema?: Record<string, any>;
}

const INITIAL_PROTOCOL_VERSION = "2025-03-26";

/** Small Streamable HTTP MCP client for the inventory service. */
export class InventoryMcpClient {
  private requestId = 0;
  private sessionId?: string;
  private protocolVersion = INITIAL_PROTOCOL_VERSION;
  private initialized = false;

  constructor(
    private readonly endpoint: string,
    private readonly apiKey: string,
  ) {}

  private async request(method: string, params?: Record<string, unknown>, notification = false): Promise<any> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      "X-API-Key": this.apiKey,
    };
    if (this.initialized) headers["MCP-Protocol-Version"] = this.protocolVersion;
    if (this.sessionId) headers["Mcp-Session-Id"] = this.sessionId;

    const request: Record<string, unknown> = { jsonrpc: "2.0", method };
    if (!notification) request.id = ++this.requestId;
    if (params) request.params = params;

    let response: Response;
    try {
      response = await fetch(this.endpoint, {
        method: "POST",
        headers,
        body: JSON.stringify(request),
        redirect: "error",
        signal: AbortSignal.timeout(20_000),
      });
    } catch (error: any) {
      if (error?.name === "TimeoutError" || error?.name === "AbortError") {
        throw new Error("Inventory MCP request timed out.");
      }
      throw new Error("Could not connect to the inventory MCP endpoint.");
    }

    const returnedSessionId = response.headers.get("Mcp-Session-Id");
    if (returnedSessionId) this.sessionId = returnedSessionId;
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        throw new Error("Inventory MCP rejected its API key or connection origin.");
      }
      throw new Error(`Inventory MCP returned HTTP ${response.status}.`);
    }
    if (notification || response.status === 202 || response.status === 204) return undefined;

    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("text/event-stream")) {
      const body = await response.text();
      const data = body.split(/\r?\n/)
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim())
        .filter(Boolean)
        .at(-1);
      if (!data) throw new Error("Inventory MCP returned an empty event stream.");
      return JSON.parse(data);
    }

    const message = await response.json();
    if (message?.error) {
      throw new Error(String(message.error.message || "Inventory MCP reported an error."));
    }
    return message?.result;
  }

  async listTools(): Promise<InventoryMcpTool[]> {
    if (!this.initialized) {
      const initialized = await this.request("initialize", {
        protocolVersion: INITIAL_PROTOCOL_VERSION,
        capabilities: {},
        clientInfo: { name: "myraa-inventory-assistant", version: "1.0.0" },
      });
      const negotiated = initialized?.protocolVersion;
      if (typeof negotiated === "string") this.protocolVersion = negotiated;
      this.initialized = true;
      await this.request("notifications/initialized", undefined, true);
    }
    const result = await this.request("tools/list", {});
    return Array.isArray(result?.tools) ? result.tools : [];
  }

  async callTool(name: string, args: Record<string, unknown>): Promise<any> {
    if (!this.initialized) await this.listTools();
    return this.request("tools/call", { name, arguments: args || {} });
  }
}
