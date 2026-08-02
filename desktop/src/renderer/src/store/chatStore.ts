import { create } from 'zustand'
import apiClient from '../api/client'
import { useWorkspaceStore } from './workspaceStore'
import {
  isUserFacingPath,
  kindOf,
  parseAttachmentMarkers,
  PREVIEWABLE_KINDS,
} from '../lib/fileKind'
import type { Artifact, ChatMessage, MessageStep, Attachment, StreamEvent, HistoryMessage } from '../types'

/**
 * Per-session chat state. Supports parallel sessions: each session keeps its
 * own message list and active stream, so switching sessions never interrupts a
 * background run. The active EventSource lives in `streams` (outside React).
 */

interface SessionRuntime {
  messages: ChatMessage[]
  isStreaming: boolean
  requestId: string | null
  // history pagination
  historyPage: number
  historyHasMore: boolean
  historyLoaded: boolean
  historyLoading: boolean
  historyError: string | null
}

interface ChatState {
  sessions: Record<string, SessionRuntime>

  getSession: (sid: string) => SessionRuntime
  ensureSession: (sid: string) => void

  send: (sid: string, text: string, attachments: Attachment[]) => Promise<void>
  cancel: (sid: string) => Promise<void>
  regenerate: (sid: string, botMessageId: string) => Promise<void>
  editUserMessage: (sid: string, messageId: string) => { text: string; attachments: Attachment[] } | null
  deleteMessage: (sid: string, userSeq: number, cascade: boolean) => Promise<void>

  loadHistory: (sid: string, page?: number) => Promise<void>
  clearContext: (sid: string) => Promise<boolean>
  clearLocal: (sid: string) => void
  reset: () => void
}

// EventSource instances kept outside the store (not serializable).
const streams: Record<string, EventSource> = {}

/** Keep recovery handles outside Zustand alongside their non-serializable EventSources. */
interface StreamController {
  requestId: string
  botId: string
  reconnect: () => Promise<void>
  dispose: () => void
}

const streamControllers: Record<string, StreamController> = {}
const SSE_RECONNECT_ATTEMPTS = 3
const SSE_RECONNECT_BASE_DELAY_MS = 400

const EMPTY: SessionRuntime = {
  messages: [],
  isStreaming: false,
  requestId: null,
  historyPage: 0,
  historyHasMore: false,
  historyLoaded: false,
  historyLoading: false,
  historyError: null,
}

function uid(prefix: string): string {
  return `${prefix}_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`
}

/**
 * History keeps the English cancel marker for the LLM; strip it for display so
 * the bubble shows a clean answer + a dedicated "cancelled" badge instead.
 */
function stripCancelMarker(text: string): string {
  if (!text) return text
  return text
    .replace(/_\(Cancelled by user\)_/g, '')
    .replace(/_\(Cancelled\)_/g, '')
    .trim()
}

/**
 * Rebuild attachments from `send`-tool results persisted in the message steps.
 * SSE `file_to_send` events aren't stored, so on history reload the only record
 * of a sent image/file is the tool result JSON. Mirrors the web console's
 * `_renderSentFileFromToolResult` so media survives an app restart.
 */
function attachmentsFromSteps(steps: MessageStep[]): Attachment[] {
  const out: Attachment[] = []
  for (const s of steps) {
    if (s.type !== 'tool' || !s.result) continue
    let payload: Record<string, unknown>
    try {
      payload = typeof s.result === 'string' ? JSON.parse(s.result) : (s.result as unknown as Record<string, unknown>)
    } catch {
      continue
    }
    if (!payload || payload.type !== 'file_to_send') continue
    const rawPath = (payload.path as string) || ''
    const url = (payload.url as string) || ''
    if (!rawPath && !url) continue
    const isRemote = url.toLowerCase().startsWith('http://') || url.toLowerCase().startsWith('https://')
    const isCapability = url.startsWith('/file/')
    // HistoryHandler refreshes a short, owner/path-bound capability for local
    // files.  Do not reconstruct a raw /api/file URL with a bearer query.
    const previewUrl = isRemote || isCapability ? url : ''
    if (!previewUrl) continue
    const kind = (payload.file_type as string) || 'file'
    const fileType: Attachment['file_type'] =
      kind === 'image' ? 'image' : kind === 'video' ? 'video' : 'file'
    out.push({
      file_path: previewUrl,
      file_name: (payload.file_name as string) || 'file',
      file_type: fileType,
      preview_url: previewUrl,
      abs_path: isRemote ? undefined : rawPath,
    })
  }
  return out
}

