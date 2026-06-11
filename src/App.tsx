import { useState, useCallback, useEffect, useRef, useMemo } from 'react';
import type { ImageAttachment, ImageSsePayload, Message, ReplLine, TurnMeta } from './types';
import type { RawSseEvent, ToolDebugPayload, WorkspaceFile } from './api';
import { 
  fetchConversationHistory, fetchModels, sendMessageStream, stopAgent,
  fetchWorkspaceFiles, readWorkspaceFile, writeWorkspaceFile, deleteWorkspaceFile,
  fetchWorkspaceFileStatus, syncFileToSandbox, syncDeleteToSandbox
} from './api';
import type { ModelOption } from './api';
import { 
  initLocalFs, writeLocalFile, readLocalFile, listLocalFiles, deleteLocalFile,
  mountLocalFolder, unmountLocalFolder, setWorkspaceRoot, getWorkspaceRoot
} from './lib/phcode-fs';
import { I18nProvider, useT } from './i18n';
import ReplShell from './components/repl/ReplShell';
import ReplStream from './components/repl/ReplStream';
import ReplPrompt from './components/repl/ReplPrompt';
import ImageLightbox from './components/ImageLightbox';
import FileEditorModal from './components/FileEditorModal';
import {
  makeDone,
  makeError,
  makeImage,
  makeRestored,
  makeText,
  makeTool,
  makeUser,
  startTurn,
} from './components/repl/lines';
import {
  base64ToBlob,
  createObjectUrl,
  deleteConversationImages,
  loadConversationImages,
  makeStorageKey,
  revokeAllObjectUrls,
  saveImage,
  type StoredImageRecord,
} from './lib/imageStore';
import type { ReplAction } from './components/repl/keymap';
import Sidebar, { type ChatSessionInfo } from './components/Sidebar';
import styles from './App.module.css';

const CONVERSATION_ID_STORAGE_KEY = 'eo_conversation_id';
const SESSIONS_STORAGE_KEY = 'eo_chat_sessions';
const MAX_INPUT_HISTORY = 50;

/** Build the localStorage key used to cache messages for a conversation. */
function messagesStorageKey(conversationId: string): string {
  return `chat_messages_${conversationId}`;
}

/** Persist an array of Message objects to localStorage for a conversation. */
function cacheMessages(conversationId: string, messages: Message[]): void {
  try {
    localStorage.setItem(messagesStorageKey(conversationId), JSON.stringify(messages));
  } catch {
    // Quota or private-mode failure is non-critical
  }
}

