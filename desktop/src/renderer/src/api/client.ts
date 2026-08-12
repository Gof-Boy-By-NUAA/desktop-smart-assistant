import type {
  ConfigData,
  ChannelInfo,
  ChannelAction,
  SkillInfo,
  ToolInfo,
  MemoryItem,
  MemoryCategory,
  MemoryPage,
  SchedulerTask,
  Attachment,
  SessionsPage,
  HistoryPage,
  ModelsData,
  ModelsAction,
  KnowledgeList,
  KnowledgeGraph,
  KnowledgeAction,
  KnowledgeImportPayload,
  KnowledgeCitationResolve,
  WorkspaceEntry,
  WorkspaceTree,
} from '../types'

interface ApiResult {
  status: string
  message?: string
  execution?: {
    status?: string
    execution_id?: string
  }
}

const BACKEND_ORIGIN = 'smart_assistant://backend'

/** Minimum EventSource surface used by the desktop UI. */
export interface BackendEventSource {
  onmessage: ((event: MessageEvent<string>) => void) | null
  onerror: ((event: Event) => void) | null
  close: () => void
}

function newStreamId(): string {
  const uuid = globalThis.crypto?.randomUUID?.()
  return `desktop_${uuid || `${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`}`
}

/**
 * An EventSource-shaped IPC facade. The renderer has no network endpoint or
 * bearer; Electron main owns the pinned TLS connection and forwards frames.
 */
class IpcBackendEventSource implements BackendEventSource {
  private readonly streamId = newStreamId()
  private closed = false
  private unsubscribe: (() => void) | null = null
  private queuedMessages: string[] = []
  private queuedErrors: string[] = []
  private messageHandler: ((event: MessageEvent<string>) => void) | null = null
  private errorHandler: ((event: Event) => void) | null = null

  constructor(path: string) {
    const api = window.electronAPI
    if (!api) {
      queueMicrotask(() => this.dispatchError('Trusted desktop bridge unavailable'))
      return
    }
    this.unsubscribe = api.onBackendStream((event) => {
      if (event.streamId !== this.streamId || this.closed) return
      if (event.kind === 'message' && typeof event.data === 'string') {
        this.dispatchMessage(event.data)
      } else if (event.kind === 'error') {
        this.dispatchError(event.error || 'Backend stream failed')
      } else if (event.kind === 'closed') {
        this.dispatchError(event.error || 'Backend stream closed')
      }
    })
    void api.openBackendStream({ streamId: this.streamId, path }).catch((error: unknown) => {
      this.dispatchError(error instanceof Error ? error.message : 'Backend stream failed')
    })
  }

  get onmessage() {
    return this.messageHandler
  }

  set onmessage(handler: ((event: MessageEvent<string>) => void) | null) {
    this.messageHandler = handler
    if (!handler || !this.queuedMessages.length) return
    const queued = this.queuedMessages.splice(0)
    for (const data of queued) handler({ data } as MessageEvent<string>)
  }

  get onerror() {
    return this.errorHandler
  }

  set onerror(handler: ((event: Event) => void) | null) {
    this.errorHandler = handler
    if (!handler || !this.queuedErrors.length) return
    this.queuedErrors.length = 0
    handler(new Event('error'))
  }

  close = () => {
    if (this.closed) return
    this.closed = true
    this.unsubscribe?.()
    this.unsubscribe = null
    void window.electronAPI?.closeBackendStream(this.streamId)
  }

  private dispatchMessage(data: string) {
    if (this.closed) return
    if (this.messageHandler) {
      this.messageHandler({ data } as MessageEvent<string>)
    } else {
      this.queuedMessages.push(data)
    }
  }

  private dispatchError(message: string) {
    if (this.closed) return
    if (this.errorHandler) {
      this.errorHandler(new Event('error'))
    } else {
      this.queuedErrors.push(message)
    }
  }
}

function base64ToBytes(value: string): Uint8Array {
  if (typeof value !== 'string' || value.length > 64 * 1024 * 1024) {
    throw new Error('Invalid desktop backend response')
  }
  try {
    const binary = atob(value)
    const output = new Uint8Array(binary.length)
    for (let index = 0; index < binary.length; index += 1) output[index] = binary.charCodeAt(index)
    return output
  } catch {
    throw new Error('Invalid desktop backend response encoding')
  }
}

function concatBytes(parts: Uint8Array[]): Uint8Array {
  const total = parts.reduce((size, part) => size + part.byteLength, 0)
  const output = new Uint8Array(total)
  let offset = 0
  for (const part of parts) {
    output.set(part, offset)
    offset += part.byteLength
  }
  return output
}

function safeMultipartHeader(value: string): string {
  return value.replace(/[\r\n]/g, '').replace(/"/g, '%22')
}

async function serializeFormData(body: FormData, headers: Headers): Promise<Uint8Array> {
  const boundary = `----smart-assistant-${newStreamId().replace(/[^A-Za-z0-9]/g, '')}`
  if (!headers.has('Content-Type')) headers.set('Content-Type', `multipart/form-data; boundary=${boundary}`)
  const encoder = new TextEncoder()
  const parts: Uint8Array[] = []
  for (const [name, value] of body.entries()) {
    let disposition = `--${boundary}\r\nContent-Disposition: form-data; name="${safeMultipartHeader(name)}"`
    if (typeof value === 'string') {
      parts.push(encoder.encode(`${disposition}\r\n\r\n${value}\r\n`))
      continue
    }
    const filename = typeof File !== 'undefined' && value instanceof File ? value.name : 'blob'
    const contentType = value.type || 'application/octet-stream'
    disposition += `; filename="${safeMultipartHeader(filename)}"\r\nContent-Type: ${safeMultipartHeader(contentType)}\r\n\r\n`
    parts.push(encoder.encode(disposition), new Uint8Array(await value.arrayBuffer()), encoder.encode('\r\n'))
  }
  parts.push(encoder.encode(`--${boundary}--\r\n`))
  return concatBytes(parts)
}

async function serializeBody(body: BodyInit | null | undefined, headers: Headers): Promise<string | Uint8Array | undefined> {
  if (body == null) return undefined
  if (typeof body === 'string') return body
  if (body instanceof FormData) return serializeFormData(body, headers)
  if (body instanceof URLSearchParams) {
    if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/x-www-form-urlencoded;charset=UTF-8')
    return body.toString()
  }
  if (body instanceof Blob) return new Uint8Array(await body.arrayBuffer())
  if (body instanceof ArrayBuffer) return new Uint8Array(body)
  if (ArrayBuffer.isView(body)) return new Uint8Array(body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength))
  throw new Error('Unsupported desktop request body')
}

function backendResourceUrl(value: string, prefix: '/file/' | '/preview/'): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > 8192 || !value.startsWith('/')) return ''
  try {
    const parsed = new URL(value, 'https://smart_assistant.invalid')
    if (parsed.origin !== 'https://smart_assistant.invalid' || !parsed.pathname.startsWith(prefix)) return ''
    return `${BACKEND_ORIGIN}${parsed.pathname}${parsed.search}`
  } catch {
    return ''
  }
}

