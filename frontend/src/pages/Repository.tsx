import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { AnimatePresence, motion } from "motion/react";
import { FilePlus2, FileText, FolderArchive, Search, Trash2 } from "lucide-react";
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
  if (normalized === "error" || normalized === "unavailable") return "danger";
  return "info";
}

export default function Repository() {
  const [documents, setDocuments] = useState<DocSummary[]>([]);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<DocSummary | null>(null);
  const { can } = useAuth();
  const navigate = useNavigate();

  function load() {
    api.listDocuments().then(setDocuments).catch((err) => setError(err.message));
  }

  useEffect(load, []);

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return documents;
    return documents.filter((document) =>
      [document.title, document.doc_class, document.status, document.ocr_status]
        .filter(Boolean)
        .some((value) => value!.toLowerCase().includes(needle))
    );
  }, [documents, query]);

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    setError("");
    try {
      await api.deleteDocument(pendingDelete.id);
      setDocuments((current) => current.filter((document) => document.id !== pendingDelete.id));
      setPendingDelete(null);
    } catch (err: any) {
      setError(err.message ?? "The document could not be deleted.");
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div>
      <PageHeader
        eyebrow="Document library"
        title="Repository"
        description={`${documents.length} ${documents.length === 1 ? "document" : "documents"} available in your workspace`}
        actions={
          can("CREATE") && (
            <button className="button primary" onClick={() => navigate("/upload")}>
              <FilePlus2 size={18} />
              Add documents
            </button>
          )
        }
      />

      {error && <div className="notice is-danger">{error}</div>}

      <SectionCard className="repository-card">
        <div className="collection-toolbar">
          <label className="search-field">
            <Search size={18} aria-hidden="true" />
            <span className="sr-only">Filter repository</span>
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filter by title, class, or status"
            />
          </label>
          <span className="result-count">{shown.length} shown</span>
        </div>

        {shown.length ? (
          <div className="table-scroll">
            <table className="data-table responsive-table">
              <thead>
                <tr>
                  <th>Document</th>
                  <th>Class</th>
                  <th>Status</th>
                  <th>Capture</th>
                  <th>Pages</th>
                  <th>Updated</th>
                  {can("DELETE") && <th><span className="sr-only">Actions</span></th>}
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
                      <Link className="document-link" to={`/documents/${document.id}`}>
                        <span className="file-symbol"><FileText size={17} /></span>
                        <span>{document.title}</span>
                      </Link>
                    </td>
                    <td data-label="Class">
                      <span>{document.doc_class || "Uncategorized"}</span>
                      {document.class_confidence != null && (
                        <small className="table-detail">{Math.round(document.class_confidence * 100)}% confidence</small>
                      )}
                    </td>
                    <td data-label="Status">
                      <StatusPill tone={statusTone(document.status)}>{document.status}</StatusPill>
                    </td>
                    <td data-label="Capture">
                      <StatusPill tone={statusTone(document.ocr_status)}>{document.ocr_status}</StatusPill>
                    </td>
                    <td data-label="Pages" className="mono-cell">{document.page_count}</td>
                    <td data-label="Updated" className="table-date">
                      {new Date(document.created_at).toLocaleString([], {
                        month: "short",
                        day: "numeric",
                        hour: "numeric",
                        minute: "2-digit",
                      })}
                    </td>
                    {can("DELETE") && (
                      <td data-label="Actions" className="row-actions">
                        <button
                          className="icon-button danger-on-hover"
                          onClick={() => setPendingDelete(document)}
                          aria-label={`Delete ${document.title}`}
                          title="Delete document"
                        >
                          <Trash2 size={17} />
                        </button>
                      </td>
                    )}
                  </motion.tr>
                  ))}
                </AnimatePresence>
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            icon={query ? Search : FolderArchive}
            title={query ? "No matching documents" : "Your repository is empty"}
            description={
              query
                ? "Try a broader title, class, or status."
                : "Upload or import your first document to begin building the archive."
            }
            action={
              query ? (
                <button className="button secondary" onClick={() => setQuery("")}>Clear filter</button>
              ) : can("CREATE") ? (
                <button className="button primary" onClick={() => navigate("/upload")}>
                  <FilePlus2 size={18} /> Add documents
                </button>
              ) : undefined
            }
          />
        )}
      </SectionCard>

      <ConfirmDialog
        open={!!pendingDelete}
        title="Delete this document?"
        description={
          pendingDelete
            ? `“${pendingDelete.title}” and its stored versions will be removed. This action cannot be undone.`
            : ""
        }
        confirmLabel="Delete document"
        onConfirm={confirmDelete}
        onCancel={() => setPendingDelete(null)}
        busy={deleting}
      />
    </div>
  );
}
