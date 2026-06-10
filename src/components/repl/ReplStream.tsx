import type { ReplLine } from '../../types';
import type { RawSseEvent } from '../../api';
import { useT } from '../../i18n';
import ReplLineRow, { ReplRawRow } from './ReplLine';
import PendingCaret from './PendingCaret';
import styles from './ReplStream.module.css';

interface Props {
  lines: ReplLine[];
  traceEvents: RawSseEvent[];
  verbose: boolean;
  /** True iff a turn is in flight AND no agent output has arrived yet. */
  showPending: boolean;
  onOpenImage?: (url: string, alt: string) => void;
  onPresetClick?: (text: string) => void;
}

/**
 * Render the REPL scroll content. Switches between:
 *   - normal mode: pretty `ReplLine[]` (+ optional pending caret tail)
 *   - verbose mode: raw SSE event log (one row per event)
 *   - empty state: beautiful welcome card with clickable presets
 */
export default function ReplStream({
  lines,
  traceEvents,
  verbose,
  showPending,
  onOpenImage,
  onPresetClick,
}: Props) {
  const { t } = useT();

  if (verbose) {
    return (
      <div className={styles.stream}>
        {traceEvents.map((ev, i) => (
          <ReplRawRow key={`raw-${i}-${ev.timestamp}`} ev={ev} />
        ))}
      </div>
    );
  }

  if (lines.length === 0) {
    return (
      <div className={styles.emptyState}>
        <div className={styles.emptyIcon}>✦</div>
        <h1 className={styles.emptyTitle}>{t('empty.title')}</h1>
        <p className={styles.emptyHint}>{t('empty.hint')}</p>
        <div className={styles.emptyFeatures}>
          {t('empty.features').split('·').map((feat, i) => (
            <span key={i} className={styles.featureBadge}>
              {feat.trim()}
            </span>
          ))}
        </div>
        
        <div className={styles.presetsGrid}>
          {[1, 2, 3, 4].map(num => (
            <button
              key={num}
              type="button"
              className={styles.presetCard}
              onClick={() => onPresetClick?.(t(`preset.${num}` as any))}
            >
              <span className={styles.presetText}>{t(`preset.${num}` as any)}</span>
              <span className={styles.presetArrow}>➔</span>
            </button>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className={styles.stream}>
      {lines.map((line, index) => {
        const isAssistantLine = ['text', 'markdown', 'tool', 'image', 'done', 'error'].includes(line.kind);
        let isFirstInTurn = false;
        if (isAssistantLine && 'turnId' in line) {
          const prevLine = index > 0 ? lines[index - 1] : null;
          const prevTurnId = prevLine && 'turnId' in prevLine ? prevLine.turnId : null;
          isFirstInTurn = line.turnId !== prevTurnId;
        }

        return (
          <ReplLineRow
            key={line.id}
            line={line}
            onOpenImage={onOpenImage}
            isFirstInTurn={isFirstInTurn}
          />
        );
      })}
      {showPending && <PendingCaret />}
    </div>
  );
}