class ApiClient {
  // Compatibility for pages that still receive the opaque backend origin.
  // Never accept a mutable HTTP origin in the renderer.
  setBaseUrl(_url: string) {}

  getBaseUrl() {
    return BACKEND_ORIGIN
  }

  private async authenticatedFetch(
    path: string,
    options?: RequestInit,
    acceptedErrorStatuses: readonly number[] = [],
  ): Promise<Response> {
    const api = window.electronAPI
    if (!api) throw new Error('Trusted desktop bridge unavailable')
    const headers = new Headers(options?.headers)
    // Electron main, not the renderer, attaches the backend bearer after the
    // request traverses the pinned TLS channel.  Let multipart serialization
    // set its own boundary; JSON callers receive the shared content type.
    if (typeof options?.body === 'string' && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json')
    }
    const response = await api.backendRequest({
      path,
      method: options?.method || 'GET',
      headers: Object.fromEntries(headers.entries()),
      body: await serializeBody(options?.body, headers),
    })
    if (!Number.isInteger(response.status) || response.status < 100 || response.status > 599) {
      throw new Error('Invalid desktop backend response')
    }
    const res = new Response(base64ToBytes(response.bodyBase64), {
      status: response.status,
      statusText: response.statusText || '',
      headers: response.headers,
    })
    if (!res.ok && !acceptedErrorStatuses.includes(res.status)) {
      let detail = ''
      try {
        const payload = await res.clone().json() as { message?: string; error?: string; error_code?: string }
        detail = payload.message || payload.error || payload.error_code || ''
      } catch {
        try { detail = (await res.clone().text()).trim() } catch { /* no response body */ }
      }
      throw new Error(
        `HTTP ${res.status}: ${detail || res.statusText || 'Request failed'}`
      )
    }
    return res
  }

