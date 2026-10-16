import { useEffect, useRef, useState } from "react";
import { Activity, Check, Copy, LayoutDashboard, LogOut, Menu, Network, Plus, RefreshCw, Server, Settings2 } from "lucide-react";
import { api, type HeaderInput, type ProviderConfig, type ProviderInput, type ProviderProtocols, type ProtocolType, type Settings, type SnowLumaSettings, type Status } from "./lib/api";
import { Button } from "./components/ui/Button";
import { Card, CardBody, CardHeader } from "./components/ui/Card";

type Page = "dashboard" | "settings" | "snowluma" | "providers";
type Notice = { kind: "success" | "error" | "info"; text: string } | null;
type ProviderForm = Omit<ProviderConfig, "providerId" | "extraHeadersConfigured" | "extraHeaderNames"> & { apiKey: string; extraHeaders: HeaderInput[] };
const emptyProvider: ProviderForm = { name: "", protocolType: "openai_responses", baseUrl: "https://api.openai.com/v1", timeoutSeconds: 30, organization: "", project: "", enabled: true, apiKeyConfigured: false, defaultStream: false, temperature: null, maxOutputTokens: 4096, extraHeaders: [], responsesModel: "gpt-4.1-mini", embeddingModel: "text-embedding-3-small", embeddingDimensions: null, nativeSearchEnabled: false, apiKey: "" };
const exclusiveFieldLabels: Record<string, string> = { organization: "组织", project: "项目", embedding_model: "Embedding 模型", embedding_dimensions: "Embedding 维度", native_search_enabled: "原生搜索" };

