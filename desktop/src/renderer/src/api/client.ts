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
}

const AUTH_TOKEN_KEY = 'cow_auth_token'
const AUTH_SUBJECT_TOKEN_KEY = 'cow_auth_subject_token'

class ApiClient {
  private baseUrl = 'http://127.0.0.1:9876'
  // Bearer token for web_password-protected backends. The desktop renderer
  // runs from a file:// origin, where cross-origin cookies to http://127.0.0.1
  // aren't sent reliably, so we authenticate via an Authorization header
  // instead. Persisted in localStorage so it survives reloads.
  private authToken: string | null =
    typeof localStorage !== 'undefined' ? localStorage.getItem(AUTH_TOKEN_KEY) : null
  private authSubjectToken: string | null =
    typeof localStorage !== 'undefined' ? localStorage.getItem(AUTH_SUBJECT_TOKEN_KEY) : null

  setBaseUrl(url: string) {
    this.baseUrl = url
  }

  getBaseUrl() {
    return this.baseUrl
  }

  setAuthToken(token: string | null) {
    this.authToken = token
    try {
      if (token) localStorage.setItem(AUTH_TOKEN_KEY, token)
      else localStorage.removeItem(AUTH_TOKEN_KEY)
    } catch {
      // localStorage may be unavailable; in-memory token still works this session
    }
  }

  private async authenticatedFetch(path: string, options?: RequestInit): Promise<Response> {
    const headers = new Headers(options?.headers)
    if (this.authToken) headers.set('Authorization', `Bearer ${this.authToken}`)
    // Let the browser add the multipart boundary for FormData. JSON callers pass
    // a string body and receive the shared content type automatically.
    if (typeof options?.body === 'string' && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json')
    }
    const res = await fetch(`${this.baseUrl}${path}`, {
      ...options,
      // Cookies still work for browser access; the desktop app relies on the
      // Authorization header above.
      credentials: 'include',
      headers,
    })
    if (!res.ok) {
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
    opts?: { stream?: boolean; attachments?: Attachment[]; isVoice?: boolean; lang?: string }
  ): Promise<{ status: string; request_id: string; stream: boolean; inline_reply?: string }> {
    return this.request('/message', {
      method: 'POST',
      body: JSON.stringify({
        session_id: sessionId,
        message,
        stream: opts?.stream ?? true,
        attachments: opts?.attachments,
        is_voice: opts?.isVoice ?? false,
        lang: opts?.lang,
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

  async cancel(opts: { requestId?: string; sessionId?: string; lang?: string }): Promise<{ status: string; cancelled: number }> {
    return this.request('/cancel', {
      method: 'POST',
      body: JSON.stringify({ request_id: opts.requestId, session_id: opts.sessionId, lang: opts.lang }),
    })
  }

  async createSSEStream(requestId: string, afterEventId = 0): Promise<EventSource> {
    const ticket = await this.request<{ status: string; ticket?: string }>('/stream/ticket', {
      method: 'POST',
      body: JSON.stringify({ request_id: requestId, after_event_id: afterEventId }),
    })
    if (ticket.status !== 'success' || !ticket.ticket) {
      throw new Error('SSE authorization ticket was not issued')
    }
    return new EventSource(
      `${this.baseUrl}/stream?request_id=${encodeURIComponent(requestId)}&ticket=${encodeURIComponent(ticket.ticket)}`
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
    if (previewUrl.startsWith('/file/')) return `${this.baseUrl}${previewUrl}`
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
    return `${this.baseUrl}${previewPath}`
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

  async runTask(taskId: string): Promise<ApiResult> {
    return this.request('/api/scheduler/run', {
      method: 'POST',
      body: JSON.stringify({ task_id: taskId }),
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
    return this.request('/api/release/evidence')
  }

  // ---------------------------------------------------------
  // Logs / version
  // ---------------------------------------------------------

  async createLogStream(): Promise<EventSource> {
    const ticket = await this.request<{ status: string; ticket?: string }>('/api/logs/ticket', {
      method: 'POST',
    })
    if (ticket.status !== 'success' || !ticket.ticket) {
      throw new Error('Log-stream authorization ticket was not issued')
    }
    return new EventSource(
      `${this.baseUrl}/api/logs?ticket=${encodeURIComponent(ticket.ticket)}`
    )
  }

  async getVersion(): Promise<string> {
    const data = await this.request<{ version: string }>('/api/version')
    return data.version
  }

  // ---------------------------------------------------------
  // Auth (web_password) — placeholder for future use
  // ---------------------------------------------------------

  async authCheck(): Promise<{ status: string; auth_required: boolean; authenticated?: boolean }> {
    return this.request('/auth/check')
  }

  async authLogin(password: string): Promise<ApiResult & { token?: string; subject_token?: string }> {
    const res = await this.request<ApiResult & { token?: string; subject_token?: string }>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ password, subject_token: this.authSubjectToken }),
    })
    if (res.status === 'success' && res.token) {
      this.setAuthToken(res.token)
      if (res.subject_token) {
        this.authSubjectToken = res.subject_token
        try {
          localStorage.setItem(AUTH_SUBJECT_TOKEN_KEY, res.subject_token)
        } catch {
          // In-memory subject still preserves ownership for this renderer session.
        }
      }
    }
    return res
  }

  async authLogout(): Promise<ApiResult> {
    // Revoke while the bearer is still attached; always clear the local copy
    // even if the backend is unreachable or the credential already expired.
    try {
      return await this.request<ApiResult>('/auth/logout', { method: 'POST' })
    } finally {
      this.setAuthToken(null)
    }
  }
}

export const apiClient = new ApiClient()
export default apiClient

