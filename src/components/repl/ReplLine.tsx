import { useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { ReplLine } from '../../types';
import type { RawSseEvent } from '../../api';
import { useT } from '../../i18n';
import styles from './ReplLine.module.css';

function formatTime(ts: number): string {
  const d = new Date(ts);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  const ms = String(d.getMilliseconds()).padStart(3, '0');
  return `${hh}:${mm}:${ss}.${ms}`;
}

function formatJson(data: unknown): string {
  if (data === null || data === undefined) return '';
  if (typeof data === 'string') return data;
  try {
    return JSON.stringify(data, null, 2);
  } catch {
    return String(data);
  }
}

function tplFill(s: string, vars: Record<string, string | number>): string {
  return s.replace(/\{(\w+)\}/g, (_, k) => String(vars[k] ?? `{${k}}`));
}

function formatBytes(bytes: number): string {
  if (!bytes || bytes < 0) return '–';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

interface Props {
  line: ReplLine;
  onOpenImage?: (url: string, alt: string) => void;
}

/** Collapsible tool call sub-component (needs its own useState for expanded). */
function ToolCallRow({ line }: { line: Extract<ReplLine, { kind: 'tool' }> }) {
  const { t } = useT();
  const [expanded, setExpanded] = useState(false);
  const status = line.status ?? 'running';
  const statusIcon = status === 'success' ? '✓' : status === 'error' ? '✗' : '◎';
  const statusCls = status === 'success'
    ? styles['toolStatus--success']
    : status === 'error'
      ? styles['toolStatus--error']
      : styles['toolStatus--running'];
  return (
    <div className={`${styles.line} ${styles.tool} ${styles.toolPanel}`}>
      <div
        className={styles.toolHeader}
        onClick={() => setExpanded(prev => !prev)}
        role="button"
        tabIndex={0}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') setExpanded(prev => !prev); }}
      >
        <span className={`${styles.toolStatus} ${statusCls}`}>{statusIcon}</span>
        <span className={styles.toolName}>{line.tool}</span>
        {line.argsPreview && !expanded && <span className={styles.toolArgs}>{line.argsPreview}</span>}
        {typeof line.durationMs === 'number' && (
          <span className={styles.toolMeta}>· {line.durationMs}ms</span>
        )}
        <span className={`${styles.chevron} ${expanded ? styles.chevronOpen : ''}`}>▸</span>
      </div>
      {expanded && (
        <div className={styles.toolBody}>
          {line.inputArgs && (
            <div className={styles.toolSection}>
              <div className={styles.toolSectionLabel}>{t('repl.tool.inputArgs')}</div>
              <pre className={styles.toolCode}>{line.inputArgs}</pre>
            </div>
          )}
          {line.outputResult && (
            <div className={styles.toolSection}>
              <div className={styles.toolSectionLabel}>{t('repl.tool.outputResult')}</div>
              <pre className={styles.toolCode}>{line.outputResult}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function ReplLineRow({ line, onOpenImage }: Props) {
  const { t } = useT();

  switch (line.kind) {
    case 'motd':
      // motd is no longer rendered as a chat line; action buttons replace it
      return null;

    case 'user':
      return (
        <div className={`${styles.line} ${styles.user}`}>
          <span className={styles.userPrompt}>{t('repl.prompt.userLabel')}</span>
          {line.text}
        </div>
      );

    case 'text':
      return (
        <div className={`${styles.line} ${styles.text}`}>
          {!line.isContinuation && (
            <span className={styles.agentPrompt}>{t('repl.prompt.agentLabel')}</span>
          )}
          {line.text}
        </div>
      );

    case 'markdown':
      return (
        <div className={`${styles.line} ${styles.text} ${styles.markdown}`}>
          {!line.isContinuation && (
            <span className={styles.agentPrompt}>{t('repl.prompt.agentLabel')}</span>
          )}
          <span className={styles.markdownInner}>
            <Markdown remarkPlugins={[remarkGfm]}>{line.text}</Markdown>
          </span>
        </div>
      );

    case 'tool':
      return <ToolCallRow line={line} />;

    case 'image': {
      const { image, toolName } = line;
      const altText = `${toolName ?? 'tool'} output (${formatBytes(image.size)})`;
      return (
        <div className={`${styles.line} ${styles.image}`}>
          <span className={styles.imageTs}>[{formatTime(line.ts)}]</span>
          <span className={styles.imageGlyph} aria-hidden>📷</span>
          {toolName && <span className={styles.imageTool}>{toolName}</span>}
          <button
            type="button"
            className={styles.imageBtn}
            onClick={() => onOpenImage?.(image.url, altText)}
            aria-label={t('repl.image.open')}
            title={t('repl.image.open')}
          >
            <img
              src={image.url}
              alt=""
              className={styles.imageThumb}
              loading="lazy"
              draggable={false}
            />
          </button>
          <span className={styles.imageMeta}>{formatBytes(image.size)}</span>
        </div>
      );
    }

    case 'done':
      return (
        <div className={`${styles.line} ${styles.done}`}>
          {tplFill(t('repl.done.summary'), {
            elapsed: (line.elapsedMs / 1000).toFixed(1),
            rounds: line.toolRounds,
          })}
        </div>
      );

    case 'error':
      return (
        <div className={`${styles.line} ${styles.error}`}>
          <span className={styles.errorPrefix}>agent ✘</span>
          {line.message}
        </div>
      );

    case 'restored':
      return (
        <div className={`${styles.line} ${styles.restored}`}>
          [{t('repl.status.restored')} · {line.count}]
        </div>
      );

    case 'sysHint': {
      // sysHint is no longer rendered as a visible line
      return null;
    }

    default: {
      // Exhaustiveness: TS would complain if we missed a kind
      const _exhaustive: never = line;
      void _exhaustive;
      return null;
    }
  }
}

interface RawProps {
  ev: RawSseEvent;
}

/** Verbose-mode renderer for a single raw SSE event. */
export function ReplRawRow({ ev }: RawProps) {
  return (
    <div className={`${styles.line} ${styles.raw}`}>
      <span className={styles.rawTs}>[{formatTime(ev.timestamp)}]</span>
      <span className={styles.rawType}>&gt;&gt;&gt; {ev.eventType}</span>
      <pre className={styles.rawData}>{formatJson(ev.data)}</pre>
    </div>
  );
}
