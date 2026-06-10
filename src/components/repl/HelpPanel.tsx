import { useEffect, useRef } from 'react';
import { useT } from '../../i18n';
import styles from './HelpPanel.module.css';

interface Props {
  onClose: () => void;
}

export default function HelpPanel({ onClose }: Props) {
  const { t } = useT();
  const panelRef = useRef<HTMLDivElement>(null);

  // Close on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onClose]);

  const shortcuts = [
    { key: 'Enter', label: t('repl.help.send') },
    { key: 'Ctrl+C', label: t('repl.help.abort') },
    { key: 'Ctrl+L', label: t('repl.help.clear') },
    { key: 'Ctrl+T', label: t('repl.help.trace') },
    { key: 'Ctrl+/', label: t('repl.help.toggleHelp') },
  ];

  return (
    <div className={styles.panel} ref={panelRef}>
      <div className={styles.title}>{t('repl.help.title')}</div>
      <ul className={styles.list}>
        {shortcuts.map(s => (
          <li key={s.key} className={styles.item}>
            <kbd className={styles.kbd}>{s.key}</kbd>
            <span className={styles.label}>{s.label}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