  private async request<T>(path: string, options?: RequestInit): Promise<T> {
    const res = await this.authenticatedFetch(path, options)
    return res.json()
  }

  // ---------------------------------------------------------
  // Chat / messages
  // ---------------------------------------------------------

  async sendMessage(
    sessionId: string,
    message: string,
    opts?: {
      stream?: boolean
      attachments?: Attachment[]
      isVoice?: boolean
      lang?: string
      idempotencyKey?: string
    }
  ): Promise<{
    status: string
    request_id: string
    stream: boolean
    inline_reply?: string
    execution_state?: 'queued' | 'running' | 'completed' | 'cancelled' | 'failed_safe' | 'in_doubt'
    queued?: boolean
    queue_position?: number
  }> {
    return this.request('/message', {
      method: 'POST',
      body: JSON.stringify({
        session_id: sessionId,
        message,
        stream: opts?.stream ?? true,
        attachments: opts?.attachments,
        is_voice: opts?.isVoice ?? false,
        lang: opts?.lang,
        idempotency_key: opts?.idempotencyKey,
      }),
    })
  }

  async poll(sessionId: string): Promise<{
    status: string
    has_content: boolean
    content?: string
    request_id?: string
    timestamp?: number
  }> {
    return this.request('/poll', {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId }),
    })
  }

  async cancel(opts: { requestId?: string; sessionId?: string; lang?: string }): Promise<{ status: string; cancelled: number; cancellation_requested?: number; cancellation_accepted?: number; message?: string }> {
    return this.request('/cancel', {
      method: 'POST',
      body: JSON.stringify({ request_id: opts.requestId, session_id: opts.sessionId, lang: opts.lang }),
    })
  }

  async createSSEStream(requestId: string, afterEventId = 0): Promise<BackendEventSource> {
    const ticket = await this.request<{ status: string; ticket?: string }>('/stream/ticket', {
      method: 'POST',
      body: JSON.stringify({ request_id: requestId, after_event_id: afterEventId }),
    })
    if (ticket.status !== 'success' || !ticket.ticket) {
      throw new Error('SSE authorization ticket was not issued')
    }
    return new IpcBackendEventSource(
      `/stream?request_id=${encodeURIComponent(requestId)}&ticket=${encodeURIComponent(ticket.ticket)}`,
    )
  }

  async deleteMessage(opts: {
    sessionId: string
    userSeq: number
    deleteUser?: boolean
    cascade?: boolean
  }): Promise<{ status: string; deleted: number }> {
    return this.request('/api/messages/delete', {
      method: 'POST',
      body: JSON.stringify({
        session_id: opts.sessionId,
        user_seq: opts.userSeq,
        delete_user: opts.deleteUser ?? true,
        cascade: opts.cascade ?? false,
      }),
    })
  }

  // ---------------------------------------------------------
  // Upload / files
  // ---------------------------------------------------------

  async uploadFile(file: File, sessionId?: string): Promise<{
    status: string
    file_path: string
    file_name: string
    file_type: string
    preview_url: string
  }> {
    const formData = new FormData()
    formData.append('file', file)
    if (sessionId) formData.append('session_id', sessionId)
    const res = await this.authenticatedFetch('/upload', {
      method: 'POST',
      body: formData,
    })
    return res.json()
  }

  getFileUrl(previewUrl: string): string {
    if (/^https?:\/\//.test(previewUrl)) return previewUrl
    // New backend responses use an expiring, path/owner-bound `/file/...`
    // capability. Do not append the long-lived bearer to a URL that may enter
    // browser history, proxy logs, or a copied link.
    if (previewUrl.startsWith('/file/')) return backendResourceUrl(previewUrl, '/file/')
    // Callers must obtain a fresh owner/path-bound capability from their
    // authenticated API response.  Refuse legacy raw paths rather than putting
    // a bearer credential into a URL.
    return ''
  }

  // ---------------------------------------------------------
  // Workspace browsing / preview
  // ---------------------------------------------------------

  async workspaceTree(path = ''): Promise<WorkspaceTree & ApiResult> {
    return this.request(`/api/workspace/tree?path=${encodeURIComponent(path)}`)
  }

  async workspaceSearch(query: string, limit = 30): Promise<{ results: WorkspaceEntry[] } & ApiResult> {
    return this.request(`/api/workspace/search?q=${encodeURIComponent(query)}&limit=${limit}`)
  }

  async workspaceResolve(path: string): Promise<{ file: WorkspaceEntry } & ApiResult> {
    return this.request(`/api/workspace/resolve?path=${encodeURIComponent(path)}`)
  }

  /** Absolute URL for a `/preview/...` path. The signed token in the path is
   *  what authorizes it, so no auth token is appended. */
  getPreviewUrl(previewPath: string): string {
    if (/^https?:\/\//.test(previewPath)) return previewPath
    return backendResourceUrl(previewPath, '/preview/')
  }

  // ---------------------------------------------------------
  // Sessions
  // ---------------------------------------------------------

  async getSessions(page = 1, pageSize = 50): Promise<SessionsPage> {
    return this.request<{ status: string } & SessionsPage>(`/api/sessions?page=${page}&page_size=${pageSize}`)
  }

  async deleteSession(sessionId: string): Promise<ApiResult> {
    return this.request(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
  }

  async renameSession(sessionId: string, title: string): Promise<ApiResult> {
    return this.request(`/api/sessions/${encodeURIComponent(sessionId)}`, {
      method: 'PUT',
      body: JSON.stringify({ title }),
    })
  }

  async generateSessionTitle(sessionId: string, userMessage: string, assistantReply?: string): Promise<{ status: string; title: string }> {
    return this.request(`/api/sessions/${encodeURIComponent(sessionId)}/generate_title`, {
      method: 'POST',
      body: JSON.stringify({ user_message: userMessage, assistant_reply: assistantReply }),
    })
  }

  async clearContext(sessionId: string): Promise<{ status: string; context_start_seq: number }> {
    return this.request(`/api/sessions/${encodeURIComponent(sessionId)}/clear_context`, { method: 'POST' })
  }

  async getHistory(sessionId: string, page = 1, pageSize = 20): Promise<HistoryPage> {
    return this.request<{ status: string } & HistoryPage>(
      `/api/history?session_id=${encodeURIComponent(sessionId)}&page=${page}&page_size=${pageSize}`
    )
  }

  // ---------------------------------------------------------
  // Config
  // ---------------------------------------------------------

  async getConfig(): Promise<ConfigData> {
    return this.request<{ status: string } & ConfigData>('/config')
  }

  async updateConfig(updates: Record<string, unknown>): Promise<{ status: string; applied: Record<string, unknown> }> {
    return this.request('/config', {
      method: 'POST',
      body: JSON.stringify({ updates }),
    })
  }

  // ---------------------------------------------------------
  // Models console
  // ---------------------------------------------------------

  async getModels(): Promise<ModelsData> {
    return this.request<{ status: string } & ModelsData>('/api/models')
  }

  async modelsAction(action: ModelsAction): Promise<Record<string, unknown> & { status: string }> {
    return this.request('/api/models', {
      method: 'POST',
      body: JSON.stringify(action),
    })
  }

  // ---------------------------------------------------------
  // Channels
  // ---------------------------------------------------------

  async getChannels(): Promise<ChannelInfo[]> {
    const data = await this.request<{ status: string; channels: ChannelInfo[] }>('/api/channels')
    return data.channels
  }

  async channelAction(
    action: ChannelAction,
    channel: string,
    config?: Record<string, unknown>
  ): Promise<Record<string, unknown> & { status: string }> {
    return this.request('/api/channels', {
      method: 'POST',
      body: JSON.stringify({ action, channel, config }),
    })
  }

  // Weixin QR login
  async getWeixinQr(): Promise<{ status: string; qrcode_url?: string; qr_image?: string; source?: string; message?: string }> {
    return this.request('/api/weixin/qrlogin')
  }

  async weixinQrAction(action: 'poll' | 'refresh'): Promise<Record<string, unknown> & { status: string }> {
    return this.request('/api/weixin/qrlogin', {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
  }

  // Feishu one-click register
  async getFeishuRegister(): Promise<{ status: string; register_status?: string; qrcode_url?: string; qr_image?: string; expire_in?: number; message?: string }> {
    return this.request('/api/feishu/register')
  }

  async feishuRegisterPoll(): Promise<Record<string, unknown> & { status: string }> {
    return this.request('/api/feishu/register', {
      method: 'POST',
      body: JSON.stringify({ action: 'poll' }),
    })
  }

  // ---------------------------------------------------------
  // Tools & skills
  // ---------------------------------------------------------

  async getTools(): Promise<ToolInfo[]> {
    const data = await this.request<{ status: string; tools: ToolInfo[] }>('/api/tools')
    return data.tools
  }

  async getSkills(): Promise<SkillInfo[]> {
    const data = await this.request<{ status: string; skills: SkillInfo[] }>('/api/skills')
    return data.skills
  }

  async toggleSkill(name: string, action: 'open' | 'close'): Promise<ApiResult> {
    return this.request('/api/skills', {
      method: 'POST',
      body: JSON.stringify({ action, name }),
    })
  }

  // ---------------------------------------------------------
  // Memory
  // ---------------------------------------------------------

  async getMemoryList(page = 1, pageSize = 20, category: MemoryCategory = 'memory'): Promise<MemoryPage> {
    return this.request<{ status: string } & MemoryPage>(
      `/api/memory?page=${page}&page_size=${pageSize}&category=${category}`
    )
  }

  async getMemoryContent(filename: string, category: MemoryCategory = 'memory'): Promise<string> {
    const data = await this.request<{ status: string; content: string }>(
      `/api/memory/content?filename=${encodeURIComponent(filename)}&category=${category}`
    )
    return data.content
  }

  // ---------------------------------------------------------
  // Knowledge
  // ---------------------------------------------------------

  async getKnowledgeList(): Promise<KnowledgeList> {
    return this.request<{ status: string } & KnowledgeList>('/api/knowledge/list')
  }

  async readKnowledge(path: string): Promise<{ status: string; content: string; path: string }> {
    return this.request(`/api/knowledge/read?path=${encodeURIComponent(path)}`)
  }

  async resolveKnowledgeCitation(uri: string): Promise<KnowledgeCitationResolve> {
    return this.request('/api/knowledge/citation/resolve', {
      method: 'POST',
      body: JSON.stringify({ uri }),
    })
  }

  async getKnowledgeGraph(): Promise<KnowledgeGraph> {
    return this.request<KnowledgeGraph>('/api/knowledge/graph')
  }

  async knowledgeAction(req: KnowledgeAction): Promise<Record<string, unknown> & { status: string }> {
    return this.request('/api/knowledge/action', {
      method: 'POST',
      body: JSON.stringify(req),
    })
  }

  // Bulk import: upload .md/.txt files into a target category (multipart).
  async importKnowledge(
    files: File[],
    targetCategory: string
  ): Promise<{ status: string; message?: string; payload?: KnowledgeImportPayload }> {
    const formData = new FormData()
    formData.append('target_category', targetCategory)
    formData.append('conflict_strategy', 'rename')
    files.forEach((file) => formData.append('files', file, file.name))
    const res = await this.authenticatedFetch('/api/knowledge/import', {
      method: 'POST',
      body: formData,
    })
    return res.json()
  }

  // ---------------------------------------------------------
  // Scheduler
  // ---------------------------------------------------------

  async getSchedulerTasks(): Promise<SchedulerTask[]> {
    const data = await this.request<{ status: string; tasks: SchedulerTask[] }>('/api/scheduler')
    return data.tasks
  }

  async runTask(taskId: string, idempotencyKey: string): Promise<ApiResult> {
    return this.request('/api/scheduler/run', {
      method: 'POST',
      body: JSON.stringify({ task_id: taskId, idempotency_key: idempotencyKey }),
    })
  }

  async toggleTask(taskId: string, enabled: boolean): Promise<{ status: string; task: SchedulerTask }> {
    return this.request('/api/scheduler/toggle', {
      method: 'POST',
      body: JSON.stringify({ task_id: taskId, enabled }),
    })
  }

  async updateTask(taskId: string, updates: Partial<Pick<SchedulerTask, 'name' | 'enabled' | 'schedule' | 'action'>>): Promise<{ status: string; task: SchedulerTask }> {
    return this.request('/api/scheduler/update', {
      method: 'POST',
      body: JSON.stringify({ task_id: taskId, ...updates }),
    })
  }

  async deleteTask(taskId: string): Promise<ApiResult> {
    return this.request('/api/scheduler/delete', {
      method: 'POST',
      body: JSON.stringify({ task_id: taskId }),
    })
  }

  // ---------------------------------------------------------
  // Voice
  // ---------------------------------------------------------

  async voiceAsr(audio: File | Blob): Promise<{ status: string; text?: string; audio_url?: string; message?: string }> {
    const formData = new FormData()
    formData.append('file', audio, 'recording.webm')
    const res = await this.authenticatedFetch('/api/voice/asr', {
      method: 'POST',
      body: formData,
    })
    return res.json()
  }

  async voiceTts(text: string, sessionId?: string): Promise<{ status: string; audio_url?: string; message?: string }> {
    return this.request('/api/voice/tts', {
      method: 'POST',
      body: JSON.stringify({ text, session_id: sessionId }),
    })
  }

  // ---------------------------------------------------------
  // Release / delivery evidence (read-only)
  // ---------------------------------------------------------

  async getReleaseEvidence(): Promise<Record<string, unknown>> {
    // A delivery result must never be rendered from an HTTP cache.  The
    // backend independently verifies the manifest on every request and sends
    // no-store too; keep the client-side request equally explicit.
    const res = await this.authenticatedFetch(
      '/api/release/evidence',
      { cache: 'no-store' },
      // Invalid evidence must use a failing HTTP status for non-UI callers,
      // while the delivery page still needs the structured fail-closed body.
      [422, 500],
    )
    return res.json()
  }

  // ---------------------------------------------------------
  // Logs / version
  // ---------------------------------------------------------

  async createLogStream(): Promise<BackendEventSource> {
    const ticket = await this.request<{ status: string; ticket?: string }>('/api/logs/ticket', {
      method: 'POST',
    })
    if (ticket.status !== 'success' || !ticket.ticket) {
      throw new Error('Log-stream authorization ticket was not issued')
    }
    return new IpcBackendEventSource(`/api/logs?ticket=${encodeURIComponent(ticket.ticket)}`)
  }

  async getVersion(): Promise<string> {
    const data = await this.request<{ version: string }>('/api/version')
    return data.version
  }

  // ---------------------------------------------------------
  // Auth (web_password). Credentials are delivered once over the IPC broker;
  // bearer and subject capabilities remain exclusively in Electron main memory.
  // ---------------------------------------------------------

  async authCheck(): Promise<{ status: string; auth_required: boolean; authenticated?: boolean }> {
    return this.request('/auth/check')
  }

  async authLogin(password: string): Promise<ApiResult> {
    return this.request<ApiResult>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ password }),
    })
  }

  async authLogout(): Promise<ApiResult> {
    // Electron main revokes and clears its in-memory bearer even if the trusted
    // channel fails. The renderer never receives a copy to clear.
    return this.request<ApiResult>('/auth/logout', { method: 'POST' })
  }
}

export const apiClient = new ApiClient()
export default apiClient

