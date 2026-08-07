import { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import {
  AlertTriangle,
  FileText,
  Loader2,
  RotateCcw,
  Search,
  Trash2,
  XCircle,
} from "lucide-react";
import { api, DocSummary } from "../api";
import { useAuth } from "../auth";
import {
  ConfirmDialog,
  EmptyState,
  PageHeader,
  SectionCard,
  StatusPill,
  StatusTone,
} from "../components/ui";

function statusTone(value: string): StatusTone {
  const normalized = value.toLowerCase();
  if (normalized === "ready" || normalized === "native") return "success";
  if (normalized === "review") return "warning";
  if (normalized === "error" || normalized === "tombstoned" || normalized === "unavailable") return "danger";
  return "info";
}

export default function Trash() {
  const { can } = useAuth();
  const [documents, setDocuments] = useState<DocSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [pendingPurge, setPendingPurge] = useState<DocSummary | null>(null);
  const [pendingEmpty, setPendingEmpty] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [restoringId, setRestoringId] = useState<number | null>(null);
  const [toast, setToast] = useState<{ message: string; type: "success" | "error" } | null>(null);

  useEffect(() => {
    loadTrash();
  }, []);

  async function loadTrash() {
    setLoading(true);
    setError(null);
    try {
      const data = await api.listTrash();
      setDocuments(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load trash bin.");
    } finally {
      setLoading(false);
    }
  }

  function showToast(message: string, type: "success" | "error" = "success") {
    setToast({ message, type });
    setTimeout(() => setToast(null), 3000);
  }

  async function handleRestore(document: DocSummary) {
    setRestoringId(document.id);
    try {
      await api.restoreDocument(document.id);
      setDocuments((prev) => prev.filter((d) => d.id !== document.id));
      showToast(`"${document.title}" restored to active repository.`);
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to restore document.", "error");
    } finally {
      setRestoringId(null);
    }
  }

  async function confirmPurge() {
    if (!pendingPurge) return;
    setActionBusy(true);
    try {
      await api.purgeDocument(pendingPurge.id);
      setDocuments((prev) => prev.filter((d) => d.id !== pendingPurge.id));
      showToast(`"${pendingPurge.title}" permanently deleted from storage.`);
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to purge document.", "error");
    } finally {
      setActionBusy(false);
      setPendingPurge(null);
    }
  }

  async function confirmEmptyTrash() {
    setActionBusy(true);
    try {
      await api.emptyTrash();
      setDocuments([]);
      showToast("Trash bin emptied. All deleted documents purged permanently.");
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to empty trash.", "error");
    } finally {
      setActionBusy(false);
      setPendingEmpty(false);
    }
  }

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return documents;
    return documents.filter(
      (d) =>
        d.title.toLowerCase().includes(q) ||
        (d.doc_class && d.doc_class.toLowerCase().includes(q))
    );
  }, [documents, query]);

  return (
    <div className="page-stage">
      <PageHeader
        eyebrow="Tombstone & Recycle Bin"
        title="Trash Bin"
        description="Documents in trash are safely tombstoned and excluded from Search, Vectors, and Ask AI. You can restore them or permanently delete them from physical storage."
      />

      {toast && (
        <div
          style={{
            marginBottom: "16px",
            padding: "10px 16px",
            borderRadius: "8px",
            fontSize: "0.88rem",
            fontWeight: 500,
            background: toast.type === "success" ? "rgba(16, 185, 129, 0.12)" : "rgba(239, 68, 68, 0.12)",
            color: toast.type === "success" ? "#10b981" : "#ef4444",
            border: `1px solid ${toast.type === "success" ? "rgba(16, 185, 129, 0.3)" : "rgba(239, 68, 68, 0.3)"}`,
          }}
        >
          {toast.message}
        </div>
      )}

      <SectionCard>
        <div className="collection-toolbar" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "16px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "16px", flex: 1, maxWidth: "440px" }}>
            <label className="search-field" style={{ flex: 1 }}>
              <Search size={18} aria-hidden="true" />
              <span className="sr-only">Filter trash</span>
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Filter trash by title or class..."
              />
            </label>
            <span className="result-count" style={{ whiteSpace: "nowrap" }}>{shown.length} in trash</span>
          </div>

          {documents.length > 0 && can("DELETE") && (
            <button
              className="button danger"
              onClick={() => setPendingEmpty(true)}
              style={{ display: "inline-flex", alignItems: "center", gap: "6px", marginLeft: "auto" }}
            >
              <Trash2 size={16} />
              <span>Empty Trash</span>
            </button>
          )}
        </div>

        {loading ? (
          <div style={{ padding: "40px 0", textAlign: "center" }}>
            <Loader2 className="spin" size={24} style={{ color: "var(--accent)" }} />
            <p style={{ marginTop: "8px", color: "var(--muted)", fontSize: "0.9rem" }}>Loading deleted items…</p>
          </div>
        ) : error ? (
          <div style={{ padding: "24px", color: "var(--danger)" }}>
            <AlertTriangle size={20} style={{ verticalAlign: "middle", marginRight: "8px" }} />
            {error}
          </div>
        ) : shown.length ? (
          <div className="table-scroll">
            <table className="data-table responsive-table">
              <thead>
                <tr>
                  <th>Document</th>
                  <th>Class</th>
                  <th>Status</th>
                  <th>Pages</th>
                  <th>Deleted At</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                <AnimatePresence initial={false}>
                  {shown.map((document) => (
                    <motion.tr
                      key={document.id}
                      layout
                      initial={{ opacity: 0 }}
                      animate={{ opacity: 1 }}
                      exit={{ opacity: 0, x: -12 }}
                    >
                      <td data-label="Document">
                        <div className="document-link" style={{ cursor: "default" }}>
                          <span className="file-symbol"><FileText size={17} /></span>
                          <span>{document.title}</span>
                        </div>
                      </td>
                      <td data-label="Class">
                        <span>{document.doc_class || "Uncategorized"}</span>
                      </td>
                      <td data-label="Status">
                        <StatusPill tone={statusTone(document.status)}>TOMBSTONED</StatusPill>
                      </td>
                      <td data-label="Pages" className="mono-cell">{document.page_count}</td>
                      <td data-label="Updated" className="table-date">
                        {new Date(document.updated_at ?? document.created_at).toLocaleString([], {
                          month: "short",
                          day: "numeric",
                          hour: "numeric",
                          minute: "2-digit",
                        })}
                      </td>
                      <td data-label="Actions" className="row-actions">
                        <div style={{ display: "flex", gap: "8px" }}>
                          {can("DELETE") && (
                            <button
                              className="button secondary compact"
                              onClick={() => handleRestore(document)}
                              disabled={restoringId === document.id}
                              title="Restore document to active repository"
                              style={{ display: "inline-flex", alignItems: "center", gap: "4px", padding: "4px 8px", fontSize: "0.8rem" }}
                            >
                              {restoringId === document.id ? (
                                <Loader2 size={14} className="spin" />
                              ) : (
                                <RotateCcw size={14} />
                              )}
                              <span>Restore</span>
                            </button>
                          )}
                          {can("DELETE") && (
                            <button
                              className="button danger compact"
                              onClick={() => setPendingPurge(document)}
                              title="Permanently delete from physical storage"
                              style={{ display: "inline-flex", alignItems: "center", gap: "4px", padding: "4px 8px", fontSize: "0.8rem" }}
                            >
                              <XCircle size={14} />
                              <span>Purge</span>
                            </button>
                          )}
                        </div>
                      </td>
                    </motion.tr>
                  ))}
                </AnimatePresence>
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            icon={Trash2}
            title={query ? "No matching deleted documents" : "Trash bin is empty"}
            description={
              query
                ? "Try a broader search term."
                : "Documents you delete from the repository will appear here before permanent deletion."
            }
            action={
              query ? (
                <button className="button secondary" onClick={() => setQuery("")}>Clear filter</button>
              ) : undefined
            }
          />
        )}
      </SectionCard>

      <ConfirmDialog
        open={!!pendingPurge}
        title="Permanently Delete Document?"
        description={
          pendingPurge
            ? `"${pendingPurge.title}" and its physical files will be permanently deleted from storage. This CANNOT be undone.`
            : ""
        }
        confirmLabel="Permanently Delete"
        onConfirm={confirmPurge}
        onCancel={() => setPendingPurge(null)}
        busy={actionBusy}
      />

      <ConfirmDialog
        open={pendingEmpty}
        title="Empty Trash Bin?"
        description="All documents in the trash bin will be permanently purged from disk and database. This action CANNOT be undone."
        confirmLabel="Empty Trash"
        onConfirm={confirmEmptyTrash}
        onCancel={() => setPendingEmpty(false)}
        busy={actionBusy}
      />
    </div>
  );
}
