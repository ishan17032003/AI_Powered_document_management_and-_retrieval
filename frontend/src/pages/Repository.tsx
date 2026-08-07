import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link, useNavigate } from "react-router-dom";
import { AnimatePresence, motion } from "motion/react";
import { CheckCircle, ChevronDown, FilePlus2, FileText, FolderArchive, Loader2, Search, Tag, Trash2 } from "lucide-react";
import { api, DocClass, DocSummary } from "../api";
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

// ── Class selector (Super Admin only) ─────────────────────────────────────────

interface ClassSelectorProps {
  document: DocSummary;
  classes: DocClass[];
  onReclassified: (updated: DocSummary) => void;
}

function ClassSelector({ document, classes, onReclassified }: ClassSelectorProps) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const [toast, setToast] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [coords, setCoords] = useState({ top: 0, left: 0 });

  // Close on outside click.
  useEffect(() => {
    if (!open) return;
    function handle(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        // We also need to check if the click was inside the fixed dropdown itself
        const dropdown = window.document.getElementById(`dropdown-${document.id}`);
        if (dropdown && dropdown.contains(e.target as Node)) return;
        
        setOpen(false);
        setQuery("");
      }
    }
    // Listen in capture phase so we get the event before it's stopped
    window.document.addEventListener("mousedown", handle, true);
    return () => window.document.removeEventListener("mousedown", handle, true);
  }, [open, document.id]);

  // Focus the input when dropdown opens, and calculate fixed position.
  useEffect(() => {
    if (open) {
      setTimeout(() => inputRef.current?.focus(), 50);
      if (ref.current) {
        const rect = ref.current.getBoundingClientRect();
        setCoords({
          top: rect.bottom + 4,
          left: rect.left,
        });
      }
    }
  }, [open]);

  async function select(classId: number | null, className?: string) {
    setOpen(false);
    setQuery("");
    setBusy(true);
    try {
      const updated = await api.reclassifyDocument(document.id, classId, className);
      onReclassified(updated);
      setToast(classId || className ? `Class set to "${className ?? updated.doc_class}"` : "Class cleared");
      setTimeout(() => setToast(null), 2500);
    } catch {
      setToast("Failed to update class");
      setTimeout(() => setToast(null), 3000);
    } finally {
      setBusy(false);
    }
  }

  const trimmed = query.trim();
  const filtered = classes.filter((c) =>
    c.name.toLowerCase().includes(trimmed.toLowerCase())
  );
  // Show "Create" option only when input doesn't match any existing class exactly.
  const showCreate =
    trimmed.length > 0 &&
    !classes.some((c) => c.name.toLowerCase() === trimmed.toLowerCase());

  const current = document.doc_class || "Uncategorized";

  const dropdownItemStyle = (active: boolean): React.CSSProperties => ({
    display: "flex",
    alignItems: "center",
    gap: "6px",
    padding: "6px 10px",
    borderRadius: "5px",
    cursor: "pointer",
    fontSize: "0.83rem",
    color: active ? "var(--accent, #7c3aed)" : "var(--text, inherit)",
    background: "transparent",
    transition: "background 0.1s",
  });

  return (
    <div className="class-selector" ref={ref as any} style={{ display: "inline-block" }}>
      <button
        className="class-selector-trigger"
        onClick={() => setOpen((o) => !o)}
        disabled={busy}
        title="Change document class (Super Admin)"
        aria-haspopup="listbox"
        aria-expanded={open}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: "4px",
          padding: "2px 8px",
          borderRadius: "6px",
          border: "1px solid var(--line)",
          background: "var(--surface-soft)",
          color: "var(--text, inherit)",
          fontSize: "0.82rem",
          cursor: busy ? "wait" : "pointer",
          transition: "background 0.15s, border-color 0.15s",
        }}
      >
        <Tag size={13} />
        <span>{current}</span>
        {busy ? <Loader2 size={13} className="spin" /> : <ChevronDown size={13} />}
      </button>

      {/* Render via Portal so it breaks out of overflow: hidden containers */}
      {open && typeof window !== "undefined" && window.document.body && createPortal(
        <AnimatePresence>
          {open && (
            <motion.div
              id={`dropdown-${document.id}`}
              role="dialog"
              aria-label="Select or create document class"
              initial={{ opacity: 0, y: -4, scale: 0.97 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -4, scale: 0.97 }}
              transition={{ duration: 0.12 }}
              style={{
                position: "fixed",
                top: coords.top,
                left: coords.left,
                zIndex: 9999,
                minWidth: "200px",
                maxWidth: "280px",
                borderRadius: "8px",
                border: "1px solid var(--line)",
                background: "var(--surface)",
                boxShadow: "var(--shadow-sm)",
                overflow: "hidden",
              }}
            >
              {/* Search / type-a-new-class input */}
              <div style={{ padding: "6px 6px 4px" }}>
                <input
                  ref={inputRef}
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && trimmed) {
                      const exact = classes.find(
                        (c) => c.name.toLowerCase() === trimmed.toLowerCase()
                      );
                      if (exact) select(exact.id);
                      else select(null, trimmed);
                    }
                    if (e.key === "Escape") { setOpen(false); setQuery(""); }
                  }}
                  placeholder="Search or type new class…"
                  aria-label="Search or create class"
                  style={{
                    width: "100%",
                    boxSizing: "border-box",
                    padding: "5px 8px",
                    borderRadius: "5px",
                    border: "1px solid var(--line-strong)",
                    background: "var(--surface-soft)",
                    color: "var(--text, inherit)",
                    fontSize: "0.82rem",
                    outline: "none",
                  }}
                />
              </div>

              <ul
                role="listbox"
                style={{
                  listStyle: "none",
                  margin: 0,
                  padding: "0 4px 4px",
                  maxHeight: "220px",
                  overflowY: "auto",
                }}
              >
                {/* Uncategorized (clear) */}
                {!trimmed && (
                  <li
                    role="option"
                    aria-selected={!document.doc_class}
                    onClick={() => select(null)}
                    style={{ ...dropdownItemStyle(!document.doc_class), fontStyle: "italic" }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = "var(--rail-line)")}
                    onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                  >
                    {!document.doc_class && <CheckCircle size={13} />}
                    Uncategorized
                  </li>
                )}

                {/* Filtered existing classes */}
                {filtered.map((cls) => (
                  <li
                    key={cls.id}
                    role="option"
                    aria-selected={document.doc_class === cls.name}
                    onClick={() => select(cls.id)}
                    style={dropdownItemStyle(document.doc_class === cls.name)}
                    onMouseEnter={(e) => (e.currentTarget.style.background = "var(--rail-line)")}
                    onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                  >
                    {document.doc_class === cls.name && <CheckCircle size={13} />}
                    {cls.name}
                  </li>
                ))}

                {/* Create new class option */}
                {showCreate && (
                  <>
                    {filtered.length > 0 && (
                      <li style={{ borderTop: "1px solid var(--line)", margin: "4px 0" }} />
                    )}
                    <li
                      role="option"
                      aria-selected={false}
                      onClick={() => select(null, trimmed)}
                      style={{
                        ...dropdownItemStyle(false),
                        color: "var(--accent, #7c3aed)",
                        fontWeight: 500,
                      }}
                      onMouseEnter={(e) => (e.currentTarget.style.background = "rgba(124,58,237,0.10)")}
                      onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                    >
                      <FilePlus2 size={13} />
                      Create &ldquo;{trimmed}&rdquo;
                    </li>
                  </>
                )}

                {/* Empty state */}
                {filtered.length === 0 && !showCreate && trimmed && (
                  <li style={{ padding: "8px 10px", fontSize: "0.8rem", color: "var(--text-muted, #aaa)", fontStyle: "italic" }}>
                    No matches
                  </li>
                )}
              </ul>
            </motion.div>
          )}
        </AnimatePresence>,
        window.document.body
      )}

      {/* Toast notification */}
      <AnimatePresence>
        {toast && (
          <motion.span
            initial={{ opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            style={{
              position: "absolute",
              top: "calc(100% + 36px)",
              left: 0,
              padding: "4px 10px",
              borderRadius: "6px",
              background: toast.includes("Failed") ? "#dc2626" : "#16a34a",
              color: "#fff",
              fontSize: "0.78rem",
              whiteSpace: "nowrap",
              zIndex: 200,
              pointerEvents: "none",
            }}
          >
            {toast}
          </motion.span>
        )}
      </AnimatePresence>
    </div>
  );
}


