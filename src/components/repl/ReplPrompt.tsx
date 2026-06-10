import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { ChangeEvent, KeyboardEvent } from 'react';
import { useT } from '../../i18n';
import type { ReplAction } from './keymap';
import HelpPanel from './HelpPanel';
import styles from './ReplPrompt.module.css';

interface Props {
  loading: boolean;
  onSubmit: (text: string) => void;
  /** Called by App when Ctrl+C in idle mode requests "clear input". */
  registerClearInput: (clear: () => void) => void;
  inputHistory: string[];
  onAction: (action: ReplAction) => void;
}

export default function ReplPrompt({
  loading,
  onSubmit,
  registerClearInput,
  inputHistory,
  onAction,
}: Props) {
  const { t } = useT();
  const [value, setValue] = useState('');
  const [historyIdx, setHistoryIdx] = useState<number | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const promptRef = useRef<HTMLDivElement>(null);

  // Expose a "clear input" handle to App for Ctrl+C behavior.
  useEffect(() => {
    registerClearInput(() => {
      setValue('');
      setHistoryIdx(null);
    });
  }, [registerClearInput]);

  // Auto-resize textarea up to max-height (CSS clamps further).
  useLayoutEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = 'auto';
    ta.style.height = `${ta.scrollHeight}px`;
  }, [value]);

  // Auto-focus on mount and whenever loading flips false.
  useEffect(() => {
    if (!loading) taRef.current?.focus();
  }, [loading]);

  // Close help panel on outside click
  useEffect(() => {
    if (!helpOpen) return;
    const handler = (e: MouseEvent) => {
      if (promptRef.current && !promptRef.current.contains(e.target as Node)) {
        setHelpOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [helpOpen]);

  function commit() {
    const text = value.trim();
    if (!text || loading) return;
    onSubmit(text);
    setValue('');
    setHistoryIdx(null);
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter without Shift → submit (same as Claude/ChatGPT convention)
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      commit();
      return;
    }

    // Toggle help with Ctrl+/
    if ((e.ctrlKey || e.metaKey) && e.key === '/') {
      e.preventDefault();
      setHelpOpen(prev => !prev);
      return;
    }

    // History navigation only when caret is on first/last line
    if (e.key === 'ArrowUp' && inputHistory.length > 0) {
      const ta = taRef.current;
      if (!ta) return;
      const onFirstLine = ta.selectionStart <= (ta.value.indexOf('\n') === -1 ? ta.value.length : ta.value.indexOf('\n'));
      if (!onFirstLine) return;
      e.preventDefault();
      setHistoryIdx(idx => {
        const nextIdx = idx === null ? inputHistory.length - 1 : Math.max(0, idx - 1);
        setValue(inputHistory[nextIdx] ?? '');
        return nextIdx;
      });
      return;
    }
    if (e.key === 'ArrowDown' && historyIdx !== null) {
      e.preventDefault();
      setHistoryIdx(idx => {
        if (idx === null) return null;
        const nextIdx = idx + 1;
        if (nextIdx >= inputHistory.length) {
          setValue('');
          return null;
        }
        setValue(inputHistory[nextIdx] ?? '');
        return nextIdx;
      });
      return;
    }
  }

  function onChange(e: ChangeEvent<HTMLTextAreaElement>) {
    setValue(e.target.value);
    if (historyIdx !== null) setHistoryIdx(null);
  }

  function handleAction(action: ReplAction) {
    if (action === 'showHelp') {
      setHelpOpen(prev => !prev);
    } else {
      onAction(action);
    }
  }

  return (
    <div className={styles.prompt} ref={promptRef}>
      {helpOpen && (
        <HelpPanel onClose={() => setHelpOpen(false)} />
      )}
      <div className={styles.inputWrap}>
        <span className={styles.statusDot} data-running={loading || undefined} />
        <textarea
          ref={taRef}
          rows={1}
          className={styles.input}
          value={value}
          disabled={loading}
          onChange={onChange}
          onKeyDown={onKeyDown}
          placeholder={t('repl.prompt.placeholder')}
          spellCheck={false}
          autoComplete="off"
          autoCorrect="off"
          autoCapitalize="off"
        />
      </div>
      <div className={styles.toolbar}>
        <button
          type="button"
          className={styles.toolBtn}
          onClick={() => handleAction('abort')}
          title={t('repl.action.abort')}
        >
          <span className={styles.toolBtnIcon}>⨯</span>
          <span className={styles.toolBtnLabel}>{t('repl.action.abort')}</span>
        </button>
        <button
          type="button"
          className={styles.toolBtn}
          onClick={() => handleAction('clearScreen')}
          title={t('repl.action.clear')}
        >
          <span className={styles.toolBtnIcon}>⌫</span>
          <span className={styles.toolBtnLabel}>{t('repl.action.clear')}</span>
        </button>
        <button
          type="button"
          className={styles.toolBtn}
          onClick={() => handleAction('toggleVerbose')}
          title={t('repl.action.trace')}
        >
          <span className={styles.toolBtnIcon}>◈</span>
          <span className={styles.toolBtnLabel}>{t('repl.action.trace')}</span>
        </button>
        <button
          type="button"
          className={styles.toolBtn}
          onClick={() => handleAction('showHelp')}
          title={t('repl.action.help')}
        >
          <span className={styles.toolBtnIcon}>?</span>
          <span className={styles.toolBtnLabel}>{t('repl.action.help')}</span>
        </button>
      </div>
    </div>
  );
}
