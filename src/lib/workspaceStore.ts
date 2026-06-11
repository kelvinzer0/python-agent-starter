/**
 * IndexedDB Workspace Store
 * 
 * Stores all workspace files in IndexedDB for offline editing.
 * Files are synced with the backend via API endpoints.
 * 
 * DB name: python-starter-workspace-db
 * Store: files (keyPath: storageKey)
 * Indexes: byConversation, byPath
 */

import { openDatabase } from './idb';

const DB_NAME = 'python-starter-workspace-db';
const DB_VERSION = 1;
const STORE_NAME = 'files';
const MANIFEST_STORE = 'manifests';

export interface StoredFileRecord {
  /** Primary key: `${conversationId}/${filepath}` */
  storageKey: string;
  conversationId: string;
  filepath: string;
  content: string;
  size: number;
  hash: string;
  updatedAt: number;
  createdAt: number;
}

export interface FileManifest {
  /** Primary key: conversationId */
  conversationId: string;
  files: Record<string, string>; // filepath -> md5 hash
  version: number;
  updatedAt: number;
}

function openDB(): Promise<IDBDatabase> {
  return openDatabase(DB_NAME, DB_VERSION, (db) => {
    if (!db.objectStoreNames.contains(STORE_NAME)) {
      const store = db.createObjectStore(STORE_NAME, { keyPath: 'storageKey' });
      store.createIndex('byConversation', 'conversationId', { unique: false });
      store.createIndex('byPath', ['conversationId', 'filepath'], { unique: false });
    }
    if (!db.objectStoreNames.contains(MANIFEST_STORE)) {
      db.createObjectStore(MANIFEST_STORE, { keyPath: 'conversationId' });
    }
  });
}

/** Generate storage key from conversationId and filepath */
export function makeFileStorageKey(conversationId: string, filepath: string): string {
  return `${conversationId}/${filepath}`;
}

/** Compute MD5-like hash of file content (simple string hash for change detection) */
export function computeFileHash(content: string): string {
  let hash = 0;
  for (let i = 0; i < content.length; i++) {
    const char = content.charCodeAt(i);
    hash = ((hash << 5) - hash) + char;
    hash = hash & hash; // Convert to 32bit integer
  }
  return hash.toString(36);
}

/** Save a file to IndexedDB */
export async function saveFile(params: {
  conversationId: string;
  filepath: string;
  content: string;
}): Promise<StoredFileRecord> {
  const { conversationId, filepath, content } = params;
  const storageKey = makeFileStorageKey(conversationId, filepath);
  const now = Date.now();

  const record: StoredFileRecord = {
    storageKey,
    conversationId,
    filepath,
    content,
    size: content.length,
    hash: computeFileHash(content),
    updatedAt: now,
    createdAt: now,
  };

  const db = await openDB();
  return new Promise<StoredFileRecord>((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readwrite');
    const store = tx.objectStore(STORE_NAME);
    
    // Check if record exists to preserve createdAt
    const getReq = store.get(storageKey);
    getReq.onsuccess = () => {
      const existing = getReq.result as StoredFileRecord | undefined;
      if (existing) {
        record.createdAt = existing.createdAt;
      }
      
      const putReq = store.put(record);
      putReq.onsuccess = () => resolve(record);
      putReq.onerror = () => reject(putReq.error);
    };
    getReq.onerror = () => reject(getReq.error);
  });
}

/** Load a file from IndexedDB */
export async function loadFile(
  conversationId: string,
  filepath: string,
): Promise<StoredFileRecord | null> {
  const storageKey = makeFileStorageKey(conversationId, filepath);
  const db = await openDB();
  
  return new Promise<StoredFileRecord | null>((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readonly');
    const store = tx.objectStore(STORE_NAME);
    const req = store.get(storageKey);
    
    req.onsuccess = () => {
      resolve(req.result ?? null);
    };
    req.onerror = () => reject(req.error);
  });
}

/** Load all files for a conversation */
export async function loadConversationFiles(
  conversationId: string,
): Promise<Record<string, string>> {
  const db = await openDB();
  
  return new Promise<Record<string, string>>((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readonly');
    const store = tx.objectStore(STORE_NAME);
    const index = store.index('byConversation');
    const req = index.getAll(conversationId);
    
    req.onsuccess = () => {
      const records = (req.result ?? []) as StoredFileRecord[];
      const files: Record<string, string> = {};
      for (const record of records) {
        files[record.filepath] = record.content;
      }
      resolve(files);
    };
    req.onerror = () => reject(req.error);
  });
}

