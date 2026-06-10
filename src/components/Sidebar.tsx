import { useT } from '../i18n';
import { Sparkle, Plus, ChatTeardropText, Trash, Sun, Moon, Monitor } from '@phosphor-icons/react';
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
}: SidebarProps) {
  const { t } = useT();

  return (
    <>
      {/* Mobile backdrop overlay */}
      {isOpen && <div className={styles.backdrop} onClick={onClose} />}

      <aside className={`${styles.sidebar} ${isOpen ? styles.open : ''}`}>
        <div className={styles.header}>
          <div className={styles.logo}>
            <Sparkle size={18} weight="fill" className={styles.logoIcon} />
            <span className={styles.logoText}>Python Starter</span>
          </div>
        </div>

        <button type="button" className={styles.newChatBtn} onClick={onNewChat}>
          <Plus size={16} className={styles.plusIcon} />
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