/**
 * Rebuild artifact cards from persisted `write`/`edit` steps. Like
 * `attachmentsFromSteps`, this exists because SSE `artifact` events aren't
 * stored — the tool call is the only record after a reload. URLs are left
 * empty and resolved lazily when the card is clicked.
 */
function artifactsFromSteps(steps: MessageStep[]): Artifact[] {
  const out: Artifact[] = []
  const seen = new Set<string>()
  for (const s of steps) {
    if (s.type !== 'tool' || s.is_error) continue
    if (s.name !== 'write' && s.name !== 'edit') continue
    const path = String((s.arguments as Record<string, unknown> | undefined)?.path || '').trim()
    if (!path || seen.has(path) || !isUserFacingPath(path)) continue
    seen.add(path)
    const name = path.split('/').pop() || path
    const kind = kindOf(name)
    out.push({
      abs_path: '',
      rel_path: path,
      file_name: name,
      kind,
      previewable: PREVIEWABLE_KINDS.has(kind),
      size: 0,
      raw_url: '',
      preview_url: '',
    })
  }
  return out
}

/** Convert a backend history message into a UI ChatMessage. */
function historyToMessage(m: HistoryMessage): ChatMessage {
  if (m.role === 'user') {
    // History persists only the prompt text, so the attachment chips have to be
    // recovered from the `[label: path]` markers appended to it.
    const { text, attachments } = parseAttachmentMarkers(m.content)
    const attachmentUrls = m.attachment_urls || {}
    const resolvedAttachments = attachments?.map((attachment) => {
      const previewUrl = attachmentUrls[attachment.file_path]
      return previewUrl ? { ...attachment, preview_url: previewUrl } : attachment
    })
    return {
      id: uid('user'),
      role: 'user',
      content: text,
      timestamp: m.created_at,
      userSeq: m._seq,
      attachments: resolvedAttachments,
    }
  }

  // The backend stores the final answer both as `content` and as the LAST
  // `content` step. Strip that trailing content step so it isn't rendered
  // twice (matches the web console's renderStepsHtml logic).
  const raw = m.steps || []
  let lastContentIdx = -1
  for (let i = raw.length - 1; i >= 0; i--) {
    if (raw[i].type === 'content') {
      lastContentIdx = i
      break
    }
  }
  const steps: MessageStep[] = raw
    .filter((_, i) => i !== lastContentIdx)
    .map((s) => ({ ...s }))
  const finalContent = m.content || (lastContentIdx >= 0 ? raw[lastContentIdx].content || '' : '')
  const attachments = attachmentsFromSteps(raw)
  const artifacts = artifactsFromSteps(raw)

  return {
    id: uid('assistant'),
    role: 'assistant',
    content: finalContent,
    timestamp: m.created_at,
    steps,
    reasoning: m.reasoning,
    kind: m.kind,
    extras: m.extras,
    botSeq: m._seq,
    attachments: attachments.length > 0 ? attachments : undefined,
    artifacts: artifacts.length > 0 ? artifacts : undefined,
  }
}

