/**
 * WebSocket File Bridge Client
 *
 * Connects to the backend WebSocket endpoint for real-time file sync
 * between IndexedDB (persistent, source of truth) and the sandbox (ephemeral).
 *
 * Replaces the broken KV-as-bridge pattern:
 *   Before: IDB ←→ KV (non-persistent) ←→ Sandbox
 *   After:  IDB ←→ WebSocket Bridge ←→ Sandbox
 *
 * Protocol:
 *   Frontend → Server:
 *     {type: "sync_all", files: Record<string, string>}
 *     {type: "file_write", path: string, content: string}
 *     {type: "file_delete", path: string}
 *     {type: "file_list_request"}
 *     {type: "ping"}
 *
 *   Server → Frontend:
 *     {type: "connected", conversation_id: string}
 *     {type: "file_write", path: string, content: string}
 *     {type: "file_delete", path: string}
 *     {type: "sync_all", files: Record<string, string>, count: number}
 *     {type: "sync_ack", count: number}
 *     {type: "file_list_response", files: Record<string, string>}
 *     {type: "pong"}
 *     {type: "error", message: string}
 */

import { saveFile, deleteFile, loadConversationFiles } from './workspaceStore';

const RECONNECT_DELAY = 2000;
const MAX_RECONNECT_DELAY = 30000;
const PING_INTERVAL = 25000;

export interface FileBridgeCallbacks {
  onFileWrite?: (path: string, content: string) => void;
  onFileDelete?: (path: string) => void;
  onSyncAll?: (files: Record<string, string>) => void;
  onConnected?: () => void;
  onDisconnected?: () => void;
  onError?: (message: string) => void;
}

export class FileBridgeClient {
  private ws: WebSocket | null = null;
  private conversationId: string = '';
  private callbacks: FileBridgeCallbacks = {};
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private reconnectDelay: number = RECONNECT_DELAY;
  private intentionalClose: boolean = false;
  private connected: boolean = false;

  /**
   * Connect to the file bridge WebSocket.
   */
  connect(conversationId: string, callbacks: FileBridgeCallbacks = {}): void {
    this.conversationId = conversationId;
    this.callbacks = callbacks;
    this.intentionalClose = false;
    this._doConnect();
  }

  /**
   * Disconnect from the file bridge.
   */
  disconnect(): void {
    this.intentionalClose = true;
    this._cleanup();
  }

  /**
   * Send full IDB state to server (on connect or reconnect).
   */
  async syncToServer(conversationId?: string): Promise<void> {
    const cid = conversationId || this.conversationId;
    if (!cid) return;

    try {
      const files = await loadConversationFiles(cid);
      if (Object.keys(files).length > 0) {
        this._send({ type: 'sync_all', files });
      }
    } catch (err) {
      console.warn('[file-bridge] Failed to sync files to server:', err);
    }
  }

  /**
   * Push a file write to the server.
   */
  pushFileWrite(path: string, content: string): void {
    this._send({ type: 'file_write', path, content });
  }

  /**
   * Push a file deletion to the server.
   */
  pushFileDelete(path: string): void {
    this._send({ type: 'file_delete', path });
  }

  /**
   * Request current file list from server.
   */
  requestFileList(): void {
    this._send({ type: 'file_list_request' });
  }

  /**
   * Check if currently connected.
   */
  isConnected(): boolean {
    return this.connected;
  }

  // ── Private ────────────────────────────────────────────────────────

  private _doConnect(): void {
    if (this.ws) {
      this._cleanup();
    }

    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${window.location.host}/ws/file-bridge`;

    try {
      this.ws = new WebSocket(url);
    } catch (err) {
      console.warn('[file-bridge] WebSocket creation failed:', err);
      this._scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      console.log('[file-bridge] Connected');
      this.connected = true;
      this.reconnectDelay = RECONNECT_DELAY;
      this._startPing();
      this.callbacks.onConnected?.();

      // Send initial IDB state to server
      this.syncToServer();
    };

    this.ws.onmessage = (event) => {
      this._handleMessage(event.data);
    };

    this.ws.onerror = (event) => {
      console.warn('[file-bridge] Error:', event);
      this.callbacks.onError?.('WebSocket error');
    };

    this.ws.onclose = () => {
      console.log('[file-bridge] Disconnected');
      this.connected = false;
      this._stopPing();
      this.callbacks.onDisconnected?.();

      if (!this.intentionalClose) {
        this._scheduleReconnect();
      }
    };
  }

  private _handleMessage(data: string): void {
    let msg: any;
    try {
      msg = JSON.parse(data);
    } catch {
      console.warn('[file-bridge] Invalid JSON:', data);
      return;
    }

    const cid = this.conversationId;

    switch (msg.type) {
      case 'connected':
        console.log(`[file-bridge] Server acknowledged: ${msg.conversation_id}`);
        break;

      case 'file_write':
        // Server pushed a file write from sandbox → save to IDB
        if (msg.path && typeof msg.content === 'string') {
          saveFile({ conversationId: cid, filepath: msg.path, content: msg.content })
            .catch(err => console.warn('[file-bridge] IDB save failed:', err));
          this.callbacks.onFileWrite?.(msg.path, msg.content);
        }
        break;

      case 'file_delete':
        // Server pushed a file deletion from sandbox → delete from IDB
        if (msg.path) {
          deleteFile(cid, msg.path)
            .catch(err => console.warn('[file-bridge] IDB delete failed:', err));
          this.callbacks.onFileDelete?.(msg.path);
        }
        break;

      case 'sync_all':
        // Server sent full file state → replace IDB
        if (msg.files && typeof msg.files === 'object') {
          const files = msg.files as Record<string, string>;
          // Bulk save to IDB
          (async () => {
            for (const [path, content] of Object.entries(files)) {
              await saveFile({ conversationId: cid, filepath: path, content });
            }
          })().catch(err => console.warn('[file-bridge] Bulk IDB save failed:', err));
          this.callbacks.onSyncAll?.(files);
        }
        break;

      case 'sync_ack':
        console.log(`[file-bridge] Server acknowledged sync: ${msg.count} files`);
        break;

      case 'file_list_response':
        if (msg.files && typeof msg.files === 'object') {
          this.callbacks.onSyncAll?.(msg.files);
        }
        break;

      case 'pong':
        // Heartbeat response
        break;

      case 'error':
        console.warn(`[file-bridge] Server error: ${msg.message}`);
        this.callbacks.onError?.(msg.message);
        break;

      default:
        console.warn(`[file-bridge] Unknown message type: ${msg.type}`);
    }
  }

  private _send(msg: object): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  private _startPing(): void {
    this._stopPing();
    this.pingTimer = setInterval(() => {
      this._send({ type: 'ping' });
    }, PING_INTERVAL);
  }

  private _stopPing(): void {
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }

  private _scheduleReconnect(): void {
    if (this.reconnectTimer) return;
    if (this.intentionalClose) return;

    console.log(`[file-bridge] Reconnecting in ${this.reconnectDelay}ms...`);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this._doConnect();
    }, this.reconnectDelay);

    // Exponential backoff
    this.reconnectDelay = Math.min(this.reconnectDelay * 2, MAX_RECONNECT_DELAY);
  }

  private _cleanup(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this._stopPing();
    if (this.ws) {
      this.ws.onopen = null;
      this.ws.onmessage = null;
      this.ws.onerror = null;
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
    this.connected = false;
  }
}

// Singleton instance
export const fileBridge = new FileBridgeClient();