/** Load file metadata (without content) for listing */
export async function loadConversationFileList(
  conversationId: string,
): Promise<Array<{ filepath: string; size: number; hash: string; updatedAt: number }>> {
  const db = await openDB();
  
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readonly');
    const store = tx.objectStore(STORE_NAME);
    const index = store.index('byConversation');
    const req = index.getAll(conversationId);
    
    req.onsuccess = () => {
      const records = (req.result ?? []) as StoredFileRecord[];
      const fileList = records.map(r => ({
        filepath: r.filepath,
        size: r.size,
        hash: r.hash,
        updatedAt: r.updatedAt,
      }));
      fileList.sort((a, b) => a.filepath.localeCompare(b.filepath));
      resolve(fileList);
    };
    req.onerror = () => reject(req.error);
  });
}

/** Delete a file from IndexedDB */
export async function deleteFile(
  conversationId: string,
  filepath: string,
): Promise<void> {
  const storageKey = makeFileStorageKey(conversationId, filepath);
  const db = await openDB();
  
  return new Promise<void>((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readwrite');
    const store = tx.objectStore(STORE_NAME);
    const req = store.delete(storageKey);
    
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  });
}

/** Delete all files for a conversation */
export async function deleteConversationFiles(
  conversationId: string,
): Promise<void> {
  const db = await openDB();
  
  return new Promise<void>((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readwrite');
    const store = tx.objectStore(STORE_NAME);
    const index = store.index('byConversation');
    const req = index.openCursor(conversationId);
    
    req.onsuccess = () => {
      const cursor = req.result;
      if (cursor) {
        cursor.delete();
        cursor.continue();
      } else {
        resolve();
      }
    };
    req.onerror = () => reject(req.error);
  });
}

/** Save manifest for a conversation */
export async function saveManifest(
  conversationId: string,
  files: Record<string, string>,
  version: number,
): Promise<void> {
  const db = await openDB();
  const manifest: FileManifest = {
    conversationId,
    files,
    version,
    updatedAt: Date.now(),
  };
  
  return new Promise<void>((resolve, reject) => {
    const tx = db.transaction(MANIFEST_STORE, 'readwrite');
    const store = tx.objectStore(MANIFEST_STORE);
    const req = store.put(manifest);
    
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  });
}

/** Load manifest for a conversation */
export async function loadManifest(
  conversationId: string,
): Promise<FileManifest | null> {
  const db = await openDB();
  
  return new Promise<FileManifest | null>((resolve, reject) => {
    const tx = db.transaction(MANIFEST_STORE, 'readonly');
    const store = tx.objectStore(MANIFEST_STORE);
    const req = store.get(conversationId);
    
    req.onsuccess = () => {
      resolve(req.result ?? null);
    };
    req.onerror = () => reject(req.error);
  });
}

/** Bulk sync files from server to local IDB */
export async function syncFilesFromServer(
  conversationId: string,
  files: Record<string, string>,
  version: number,
): Promise<void> {
  const db = await openDB();
  
  // First, delete all existing files for this conversation
  await deleteConversationFiles(conversationId);
  
  // Then, save all new files
  const tx = db.transaction(STORE_NAME, 'readwrite');
  const store = tx.objectStore(STORE_NAME);
  
  const now = Date.now();
  const filesHashes: Record<string, string> = {};
  
  for (const [filepath, content] of Object.entries(files)) {
    const storageKey = makeFileStorageKey(conversationId, filepath);
    const record: StoredFileRecord = {
      storageKey,
      conversationId,
      filepath,
      content,
      size: content.length,
      hash: computeFileHash(content),
      updatedAt: now,
      createdAt: now,
    };
    store.put(record);
    filesHashes[filepath] = record.hash;
  }
  
  // Save manifest
  const manifestStore = tx.objectStore(MANIFEST_STORE);
  const manifest: FileManifest = {
    conversationId,
    files: filesHashes,
    version,
    updatedAt: now,
  };
  manifestStore.put(manifest);
}


