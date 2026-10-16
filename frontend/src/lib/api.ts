export type Settings = Record<string, string | boolean | number | null>;
export type SnowLumaSettings = { connection_mode: "reverse_ws" | "forward_ws" | "http"; websocket_url: string; http_url: string; token: string; token_configured: boolean };
export type Status = { application: string; database: string; version: string; migration: string };
export type ProviderConfig = {
  providerId: string;
  name: string;
  protocolType: "openai_responses";
  baseUrl: string;
  timeoutSeconds: number;
  organization: string;
  enabled: boolean;
  apiKeyConfigured: boolean;
  responsesModel: string;
  embeddingModel: string;
  embeddingDimensions: number | null;
  nativeSearchEnabled: boolean;
};
export type ProviderInput = Omit<ProviderConfig, "providerId" | "apiKeyConfigured"> & { apiKey?: string };

function providerPayload(values: Partial<ProviderInput>): Record<string, unknown> {
  return {
    name: values.name,
    protocol_type: values.protocolType,
    base_url: values.baseUrl,
    timeout_seconds: values.timeoutSeconds,
    organization: values.organization,
    enabled: values.enabled,
    ...(values.apiKey !== undefined ? { api_key: values.apiKey } : {}),
    responses_model: values.responsesModel,
    embedding_model: values.embeddingModel,
    embedding_dimensions: values.embeddingDimensions,
    native_search_enabled: values.nativeSearchEnabled,
  };
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", ...options, headers: { "Content-Type": "application/json", ...(options?.headers ?? {}) } });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event("vireyabot:unauthorized"));
    const detail = Array.isArray(body.detail) ? body.detail.map((item: { msg?: string }) => item.msg).filter(Boolean).join("；") : body.detail;
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
  createProvider: (values: ProviderInput) => request<ProviderConfig>("/api/providers", { method: "POST", body: JSON.stringify(providerPayload(values)) }),
  updateProvider: (providerId: string, values: Partial<ProviderInput>) => request<ProviderConfig>(`/api/providers/${encodeURIComponent(providerId)}`, { method: "PUT", body: JSON.stringify(providerPayload(values)) }),
};