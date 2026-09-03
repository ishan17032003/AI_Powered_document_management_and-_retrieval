import { FormEvent, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { AnimatePresence, motion } from "motion/react";
import {
  ArrowRight,
  Check,
  FileArchive,
  FileText,
  Film,
  FolderInput,
  Image as ImageIcon,
  Music,
  UploadCloud,
} from "lucide-react";
import { api, ImportResult, IngestionStatus, UploadResult } from "../api";
import { useAuth } from "../auth";
import { PageHeader, SectionCard, StatusPill, StatusTone } from "../components/ui";

function getFileIcon(filename: string) {
  const ext = filename.toLowerCase().slice(filename.lastIndexOf("."));
  if ([".mp4", ".avi", ".mov", ".mkv", ".webm"].includes(ext)) {
    return <Film size={17} />;
  }
  if ([".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"].includes(ext)) {
    return <Music size={17} />;
  }
  if ([".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp"].includes(ext)) {
    return <ImageIcon size={17} />;
  }
  if ([".zip", ".tar", ".gz", ".rar", ".7z"].includes(ext)) {
    return <FileArchive size={17} />;
  }
  return <FileText size={17} />;
}

interface UploadRow {
  id: string;
  name: string;
  result?: UploadResult;
  ingestion?: IngestionStatus;
  error?: string;
}

function resultTone(value: string): StatusTone {
  const normalized = value.toLowerCase();
  if (["ready", "native", "imported", "unique", "completed", "succeeded", "done"].includes(normalized)) return "success";
  if (["review", "duplicate"].includes(normalized)) return "warning";
  if (["error", "failed", "cancelled", "unavailable"].includes(normalized)) return "danger";
  return "info";
}

function FolderImport() {
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [error, setError] = useState("");

  async function run(event: FormEvent) {
    event.preventDefault();
    if (!path.trim() || busy) return;
    setBusy(true);
    setError("");
    setResult(null);
    try {
      setResult(await api.importFolder(path.trim()));
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <SectionCard className="folder-import-card">
      <span className="capture-card-icon"><FolderInput size={22} /></span>
      <div className="capture-card-heading">
        <span className="section-kicker">Connected folders</span>
        <h2>Import a folder</h2>
        <p>Process a local or Google Drive–synced folder through the same capture workflow.</p>
      </div>

      <form className="folder-form" onSubmit={run}>
        <label className="field">
          <span>Folder path</span>
          <input
            value={path}
            onChange={(event) => setPath(event.target.value)}
            placeholder="/Users/you/Documents or ~/Google Drive/Docs"
            disabled={busy}
          />
        </label>
        <button className="button primary" disabled={busy || !path.trim()}>
          {busy ? "Importing…" : "Import folder"}
          {!busy && <ArrowRight size={17} />}
        </button>
      </form>

      {error && <div className="notice is-danger">{error}</div>}

      {result && (
        <motion.div className="import-summary" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
          <div className="summary-pills">
            <StatusPill tone="success">{result.imported} imported</StatusPill>
            <StatusPill tone="warning">{result.duplicates} duplicates</StatusPill>
            <StatusPill>{result.skipped} skipped</StatusPill>
            {result.errors > 0 && <StatusPill tone="danger">{result.errors} errors</StatusPill>}
          </div>
          <div className="import-items">
            {result.items.slice(0, 5).map((item, index) => (
              <div key={`${item.filename}-${index}`}>
                <span>
                  <FileText size={15} />
                  {item.document_id ? (
                    <Link to={`/documents/${item.document_id}`}>{item.filename}</Link>
                  ) : (
                    item.filename
                  )}
                </span>
                <StatusPill tone={resultTone(item.status)}>{item.status}</StatusPill>
              </div>
            ))}
          </div>
        </motion.div>
      )}
    </SectionCard>
  );
}

export default function Upload() {
  const { can } = useAuth();
  const [rows, setRows] = useState<UploadRow[]>([]);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [jobAction, setJobAction] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const mountedRef = useRef(true);

  useEffect(() => () => {
    mountedRef.current = false;
  }, []);

  async function watchIngestion(rowId: string, jobId: string) {
    for (let attempt = 0; attempt < 200 && mountedRef.current; attempt += 1) {
      try {
        const status = await api.ingestionStatus(jobId);
        if (!mountedRef.current) return;
        setRows((current) => current.map((row) => row.id === rowId ? { ...row, ingestion: status } : row));
        if (status.terminal) return;
      } catch {
        return;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1500));
    }
  }

  async function updateJob(row: UploadRow, action: "retry" | "cancel") {
    const jobId = row.result?.job_id;
    if (!jobId || jobAction) return;
    setJobAction(`${row.id}:${action}`);
    try {
      const status = action === "retry" ? await api.retryIngestion(jobId) : await api.cancelIngestion(jobId);
      setRows((current) => current.map((item) => item.id === row.id ? { ...item, ingestion: status } : item));
      if (!status.terminal) void watchIngestion(row.id, jobId);
    } catch (err: any) {
      setRows((current) => current.map((item) => item.id === row.id ? { ...item, error: err.message || `Could not ${action} this job.` } : item));
    } finally {
      setJobAction(null);
    }
  }

  async function handleFiles(files: FileList | null) {
    if (!files?.length) return;
    setBusy(true);
    for (const file of Array.from(files)) {
      const id = `${file.name}-${file.lastModified}-${Math.random().toString(16).slice(2)}`;
      try {
        const result = await api.upload(file);
        setRows((current) => [{ id, name: file.name, result }, ...current]);
        if (result.job_id) void watchIngestion(id, result.job_id);
      } catch (err: any) {
        setRows((current) => [
          { id, name: file.name, error: err.message || "This file could not be processed." },
          ...current,
        ]);
      }
    }
    setBusy(false);
  }

  return (
    <div>
      <PageHeader
        eyebrow="Capture workspace"
        title="Add documents"
        description="Every file is stored, read, classified, checked for duplicates, and indexed."
      />

      <div className="capture-grid">
        <SectionCard className="file-capture-card">
          <div
            className={`dropzone ${dragging ? "is-dragging" : ""}`}
            onDragOver={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              handleFiles(event.dataTransfer.files);
            }}
            onClick={() => inputRef.current?.click()}
            role="button"
            tabIndex={0}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") inputRef.current?.click();
            }}
          >
            <motion.span
              className="dropzone-icon"
              animate={dragging ? { y: -5 } : { y: 0 }}
              transition={{ type: "spring", stiffness: 430, damping: 27 }}
            >
              <UploadCloud size={28} />
            </motion.span>
            <span className="section-kicker">Direct upload</span>
            <h2>{busy ? "Processing files…" : "Drop files into the archive"}</h2>
            <p>Drag files here or choose them from your computer.</p>
            <button
              type="button"
              className="button secondary"
              onClick={(event) => {
                event.stopPropagation();
                inputRef.current?.click();
              }}
              disabled={busy}
            >
              Choose files
            </button>
            <small>PDF, Audio (WAV/MP3/M4A/FLAC), Video (MP4/MOV/AVI), Images, Office, Text, CSV, Markdown, and more</small>
            <input
              ref={inputRef}
              type="file"
              multiple
              hidden
              onChange={(event) => handleFiles(event.target.files)}
            />
          </div>
        </SectionCard>

        <FolderImport />
      </div>

      {rows.length > 0 && (
        <SectionCard className="upload-results-card">
          <div className="section-heading">
            <div>
              <span className="section-kicker">This session</span>
              <h2>Capture results</h2>
            </div>
            <StatusPill tone={busy ? "info" : "success"}>
              {busy ? "Processing" : <><Check size={13} /> Complete</>}
            </StatusPill>
          </div>

          <div className="table-scroll">
            <table className="data-table responsive-table">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Status</th>
                  <th>Pipeline</th>
                  <th>Capture</th>
                  <th>Class</th>
                  <th>Duplicate check</th>
                  <th><span className="sr-only">Open</span></th>
                </tr>
              </thead>
              <tbody>
                <AnimatePresence initial={false}>
                  {rows.map((row) => {
                    const result = row.result;
                    return (
                    <motion.tr
                      key={row.id}
                      layout
                      initial={{ opacity: 0, y: -6 }}
                      animate={{ opacity: 1, y: 0 }}
                    >
                      <td data-label="File">
                        <span className="document-link">
                          <span className="file-symbol">{getFileIcon(row.name)}</span>
                          <span>{row.name}</span>
                        </span>
                      </td>
                      {row.error || !result ? (
                        <td data-label="Result" colSpan={6}>
                          <span className="inline-error">{row.error}</span>
                        </td>
                      ) : (
                        <>
                          <td data-label="Status">
                            <StatusPill tone={resultTone(row.ingestion?.state || result.status)}>{row.ingestion?.state || result.status}</StatusPill>
                          </td>
                          <td data-label="Pipeline">
                            {row.ingestion ? (
                              <span className="pipeline-status">
                                <StatusPill tone={row.ingestion.terminal ? resultTone(row.ingestion.state) : "info"}>{row.ingestion.stage}</StatusPill>
                                <small>attempt {row.ingestion.attempt_count}</small>
                              </span>
                            ) : (
                              <span className="muted-label">Starting…</span>
                            )}
                          </td>
                          <td data-label="Capture">
                            <StatusPill tone={resultTone(result.ocr_status)}>{result.ocr_status}</StatusPill>
                          </td>
                          <td data-label="Class">{result.doc_class || "Uncategorized"}</td>
                          <td data-label="Duplicate check">
                            {result.duplicate_of ? (
                              <StatusPill tone="warning">Matches #{result.duplicate_of}</StatusPill>
                            ) : (
                              <StatusPill tone="success">Unique</StatusPill>
                            )}
                          </td>
                          <td data-label="Open" className="row-actions">
                            {can("ADMIN") && row.ingestion?.retryable && result.job_id && (
                              <button className="text-button compact" onClick={() => updateJob(row, "retry")} disabled={!!jobAction}>
                                {jobAction === `${row.id}:retry` ? "Retrying…" : "Retry"}
                              </button>
                            )}
                            {can("ADMIN") && row.ingestion?.cancellable && result.job_id && (
                              <button className="text-button compact is-danger" onClick={() => updateJob(row, "cancel")} disabled={!!jobAction}>
                                {jobAction === `${row.id}:cancel` ? "Cancelling…" : "Cancel"}
                              </button>
                            )}
                            <Link className="icon-button" to={`/documents/${result.id}`} aria-label={`Open ${row.name}`}>
                              <ArrowRight size={17} />
                            </Link>
                          </td>
                        </>
                      )}
                    </motion.tr>
                    );
                  })}
                </AnimatePresence>
              </tbody>
            </table>
          </div>
        </SectionCard>
      )}
    </div>
  );
}
