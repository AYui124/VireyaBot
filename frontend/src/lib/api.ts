export type Settings = Record<string, string | boolean>;
export type SnowLumaSettings = { connection_mode: "reverse_ws" | "forward_ws" | "http"; websocket_url: string; http_url: string; token: string; token_configured: boolean };
export type Status = { application: string; database: string; version: string; migration: string };

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", ...options, headers: { "Content-Type": "application/json", ...(options?.headers ?? {}) } });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event("vireyabot:unauthorized"));
    throw new Error(typeof body.detail === "string" ? body.detail : "请求失败，请稍后重试");
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
};
