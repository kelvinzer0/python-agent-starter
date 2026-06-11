import { useState, useEffect } from 'react';
import { FileText, X, FloppyDisk } from '@phosphor-icons/react';
import styles from './FileEditorModal.module.css';

interface FileEditorModalProps {
  filename: string;
  initialContent: string;
  loading: boolean;
  saving: boolean;
  onSave: (content: string) => void;
  onClose: () => void;
}

export default function FileEditorModal({
  filename,
  initialContent,
  loading,
  saving,
  onSave,
  onClose,
}: FileEditorModalProps) {
  const [content, setContent] = useState(initialContent);

  useEffect(() => {
    if (!loading) {
      setContent(initialContent);
    }
  }, [initialContent, loading]);

  const handleSave = () => {
    onSave(content);
  };

  const lineCount = content ? content.split('\n').length : 0;
  const charCount = content ? content.length : 0;

  return (
    <div className={styles.overlay} onClick={onClose}>
      <div className={styles.modal} onClick={e => e.stopPropagation()}>
        <div className={styles.header}>
          <div className={styles.titleContainer}>
            <FileText size={20} className={styles.titleIcon} />
            <span className={styles.title}>{filename}</span>
          </div>
          <button 
            type="button" 
            className={styles.closeBtn} 
            onClick={onClose}
            title="Close"
          >
            <X size={18} />
          </button>
        </div>

        <div className={styles.editorContainer}>
          {loading && (
            <div className={styles.loadingOverlay}>
              <div className={styles.spinner} />
              <span>Loading file...</span>
            </div>
          )}
          <textarea
            className={styles.textarea}
            value={content}
            onChange={e => setContent(e.target.value)}
            placeholder="Type your markdown, code, or notes here..."
            disabled={loading || saving}
            spellCheck={false}
          />
        </div>

        <div className={styles.footer}>
          <div className={styles.stats}>
            {lineCount} lines • {charCount} characters
          </div>
          <div className={styles.actions}>
            <button
              type="button"
              className={styles.btnCancel}
              onClick={onClose}
              disabled={saving}
            >
              Cancel
            </button>
            <button
              type="button"
              className={styles.btnSave}
              onClick={handleSave}
              disabled={loading || saving}
            >
              {saving ? (
                <>Saving...</>
              ) : (
                <>
                  <FloppyDisk size={14} style={{ marginRight: '6px', display: 'inline-block', verticalAlign: 'middle' }} />
                  Save Changes
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
