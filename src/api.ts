/**
 * Backend API (EdgeOne Makers — Python)
 *
 * Route mapping (file → route):
 *   agents/chat/index.py             → POST /chat          Main chat endpoint (SSE streaming)
 *   agents/chat/stop.py              → POST /chat/stop     Abort the active agent run
 *   agents/history/index.py           → POST /history       Get conversation history (agents runtime)
 *   agents/auth/register.py          → POST /auth/register  Register new user
 *   agents/auth/login.py             → POST /auth/login     Login user
 *   agents/auth/me.py                → POST /auth/me        Get current user info
 *
 * This file defines all API paths and request wrappers. The frontend is
 * agnostic to backend language — node-starter and python-starter share
 * the same wire protocol (text_delta / tool_called / image / done / error).
 */

import type { Message, ImageSsePayload } from './types';

export interface ModelOption {
  id: string;
  owned_by?: string;
}

export interface AuthUser {
  user_id: string;
  email: string;
  username: string;
  token: string;
}

export const API = {
  chat: '/chat',
  chatStop: '/chat/stop',
  history: '/history',
  models: '/models',
  workspaceFiles: '/workspace/files',
  authRegister: '/auth/register',
  authLogin: '/auth/login',
  authMe: '/auth/me',
} as const;

// ── Auth Token Management ──
const AUTH_TOKEN_KEY = 'eo_auth_token';
const AUTH_USER_KEY = 'eo_auth_user';
const AUTH_USERS_KEY = 'eo_users';

export function getAuthToken(): string | null {
  try {
    return localStorage.getItem(AUTH_TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setAuthToken(token: string): void {
  try {
    localStorage.setItem(AUTH_TOKEN_KEY, token);
  } catch {
    // Non-critical
  }
}

export function clearAuthToken(): void {
  try {
    localStorage.removeItem(AUTH_TOKEN_KEY);
    localStorage.removeItem(AUTH_USER_KEY);
  } catch {
    // Non-critical
  }
}

function authHeaders(): Record<string, string> {
  const token = getAuthToken();
  const headers: Record<string, string> = {};
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  // EdgeOne agents runtime requires makers-conversation-id on every request.
  headers['makers-conversation-id'] = 'auth-session-001';
  return headers;
}

function hashPassword(password: string): string {
  // Simple hash for client-side auth (not for security, just for storage)
  let hash = 0;
  const salt = 'warung_lakku_salt_2024';
  const str = salt + password;
  for (let i = 0; i < str.length; i++) {
    const char = str.charCodeAt(i);
    hash = ((hash << 5) - hash) + char;
    hash = hash & hash;
  }
  return hash.toString(36);
}

function getUsers(): Record<string, { user_id: string; email: string; username: string; password_hash: string }> {
  try {
    const data = localStorage.getItem(AUTH_USERS_KEY);
    return data ? JSON.parse(data) : {};
  } catch {
    return {};
  }
}

function saveUsers(users: Record<string, { user_id: string; email: string; username: string; password_hash: string }>): void {
  try {
    localStorage.setItem(AUTH_USERS_KEY, JSON.stringify(users));
  } catch {
    // Non-critical
  }
}

function generateToken(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(32)))
    .map(b => b.toString(16).padStart(2, '0'))
    .join('');
}

// ── Auth API (client-side) ──

export async function registerUser(email: string, username: string, password: string): Promise<AuthUser | { error: string }> {
  email = email.trim().toLowerCase();
  username = username.trim();

  if (!email || !username || !password) {
    return { error: 'Email, username, and password are required' };
  }
  if (password.length < 6) {
    return { error: 'Password must be at least 6 characters' };
  }

  const users = getUsers();
  if (users[email]) {
    return { error: 'Email already registered' };
  }

  const user_id = crypto.randomUUID();
  const token = generateToken();
  const password_hash = hashPassword(password);

  users[email] = { user_id, email, username, password_hash };
  saveUsers(users);

  localStorage.setItem(AUTH_USER_KEY, JSON.stringify({ user_id, email, username }));
  localStorage.setItem(AUTH_TOKEN_KEY, token);

  return { user_id, email, username, token };
}

export async function loginUser(email: string, password: string): Promise<AuthUser | { error: string }> {
  email = email.trim().toLowerCase();

  if (!email || !password) {
    return { error: 'Email and password are required' };
  }

  const users = getUsers();
  const user = users[email];

  if (!user || user.password_hash !== hashPassword(password)) {
    return { error: 'Invalid email or password' };
  }

  const token = generateToken();

  localStorage.setItem(AUTH_USER_KEY, JSON.stringify({ user_id: user.user_id, email: user.email, username: user.username }));
  localStorage.setItem(AUTH_TOKEN_KEY, token);

  return { user_id: user.user_id, email: user.email, username: user.username, token };
}

export async function fetchCurrentUser(): Promise<AuthUser | null> {
  const token = getAuthToken();
  if (!token) return null;

  try {
    const raw = localStorage.getItem(AUTH_USER_KEY);
    if (!raw) return null;
    const user = JSON.parse(raw);
    if (user && user.user_id && user.email) {
      return { user_id: user.user_id, email: user.email, username: user.username, token };
    }
    return null;
  } catch {
    return null;
  }
}

/** Fetch available models from the backend. */
export async function fetchModels(conversationId?: string): Promise<ModelOption[]> {
  try {
    const headers: Record<string, string> = {
      ...authHeaders(),
    };
    if (conversationId) {
      headers['makers-conversation-id'] = conversationId;
    }
    const res = await fetch(API.models, { headers });
    if (!res.ok) return [];
    const data = await res.json().catch(() => null) as { models?: ModelOption[] } | null;
    return Array.isArray(data?.models) ? data.models! : [];
  } catch {
    return [];
  }
}

export interface RawSseEvent {
  eventType: string;
  data: unknown;
  raw: string;
  timestamp: number;
}

export interface StreamCallbacks {
  onTextDelta: (delta: string) => void;
  onToolCalled: (toolName: string, filesSnapshot?: Record<string, string> | null) => void;
  onToolDebug?: (payload: ToolDebugPayload) => void;
  onImage: (payload: ImageSsePayload) => void;
  onDone: () => void;
  onError: (err: Error) => void;
  onRawEvent?: (event: RawSseEvent) => void;
  onFileChanged?: (payload: { version: number; changed?: string[]; files_snapshot?: Record<string, string> }) => void;
}

export interface ToolDebugPayload {
  phase: 'call' | 'result';
  tool: string;
  id: string;
  argumentsPreview?: string;
  resultPreview?: string;
  resultLength?: number;
  durationMs?: number;
  imageCount?: number;
  files_snapshot?: Record<string, string> | null;
}

/** Get conversation history for restoring the chat window after page refresh. */
export async function fetchConversationHistory(conversationId: string): Promise<Message[]> {
  try {
    const res = await fetch(API.history, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'makers-conversation-id': conversationId,
      },
      body: JSON.stringify({ conversation_id: conversationId }),
    });

    if (!res.ok) return [];

    const data = await res.json().catch(() => null) as { messages?: Message[] } | null;
    return Array.isArray(data?.messages) ? data.messages : [];
  } catch {
    return [];
  }
}

