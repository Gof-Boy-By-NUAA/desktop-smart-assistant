import { create } from 'zustand'
import apiClient from '../api/client'
import type { SessionItem } from '../types'

const ACTIVE_KEY = 'cow_session_id'
const DRAFT_KEY = 'cow_draft_session_id'

interface SessionState {
  sessions: SessionItem[]
  total: number
  page: number
  hasMore: boolean
  loading: boolean
  error: string | null
  activeId: string
  draftId: string | null

  loadSessions: (page?: number) => Promise<void>
  loadMore: () => Promise<void>
  setActive: (id: string) => void
  newSession: () => string
  rename: (id: string, title: string) => Promise<void>
  remove: (id: string) => Promise<void>
  reset: () => void
}

function genId(): string {
  return `session_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`
}

function newDraft(): { activeId: string; draftId: string } {
  const id = genId()
  localStorage.setItem(ACTIVE_KEY, id)
  localStorage.setItem(DRAFT_KEY, id)
  return { activeId: id, draftId: id }
}

const savedActive = localStorage.getItem(ACTIVE_KEY)
const initialSession = savedActive
  ? { activeId: savedActive, draftId: localStorage.getItem(DRAFT_KEY) === savedActive ? savedActive : null }
  : newDraft()

export const useSessionStore = create<SessionState>((set, get) => ({
  sessions: [],
  total: 0,
  page: 1,
  hasMore: false,
  loading: false,
  error: null,
  ...initialSession,

  loadSessions: async (page = 1) => {
    set({ loading: true, error: null })
    try {
      const res = await apiClient.getSessions(page, 50)
      // 仅本地创建且尚未持久化的草稿可以跳过历史查询；未知 ID 仍需向后端核验。
      const draftId = get().draftId
      if (draftId && res.sessions.some((session) => session.session_id === draftId)) {
        localStorage.removeItem(DRAFT_KEY)
        set({ draftId: null })
      }
      set((s) => ({
        sessions: page === 1 ? res.sessions : [...s.sessions, ...res.sessions],
        total: res.total,
        page: res.page,
        hasMore: res.has_more,
        loading: false,
      }))
    } catch (err) {
      set({ loading: false, error: err instanceof Error ? err.message : String(err) })
    }
  },

  loadMore: async () => {
    const { hasMore, loading, page } = get()
    if (!hasMore || loading) return
    await get().loadSessions(page + 1)
  },

  setActive: (id) => {
    localStorage.setItem(ACTIVE_KEY, id)
    localStorage.removeItem(DRAFT_KEY)
    set({ activeId: id, draftId: null })
  },

  newSession: () => {
    const draft = newDraft()
    set(draft)
    return draft.activeId
  },

  rename: async (id, title) => {
    try {
      await apiClient.renameSession(id, title)
      set((s) => ({
        sessions: s.sessions.map((sess) => (sess.session_id === id ? { ...sess, title } : sess)),
        error: null,
      }))
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) })
      throw err
    }
  },

  remove: async (id) => {
    try {
      await apiClient.deleteSession(id)
      set((s) => ({ sessions: s.sessions.filter((sess) => sess.session_id !== id), error: null }))
      // If we removed the active one, start a fresh session
      if (get().activeId === id) get().newSession()
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) })
      throw err
    }
  },

  reset: () => {
    set({
      sessions: [],
      total: 0,
      page: 1,
      hasMore: false,
      loading: false,
      error: null,
      ...newDraft(),
    })
  },
}))
