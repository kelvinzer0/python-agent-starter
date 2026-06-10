import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { ChangeEvent, KeyboardEvent } from 'react';
import { PaperPlaneRight, Stop, Trash, TerminalWindow, Question } from '@phosphor-icons/react';
import { useT } from '../../i18n';
import type { ReplAction } from './keymap';
import HelpPanel from './HelpPanel';
import styles from './ReplPrompt.module.css';

interface Props {
  loading: boolean;
  onSubmit: (text: string) => void;
  /** Called by App when Ctrl+C in idle mode requests "clear input". */
  registerClearInput: (clear: () => void) => void;
  /** Called by App to expose input value setter. */
  registerSetPromptValue?: (setter: (text: string) => void) => void;
  inputHistory: string[];
  onAction: (action: ReplAction) => void;
}

export default function ReplPrompt({
  loading,
  onSubmit,
  registerClearInput,
  registerSetPromptValue,
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

  // Expose a "set prompt value" handle to App.
  useEffect(() => {
    registerSetPromptValue?.((text: string) => {
      setValue(text);
      if (taRef.current) {
        taRef.current.focus();
        taRef.current.style.height = 'auto';
        taRef.current.style.height = `${taRef.current.scrollHeight}px`;
      }
    });
  }, [registerSetPromptValue]);

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
      <div className={styles.inputCard}>
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
        <div className={styles.actionsRow}>
          <div className={styles.leftActions}>
            <span className={styles.statusDot} data-running={loading || undefined} />
            <div className={styles.toolbar}>
              <button
                type="button"
                className={styles.toolBtn}
                onClick={() => handleAction('abort')}
                title={t('repl.action.abort')}
                aria-label={t('repl.action.abort')}
              >
                <Stop size={14} />
              </button>
              <button
                type="button"
                className={styles.toolBtn}
                onClick={() => handleAction('clearScreen')}
                title={t('repl.action.clear')}
                aria-label={t('repl.action.clear')}
              >
                <Trash size={14} />
              </button>
              <button
                type="button"
                className={styles.toolBtn}
                onClick={() => handleAction('toggleVerbose')}
                title={t('repl.action.trace')}
                aria-label={t('repl.action.trace')}
              >
                <TerminalWindow size={14} />
              </button>
              <button
                type="button"
                className={styles.toolBtn}
                onClick={() => handleAction('showHelp')}
                title={t('repl.action.help')}
                aria-label={t('repl.action.help')}
              >
                <Question size={14} />
              </button>
            </div>
          </div>
          <button
            type="button"
            className={styles.sendBtn}
            onClick={commit}
            disabled={loading || !value.trim()}
            title={t('repl.help.send')}
            aria-label={t('repl.help.send')}
          >
            <PaperPlaneRight size={14} weight="fill" className={styles.sendIcon} />
          </button>
        </div>
      </div>
    </div>
  );
}