/** Read cached Message[] from localStorage; returns [] on miss or parse error. */
function readCachedMessages(conversationId: string): Message[] {
  try {
    const raw = localStorage.getItem(messagesStorageKey(conversationId));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

/** Remove the cached messages for a conversation from localStorage. */
function removeCachedMessages(conversationId: string): void {
  try {
    localStorage.removeItem(messagesStorageKey(conversationId));
  } catch {
    // Non-critical
  }
}

function getStoredSessions(): ChatSessionInfo[] {
  try {
    const data = localStorage.getItem(SESSIONS_STORAGE_KEY);
    return data ? JSON.parse(data) : [];
  } catch {
    return [];
  }
}

function saveStoredSessions(sessions: ChatSessionInfo[]) {
  localStorage.setItem(SESSIONS_STORAGE_KEY, JSON.stringify(sessions));
}

function getExistingConversationId(): string | null {
  return localStorage.getItem(CONVERSATION_ID_STORAGE_KEY);
}

/**
 * Collapse contiguous runs of `text` ReplLines that share `turnId` into a
 * single `markdown` ReplLine. Tool / image / done / error / restored / etc.
 * lines are passthrough — they stay where they are and act as run boundaries.
 *
 * Used in onDone (and onError, on the rare path where the agent emitted
 * partial text before failing) to upgrade just-finished assistant text to
 * post-stream markdown rendering. Idempotent: lines already of kind
 * `markdown` are passthrough too, so calling this twice on the same array
 * is a no-op.
 */
function collapseTurnTextToMarkdown(lines: ReplLine[], turnId: string): ReplLine[] {
  const out: ReplLine[] = [];
  let buf: ReplLine[] = []; // accumulating contiguous `text` lines for `turnId`

  const flush = () => {
    if (buf.length === 0) return;
    const first = buf[0] as Extract<ReplLine, { kind: 'text' }>;
    const merged: ReplLine = {
      kind: 'markdown',
      // Reuse the FIRST text line's id/ts/isContinuation so React can keep
      // this row in place (no key churn) and the on-screen position
      // doesn't jump when we replace the run.
      id: first.id,
      turnId: first.turnId,
      ts: first.ts,
      isContinuation: first.isContinuation,
      text: buf.map(l => (l as Extract<ReplLine, { kind: 'text' }>).text).join(''),
    };
    out.push(merged);
    buf = [];
  };

  for (const line of lines) {
    if (line.kind === 'text' && line.turnId === turnId) {
      buf.push(line);
      continue;
    }
    flush();
    out.push(line);
  }
  flush();
  return out;
}

/** Map `Message[]` from /history into ReplLine[] (user / markdown). */
function historyToLines(history: Message[]): ReplLine[] {
  const out: ReplLine[] = [];
  for (const m of history) {
    if (!m.content && m.role === 'assistant') continue;
    if (m.role === 'user') {
      out.push({ kind: 'user', id: m.id, text: m.content, ts: m.timestamp });
    } else {
      // Restored assistant turns are emitted as `markdown` directly:
      // history has no streaming-chunk concept, the message is already
      // a complete blob, and we want it to render with the same
      // post-stream markdown affordances as a freshly-finished turn.
      out.push({
        kind: 'markdown',
        id: m.id,
        turnId: `restored-${m.id}`,
        text: m.content,
        ts: m.timestamp,
        // Restored assistant turns have no intermediate tool events,
        // so they always carry the agent▸ prefix.
        isContinuation: false,
      });
    }
  }
  return out;
}

/** Map IndexedDB image records into ReplLine.image variants. */
function imagesToLines(records: StoredImageRecord[]): ReplLine[] {
  return records.map(r => {
    const url = createObjectUrl(r.storageKey, r.blob);
    const attachment: ImageAttachment = {
      imageId:    r.imageId,
      storageKey: r.storageKey,
      url,
      mimeType:   r.mimeType,
      size:       r.size,
    };
    return makeImage(`restored-${r.messageId}`, attachment, r.toolName, r.toolCallId, r.createdAt);
  });
}

/** Stable merge of two ts-sorted line arrays. Keeps relative order within
 *  each list when timestamps tie — text lines first (history), images after. */
function mergeByTs(textLines: ReplLine[], imageLines: ReplLine[]): ReplLine[] {
  const out: ReplLine[] = [];
  let i = 0;
  let j = 0;
  while (i < textLines.length && j < imageLines.length) {
    const a = textLines[i];
    const b = imageLines[j];
    const ta = 'ts' in a ? a.ts : 0;
    const tb = 'ts' in b ? b.ts : 0;
    if (ta <= tb) {
      out.push(a);
      i++;
    } else {
      out.push(b);
      j++;
    }
  }
  while (i < textLines.length) out.push(textLines[i++]);
  while (j < imageLines.length) out.push(imageLines[j++]);
  return out;
}

function tplFill(s: string, vars: Record<string, string | number>): string {
  return s.replace(/\{(\w+)\}/g, (_, k) => String(vars[k] ?? `{${k}}`));
}

/** Extract cacheable Message[] from the current ReplLine[].
 *  Only `user` and `markdown` lines carry meaningful content for the cache. */
function linesToMessages(lines: ReplLine[]): Message[] {
  const out: Message[] = [];
  for (const l of lines) {
    if (l.kind === 'user') {
      out.push({ id: l.id, role: 'user', content: l.text, timestamp: l.ts });
    } else if (l.kind === 'markdown') {
      out.push({ id: l.id, role: 'assistant', content: l.text, timestamp: l.ts });
    }
  }
  return out;
}

function AppInner() {
  const { t } = useT();

  const [theme, setTheme] = useState<'light' | 'dark' | 'system'>(() => {
    const saved = localStorage.getItem('eo_theme');
    return (saved as 'light' | 'dark' | 'system') || 'system';
  });

  useEffect(() => {
    const root = document.documentElement;
    if (theme === 'system') {
      root.removeAttribute('data-theme');
    } else {
      root.setAttribute('data-theme', theme);
    }
    localStorage.setItem('eo_theme', theme);
  }, [theme]);

  const [conversationId, setConversationId] = useState<string>(() => {
    const existing = getExistingConversationId();
    if (existing) return existing;
    const id = crypto.randomUUID();
    localStorage.setItem(CONVERSATION_ID_STORAGE_KEY, id);
    return id;
  });
  const [sessions, setSessions] = useState<ChatSessionInfo[]>(() => getStoredSessions());
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // Workspace files states
  const [workspaceFiles, setWorkspaceFiles] = useState<WorkspaceFile[]>([]);
  const [editingFile, setEditingFile] = useState<{ name: string; content: string; isNew: boolean } | null>(null);
  const [editorLoading, setEditorLoading] = useState(false);
  const [editorSaving, setEditorSaving] = useState(false);
  const [mountedPath, setMountedPath] = useState<string>(() => localStorage.getItem('mounted_folder_path') || '');
  const lastSyncedRef = useRef<Map<string, { size: number; mtime: number }>>(new Map());

  useEffect(() => {
    if (mountedPath) {
      setWorkspaceRoot(mountedPath);
    } else {
      setWorkspaceRoot('/');
    }
  }, [mountedPath]);

  const refreshLocalFiles = useCallback(async () => {
    try {
      await initLocalFs();
      const localFiles = await listLocalFiles();
      setWorkspaceFiles(localFiles);
      
      // Update last synced map so focus scanning can ignore these
      lastSyncedRef.current.clear();
      for (const f of localFiles) {
        lastSyncedRef.current.set(f.name, { size: f.size, mtime: f.mtime });
      }
    } catch (err) {
      console.error('Failed to load local files:', err);
    }
  }, []);

  const syncFilesFromCloud = useCallback(async (cid: string) => {
    try {
      await initLocalFs();
      const cloudFiles = await fetchWorkspaceFiles(cid);
      const localFiles = await listLocalFiles();
      const cloudFileNames = new Set(cloudFiles.map(f => f.name));

      for (const localF of localFiles) {
        if (!cloudFileNames.has(localF.name)) {
          await deleteLocalFile(localF.name);
          lastSyncedRef.current.delete(localF.name);
        }
      }

      await Promise.all(cloudFiles.map(async (cloudF) => {
        const content = await readWorkspaceFile(cid, cloudF.name);
        await writeLocalFile(cloudF.name, content);
      }));

      const updatedLocalFiles = await listLocalFiles();
      setWorkspaceFiles(updatedLocalFiles);
      
      // Keep local sync cache aligned
      lastSyncedRef.current.clear();
      for (const f of updatedLocalFiles) {
        lastSyncedRef.current.set(f.name, { size: f.size, mtime: f.mtime });
      }
    } catch (err) {
      console.error('Failed to sync files from cloud:', err);
    }
  }, []);

  const loadFiles = useCallback(async (cid: string) => {
    if (!cid) return;
    await syncFilesFromCloud(cid);
  }, [syncFilesFromCloud]);

  const handleMountFolder = useCallback(async () => {
    try {
      await initLocalFs();
      const path = await mountLocalFolder();
      setMountedPath(path);
      localStorage.setItem('mounted_folder_path', path);
      setWorkspaceRoot(path);
      
      console.log(`[mount] Folder mounted at ${path}. Syncing files to sandbox...`);
      const localFiles = await listLocalFiles();
      
      // Sync local files to cloud store & sandbox
      for (const f of localFiles) {
        const content = await readLocalFile(f.name);
        await writeWorkspaceFile(conversationIdRef.current, f.name, content);
        await syncFileToSandbox(conversationIdRef.current, f.name, content);
      }
      
      await refreshLocalFiles();
      alert(`Directory mounted successfully at: ${path}\nBidirectional sync is now active!`);
    } catch (err) {
      console.error('Mounting failed:', err);
      alert('Mounting local folder failed or was cancelled.');
    }
  }, [refreshLocalFiles]);

  const handleUnmountFolder = useCallback(() => {
    unmountLocalFolder();
    setMountedPath('');
    localStorage.removeItem('mounted_folder_path');
    refreshLocalFiles();
  }, [refreshLocalFiles]);

  const [lines, setLines] = useState<ReplLine[]>([]);
  const [traceEvents, setTraceEvents] = useState<RawSseEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [verbose, setVerbose] = useState(false);
  const [inputHistory, setInputHistory] = useState<string[]>([]);
  const [lightboxUrl, setLightboxUrl] = useState<string | null>(null);
  const [lightboxAlt, setLightboxAlt] = useState<string>('');
  // Set on send, cleared on first agent output / done / error / abort.
  // The pending caret in <ReplStream> is shown iff this is non-null.
  // Using a single piece of state instead of inserting+filtering a placeholder
  // row across 5 SSE handlers — see PendingCaret.tsx for the rationale.
  const [pendingTurnId, setPendingTurnId] = useState<string | null>(null);
  const [availableModels, setAvailableModels] = useState<ModelOption[]>([]);
  const [selectedModel, setSelectedModel] = useState<string>('');
  const selectedModelRef = useRef<string>('');

  const turnMetaRef = useRef<TurnMeta | null>(null);
  const abortCtrlRef = useRef<AbortController | null>(null);
  
  const conversationIdRef = useRef<string>(conversationId);
  const knownVersionRef = useRef<number>(0);
  const clearInputRef = useRef<() => void>(() => {});
  const setPromptValueRef = useRef<(text: string) => void>(() => {});
  const registerSetPromptValue = useCallback((fn: (text: string) => void) => {
    setPromptValueRef.current = fn;
  }, []);
  const verboseRef = useRef<boolean>(false);

  const handlePresetClick = useCallback((text: string) => {
    setPromptValueRef.current?.(text);
  }, []);

  // Sync state to ref for stale-closure safety in SSE loops
  useEffect(() => {
    conversationIdRef.current = conversationId;
  }, [conversationId]);

  // Ensure there is always a session in the list on mount
  useEffect(() => {
    const saved = getStoredSessions();
    if (saved.length === 0) {
      const activeId = conversationIdRef.current;
      const initial = [{ id: activeId, title: 'New Chat', timestamp: Date.now() }];
      saveStoredSessions(initial);
      setSessions(initial);
    }
  }, []);

  // Restore conversation history whenever active conversationId changes.
  useEffect(() => {
    const saved = getStoredSessions();
    const hasHistory = saved.some(s => s.id === conversationId);

    loadFiles(conversationId);

    if (!hasHistory) {
      // Brand-new session → clear chat window, skip network history fetch
      setLines([]);
      setTraceEvents([]);
      setHistoryLoading(false);
      return;
    }

    setHistoryLoading(true);
    setTraceEvents([]);
    const cid = conversationId;

    // ── Instant restore from localStorage cache ──
    const cached = readCachedMessages(cid);
    if (cached.length > 0) {
      const textLines = historyToLines(cached);
      if (textLines.length > 0) {
        const marker = makeRestored(cached.length);
        setLines([...textLines, marker]);
      }
    } else {
      setLines([]);
    }

    // ── Background fetch to validate / refresh ──
    Promise.all([
      fetchConversationHistory(cid),
      loadConversationImages(cid).catch(err => {
        console.warn('[image-store] failed to load conversation images:', err);
        return [] as StoredImageRecord[];
      }),
    ])
      .then(([history, imageRecords]) => {
        if (conversationIdRef.current !== cid) return;

        // Update localStorage cache with fresh server data
        if (history.length > 0) {
          cacheMessages(cid, history);
        }

        const textLines = historyToLines(history);
        const imageLines = imagesToLines(imageRecords);
        const merged = mergeByTs(textLines, imageLines);
        
        if (merged.length > 0) {
          const marker = makeRestored(history.length);
          setLines([...merged, marker]);
        } else {
          setLines([]);
        }
      })
      .catch(err => {
        console.error('Failed to load history:', err);
      })
      .finally(() => {
        if (conversationIdRef.current === cid) {
          setHistoryLoading(false);
        }
      });
  }, [conversationId]);

  // Revoke all blob: URLs on unmount. We don't try to revoke URL-by-URL on
  // line removal — clearScreen / resetSession replace the lines array as a
  // whole, and the revokeAllObjectUrls() called there is the single source
  // of truth. This effect just covers the page-close / route-leave path.
  useEffect(() => {
    return () => {
      revokeAllObjectUrls();
    };
  }, []);

  // Fetch available models on mount
  useEffect(() => {
    fetchModels(conversationId).then(models => {
      setAvailableModels(models);
      if (models.length > 0 && !selectedModelRef.current) {
        const first = models[0].id;
        setSelectedModel(first);
        selectedModelRef.current = first;
      }
    }).catch(() => {
      // Non-critical — model dropdown will show fallback name
    });
  }, []);

  // Poll workspace version for changes when not streaming
  useEffect(() => {
    if (loading) return;
    const interval = setInterval(async () => {
      const cid = conversationIdRef.current;
      if (!cid) return;
      try {
        const status = await fetchWorkspaceFileStatus(cid);
        if (status.version > knownVersionRef.current) {
          knownVersionRef.current = status.version;
          await syncFilesFromCloud(cid);
        }
      } catch {}
    }, 5000);
    return () => clearInterval(interval);
  }, [loading, syncFilesFromCloud]);

  // Listen for window focus to check for local changes when folder is mounted
  useEffect(() => {
    const handleFocus = async () => {
      const root = getWorkspaceRoot();
      if (root === '/') return; // Only sync when a local folder is mounted
      
      console.log('[fs_sync] Window focused. Scanning for local modifications...');
      try {
        await initLocalFs();
        const localFiles = await listLocalFiles();
        const localFileNames = new Set(localFiles.map(f => f.name));
        
        let changed = false;
        
        // Find modified or new files
        for (const localF of localFiles) {
          const synced = lastSyncedRef.current.get(localF.name);
          if (!synced || synced.size !== localF.size || synced.mtime !== localF.mtime) {
            console.log(`[fs_sync] Local change detected in ${localF.name} (size: ${localF.size}, mtime: ${localF.mtime})`);
            const content = await readLocalFile(localF.name);
            
            await writeWorkspaceFile(conversationIdRef.current, localF.name, content);
            await syncFileToSandbox(conversationIdRef.current, localF.name, content);
            
            lastSyncedRef.current.set(localF.name, { size: localF.size, mtime: localF.mtime });
            changed = true;
          }
        }
        
        // Find deleted files
        for (const name of lastSyncedRef.current.keys()) {
          if (!localFileNames.has(name)) {
            console.log(`[fs_sync] Local deletion detected for ${name}`);
            await deleteWorkspaceFile(conversationIdRef.current, name);
            await syncDeleteToSandbox(conversationIdRef.current, name);
            
            lastSyncedRef.current.delete(name);
            changed = true;
          }
        }
        
        if (changed) {
          const updated = await listLocalFiles();
          setWorkspaceFiles(updated);
        }
      } catch (err) {
        console.warn('[fs_sync] Window focus sync failed:', err);
      }
    };

    window.addEventListener('focus', handleFocus);
    return () => {
      window.removeEventListener('focus', handleFocus);
    };
  }, []);


  // ─── SSE handlers (turn-scoped) ─────────────────────────────────────
  const finishStream = useCallback(() => {
    setLoading(false);
    // Hide the pending caret on every terminal path (done/error/abort).
    // Idempotent — safe to call when no turn was pending.
    setPendingTurnId(null);
    abortCtrlRef.current = null;
    loadFiles(conversationIdRef.current);
  }, [loadFiles]);

  const handleImage = useCallback((payload: ImageSsePayload) => {
    const cid = conversationIdRef.current;
    const meta = turnMetaRef.current;
    const turnId = meta?.turnId ?? 'orphan';

    let blob: Blob;
    try {
      blob = base64ToBlob(payload.base64, payload.mimeType);
    } catch (err) {
      // Corrupt base64 → no image, but the stream continues. Surface a hint
      // so the user knows something was dropped.
      console.warn('[image] base64 decode failed:', err);
      // Image broken - no longer rendered as a sysHint line
      return;
    }

    // CRITICAL: append the line SYNCHRONOUSLY, before any await. Multiple
    // image events arriving back-to-back (a tool that returned 5 screenshots)
    // each spawn an independent IDB readwrite transaction, and IndexedDB
    // does NOT guarantee resolution order across distinct transactions on the
    // same store. Appending after `await saveImage` would let images render
    // out of arrival order. The blob URL is created from the in-memory blob,
    // not from the IDB record, so we don't need persistence to land first.
    const storageKey = makeStorageKey(cid, payload.imageId);
    const url = createObjectUrl(storageKey, blob);
    const attachment: ImageAttachment = {
      imageId:    payload.imageId,
      storageKey,
      url,
      mimeType:   payload.mimeType,
      size:       payload.size || blob.size,
    };
    const imageLine = makeImage(turnId, attachment, payload.toolName, payload.toolCallId);
    setLines(prev => [...prev, imageLine]);

    // Fire-and-forget persist. Failure (quota, private mode, etc.) is not
    // fatal — the image stays visible for the current session via the blob
    // URL we just created.
    void saveImage({
      conversationId: cid,
      messageId:      turnId,
      imageId:        payload.imageId,
      blob,
      mimeType:       payload.mimeType,
      toolName:       payload.toolName,
      toolCallId:     payload.toolCallId,
    }).catch(err => {
      console.warn('[image] saveImage failed; rendering without persistence:', err);
    });
  }, []);

  const handleSend = useCallback(
    (text: string) => {
      if (loading) return;

      // Push user echo, kick the pending caret, then start a new turn.
      // Pending visibility is driven by `pendingTurnId` state — cleared on
      // first agent output (text_delta/tool_called/image), or on
      // done/error/abort. See PendingCaret.tsx for rationale.
      const turnId = crypto.randomUUID();
      const userLine = makeUser(text);
      setLines(prev => {
        const next = [...prev, userLine];
        // Cache the user message immediately so it's available on session switch
        const cid = conversationIdRef.current;
        const msgs = linesToMessages(next);
        if (msgs.length > 0) cacheMessages(cid, msgs);
        return next;
      });
      setInputHistory(prev => {
        const next = [...prev.filter(s => s !== text), text];
        return next.length > MAX_INPUT_HISTORY ? next.slice(-MAX_INPUT_HISTORY) : next;
      });

      // Update session title & save to sessions list
      setSessions(prev => {
        const activeId = conversationIdRef.current;
        const exists = prev.some(s => s.id === activeId);
        let next = [...prev];
        const displayTitle = text.length > 28 ? text.slice(0, 28) + '...' : text;
        if (!exists) {
          next.unshift({
            id: activeId,
            title: displayTitle,
            timestamp: Date.now()
          });
        } else {
          next = prev.map(s => {
            if (s.id === activeId && (s.title === 'New Chat' || s.title === '新建会话')) {
              return {
                ...s,
                title: displayTitle
              };
            }
            return s;
          });
        }
        saveStoredSessions(next);
        return next;
      });

      turnMetaRef.current = startTurn(turnId);
      setPendingTurnId(turnId);
      setLoading(true);

      (async () => {
        try {
          const root = getWorkspaceRoot();
          if (root !== '/') {
            await initLocalFs();
            const localFiles = await listLocalFiles();
            const localFileNames = new Set(localFiles.map(f => f.name));
            
            for (const localF of localFiles) {
              const synced = lastSyncedRef.current.get(localF.name);
              if (!synced || synced.size !== localF.size || synced.mtime !== localF.mtime) {
                console.log(`[handleSend] Syncing local change: ${localF.name}`);
                const content = await readLocalFile(localF.name);
                await writeWorkspaceFile(conversationIdRef.current, localF.name, content);
                lastSyncedRef.current.set(localF.name, { size: localF.size, mtime: localF.mtime });
              }
            }
            
            for (const name of lastSyncedRef.current.keys()) {
              if (!localFileNames.has(name)) {
                console.log(`[handleSend] Syncing local delete: ${name}`);
                await deleteWorkspaceFile(conversationIdRef.current, name);
                lastSyncedRef.current.delete(name);
              }
            }
            
            const updated = await listLocalFiles();
            setWorkspaceFiles(updated);
          }
        } catch (err) {
          console.warn('[handleSend] Sync before stream failed:', err);
        }

        const ctrl = sendMessageStream(
          text,
          {
            onTextDelta: delta => {
              const meta = turnMetaRef.current;
              if (!meta) return;

              // CRITICAL: do NOT generate ids or mutate refs *inside* the
              // setLines updater. React 18 StrictMode invokes updaters twice
              // in dev; any side-effect (UUID generation, ref mutation) makes
              // the two calls disagree and React keeps only the second return.
              //
              // We decide the target line id BEFORE setLines, mutate the ref
              // at the same time, then run a pure updater.
              if (meta.currentTextLineId === null) {
                // Continuation = a text line that comes AFTER a tool call in the
                // same turn. The very first text line of the turn gets the
                // agent▸ prefix; later segments don't, to avoid visual noise.
                const isContinuation = meta.toolRounds > 0;
                const fresh = makeText(meta.turnId, '', isContinuation);
                meta.currentTextLineId = fresh.id;
                meta.hasText = true;
                setPendingTurnId(null);
                setLines(prev => [...prev, { ...fresh, text: delta }]);
              } else {
                const target = meta.currentTextLineId;
                meta.hasText = true;
                setLines(prev =>
                  prev.map(l =>
                    l.kind === 'text' && l.id === target ? { ...l, text: l.text + delta } : l,
                  ),
                );
              }
            },

            onToolCalled: (toolName, filesSnapshot) => {
              const meta = turnMetaRef.current;
              if (!meta) return;
              // Each tool call ends the current text line; the next text_delta
              // will start a fresh one.
              meta.currentTextLineId = null;
              meta.toolRounds += 1;
              // Build the line OUTSIDE the updater so its id is stable across
              // StrictMode's double invocation.
              const toolLine = makeTool(meta.turnId, toolName, { status: 'running' });
              setPendingTurnId(null);
              setLines(prev => [...prev, toolLine]);

              // Merge files snapshot into local workspace state if provided
              if (filesSnapshot && typeof filesSnapshot === 'object' && Object.keys(filesSnapshot).length > 0) {
                (async () => {
                  try {
                    await initLocalFs();
                    const localFiles = await listLocalFiles();
                    const snapshotNames = new Set(Object.keys(filesSnapshot));
                    // Remove local files that no longer exist in the snapshot
                    for (const lf of localFiles) {
                      if (!snapshotNames.has(lf.name)) {
                        await deleteLocalFile(lf.name);
                      }
                    }
                    // Write all snapshot files to local fs
                    await Promise.all(
                      Object.entries(filesSnapshot).map(([name, content]) => writeLocalFile(name, content))
                    );
                    await refreshLocalFiles();
                  } catch (err) {
                    console.warn('[files_snapshot] Failed to merge snapshot into local fs:', err);
                  }
                })();
              }
            },

            onToolDebug: (payload: ToolDebugPayload) => {
              if (payload.phase === 'call') {
                // Merge call-phase data into the most recent matching tool line
                const toolName = payload.tool;
                setLines(prev => {
                  // Find the last tool line matching this tool name
                  for (let i = prev.length - 1; i >= 0; i--) {
                    const l = prev[i];
                    if (l.kind === 'tool' && l.tool === toolName) {
                      const updated = {
                        ...l,
                        argsPreview: payload.argumentsPreview ?? l.argsPreview,
                        inputArgs: payload.argumentsPreview ?? l.inputArgs,
                      };
                      return [...prev.slice(0, i), updated, ...prev.slice(i + 1)];
                    }
                  }
                  return prev;
                });
              } else if (payload.phase === 'result') {
                // Merge result-phase data and set status
                const toolName = payload.tool;
                const durationMs = payload.durationMs;
                setLines(prev => {
                  for (let i = prev.length - 1; i >= 0; i--) {
                    const l = prev[i];
                    if (l.kind === 'tool' && l.tool === toolName) {
                      const updated = {
                        ...l,
                        status: 'success' as const,
                        durationMs: durationMs ?? l.durationMs,
                        outputResult: payload.resultPreview ?? l.outputResult,
                        resultSummary: payload.resultPreview
                          ? (payload.resultPreview.length > 80 ? payload.resultPreview.slice(0, 80) + '…' : payload.resultPreview)
                          : l.resultSummary,
                      };
                      return [...prev.slice(0, i), updated, ...prev.slice(i + 1)];
                    }
                  }
                  return prev;
                });
              }
            },

            onImage: payload => {
              handleImage(payload);
            },

            onFileChanged: payload => {
              if (payload.version > knownVersionRef.current) {
                knownVersionRef.current = payload.version;
                syncFilesFromCloud(conversationIdRef.current);
              }
            },

            onRawEvent: ev => {
              // Coalesce consecutive text_delta events into a single growing entry,
              // so a multi-paragraph response doesn't flood the trace panel with
              // hundreds of one-token rows.
              if (ev.eventType === 'text_delta') {
                const delta = (ev.data as { delta?: string } | null)?.delta ?? '';
                setTraceEvents(prev => {
                  const last = prev[prev.length - 1];
                  if (last && last.eventType === 'text_delta') {
                    const prevDelta = (last.data as { delta?: string } | null)?.delta ?? '';
                    const merged: RawSseEvent = {
                      ...last,
                      data: { delta: prevDelta + delta },
                      raw: last.raw + delta,
                      timestamp: ev.timestamp,
                    };
                    return [...prev.slice(0, -1), merged];
                  }
                  return [...prev, ev];
                });
                return;
              }
              // Mirror to trace buffer for verbose mode.
              setTraceEvents(prev => [...prev, ev]);
            },

            onDone: () => {
              const meta = turnMetaRef.current;
              if (meta) {
                const doneLine = makeDone(meta.turnId, meta.startTs, meta.toolRounds);
                setLines(prev => {
                  const next = [...collapseTurnTextToMarkdown(prev, meta.turnId), doneLine];
                  // Cache the completed conversation to localStorage
                  const cid = conversationIdRef.current;
                  const msgs = linesToMessages(next);
                  if (msgs.length > 0) cacheMessages(cid, msgs);
                  return next;
                });
                turnMetaRef.current = null;
              }
              finishStream();
            },

            onError: err => {
              const meta = turnMetaRef.current;
              const errLine = makeError(err.message || t('status.error'), meta?.turnId);
              const padLine =
                meta && !meta.hasText ? makeText(meta.turnId, '', true) : null;
              setLines(prev => {
                // Even on error, collapse whatever text the model managed to
                // emit so the user sees rendered markdown rather than a
                // dangling streaming run alongside the error line.
                let collapsed = meta ? collapseTurnTextToMarkdown(prev, meta.turnId) : prev;
                // Mark any still-running tool lines as error
                collapsed = collapsed.map(l =>
                  l.kind === 'tool' && l.status === 'running'
                    ? { ...l, status: 'error' as const }
                    : l,
                );
                const next = padLine ? [...collapsed, errLine, padLine] : [...collapsed, errLine];
                // Cache partial conversation even on error
                const cid = conversationIdRef.current;
                const msgs = linesToMessages(next);
                if (msgs.length > 0) cacheMessages(cid, msgs);
                return next;
              });
              if (meta) turnMetaRef.current = null;
              finishStream();
            },
          },
          conversationIdRef.current,
          selectedModelRef.current || undefined,
        );

        abortCtrlRef.current = ctrl;
      })();
    },
    [loading, t, finishStream, handleImage],
  );

  // ─── Action handlers (keyboard) ─────────────────────────────────────
  const handleStop = useCallback(() => {
    if (abortCtrlRef.current) {
      abortCtrlRef.current.abort();
      abortCtrlRef.current = null;
    }
    const meta = turnMetaRef.current;
    // Collapse any partial text the model emitted before abort so the row
    // settles into its final markdown form (matches onDone / onError).
    setLines(prev => {
      const collapsed = meta ? collapseTurnTextToMarkdown(prev, meta.turnId) : prev;
      return collapsed;
    });
    setLoading(false);
    setPendingTurnId(null);

    stopAgent(conversationIdRef.current).catch(() => {
      // stop failure is non-critical
    });
    if (meta) turnMetaRef.current = null;
  }, []);

  const handleClearScreen = useCallback(() => {
    // Clearing the screen drops references to all currently-rendered image
    // rows, so revoke their blob URLs to release memory. The IDB records are
    // intentionally preserved — server history is preserved on /clear too.
    revokeAllObjectUrls();
    setLines([]);
  }, []);

  const handleToggleSidebar = useCallback(() => {
    setSidebarOpen(prev => !prev);
  }, []);

  const handleSelectSession = useCallback((id: string) => {
    localStorage.setItem(CONVERSATION_ID_STORAGE_KEY, id);
    setConversationId(id);
  }, []);

  const handleNewChat = useCallback(() => {
    if (abortCtrlRef.current) {
      abortCtrlRef.current.abort();
      abortCtrlRef.current = null;
    }
    setLoading(false);
    setPendingTurnId(null);
    turnMetaRef.current = null;

    const newId = crypto.randomUUID();
    localStorage.setItem(CONVERSATION_ID_STORAGE_KEY, newId);
    setConversationId(newId);
    setLines([]);
    setTraceEvents([]);
    removeCachedMessages(newId);

    setSessions(prev => {
      const exists = prev.some(s => s.id === newId);
      if (exists) return prev;
      const next = [{ id: newId, title: 'New Chat', timestamp: Date.now() }, ...prev];
      saveStoredSessions(next);
      return next;
    });
  }, []);

  const handleDeleteSession = useCallback((id: string) => {
    setSessions(prev => {
      const next = prev.filter(s => s.id !== id);
      saveStoredSessions(next);
      return next;
    });
    
    // Revoke images for deleted session
    deleteConversationImages(id).catch(() => {});
    removeCachedMessages(id);

    if (id === conversationIdRef.current) {
      const remaining = getStoredSessions().filter(s => s.id !== id);
      if (remaining.length > 0) {
        setConversationId(remaining[0].id);
      } else {
        handleNewChat();
      }
    }
  }, [handleNewChat]);

  const handleClearAll = useCallback(() => {
    if (confirm('Clear all conversation history? This cannot be undone.')) {
      if (abortCtrlRef.current) {
        abortCtrlRef.current.abort();
        abortCtrlRef.current = null;
      }
      setLoading(false);
      setPendingTurnId(null);
      turnMetaRef.current = null;

      // Revoke URLs to release memory
      revokeAllObjectUrls();

      // Clear all images from IndexedDB
      sessions.forEach(s => {
        deleteConversationImages(s.id).catch(() => {});
        removeCachedMessages(s.id);
      });

      // Wipe localStorage keys
      localStorage.removeItem(CONVERSATION_ID_STORAGE_KEY);
      localStorage.removeItem(SESSIONS_STORAGE_KEY);

      // Set clean states
      setSessions([]);
      const newId = crypto.randomUUID();
      localStorage.setItem(CONVERSATION_ID_STORAGE_KEY, newId);
      setConversationId(newId);
      setLines([]);
      setTraceEvents([]);
    }
  }, [sessions, handleNewChat]);

  const handleOpenFile = useCallback(async (filename: string) => {
    setEditingFile({ name: filename, content: '', isNew: false });
    setEditorLoading(true);
    try {
      await initLocalFs();
      const content = await readLocalFile(filename);
      setEditingFile(prev => prev ? { ...prev, content } : null);
    } catch (err) {
      alert('Failed to read file contents');
    } finally {
      setEditorLoading(false);
    }
  }, []);

  const handleSaveFile = useCallback(async (filename: string, content: string) => {
    setEditorSaving(true);
    try {
      await initLocalFs();
      await writeLocalFile(filename, content);
      const result = await writeWorkspaceFile(conversationId, filename, content, knownVersionRef.current || undefined);
      if (result.success) {
        // Push to sandbox
        syncFileToSandbox(conversationId, filename, content).catch(() => {});
        knownVersionRef.current += 1;
        setEditingFile(null);
        await refreshLocalFiles();
      } else if (result.conflict) {
        // Version conflict — re-sync and retry once
        await syncFilesFromCloud(conversationId);
        const retryResult = await writeWorkspaceFile(conversationId, filename, content);
        if (retryResult.success) {
          syncFileToSandbox(conversationId, filename, content).catch(() => {});
          setEditingFile(null);
          await refreshLocalFiles();
        } else {
          alert('Failed to save file after sync — please try again');
        }
      } else {
        alert('Failed to save file to cloud');
      }
    } catch (err) {
      alert('Failed to save file');
    } finally {
      setEditorSaving(false);
    }
  }, [conversationId, refreshLocalFiles, syncFilesFromCloud]);

  const handleDeleteFile = useCallback(async (filename: string) => {
    try {
      await initLocalFs();
      await deleteLocalFile(filename);
      const result = await deleteWorkspaceFile(conversationId, filename, knownVersionRef.current || undefined);
      if (result.success) {
        syncDeleteToSandbox(conversationId, filename).catch(() => {});
        knownVersionRef.current += 1;
        await refreshLocalFiles();
      } else if (result.conflict) {
        await syncFilesFromCloud(conversationId);
        const retryResult = await deleteWorkspaceFile(conversationId, filename);
        if (retryResult.success) {
          syncDeleteToSandbox(conversationId, filename).catch(() => {});
          await refreshLocalFiles();
        } else {
          alert('Failed to delete file after sync — please try again');
        }
      } else {
        alert('Failed to delete file from cloud');
      }
    } catch (err) {
      alert('Failed to delete file');
    }
  }, [conversationId, refreshLocalFiles, syncFilesFromCloud]);

  const handleCreateFile = useCallback(() => {
    const filename = prompt('Enter new file name:');
    if (!filename) return;
    const cleanName = filename.trim();
    if (!cleanName) return;
    
    // Check if file already exists
    if (workspaceFiles.some(f => f.name.toLowerCase() === cleanName.toLowerCase())) {
      alert('File already exists');
      return;
    }
    
    setEditingFile({ name: cleanName, content: '', isNew: true });
  }, [workspaceFiles]);

  const handleResetSession = useCallback(() => {
    handleNewChat();
  }, [handleNewChat]);

  const handleToggleVerbose = useCallback(() => {
    // Compute next value via ref to avoid nesting setLines inside a setVerbose
    // updater (which StrictMode invokes twice → would append the hint twice).
    const next = !verboseRef.current;
    verboseRef.current = next;
    setVerbose(next);
  }, []);

  const handleShowHelp = useCallback(() => {
    // Help is now shown as a popover from the Help button, no longer as chat lines
  }, []);

  const handleOpenImage = useCallback((url: string, alt: string) => {
    setLightboxUrl(url);
    setLightboxAlt(alt);
  }, []);

  const handleCloseLightbox = useCallback(() => {
    setLightboxUrl(null);
  }, []);

  const onAction = useCallback(
    (action: ReplAction) => {
      switch (action) {
        case 'abort':
          handleStop();
          return;
        case 'clearInput':
          clearInputRef.current?.();
          return;
        case 'clearScreen':
          handleClearScreen();
          return;
        case 'resetSession':
          handleResetSession();
          return;
        case 'toggleVerbose':
          handleToggleVerbose();
          return;
        case 'showHelp':
          handleShowHelp();
          return;
      }
    },
    [handleStop, handleClearScreen, handleResetSession, handleToggleVerbose, handleShowHelp],
  );

  const registerClearInput = useCallback((fn: () => void) => {
    clearInputRef.current = fn;
  }, []);

  const historyHint = useMemo(() => {
    const id = conversationId.slice(0, 8);
    return tplFill(t('repl.status.restoring'), { id, n: 0 });
  }, [conversationId, t]);

  return (
    <div className={styles.app}>
      <Sidebar
        sessions={sessions}
        activeSessionId={conversationId}
        onSelectSession={handleSelectSession}
        onDeleteSession={handleDeleteSession}
        onNewChat={handleNewChat}
        onClearAll={handleClearAll}
        isOpen={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        theme={theme}
        onThemeChange={setTheme}
        workspaceFiles={workspaceFiles}
        onOpenFile={handleOpenFile}
        onDeleteFile={handleDeleteFile}
        onCreateFile={handleCreateFile}
        mountedPath={mountedPath}
        onMountFolder={handleMountFolder}
        onUnmountFolder={handleUnmountFolder}
      />
      <ReplShell
        modelName={selectedModel || 'Agent'}
        loading={loading}
        historyLoading={historyLoading}
        historyHint={historyHint}
        onAction={onAction}
        availableModels={availableModels}
        selectedModel={selectedModel}
        onModelChange={(model: string) => {
          setSelectedModel(model);
          selectedModelRef.current = model;
        }}
        onToggleSidebar={handleToggleSidebar}
        footer={
          <ReplPrompt
            loading={loading}
            onSubmit={handleSend}
            registerClearInput={registerClearInput}
            registerSetPromptValue={registerSetPromptValue}
            inputHistory={inputHistory}
            onAction={onAction}
          />
        }
      >
        <ReplStream
          lines={lines}
          traceEvents={traceEvents}
          verbose={verbose}
          showPending={pendingTurnId !== null}
          onOpenImage={handleOpenImage}
          onPresetClick={handlePresetClick}
        />
      </ReplShell>
      <ImageLightbox url={lightboxUrl} alt={lightboxAlt} onClose={handleCloseLightbox} />

      {editingFile && (
        <FileEditorModal
          filename={editingFile.name}
          initialContent={editingFile.content}
          loading={editorLoading}
          saving={editorSaving}
          onSave={content => handleSaveFile(editingFile.name, content)}
          onClose={() => setEditingFile(null)}
        />
      )}
    </div>
  );
}

export default function App() {
  return (
    <I18nProvider>
      <AppInner />
    </I18nProvider>
  );
}
