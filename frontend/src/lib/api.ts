export type Settings = Record<string, string | boolean | number | null>;
export type SnowLumaSettings = { connection_mode: "reverse_ws" | "forward_ws" | "http"; websocket_url: string; http_url: string; token: string; token_configured: boolean };
export type Status = { application: string; database: string; version: string; migration: string };
export type ProtocolType = "openai_responses" | "openai_chat_completions" | "anthropic_messages";
export type ProviderConfig = {
  providerId: string;
  name: string;
  protocolType: ProtocolType;
  baseUrl: string;
  timeoutSeconds: number;
  organization: string;
  project: string;
  enabled: boolean;
  apiKeyConfigured: boolean;
  responsesModel: string;
  embeddingModel: string;
  embeddingDimensions: number | null;
  nativeSearchEnabled: boolean;
  defaultStream: boolean;
  temperature: number | null;
  maxOutputTokens: number;
  extraHeadersConfigured: boolean;
  extraHeaderNames: string[];
};
export type HeaderInput = { name: string; value: string; previous_name?: string };
export type ProviderInput = Omit<ProviderConfig, "providerId" | "apiKeyConfigured" | "extraHeadersConfigured" | "extraHeaderNames"> & { apiKey?: string; extraHeaders?: HeaderInput[]; confirmProtocolChange?: boolean };
export type ProviderProtocol = { label: string; fields: string[]; temperature_max: number; defaults: { responses_model: string; embedding_model: string; base_url: string; max_output_tokens: number }; capabilities: string[] };
export type ProviderProtocols = Record<ProtocolType, ProviderProtocol>;

function providerPayload(values: Partial<ProviderInput>): Record<string, unknown> {
  return {
    name: values.name,
    protocol_type: values.protocolType,
    base_url: values.baseUrl,
    timeout_seconds: values.timeoutSeconds,
    organization: values.organization,
    project: values.project,
    enabled: values.enabled,
    ...(values.apiKey !== undefined ? { api_key: values.apiKey } : {}),
    responses_model: values.responsesModel,
    embedding_model: values.embeddingModel,
    embedding_dimensions: values.embeddingDimensions,
    native_search_enabled: values.nativeSearchEnabled,
    default_stream: values.defaultStream,
    temperature: values.temperature,
    max_output_tokens: values.maxOutputTokens,
    extra_headers: values.extraHeaders,
    confirm_protocol_change: values.confirmProtocolChange,
  };
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", ...options, headers: { "Content-Type": "application/json", ...(options?.headers ?? {}) } });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event("vireyabot:unauthorized"));
    const detail = Array.isArray(body.detail) ? body.detail.map((item: { msg?: string; loc?: (string | number)[] }) => `${item.loc?.join(".") || "请求"}：${item.msg || "字段无效"}`).join("；") : body.detail;
    throw new Error(typeof detail === "string" ? detail : "请求失败，请稍后重试");
  }
  return body as T;
}

export const api = {
  login: (username: string, password: string) => request<{ status: string }>("/api/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  logout: () => request<{ status: string }>("/api/logout", { method: "POST" }),
  me: () => request<{ username: string }>("/api/me"),
  status: () => request<Status>("/api/status"),
  settings: () => request<Settings>("/api/settings"),
  saveSettings: (values: Settings) => request<{ status: string }>("/api/settings", { method: "PUT", body: JSON.stringify({ values }) }),
  snowluma: () => request<SnowLumaSettings>("/api/snowluma"),
  saveSnowLuma: (values: Partial<SnowLumaSettings>) => request<{ status: string }>("/api/snowluma", { method: "PUT", body: JSON.stringify(values) }),
  providers: () => request<ProviderConfig[]>("/api/providers"),
  providerProtocols: () => request<ProviderProtocols>("/api/provider-protocols"),
  createProvider: (values: Partial<ProviderInput>) => request<ProviderConfig>("/api/providers", { method: "POST", body: JSON.stringify(providerPayload(values)) }),
  updateProvider: (providerId: string, values: Partial<ProviderInput>) => request<ProviderConfig>(`/api/providers/${encodeURIComponent(providerId)}`, { method: "PUT", body: JSON.stringify(providerPayload(values)) }),
};