function Brand() { return <div className="brand"><div className="brand-mark">V</div><div><div className="brand-name">VireyaBot</div><div className="brand-subtitle">管理控制台</div></div></div>; }
function Login({ onLoggedIn }: { onLoggedIn: () => void }) { const [username, setUsername] = useState("admin"); const [password, setPassword] = useState(""); const [error, setError] = useState(""); const [busy, setBusy] = useState(false); async function submit(event: React.FormEvent) { event.preventDefault(); setError(""); setBusy(true); try { await api.login(username.trim(), password); onLoggedIn(); } catch { setError("用户名或密码不正确"); } finally { setBusy(false); } } return <main className="login-shell"><Card className="login-card"><div className="login-brand"><div className="brand-mark">V</div><div><div className="brand-name">VireyaBot</div><div className="muted">安全的管理控制台</div></div></div><h1>欢迎回来</h1><p className="muted">登录以管理系统配置与 Provider 参数。</p><form className="stack" onSubmit={submit}><div className="field"><label htmlFor="username">管理员账号</label><input id="username" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required /></div><div className="field"><label htmlFor="password">密码</label><input id="password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required /></div>{error && <div className="alert alert-error">{error}</div>}<Button type="submit" disabled={busy}>{busy ? "登录中…" : "登录"}</Button></form></Card></main>; }
function Sidebar({ page, setPage, open, close }: { page: Page; setPage: (page: Page) => void; open: boolean; close: () => void }) { const nav = [{ id: "dashboard" as const, label: "系统概览", icon: LayoutDashboard }, { id: "providers" as const, label: "Provider 参数", icon: Server }, { id: "settings" as const, label: "通用设置", icon: Settings2 }, { id: "snowluma" as const, label: "SnowLuma 配置", icon: Network }]; return <aside className={`sidebar ${open ? "open" : ""}`}><Brand /><div className="nav-label">工作区</div>{nav.map(({ id, label, icon: Icon }) => <a className={`nav-link ${page === id ? "active" : ""}`} href={id === "dashboard" ? "/" : `/${id}`} key={id} onClick={(e) => { e.preventDefault(); setPage(id); close(); }}><Icon size={17} />{label}</a>)}<div className="sidebar-footer">配置写入 PostgreSQL<br /><span>密钥使用 Fernet 加密存储</span></div></aside>; }
function Shell({ page, setPage, children, onLogout }: { page: Page; setPage: (page: Page) => void; children: React.ReactNode; onLogout: () => void }) { const [open, setOpen] = useState(false); return <div className="app-shell"><Sidebar page={page} setPage={setPage} open={open} close={() => setOpen(false)} /><div className="main"><header className="topbar"><button className="mobile-menu" aria-label="打开导航" onClick={() => setOpen(true)}><Menu size={22} /></button><div className="muted">管理员控制台</div><Button variant="ghost" onClick={onLogout}><LogOut size={16} />退出登录</Button></header>{children}</div></div>; }
function PageHeading({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: React.ReactNode }) { return <div className="page-heading"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p className="muted">{description}</p></div>{action}</div>; }
function Dashboard({ setPage }: { setPage: (page: Page) => void }) { const [status, setStatus] = useState<Status | null>(null); const [error, setError] = useState(""); useEffect(() => { api.status().then(setStatus).catch((e) => setError(e instanceof Error ? e.message : "读取系统状态失败")); }, []); return <main className="page"><PageHeading eyebrow="Overview" title="系统概览" description="查看 VireyaBot 当前运行状态与配置入口。" action={<Button variant="secondary" onClick={() => setPage("providers")}><Server size={16} />管理 Provider</Button>} />{error && <div className="alert alert-error">{error}</div>}<div className="grid-3"><Card className="stat-card"><div className="stat-label">应用状态</div><div className="stat-value">{status ? "正常" : "读取中"}</div><span className="badge badge-green"><Activity size={13} />{status?.version ?? "—"}</span></Card><Card className="stat-card"><div className="stat-label">数据库</div><div className="stat-value">{status?.database ?? "读取中"}</div></Card><Card className="stat-card"><div className="stat-label">迁移</div><div className="stat-value">{status?.migration ?? "—"}</div></Card></div></main>; }
function ProviderPage() {
  const [providers, setProviders] = useState<ProviderConfig[]>([]);
  const [protocols, setProtocols] = useState<ProviderProtocols | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [form, setForm] = useState<ProviderForm>(emptyProvider);
  const [creating, setCreating] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [copied, setCopied] = useState(false);
  const submitting = useRef(false);
  function select(item: ProviderConfig) {
    setCreating(false); setSelectedId(item.providerId); setCopied(false);
    setForm({ ...item, apiKey: "", extraHeaders: item.extraHeaderNames.map((name) => ({ name, previous_name: name, value: "" })) });
  }
  async function load() {
    setLoading(true); setLoaded(false); setNotice(null);
    try {
      const [next, metadata] = await Promise.all([api.providers(), api.providerProtocols()]);
      setProviders(next); setProtocols(metadata);
      const item = next.find((provider) => provider.providerId === selectedId) ?? next[0];
      if (item) select(item); else { setSelectedId(null); setCreating(false); }
      setLoaded(true);
    } catch (e) { setNotice({ kind: "error", text: e instanceof Error ? e.message : "读取 Provider 失败" }); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);
  function update<K extends keyof ProviderForm>(key: K, value: ProviderForm[K]) { setForm((current) => ({ ...current, [key]: value })); }
  const metadata = protocols?.[form.protocolType];
  const has = (field: string) => metadata?.fields.includes(field) ?? false;
  function changeProtocol(protocol: ProtocolType) {
    if (!protocols || protocol === form.protocolType) return;
    const next = protocols[protocol];
    const removed = protocols[form.protocolType].fields.filter((field) => !next.fields.includes(field));
    const removalText = removed.length ? removed.map((field) => exclusiveFieldLabels[field] ?? field).join("、") : "无专属字段";
    if (!window.confirm(`切换为 ${next.label}？保存时将移除：${removalText}。模型将采用新协议默认值；名称、ID、密钥及共用连接配置保持不变。取消不会修改或发送配置。`)) return;
    setForm((current) => ({ ...current, protocolType: protocol, baseUrl: creating && current.baseUrl === protocols[current.protocolType].defaults.base_url ? next.defaults.base_url : current.baseUrl, responsesModel: next.defaults.responses_model,
      embeddingModel: next.fields.includes("embedding_model") ? (protocols[current.protocolType].fields.includes("embedding_model") ? current.embeddingModel : next.defaults.embedding_model) : "",
      embeddingDimensions: next.fields.includes("embedding_dimensions") ? current.embeddingDimensions : null,
      organization: next.fields.includes("organization") ? current.organization : "", project: next.fields.includes("project") ? current.project : "",
      nativeSearchEnabled: next.fields.includes("native_search_enabled") ? current.nativeSearchEnabled : false,
      temperature: current.temperature !== null && current.temperature > next.temperature_max ? null : current.temperature,
    }));
    setNotice({ kind: "info", text: `已切换协议，尚未保存。将移除：${removalText}；请确认模型与端点的协议兼容性。超出新协议范围的温度已恢复默认。` });
  }
  function newProvider() {
    if (!protocols) return;
    const defaults = protocols.openai_responses.defaults;
    setCreating(true); setSelectedId(null); setCopied(false); setNotice(null);
    setForm({ ...emptyProvider, extraHeaders: [], baseUrl: defaults.base_url, responsesModel: defaults.responses_model, embeddingModel: defaults.embedding_model, maxOutputTokens: defaults.max_output_tokens });
  }
  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!loaded || loading || submitting.current || !metadata || (!creating && !selectedId)) return;
    try {
      const url = new URL(form.baseUrl);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash || /\/(responses|chat\/completions|messages|embeddings)\/?$/i.test(url.pathname)) throw new Error();
    } catch { setNotice({ kind: "error", text: "base_url：请输入绝对 HTTP(S) 基础地址，不得包含凭据、查询参数、片段或具体 API 端点后缀。" }); return; }
    if (!(form.timeoutSeconds > 0 && form.timeoutSeconds <= 600) || !Number.isInteger(form.maxOutputTokens) || form.maxOutputTokens < 1 || form.maxOutputTokens > 32768 || (form.temperature !== null && (!Number.isFinite(form.temperature) || form.temperature < 0 || form.temperature > metadata.temperature_max)) || (has("embedding_dimensions") && form.embeddingDimensions !== null && (!Number.isInteger(form.embeddingDimensions) || form.embeddingDimensions <= 0 || form.embeddingDimensions > 2147483647))) {
      setNotice({ kind: "error", text: "数值字段无效：超时须大于 0 且不超过 600，输出 Token 为 1–32768 的整数，维度为正整数，温度须在当前协议范围内。" }); return;
    }
    if (form.extraHeaders.some((row) => !row.name.trim() || (!row.previous_name && !row.value))) { setNotice({ kind: "error", text: "extra_headers：Header 名称不能为空，新增 Header 必须填写值。" }); return; }
    submitting.current = true; setBusy(true); setNotice(null);
    try {
      const payload: Partial<ProviderInput> = { name: form.name, protocolType: form.protocolType, baseUrl: form.baseUrl, timeoutSeconds: form.timeoutSeconds, enabled: form.enabled, responsesModel: form.responsesModel, defaultStream: form.defaultStream, temperature: form.temperature, maxOutputTokens: form.maxOutputTokens, extraHeaders: form.extraHeaders,
        ...(form.apiKey ? { apiKey: form.apiKey } : {}),
        ...(has("organization") ? { organization: form.organization } : {}), ...(has("project") ? { project: form.project } : {}),
        ...(has("embedding_model") ? { embeddingModel: form.embeddingModel } : {}), ...(has("embedding_dimensions") ? { embeddingDimensions: form.embeddingDimensions } : {}),
        ...(has("native_search_enabled") ? { nativeSearchEnabled: form.nativeSearchEnabled } : {}),
        ...(!creating && providers.find((item) => item.providerId === selectedId)?.protocolType !== form.protocolType ? { confirmProtocolChange: true } : {}),
      };
      const saved = creating ? await api.createProvider(payload) : await api.updateProvider(selectedId!, payload);
      setProviders((current) => creating ? [...current, saved] : current.map((item) => item.providerId === saved.providerId ? saved : item)); select(saved);
      setNotice({ kind: "success", text: "Provider 已保存；未发起真实服务请求。" });
    } catch (e) { setNotice({ kind: "error", text: e instanceof Error ? e.message : "保存失败，请检查字段" }); }
    finally { submitting.current = false; setBusy(false); }
  }
  async function toggle() {
    if (!selectedId || !loaded || submitting.current) return;
    submitting.current = true; setBusy(true);
    try {
      const saved = await api.updateProvider(selectedId, { enabled: !form.enabled });
      setProviders((current) => current.map((item) => item.providerId === saved.providerId ? saved : item));
      update("enabled", saved.enabled);
      setNotice({ kind: "success", text: saved.enabled ? "Provider 已启用。其他未保存修改仍保留。" : "Provider 已停用。其他未保存修改仍保留。" });
    } catch (e) { setNotice({ kind: "error", text: e instanceof Error ? e.message : "状态保存失败" }); }
    finally { submitting.current = false; setBusy(false); }
  }
  async function copyId() {
    if (!selectedId) return;
    try { await navigator.clipboard.writeText(selectedId); setCopied(true); setNotice({ kind: "success", text: "Provider ID 已复制。" }); }
    catch { setCopied(false); setNotice({ kind: "error", text: "复制失败，请手动选择并复制 Provider ID。" }); }
  }
  const disabled = !loaded || loading || busy;
  return <main className="page">
    <PageHeading eyebrow="Model integrations" title="Provider 参数" description="管理多个 Provider。协议类型决定请求格式，不会根据地址猜测或自动回退。" action={<div className="actions"><Button variant="secondary" onClick={() => void load()} disabled={loading || busy}><RefreshCw size={16} />刷新</Button><Button onClick={newProvider} disabled={disabled}><Plus size={16} />新增 Provider</Button></div>} />
    {notice && <div role="status" className={`alert alert-${notice.kind}`}>{notice.text}</div>}
    <div className="provider-layout"><Card><CardHeader><h2>Provider 列表</h2></CardHeader><CardBody>
      {loading ? <div className="empty">正在读取…</div> : providers.length === 0 ? <div className="empty">{loaded ? "暂无 Provider，请新增配置。" : "读取失败，请刷新重试。"}</div> : providers.map((item) => <button disabled={disabled} className={`provider-row ${selectedId === item.providerId ? "selected" : ""}`} key={item.providerId} onClick={() => { select(item); setNotice(null); }}><span><strong>{item.name}</strong><small>{protocols?.[item.protocolType].label ?? item.protocolType}</small></span><span className={`badge ${item.enabled ? "badge-green" : "badge-slate"}`}>{item.enabled ? "启用" : "停用"}</span></button>)}
    </CardBody></Card><Card><CardHeader><div><h2>{creating ? "新增 Provider" : "编辑 Provider"}</h2><p className="muted">{selectedId ? <>providerId：<code>{selectedId}</code> <button type="button" className="icon-button" onClick={() => void copyId()} aria-label="复制 providerId">{copied ? <Check size={15} /> : <Copy size={15} />}</button></> : "协议和密钥按配置单独保存。"}</p></div></CardHeader>
      {!selectedId && !creating ? <CardBody><div className="empty">请选择或新增 Provider。</div></CardBody> : <form onSubmit={save}><CardBody><fieldset className="provider-fields stack" disabled={disabled}>
        <div className="grid-2"><div className="field"><label htmlFor="provider-name">名称</label><input id="provider-name" value={form.name} onChange={(e) => update("name", e.target.value)} required /></div><div className="field"><label htmlFor="protocol">协议类型</label><select id="protocol" value={form.protocolType} onChange={(e) => changeProtocol(e.target.value as ProtocolType)}>{protocols && (Object.keys(protocols) as ProtocolType[]).map((protocol) => <option key={protocol} value={protocol}>{protocols[protocol].label}</option>)}</select></div></div>
        <div className="alert alert-info">{metadata?.label}：适配器能力为 {metadata?.capabilities.join("、")}。切换协议不会删除已保存的 API Key。原生搜索需单独启用，真实端点与模型能力仍需验收。</div>
        <div className="field"><label htmlFor="base-url">端点地址</label><input id="base-url" type="url" value={form.baseUrl} onChange={(e) => update("baseUrl", e.target.value)} required /><small className="muted">填写基础地址（例如 {metadata?.defaults.base_url}），不含凭据、查询参数、片段或具体请求端点。</small></div>
        <div className="grid-2"><div className="field"><label htmlFor="api-key">{form.apiKeyConfigured ? "API Key（已配置，留空保持不变）" : "API Key（未配置）"}</label><input id="api-key" type="password" value={form.apiKey} onChange={(e) => update("apiKey", e.target.value)} autoComplete="new-password" /></div><div className="field"><label htmlFor="model">模型</label><input id="model" value={form.responsesModel} onChange={(e) => update("responsesModel", e.target.value)} required /></div></div>
        {has("organization") && <div className="grid-2"><div className="field"><label htmlFor="organization">组织（Organization）</label><input id="organization" value={form.organization} onChange={(e) => update("organization", e.target.value)} /></div>{has("project") && <div className="field"><label htmlFor="project">项目（Project）</label><input id="project" value={form.project} onChange={(e) => update("project", e.target.value)} /></div>}</div>}
        {has("embedding_model") && <div className="grid-2"><div className="field"><label htmlFor="embedding-model">Embedding 模型</label><input id="embedding-model" value={form.embeddingModel} onChange={(e) => update("embeddingModel", e.target.value)} required /></div><div className="field"><label htmlFor="dimensions">Embedding 维度</label><input id="dimensions" type="number" min="1" step="1" value={form.embeddingDimensions ?? ""} onChange={(e) => update("embeddingDimensions", e.target.value ? Number(e.target.value) : null)} /></div></div>}
        <div className="grid-2"><div className="field"><label htmlFor="timeout">超时（秒）</label><input id="timeout" type="number" min="0" max="600" step="any" value={form.timeoutSeconds} onChange={(e) => update("timeoutSeconds", Number(e.target.value))} required /></div><div className="field"><label htmlFor="max-output-tokens">最大输出 Token</label><input id="max-output-tokens" type="number" min="1" max="32768" step="1" value={form.maxOutputTokens} onChange={(e) => update("maxOutputTokens", Number(e.target.value))} required /></div></div>
        <div className="grid-2"><label className="checkbox"><input type="checkbox" checked={form.defaultStream} onChange={(e) => update("defaultStream", e.target.checked)} />默认启用流式</label>{has("native_search_enabled") && <label className="checkbox"><input type="checkbox" checked={form.nativeSearchEnabled} onChange={(e) => update("nativeSearchEnabled", e.target.checked)} />启用原生搜索</label>}</div>
        <div className="field"><label htmlFor="temperature">温度（Temperature，0–{metadata?.temperature_max}，留空使用默认值）</label><input id="temperature" type="number" min="0" max={metadata?.temperature_max} step="any" value={form.temperature ?? ""} onChange={(e) => update("temperature", e.target.value ? Number(e.target.value) : null)} /></div>
        <section className="stack" aria-label="附加 Header"><div><h3>附加 Header</h3><p className="muted">值由服务端加密保存，不会回显。已有行留空保持原值（重命名也保留）；删除行将在保存时移除。新增行必须填写值。</p></div>
          {form.extraHeaders.map((row, index) => <div className="provider-header-row" key={index}><div className="field"><label htmlFor={`header-name-${index}`}>Header 名称 {index + 1}</label><input id={`header-name-${index}`} value={row.name} onChange={(e) => update("extraHeaders", form.extraHeaders.map((item, rowIndex) => rowIndex === index ? { ...item, name: e.target.value } : item))} required /></div><div className="field"><label htmlFor={`header-value-${index}`}>Header 值 {index + 1}{row.previous_name ? "（留空保持不变）" : "（必填）"}</label><input id={`header-value-${index}`} type="password" autoComplete="new-password" value={row.value} onChange={(e) => update("extraHeaders", form.extraHeaders.map((item, rowIndex) => rowIndex === index ? { ...item, value: e.target.value } : item))} required={!row.previous_name} /></div><Button type="button" variant="secondary" aria-label={`删除 Header ${index + 1}`} onClick={() => update("extraHeaders", form.extraHeaders.filter((_, rowIndex) => rowIndex !== index))}>删除</Button></div>)}
          <div><Button type="button" variant="secondary" onClick={() => update("extraHeaders", [...form.extraHeaders, { name: "", value: "" }])}><Plus size={16} />添加 Header</Button></div>
        </section>
        <div className="actions"><Button type="submit" disabled={disabled}>{busy ? "保存中…" : "保存 Provider"}</Button>{selectedId && <Button type="button" variant="secondary" onClick={() => void toggle()} disabled={disabled}>{form.enabled ? "停用" : "启用"}</Button>}</div>
      </fieldset></CardBody></form>}
    </Card></div>
  </main>;
}
function SettingsPage() {
  const [values, setValues] = useState<Settings>({});
  const [savedValues, setSavedValues] = useState<Settings>({});
  const [initializing, setInitializing] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const submitting = useRef(false);
  async function load() { setInitializing(true); setLoaded(false); setNotice(null); try { const next = await api.settings(); setValues(next); setSavedValues(next); setLoaded(true); } catch (e) { setNotice({ kind: "error", text: e instanceof Error ? e.message : "读取配置失败" }); } finally { setInitializing(false); } }
  useEffect(() => { void load(); }, []);
  async function save(e: React.FormEvent) { e.preventDefault(); if (!loaded || initializing || submitting.current) return; submitting.current = true; setBusy(true); try { const next = { "app.name": values["app.name"] ?? "", "logging.level": values["logging.level"] ?? "INFO", "features.proactive_enabled": values["features.proactive_enabled"] ?? false }; await api.saveSettings(next); setSavedValues({ ...values, ...next }); setNotice({ kind: "success", text: "通用设置已保存。" }); } catch (err) { setNotice({ kind: "error", text: err instanceof Error ? err.message : "保存失败" }); } finally { submitting.current = false; setBusy(false); } }
  function cancel() { setValues(savedValues); setNotice({ kind: "info", text: "已取消未保存的修改。" }); }
  return <main className="page"><PageHeading eyebrow="Workspace settings" title="通用设置" description="管理应用显示名称与运行行为。" action={<Button variant="secondary" onClick={() => void load()} disabled={initializing || busy}><RefreshCw size={16} />重新读取</Button>} />{notice && <div className={`alert alert-${notice.kind}`}>{notice.text}</div>}<Card><CardHeader><h2>应用偏好</h2></CardHeader><form onSubmit={save}><CardBody className="stack">{initializing ? <div className="empty">正在读取配置…</div> : <><div className="field"><label htmlFor="app-name">应用名称</label><input id="app-name" value={String(values["app.name"] ?? "")} onChange={(e) => setValues({ ...values, "app.name": e.target.value })} /></div><div className="field"><label htmlFor="logging-level">日志级别</label><select id="logging-level" value={String(values["logging.level"] ?? "INFO")} onChange={(e) => setValues({ ...values, "logging.level": e.target.value })}><option>DEBUG</option><option>INFO</option><option>WARNING</option><option>ERROR</option></select></div><label className="checkbox"><input type="checkbox" checked={Boolean(values["features.proactive_enabled"])} onChange={(e) => setValues({ ...values, "features.proactive_enabled": e.target.checked })} />启用主动会话</label><div className="actions"><Button type="submit" disabled={!loaded || busy}>保存设置</Button><Button type="button" variant="secondary" onClick={cancel} disabled={!loaded || busy}>取消</Button></div></>}</CardBody></form></Card></main>;
}
function SnowLumaPage() { const [values, setValues] = useState<SnowLumaSettings>({ connection_mode: "forward_ws", websocket_url: "", http_url: "", token: "", token_configured: false }); const [initializing, setInitializing] = useState(true); const [loaded, setLoaded] = useState(false); const [busy, setBusy] = useState(false); const [notice, setNotice] = useState<Notice>(null); const submitting = useRef(false); async function load() { setInitializing(true); setLoaded(false); try { setValues(await api.snowluma()); setLoaded(true); } catch (e) { setNotice({ kind: "error", text: e instanceof Error ? e.message : "读取 SnowLuma 配置失败" }); } finally { setInitializing(false); } } useEffect(() => { void load(); }, []); async function save(e: React.FormEvent) { e.preventDefault(); if (!loaded || initializing || submitting.current) return; submitting.current = true; setBusy(true); try { await api.saveSnowLuma({ connection_mode: values.connection_mode, websocket_url: values.connection_mode === "http" ? "" : values.websocket_url, http_url: values.connection_mode === "http" ? values.http_url : "", ...(values.token && values.token !== "••••••••" ? { token: values.token } : {}) }); await load(); setNotice({ kind: "success", text: "SnowLuma 参数已保存；未进行连接验证。" }); } catch (err) { setNotice({ kind: "error", text: err instanceof Error ? err.message : "保存失败，请检查模式与地址" }); } finally { submitting.current = false; setBusy(false); } } return <main className="page"><PageHeading eyebrow="External integration" title="SnowLuma 配置" description="保存 OneBot v11 连接参数。参数会持久化，但不会自动连接外部实例。" action={<Button variant="secondary" onClick={() => void load()} disabled={initializing || busy}><RefreshCw size={16} />重新读取</Button>} />{notice && <div className={`alert alert-${notice.kind}`}>{notice.text}</div>}<Card><CardHeader><h2>连接参数</h2></CardHeader><form onSubmit={save}><CardBody className="stack">{initializing ? <div className="empty">正在读取配置…</div> : <><div className="field"><label htmlFor="connection-mode">连接模式</label><select id="connection-mode" value={values.connection_mode} onChange={(e) => setValues({ ...values, connection_mode: e.target.value as SnowLumaSettings["connection_mode"] })}><option value="reverse_ws">反向 WebSocket</option><option value="forward_ws">正向 WebSocket</option><option value="http">HTTP</option></select></div>{values.connection_mode === "http" ? <div className="field"><label htmlFor="http-url">HTTP 地址</label><input id="http-url" type="url" value={values.http_url} onChange={(e) => setValues({ ...values, http_url: e.target.value })} required /></div> : <div className="field"><label htmlFor="websocket-url">WebSocket 地址</label><input id="websocket-url" type="url" value={values.websocket_url} onChange={(e) => setValues({ ...values, websocket_url: e.target.value })} required /></div>}<div className="field"><label htmlFor="snowluma-token">Token {values.token_configured && "（已配置，留空保持不变）"}</label><input id="snowluma-token" type="password" value={values.token} onChange={(e) => setValues({ ...values, token: e.target.value })} autoComplete="new-password" /></div><Button type="submit" disabled={!loaded || initializing || busy}>保存 SnowLuma 配置</Button></>}</CardBody></form></Card></main>; }
export default function App() { const [authenticated, setAuthenticated] = useState<boolean | null>(null); const [page, setPage] = useState<Page>(() => { const path = window.location.pathname; return path === "/providers" ? "providers" : path === "/snowluma" ? "snowluma" : path === "/settings" ? "settings" : "dashboard"; }); useEffect(() => { api.me().then(() => setAuthenticated(true)).catch(() => setAuthenticated(false)); const onUnauthorized = () => setAuthenticated(false); window.addEventListener("vireyabot:unauthorized", onUnauthorized); return () => window.removeEventListener("vireyabot:unauthorized", onUnauthorized); }, []); function navigate(next: Page) { setPage(next); window.history.replaceState({}, "", next === "dashboard" ? "/" : `/${next}`); } if (authenticated === null) return <div className="login-shell"><div className="muted">正在验证登录状态…</div></div>; if (!authenticated) return <Login onLoggedIn={() => setAuthenticated(true)} />; return <Shell page={page} setPage={navigate} onLogout={() => { void api.logout(); setAuthenticated(false); }}>{page === "dashboard" ? <Dashboard setPage={navigate} /> : page === "providers" ? <ProviderPage /> : page === "settings" ? <SettingsPage /> : <SnowLumaPage />}</Shell>; }
