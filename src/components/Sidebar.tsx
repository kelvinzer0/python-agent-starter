import { useState } from 'react';
import { useT } from '../i18n';
import { 
  Sparkle, Plus, ChatTeardropText, Trash, Sun, Moon, Monitor, 
  FolderOpen, FileText, CaretDown, CaretRight 
} from '@phosphor-icons/react';
import type { WorkspaceFile } from '../api';
import styles from './Sidebar.module.css';

export interface ChatSessionInfo {
  id: string;
  title: string;
  timestamp: number;
}

interface SidebarProps {
  sessions: ChatSessionInfo[];
  activeSessionId: string;
  onSelectSession: (id: string) => void;
  onDeleteSession: (id: string) => void;
  onNewChat: () => void;
  onClearAll: () => void;
  isOpen: boolean;
  onClose: () => void;
  theme: 'light' | 'dark' | 'system';
  onThemeChange: (theme: 'light' | 'dark' | 'system') => void;

  // Workspace files
  workspaceFiles: WorkspaceFile[];
  onOpenFile: (filename: string) => void;
  onDeleteFile: (filename: string) => void;
  onCreateFile: () => void;

  // Mounting props
  mountedPath?: string;
  onMountFolder?: () => void;
  onUnmountFolder?: () => void;
  hasPermission?: boolean;
  onRequestPermission?: () => void;
}