/**
 * Stream POST /chat via SSE
 * Backend pushes events: text_delta / tool_called / image / done / error
 *
 * Returns an AbortController the caller can use to abort the request (or pair with /chat/stop for graceful abort).
 */
export function sendMessageStream(
  message: string,
  callbacks: StreamCallbacks,
  conversationId?: string,
  model?: string,
): AbortController {
  const ctrl = new AbortController();

  (async () => {
    try {
      const headers: Record<string, string> = {
        'Content-Type': 'application/json',
      };
      if (conversationId) {
        headers['makers-conversation-id'] = conversationId;
      }

      const res = await fetch(API.chat, {
        method: 'POST',
        headers,
        body: JSON.stringify({ message, model }),
        signal: ctrl.signal,
      });

      if (!res.ok) {
        callbacks.onError(new Error(`HTTP ${res.status}: ${await res.text().catch(() => '')}`));
        return;
      }

      const reader = res.body?.getReader();
      if (!reader) {
        callbacks.onError(new Error('ReadableStream not supported'));
        return;
      }

      const decoder = new TextDecoder();
      let buffer = '';
      let doneReceived = false;

      // Raw SSE event capture for debugging
      const rawEvents: Array<{eventType: string; dataLen: number; ts: number}> = [];
      (window as any).__raw_sse_events = rawEvents;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // SSE format: each event ends with \n\n
        const parts = buffer.split('\n\n');
        buffer = parts.pop() || '';

        for (const part of parts) {
          if (!part.trim()) continue;
          dispatchSseChunk(part, callbacks, () => { doneReceived = true; });
        }
      }

      if (!doneReceived) {
        callbacks.onDone();
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return;
      callbacks.onError(err instanceof Error ? err : new Error(String(err)));
    }
  })();

  return ctrl;
}

