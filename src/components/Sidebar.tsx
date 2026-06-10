import { useT } from '../i18n';
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
}: SidebarProps) {
  const { t } = useT();

  return (
    <>
      {/* Mobile backdrop overlay */}
      {isOpen && <div className={styles.backdrop} onClick={onClose} />}

      <aside className={`${styles.sidebar} ${isOpen ? styles.open : ''}`}>
        <div className={styles.header}>
          <div className={styles.logo}>
            <span className={styles.logoIcon}>✦</span>
            <span className={styles.logoText}>Python Starter</span>
          </div>
        </div>

        <button type="button" className={styles.newChatBtn} onClick={onNewChat}>
          <span className={styles.plusIcon}>+</span>
          {t('repl.session.newChat')}
        </button>

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
                  <span className={styles.chatIcon}>💬</span>
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
                    <svg
                      viewBox="0 0 24 24"
                      width="14"
                      height="14"
                      stroke="currentColor"
                      strokeWidth="2"
                      fill="none"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <polyline points="3 6 5 6 21 6"></polyline>
                      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                    </svg>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {sessions.length > 0 && (
          <div className={styles.footer}>
            <button type="button" className={styles.clearAllBtn} onClick={onClearAll}>
              <span className={styles.trashIcon}>🗑</span>
              {t('repl.session.clearAll')}
            </button>
          </div>
        )}
      </aside>
    </>
  );
}