export const useChatStore = create<ChatState>((set, get) => {
  // --- helpers operating on a single session immutably ---
  const patchSession = (sid: string, patch: Partial<SessionRuntime>) =>
    set((st) => ({
      sessions: { ...st.sessions, [sid]: { ...(st.sessions[sid] || EMPTY), ...patch } },
    }))

  const patchMessages = (sid: string, fn: (msgs: ChatMessage[]) => ChatMessage[]) =>
    set((st) => {
      const cur = st.sessions[sid] || EMPTY
      return { sessions: { ...st.sessions, [sid]: { ...cur, messages: fn(cur.messages) } } }
    })

  const updateMsg = (sid: string, id: string, fn: (m: ChatMessage) => ChatMessage) =>
    patchMessages(sid, (msgs) => msgs.map((m) => (m.id === id ? fn(m) : m)))

  /** Attach an EventSource for a request and wire all SSE events to a bot message. */
  const attachStream = async (sid: string, requestId: string, botId: string) => {
    streamControllers[sid]?.dispose()
    let es: EventSource | null = null
    let tailTimer: ReturnType<typeof setTimeout> | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let reconnecting = false
    let terminal = false
    let mainReplyDone = false
    let lastEventId = 0

    const closeStream = () => {
      if (tailTimer) {
        clearTimeout(tailTimer)
        tailTimer = null
      }
      const current = es
      es = null
      current?.close()
      if (streams[sid] === current) delete streams[sid]
    }

    // Mark the turn as complete: UI becomes interactive again immediately.
    const completeTurn = () => {
      if (get().sessions[sid]?.requestId === requestId) {
        patchSession(sid, { isStreaming: false, requestId: null })
      }
      updateMsg(sid, botId, (m) => ({ ...m, isStreaming: false, isStreamInterrupted: false }))
    }

    const finishStream = () => {
      terminal = true
      completeTurn()
      closeStream()
      if (streamControllers[sid]?.requestId === requestId) delete streamControllers[sid]
    }

    const onMessage = (event: MessageEvent<string>) => {
      const receivedEventId = Number(event.lastEventId)
      if (Number.isSafeInteger(receivedEventId) && receivedEventId >= lastEventId) {
        lastEventId = receivedEventId
      }
      let data: StreamEvent
      try {
        data = JSON.parse(event.data)
      } catch {
        return // keepalive
      }

      switch (data.type) {
        case 'reasoning':
          updateMsg(sid, botId, (m) => ({ ...m, reasoning: (m.reasoning || '') + (data.content || '') }))
          break

        case 'delta':
          updateMsg(sid, botId, (m) => ({ ...m, content: m.content + (data.content || '') }))
          break

        case 'message_end':
          // Freeze accumulated text as a content step when tool calls follow,
          // mirroring the web console's interleaved step model.
          if (data.has_tool_calls) {
            updateMsg(sid, botId, (m) => {
              if (!m.content.trim()) return m
              const steps = [...(m.steps || []), { type: 'content' as const, content: m.content.trim() }]
              return { ...m, steps, content: '' }
            })
          }
          break

        case 'tool_start':
          updateMsg(sid, botId, (m) => {
            // commit any reasoning into a thinking step
            const steps = [...(m.steps || [])]
            if (m.reasoning && m.reasoning.trim()) {
              steps.push({ type: 'thinking', content: m.reasoning.trim() })
            }
            steps.push({
              type: 'tool',
              id: data.tool_call_id,
              name: data.tool,
              arguments: data.arguments,
              status: 'running',
            })
            return { ...m, steps, reasoning: '', content: '' }
          })
          break

        case 'tool_progress':
          updateMsg(sid, botId, (m) => ({
            ...m,
            steps: (m.steps || []).map((s) =>
              s.type === 'tool' && s.id === data.tool_call_id ? { ...s, result: data.content } : s
            ),
          }))
          break

        case 'tool_end':
          updateMsg(sid, botId, (m) => ({
            ...m,
            steps: (m.steps || []).map((s) =>
              s.type === 'tool' && s.id === data.tool_call_id
                ? {
                    ...s,
                    status: data.status,
                    result: data.result ?? s.result,
                    execution_time: data.execution_time,
                    is_error: data.status !== 'success',
                  }
                : s
            ),
          }))
          break

        case 'image':
        case 'file': {
          // Media pushed by the `send` tool (file_to_send). `content` is either
          // an owner/path-bound /file capability or a passed-through http(s) URL.
          const url = data.content || ''
          if (!url) break
          // Prefer the concrete media kind from the backend (image/video/...);
          // fall back to the coarse SSE event type.
          const kind = data.file_type || (data.type === 'image' ? 'image' : 'file')
          const attType: Attachment['file_type'] =
            kind === 'image' ? 'image' : kind === 'video' ? 'video' : 'file'
          const att: Attachment = {
            file_path: url,
            file_name: data.file_name || 'file',
            file_type: attType,
            preview_url: url,
            abs_path: data.abs_path,
          }
          updateMsg(sid, botId, (m) => ({
            ...m,
            attachments: [...(m.attachments || []), att],
          }))
          break
        }

        case 'artifact': {
          if (!data.abs_path) break
          const artifact: Artifact = {
            abs_path: data.abs_path,
            rel_path: data.rel_path || data.file_name || '',
            file_name: data.file_name || '',
            kind: data.kind || 'file',
            previewable: !!data.previewable,
            size: data.size || 0,
            raw_url: data.raw_url || '',
            preview_url: data.preview_url || '',
          }
          updateMsg(sid, botId, (m) =>
            (m.artifacts || []).some((a) => a.abs_path === artifact.abs_path)
              ? m
              : { ...m, artifacts: [...(m.artifacts || []), artifact] }
          )
          useWorkspaceStore.getState().addTurnArtifact(artifact)
          break
        }

        case 'cancelled':
          updateMsg(sid, botId, (m) => ({
            ...m,
            isCancelled: true,
            isCancelPending: false,
            isCancelUnconfirmed: false,
          }))
          break

        case 'done':
          mainReplyDone = true
          updateMsg(sid, botId, (m) => {
            const next = stripCancelMarker(data.content || m.content)
            return {
              ...m,
              content: next,
              botSeq: data.bot_seq ?? m.botSeq,
              isStreaming: false,
              isStreamInterrupted: false,
            }
          })
          // backfill the preceding user message's seq for edit/delete
          if (data.user_seq != null) {
            patchMessages(sid, (msgs) => {
              const idx = msgs.findIndex((m) => m.id === botId)
              for (let i = idx - 1; i >= 0; i--) {
                if (msgs[i].role === 'user') {
                  msgs[i] = { ...msgs[i], userSeq: data.user_seq }
                  break
                }
              }
              return [...msgs]
            })
          }
          // The answer is final: free the UI now (don't wait for onerror).
          completeTurn()
          useWorkspaceStore.getState().maybeAutoOpen()
          // Backend keeps the stream open for a short tail (e.g. TTS audio via
          // voice_attach). Close it ourselves if nothing else arrives.
          if (tailTimer) clearTimeout(tailTimer)
          tailTimer = setTimeout(dispose, 1500)
          break

        case 'voice_attach':
          if (data.audio_url) {
            updateMsg(sid, botId, (m) => ({
              ...m,
              extras: { ...(m.extras || {}), audio: data.audio_url },
            }))
          }
          finishStream()
          break

        case 'error':
          updateMsg(sid, botId, (m) => ({ ...m, error: data.message || 'stream error', isStreaming: false }))
          finishStream()
          break
      }
    }

    let wakeReconnect: (() => void) | null = null

    const dispose = () => {
      terminal = true
      if (reconnectTimer) {
        clearTimeout(reconnectTimer)
        reconnectTimer = null
      }
      wakeReconnect?.()
      wakeReconnect = null
      closeStream()
      if (streamControllers[sid]?.requestId === requestId) delete streamControllers[sid]
    }

    const waitBeforeReconnect = (delayMs: number) =>
      new Promise<void>((resolve) => {
        wakeReconnect = () => {
          if (reconnectTimer) clearTimeout(reconnectTimer)
          reconnectTimer = null
          resolve()
        }
        reconnectTimer = setTimeout(() => {
          const wake = wakeReconnect
          wakeReconnect = null
          wake?.()
        }, delayMs)
      })

    const reconnect = async (failed?: EventSource) => {
      if (terminal || mainReplyDone || reconnecting || get().sessions[sid]?.requestId !== requestId) return
      reconnecting = true
      if (failed) {
        failed.close()
        if (streams[sid] === failed) delete streams[sid]
      }
      try {
        for (let attempt = 0; attempt < SSE_RECONNECT_ATTEMPTS; attempt += 1) {
          if (attempt > 0) {
            await waitBeforeReconnect(SSE_RECONNECT_BASE_DELAY_MS * 2 ** (attempt - 1))
          }
          if (terminal || mainReplyDone || get().sessions[sid]?.requestId !== requestId) return
          try {
            // Each attempt first exchanges header/cookie authentication for a
            // fresh, request-bound, one-shot ticket. Never reuse the failed
            // EventSource URL: browser-native retry would reuse its ticket.
            const next = await apiClient.createSSEStream(requestId, lastEventId)
            if (terminal || mainReplyDone || get().sessions[sid]?.requestId !== requestId) {
              next.close()
              return
            }
            bindStream(next)
            updateMsg(sid, botId, (m) => ({ ...m, isStreamInterrupted: false }))
            return
          } catch {
            // Retry the ticket exchange with bounded exponential backoff.
          }
        }
        // Backend execution may continue after a transport failure. Preserve
        // the request identity and Stop action; never render this as success.
        updateMsg(sid, botId, (m) => ({ ...m, isStreaming: true, isStreamInterrupted: true }))
      } finally {
        reconnecting = false
      }
    }

    const bindStream = (next: EventSource) => {
      es = next
      streams[sid] = next
      next.onmessage = onMessage
      next.onerror = () => {
        if (terminal || es !== next) return
        if (mainReplyDone) {
          dispose()
          return
        }
        void reconnect(next)
      }
    }

    streamControllers[sid] = { requestId, botId, reconnect, dispose }
    // Initial connection uses the same bounded recovery path as a later
    // disconnect. A failed ticket exchange must not create an invisible task
    // or be rendered as a completed/failed agent answer.
    await reconnect()
  }

  return {
    sessions: {},

    getSession: (sid) => get().sessions[sid] || EMPTY,

    ensureSession: (sid) => {
      if (!get().sessions[sid]) patchSession(sid, { ...EMPTY })
    },

    send: async (sid, text, attachments) => {
      const userMsg: ChatMessage = {
        id: uid('user'),
        role: 'user',
        content: text,
        timestamp: Date.now() / 1000,
        attachments: attachments.length ? attachments : undefined,
      }
      const botId = uid('assistant')
      const botMsg: ChatMessage = {
        id: botId,
        role: 'assistant',
        content: '',
        timestamp: Date.now() / 1000,
        steps: [],
        isStreaming: true,
      }
      patchMessages(sid, (msgs) => [...msgs, userMsg, botMsg])
      patchSession(sid, { isStreaming: true })
      useWorkspaceStore.getState().resetTurnArtifacts()

      try {
        const res = await apiClient.sendMessage(sid, text, {
          stream: true,
          attachments: attachments.length ? attachments : undefined,
        })
        if (res.status === 'success' && res.stream && res.request_id) {
          patchSession(sid, { requestId: res.request_id })
          await attachStream(sid, res.request_id, botId)
        } else if (res.inline_reply) {
          updateMsg(sid, botId, (m) => ({ ...m, content: res.inline_reply || '', isStreaming: false }))
          patchSession(sid, { isStreaming: false })
        } else {
          updateMsg(sid, botId, (m) => ({ ...m, error: 'send failed', isStreaming: false }))
          patchSession(sid, { isStreaming: false })
        }
      } catch (err) {
        updateMsg(sid, botId, (m) => ({ ...m, error: `${err}`, isStreaming: false }))
        patchSession(sid, { isStreaming: false })
      }
    },

    cancel: async (sid) => {
      const s = get().sessions[sid]
      if (!s?.requestId) return
      const requestId = s.requestId
      // Cancellation is a two-phase user-visible operation. Keep the SSE and
      // request identity alive until the backend confirms that it accepted the
      // cancellation; a network failure must never be rendered as "cancelled".
      patchMessages(sid, (msgs) => {
        for (let i = msgs.length - 1; i >= 0; i--) {
          if (msgs[i].role === 'assistant') {
            msgs[i] = {
              ...msgs[i],
              isCancelPending: true,
              isCancelUnconfirmed: false,
            }
            break
          }
        }
        return [...msgs]
      })
      try {
        const result = await apiClient.cancel({ requestId, sessionId: sid })
        if (result.status !== 'success' || result.cancelled < 1) {
          throw new Error('backend did not confirm cancellation')
        }
        // REST acceptance only proves the signal was delivered. The task can
        // still emit tool/output events, so retain the SSE and request id until
        // `cancelled` and terminal `done` are observed.
        const controller = streamControllers[sid]
        if (controller?.requestId === requestId && !streams[sid]) {
          void controller.reconnect()
        }
      } catch {
        patchMessages(sid, (msgs) => {
          for (let i = msgs.length - 1; i >= 0; i--) {
            if (msgs[i].role === 'assistant') {
              msgs[i] = {
                ...msgs[i],
                isCancelPending: false,
                isCancelUnconfirmed: true,
              }
              break
            }
          }
          return [...msgs]
        })
        // Deliberately retain requestId, streaming state and SSE so the user can
        // observe the still-running task and retry cancellation.
      }
    },

    regenerate: async (sid, botMessageId) => {
      const s = get().sessions[sid] || EMPTY
      const idx = s.messages.findIndex((m) => m.id === botMessageId)
      if (idx < 0) return
      // find the user message that produced this bot reply
      let userMsg: ChatMessage | null = null
      for (let i = idx - 1; i >= 0; i--) {
        if (s.messages[i].role === 'user') {
          userMsg = s.messages[i]
          break
        }
      }
      if (!userMsg) return
      // delete the turn on the backend (by the user's seq) then resend
      if (userMsg.userSeq != null) {
        try {
          await apiClient.deleteMessage({ sessionId: sid, userSeq: userMsg.userSeq, deleteUser: true, cascade: true })
        } catch (err) {
          patchSession(sid, { historyError: err instanceof Error ? err.message : String(err) })
          return
        }
      }
      // drop the user+bot messages locally from idx-? : remove from the user msg onward
      const userIdx = s.messages.indexOf(userMsg)
      patchMessages(sid, (msgs) => msgs.slice(0, userIdx))
      await get().send(sid, userMsg.content, userMsg.attachments || [])
    },

    editUserMessage: (sid, messageId) => {
      const s = get().sessions[sid] || EMPTY
      const msg = s.messages.find((m) => m.id === messageId)
      if (!msg || msg.role !== 'user') return null
      const userIdx = s.messages.indexOf(msg)
      // cascade-delete this turn on the backend
      if (msg.userSeq != null) {
        apiClient
          .deleteMessage({ sessionId: sid, userSeq: msg.userSeq, deleteUser: true, cascade: true })
          .catch(() => {})
      }
      patchMessages(sid, (msgs) => msgs.slice(0, userIdx))
      return { text: msg.content, attachments: msg.attachments || [] }
    },

    deleteMessage: async (sid, userSeq, cascade) => {
      try {
        await apiClient.deleteMessage({ sessionId: sid, userSeq, deleteUser: true, cascade })
      } catch (err) {
        patchSession(sid, { historyError: err instanceof Error ? err.message : String(err) })
        return
      }
      // reload history to reflect server state
      await get().loadHistory(sid, 1)
    },

    loadHistory: async (sid, page = 1) => {
      patchSession(sid, { historyLoading: true, historyError: null })
      try {
        const res = await apiClient.getHistory(sid, page, 20)
        const uiMsgs = res.messages.map(historyToMessage)
        patchSession(sid, {
          historyPage: res.page,
          historyHasMore: res.has_more,
          historyLoaded: true,
          historyLoading: false,
          historyError: null,
        })
        if (page === 1) {
          patchMessages(sid, () => uiMsgs)
        } else {
          // older page: prepend
          patchMessages(sid, (msgs) => [...uiMsgs, ...msgs])
        }
      } catch (err) {
        patchSession(sid, {
          historyLoaded: true,
          historyLoading: false,
          historyError: err instanceof Error ? err.message : String(err),
        })
      }
    },

    clearContext: async (sid) => {
      try {
        const res = await apiClient.clearContext(sid)
        if (res.status !== 'success') return false
        // Append a visual divider so the user sees the context was cleared
        // (mirrors the web console's context-divider).
        patchMessages(sid, (msgs) => [
          ...msgs,
          {
            id: uid('divider'),
            role: 'system',
            kind: 'divider',
            content: '',
            timestamp: Date.now() / 1000,
          },
        ])
        return true
      } catch {
        return false
      }
    },

    clearLocal: (sid) => {
      streamControllers[sid]?.dispose()
      patchSession(sid, { ...EMPTY })
    },

    reset: () => {
      for (const controller of Object.values(streamControllers)) controller.dispose()
      set({ sessions: {} })
    },
  }
})