/** Parse a single SSE event and dispatch to the corresponding callback */
function dispatchSseChunk(part: string, cb: StreamCallbacks, markDone: () => void): void {
  let eventType = '';
  let data = '';

  for (const line of part.split('\n')) {
    if (line.startsWith('event: ')) {
      eventType = line.slice(7);
    } else if (line.startsWith('data: ')) {
      data += (data ? '\n' : '') + line.slice(6);
    }
  }

  if (!eventType || !data) return;

  // Capture raw event for debugging
  try { (window as any).__raw_sse_events?.push({ eventType, dataLen: data.length, ts: Date.now() }); } catch {}

  try {
    const parsed = JSON.parse(data);

    if (cb.onRawEvent) {
      cb.onRawEvent({
        eventType,
        data: parsed,
        raw: data,
        timestamp: Date.now(),
      });
    }

    switch (eventType) {
      case 'text_delta':
        cb.onTextDelta(parsed.delta);
        break;
      case 'tool_called': {
        const fsSnap = typeof parsed?.files_snapshot === 'object' ? (parsed.files_snapshot as Record<string, string> | null) ?? undefined : undefined;
        cb.onToolCalled(parsed.tool, fsSnap);
        break;
      }
      case 'tool_debug':
        if (cb.onToolDebug && typeof parsed?.tool === 'string' && typeof parsed?.id === 'string') {
          cb.onToolDebug({
            phase: parsed.phase,
            tool: parsed.tool,
            id: parsed.id,
            argumentsPreview: typeof parsed.argumentsPreview === 'string' ? parsed.argumentsPreview : undefined,
            resultPreview: typeof parsed.resultPreview === 'string' ? parsed.resultPreview : undefined,
            resultLength: typeof parsed.resultLength === 'number' ? parsed.resultLength : undefined,
            durationMs: typeof parsed.durationMs === 'number' ? parsed.durationMs : undefined,
            imageCount: typeof parsed.imageCount === 'number' ? parsed.imageCount : undefined,
            files_snapshot: typeof parsed?.files_snapshot === 'object' ? (parsed.files_snapshot as Record<string, string> | null) ?? undefined : undefined,
          });
        }
        break;
      case 'image':
        if (typeof parsed?.base64 === 'string' && typeof parsed?.imageId === 'string') {
          cb.onImage({
            imageId:    parsed.imageId,
            base64:     parsed.base64,
            mimeType:   typeof parsed.mimeType === 'string' ? parsed.mimeType : 'image/png',
            size:       typeof parsed.size === 'number' ? parsed.size : 0,
            toolName:   typeof parsed.toolName === 'string' ? parsed.toolName : undefined,
            toolCallId: typeof parsed.toolCallId === 'string' ? parsed.toolCallId : undefined,
          });
        }
        break;
      case 'file_changed':
        if (cb.onFileChanged) {
          cb.onFileChanged({
            version: typeof parsed.version === 'number' ? parsed.version : 0,
            changed: Array.isArray(parsed.changed) ? parsed.changed : undefined,
            files_snapshot: typeof parsed.files_snapshot === 'object' ? parsed.files_snapshot : undefined,
          });
        }
        break;
      case 'error':
        cb.onError(new Error(parsed.message || 'agent returned error'));
        break;
      case 'done':
        markDone();
        cb.onDone();
        break;
    }
  } catch {
    if (cb.onRawEvent) {
      cb.onRawEvent({
        eventType,
        data: null,
        raw: data,
        timestamp: Date.now(),
      });
    }
  }
}

/**
 * Request the backend to abort the currently running agent.
 * Maps to agents/chat/stop.py → POST /chat/stop
 */