function formatBytes(bytes: number, decimals = 1) {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const dm = decimals < 0 ? 0 : decimals;
  const sizes = ['B', 'KB', 'MB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  const safeI = Math.min(i, sizes.length - 1);
  return parseFloat((bytes / Math.pow(k, safeI)).toFixed(dm)) + ' ' + sizes[safeI];
}

export default function Sidebar({
  sessions,
  activeSessionId,
  onSelectSession,
  onDeleteSession,
  onNewChat,
  onClearAll,
  isOpen,
  onClose,
  theme,
  onThemeChange,
  workspaceFiles,
  onOpenFile,
  onDeleteFile,
  onCreateFile,
  mountedPath = '',
  onMountFolder,
  onUnmountFolder,
  hasPermission = true,
  onRequestPermission,
}: SidebarProps) {
  const { t } = useT();
  const [historyExpanded, setHistoryExpanded] = useState(true);
  const [filesExpanded, setFilesExpanded] = useState(true);

  return (
    <>
      {/* Mobile backdrop overlay */}
      {isOpen && <div className={styles.backdrop} onClick={onClose} />}

      <aside className={`${styles.sidebar} ${isOpen ? styles.open : ''}`}>
        <div className={styles.header}>
          <div className={styles.logo}>
            <Sparkle size={18} weight="fill" className={styles.logoIcon} />
            <span className={styles.logoText}>AI Studio Warung Lakku</span>
          </div>
        </div>

        <button type="button" className={styles.newChatBtn} onClick={onNewChat}>
          <Plus size={16} className={styles.plusIcon} />
          {t('repl.session.newChat')}
        </button>

        {/* Chat History Section */}
        <div className={styles.sectionHeader} onClick={() => setHistoryExpanded(!historyExpanded)}>
          <div className={styles.sectionHeaderTitle}>
            {historyExpanded ? <CaretDown size={12} /> : <CaretRight size={12} />}
            <span>{t('repl.session.history' as any) || 'Chat History'}</span>
          </div>
        </div>

        {historyExpanded && (
          <div className={styles.listContainer}>
            {sessions.length === 0 ? (
              <div className={styles.emptyList}>No history yet</div>
            ) : (
              <ul className={styles.list}>
                {sessions.map(s => (
                  <li
                    key={s.id}
                    className={`${styles.item} ${s.id === activeSessionId ? styles.active : ''}`}
                    onClick={() => {
                      onSelectSession(s.id);
                      onClose(); // Auto close on mobile
                    }}
                  >
                    <ChatTeardropText size={16} className={styles.chatIcon} />
                    <span className={styles.itemTitle} title={s.title}>
                      {s.title || 'Untitled Chat'}
                    </span>
                    <button
                      type="button"
                      className={styles.deleteBtn}
                      onClick={e => {
                        e.stopPropagation(); // Avoid selecting the session when deleting
                        if (confirm(t('repl.session.confirmDelete' as any) || 'Delete this session?')) {
                          onDeleteSession(s.id);
                        }
                      }}
                      title="Delete Chat"
                      aria-label="Delete Chat"
                    >
                      <Trash size={14} />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        <div className={styles.sectionSeparator} />

        {/* Workspace Files Section */}
        <div className={styles.sectionHeader}>
          <div className={styles.sectionHeaderTitle} onClick={() => setFilesExpanded(!filesExpanded)}>
            {filesExpanded ? <CaretDown size={12} /> : <CaretRight size={12} />}
            <FolderOpen size={14} className={styles.folderIcon} />
            <span>Workspace Files</span>
          </div>
          <button
            type="button"
            className={styles.addFileBtn}
            onClick={onCreateFile}
            title="Create New File"
            aria-label="Create New File"
          >
            <Plus size={14} />
          </button>
        </div>

        {filesExpanded && (
          <div className={styles.mountPanel}>
            {mountedPath ? (
              hasPermission ? (
                <div className={styles.mountedInfo}>
                  <span className={styles.mountPath} title={mountedPath}>
                    📁 {mountedPath.split('/').pop() || 'Mounted Directory'}
                  </span>
                  <button 
                    type="button" 
                    className={styles.unmountBtn} 
                    onClick={onUnmountFolder}
                    title="Disconnect Local Folder"
                  >
                    Disconnect
                  </button>
                </div>
              ) : (
                <div className={styles.mountedInfo}>
                  <button 
                    type="button" 
                    className={styles.authorizeBtn} 
                    onClick={onRequestPermission}
                    title="Grant Permission to read/write local folder"
                  >
                    🔑 Authorize Access
                  </button>
                  <button 
                    type="button" 
                    className={styles.unmountBtn} 
                    onClick={onUnmountFolder}
                    title="Disconnect Local Folder"
                  >
                    Disconnect
                  </button>
                </div>
              )
            ) : (
              <button 
                type="button" 
                className={styles.mountBtn} 
                onClick={onMountFolder}
              >
                📁 Mount Local Folder
              </button>
            )}
          </div>
        )}

        {filesExpanded && (
          <div className={styles.listContainer}>
            {workspaceFiles.length === 0 ? (
              <div className={styles.emptyList}>No files in workspace</div>
            ) : (
              <ul className={styles.list}>
                {workspaceFiles.map(f => (
                  <li
                    key={f.name}
                    className={styles.item}
                    onClick={() => onOpenFile(f.name)}
                  >
                    <FileText size={16} className={styles.chatIcon} />
                    <span className={styles.itemTitle} title={f.name}>
                      {f.name}
                    </span>
                    <span className={styles.fileSize}>
                      {formatBytes(f.size)}
                    </span>
                    <button
                      type="button"
                      className={styles.deleteBtn}
                      onClick={e => {
                        e.stopPropagation();
                        if (confirm(`Delete file ${f.name}?`)) {
                          onDeleteFile(f.name);
                        }
                      }}
                      title="Delete File"
                      aria-label="Delete File"
                    >
                      <Trash size={14} />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        <div className={styles.footer}>
          {sessions.length > 0 && (
            <button type="button" className={styles.clearAllBtn} onClick={onClearAll}>
              <Trash size={16} className={styles.trashIcon} />
              {t('repl.session.clearAll')}
            </button>
          )}
          <div className={styles.themeSwitchContainer}>
            <button
              type="button"
              className={`${styles.themeBtn} ${theme === 'light' ? styles.themeBtnActive : ''}`}
              onClick={() => onThemeChange('light')}
              title={t('repl.theme.light')}
              aria-label={t('repl.theme.light')}
            >
              <Sun size={14} />
              <span className={styles.themeLabel}>{t('repl.theme.light')}</span>
            </button>
            <button
              type="button"
              className={`${styles.themeBtn} ${theme === 'dark' ? styles.themeBtnActive : ''}`}
              onClick={() => onThemeChange('dark')}
              title={t('repl.theme.dark')}
              aria-label={t('repl.theme.dark')}
            >
              <Moon size={14} />
              <span className={styles.themeLabel}>{t('repl.theme.dark')}</span>
            </button>
            <button
              type="button"
              className={`${styles.themeBtn} ${theme === 'system' ? styles.themeBtnActive : ''}`}
              onClick={() => onThemeChange('system')}
              title={t('repl.theme.system')}
              aria-label={t('repl.theme.system')}
            >
              <Monitor size={14} />
              <span className={styles.themeLabel}>{t('repl.theme.system')}</span>
            </button>
          </div>
        </div>
      </aside>
    </>
  );
}