// ── Main Repository page ───────────────────────────────────────────────────────

export default function Repository() {
  const [documents, setDocuments] = useState<DocSummary[]>([]);
  const [docClasses, setDocClasses] = useState<DocClass[]>([]);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<DocSummary | null>(null);
  const { can, user } = useAuth();
  const navigate = useNavigate();

  const isSuperAdmin = !!user?.roles.includes("Super Admin");

  function load() {
    api.listDocuments().then(setDocuments).catch((err) => setError(err.message));
  }

  useEffect(load, []);

  // Fetch classes only if the user is Super Admin.
  useEffect(() => {
    if (!isSuperAdmin) return;
    api.listDocClasses().then(setDocClasses).catch(() => {/* non-critical */});
  }, [isSuperAdmin]);

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return documents;
    return documents.filter((document) =>
      [document.title, document.doc_class, document.status, document.ocr_status]
        .filter(Boolean)
        .some((value) => value!.toLowerCase().includes(needle))
    );
  }, [documents, query]);

  function handleReclassified(updated: DocSummary) {
    setDocuments((current) =>
      current.map((d) => (d.id === updated.id ? updated : d))
    );
  }

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

      {isSuperAdmin && (
        <div className="notice is-info" style={{ marginBottom: "12px", fontSize: "0.85rem" }}>
          <Tag size={14} style={{ marginRight: 6, verticalAlign: "middle" }} />
          You can change a document's class by clicking the class badge in the table.
          The binary will be moved to the corresponding MinIO bucket automatically.
        </div>
      )}

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
                  <th>
                    Class
                    {isSuperAdmin && (
                      <span
                        style={{
                          marginLeft: 6,
                          fontSize: "0.72rem",
                          color: "var(--accent, #7c3aed)",
                          fontWeight: 500,
                          verticalAlign: "middle",
                        }}
                      >
                        (editable)
                      </span>
                    )}
                  </th>
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
                      {isSuperAdmin ? (
                        <ClassSelector
                          document={document}
                          classes={docClasses}
                          onReclassified={handleReclassified}
                        />
                      ) : (
                        <>
                          <span>{document.doc_class || "Uncategorized"}</span>
                          {document.class_confidence != null && (
                            <small className="table-detail">{Math.round(document.class_confidence * 100)}% confidence</small>
                          )}
                        </>
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
                      {new Date(document.updated_at ?? document.created_at).toLocaleString([], {
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
        title="Move to Trash?"
        description={
          pendingDelete
            ? `"${pendingDelete.title}" will be moved to the Trash Bin. Its vectors and search index will be removed immediately so it cannot be used in Ask AI or Search. You can restore or permanently delete it anytime from Trash.`
            : ""
        }
        confirmLabel="Move to Trash"
        onConfirm={confirmDelete}
        onCancel={() => setPendingDelete(null)}
        busy={deleting}
      />
    </div>
  );
}