export async function stopAgent(conversationId?: string): Promise<boolean> {
  try {
    /**
     * EdgeOne agents/ runtime requires Markers-Conversation-Id on every
     * agents/* request (since 2026-06-05 platform upgrade) — without it
     * the runtime returns 400 (`AGENT_CONVERSATION_ID_REQUIRED`) before
     * the handler runs.
     *
     * Earlier comments in this codebase warned that adding the header on
     * /stop would overwrite chat's abort signal slot. The new runtime is
     * expected to no longer have that bug; if you observe stop succeeding
     * but chat not actually aborting, revisit this and use a different
     * cancellation channel.
     */
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };
    if (conversationId) {
      headers['makers-conversation-id'] = conversationId;
    }
    const res = await fetch(API.chatStop, {
      method: 'POST',
      headers,
      body: JSON.stringify({ conversation_id: conversationId }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

export interface WorkspaceFile {
  name: string;
  size: number;
}

/** Fetch all workspace files metadata from the KV store */
export async function fetchWorkspaceFiles(conversationId: string): Promise<WorkspaceFile[]> {
  try {
    const res = await fetch(API.workspaceFiles, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'makers-conversation-id': conversationId,
      },
      body: JSON.stringify({ action: 'list', conversationId }),
    });
    if (!res.ok) return [];
    const data = await res.json() as { files?: WorkspaceFile[] };
    return Array.isArray(data?.files) ? data.files : [];
  } catch {
    return [];
  }
}

/** Read a workspace file's content from the KV store */
export async function readWorkspaceFile(conversationId: string, filename: string): Promise<string> {
  try {
    const res = await fetch(API.workspaceFiles, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'makers-conversation-id': conversationId,
      },
      body: JSON.stringify({ action: 'read', filename, conversationId }),
    });
    if (!res.ok) return '';
    const data = await res.json() as { content?: string };
    return typeof data?.content === 'string' ? data.content : '';
  } catch {
    return '';
  }
}

/** Fetch workspace file status (metadata + version) from the backend */
export async function fetchWorkspaceFileStatus(conversationId: string): Promise<{ files: WorkspaceFile[]; version: number }> {
  try {
    const res = await fetch(API.workspaceFiles, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'makers-conversation-id': conversationId,
      },
      body: JSON.stringify({ action: 'status', conversationId }),
    });
    if (!res.ok) return { files: [], version: 0 };
    const data = await res.json();
    return {
      files: Array.isArray(data?.files) ? data.files : [],
      version: typeof data?.version === 'number' ? data.version : 0,
    };
  } catch {
    return { files: [], version: 0 };
  }
}

/** Write/Save a workspace file's content back to the KV store */
export async function writeWorkspaceFile(conversationId: string, filename: string, content: string, expectedVersion?: number): Promise<{ success: boolean; conflict?: boolean; currentVersion?: number }> {
  try {
    const body: Record<string, unknown> = { action: 'write', filename, content, conversationId };
    if (expectedVersion !== undefined) body.expectedVersion = expectedVersion;
    const res = await fetch(API.workspaceFiles, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'makers-conversation-id': conversationId,
      },
      body: JSON.stringify(body),
    });
    if (!res.ok) return { success: false };
    const data = await res.json();
    if (data?.error === 'version_conflict') return { success: false, conflict: true, currentVersion: data.currentVersion };
    return { success: data?.success === true };
  } catch {
    return { success: false };
  }
}

/** Delete a workspace file from the KV store */
export async function deleteWorkspaceFile(conversationId: string, filename: string, expectedVersion?: number): Promise<{ success: boolean; conflict?: boolean; currentVersion?: number }> {
  try {
    const body: Record<string, unknown> = { action: 'delete', filename, conversationId };
    if (expectedVersion !== undefined) body.expectedVersion = expectedVersion;
    const res = await fetch(API.workspaceFiles, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'makers-conversation-id': conversationId,
      },
      body: JSON.stringify(body),
    });
    if (!res.ok) return { success: false };
    const data = await res.json();
    if (data?.error === 'version_conflict') return { success: false, conflict: true, currentVersion: data.currentVersion };
    return { success: data?.success === true };
  } catch {
    return { success: false };
  }
}

/** Sync a file's content to the sandbox (write-through) */
export async function syncFileToSandbox(conversationId: string, filename: string, content: string): Promise<boolean> {
  try {
    const res = await fetch(API.workspaceFiles, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'makers-conversation-id': conversationId,
      },
      body: JSON.stringify({ action: 'sync', filename, content, conversationId }),
    });
    if (!res.ok) return false;
    const data = await res.json();
    return data?.success === true;
  } catch {
    return false;
  }
}

/** Sync a file deletion to the sandbox (write-through) */
export async function syncDeleteToSandbox(conversationId: string, filename: string): Promise<boolean> {
  try {
    const res = await fetch(API.workspaceFiles, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'makers-conversation-id': conversationId,
      },
      body: JSON.stringify({ action: 'sync', filename, action_type: 'delete', conversationId }),
    });
    if (!res.ok) return false;
    const data = await res.json();
    return data?.success === true;
  } catch {
    return false;
  }
}
