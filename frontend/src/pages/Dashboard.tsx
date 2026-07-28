import { CSSProperties, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { motion, useReducedMotion } from "motion/react";
import {
  ArrowRight,
  Bot,
  Check,
  Copy,
  FileCheck2,
  FileClock,
  FileText,
  FolderInput,
  HardDrive,
  LucideIcon,
  Search,
  ShieldCheck,
  UploadCloud,
} from "lucide-react";
import { api, DocSummary, RagStatus, Stats } from "../api";
import { useAuth } from "../auth";
import { PageHeader, SectionCard, StatusPill, StatusTone } from "../components/ui";

function fmtBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function formatUpdated(value: string) {
  return new Date(value).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function documentTone(status: string): StatusTone {
  const normalized = status.toLowerCase();
  if (normalized === "ready") return "success";
  if (normalized === "review") return "warning";
  if (normalized === "error") return "danger";
  return "info";
}

type AttentionItemProps = {
  icon: LucideIcon;
  title: string;
  description: string;
  action: string;
  to: string;
  tone: "warning" | "danger" | "success";
  delay: number;
};

function AttentionItem({
  icon: Icon,
  title,
  description,
  action,
  to,
  tone,
  delay,
}: AttentionItemProps) {
  const navigate = useNavigate();
  return (
    <motion.div
      className="attention-row"
      layout
      initial={{ opacity: 0, x: 10 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ delay, opacity: { duration: 0.18 } }}
    >
      <span className={`attention-icon is-${tone}`} aria-hidden="true">
        <Icon size={21} strokeWidth={1.8} />
      </span>
      <span className="attention-copy">
        <strong>{title}</strong>
        <small>{description}</small>
      </span>
      <button className="button secondary compact" onClick={() => navigate(to)}>
        {action}
      </button>
    </motion.div>
  );
}

const archiveColors = {
  contract: "var(--archive-contract)",
  invoice: "var(--archive-invoice)",
  report: "var(--archive-report)",
  other: "var(--archive-other)",
};

export default function Dashboard() {
  const { user, can } = useAuth();
  const navigate = useNavigate();
  const reduceMotion = useReducedMotion();
  const [stats, setStats] = useState<Stats | null>(null);
  const [ragStatus, setRagStatus] = useState<RagStatus | null>(null);
  const [documents, setDocuments] = useState<DocSummary[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    api.stats().then(setStats).catch((err) => setError(err.message));
    api.ragStatus().then(setRagStatus).catch(() => undefined);
    api.listDocuments().then(setDocuments).catch(() => undefined);
  }, []);

  const archiveGroups = useMemo(() => {
    const groups = { contract: 0, invoice: 0, report: 0, other: 0 };
    for (const document of documents) {
      const value = (document.doc_class || "").toLowerCase();
      if (value.includes("contract")) groups.contract += 1;
      else if (value.includes("invoice")) groups.invoice += 1;
      else if (value.includes("report")) groups.report += 1;
      else groups.other += 1;
    }
    return [
      { key: "contract", label: "Contracts", count: groups.contract, color: archiveColors.contract },
      { key: "invoice", label: "Invoices", count: groups.invoice, color: archiveColors.invoice },
      { key: "report", label: "Reports", count: groups.report, color: archiveColors.report },
      { key: "other", label: "Other", count: groups.other, color: archiveColors.other },
    ];
  }, [documents]);

  const recentDocuments = useMemo(
    () =>
      [...documents]
        .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
        .slice(0, 3),
    [documents]
  );

  const missingEngines = stats
    ? Object.values(stats.engine).filter((available) => !available).length
    : 0;
  const attentionCount = stats
    ? stats.open_duplicate_groups + stats.needs_review + missingEngines
    : 0;
  const dateLabel = new Intl.DateTimeFormat("en-GB", {
    weekday: "long",
    day: "2-digit",
    month: "long",
  })
    .format(new Date())
    .toUpperCase();

  return (
    <div className="dashboard-page">
      <PageHeader
        eyebrow={dateLabel}
        title={`Good morning, ${user?.name || "Platform Admin"}`}
        description={
          stats
            ? attentionCount
              ? `${attentionCount} ${attentionCount === 1 ? "item needs" : "items need"} your attention`
              : "Your archive is up to date"
            : "Review document activity and archive health"
        }
        actions={
          stats && (
            <StatusPill tone={stats.processing > 0 ? "info" : "success"}>
              <span className="status-dot" />
              {stats.processing > 0 ? `${stats.processing} processing` : "Archive ready"}
            </StatusPill>
          )
        }
      />

      {error && <div className="notice is-danger">{error}</div>}

      {!stats ? (
        <div className="dashboard-skeleton" aria-label="Loading dashboard">
          <div />
          <div />
          <div />
        </div>
      ) : (
        <>
          <div className="dashboard-primary-grid">
            <SectionCard className="attention-card" ariaLabel="Attention queue">
              <div className="section-heading">
                <div>
                  <span className="section-kicker">Today</span>
                  <h2>Attention queue</h2>
                </div>
                <span className="section-count">{attentionCount}</span>
              </div>

              <div className="attention-list">
                <AttentionItem
                  icon={Copy}
                  title={
                    stats.open_duplicate_groups
                      ? `${stats.open_duplicate_groups} duplicate ${stats.open_duplicate_groups === 1 ? "group" : "groups"}`
                      : "No duplicate conflicts"
                  }
                  description={
                    stats.open_duplicate_groups
                      ? "Choose the primary document"
                      : "Every document has a clear source"
                  }
                  action={stats.open_duplicate_groups ? "Review" : "Open"}
                  to="/duplicates"
                  tone={stats.open_duplicate_groups ? "warning" : "success"}
                  delay={0.04}
                />
                <AttentionItem
                  icon={FileCheck2}
                  title={
                    stats.needs_review
                      ? `${stats.needs_review} ${stats.needs_review === 1 ? "file needs" : "files need"} classification`
                      : "Classification queue is clear"
                  }
                  description={
                    stats.needs_review
                      ? "Confirm the suggested document type"
                      : "All available suggestions are resolved"
                  }
                  action="Repository"
                  to="/repository"
                  tone={stats.needs_review ? "warning" : "success"}
                  delay={0.08}
                />
                <AttentionItem
                  icon={missingEngines ? FileClock : ShieldCheck}
                  title={
                    missingEngines
                      ? `${missingEngines} capture ${missingEngines === 1 ? "engine is" : "engines are"} unavailable`
                      : "Capture engines are ready"
                  }
                  description={
                    missingEngines
                      ? "Fallback processing remains available"
                      : "Files can be processed normally"
                  }
                  action="Capture"
                  to="/upload"
                  tone={missingEngines ? "danger" : "success"}
                  delay={0.12}
                />
              </div>
            </SectionCard>

            <SectionCard className="archive-map-card" ariaLabel="Archive map">
              <div className="section-heading">
                <div>
                  <span className="section-kicker">Live collection</span>
                  <h2>Archive map</h2>
                </div>
                <span className="archive-total">
                  <strong>{stats.total_documents}</strong>
                  <small>documents</small>
                </span>
              </div>

              <div className="archive-visual" aria-label="Documents grouped by class">
                <div className="archive-bays">
                  {archiveGroups.map((group, index) => {
                    const stackHeight = Math.max(34, Math.min(104, 34 + group.count * 4));
                    return (
                      <div className="archive-bay" key={group.key}>
                        <motion.div
                          className="archive-stack"
                          initial={reduceMotion ? false : { opacity: 0, scaleY: 0.6 }}
                          animate={{ opacity: 1, scaleY: 1 }}
                          transition={{ delay: 0.08 + index * 0.08, type: "spring", stiffness: 330, damping: 28 }}
                          style={
                            {
                              "--stack-height": `${stackHeight}px`,
                              "--archive-color": group.color,
                            } as CSSProperties
                          }
                        >
                          <span className="archive-tab" />
                          <span className="archive-pages" />
                        </motion.div>
                      </div>
                    );
                  })}
                </div>
                <div className="archive-shelf" />
              </div>

              <div className="archive-legend">
                {archiveGroups.map((group) => (
                  <div key={group.key}>
                    <span className="legend-swatch" style={{ backgroundColor: group.color }} />
                    <span>{group.label}</span>
                    <strong>{group.count}</strong>
                  </div>
                ))}
              </div>

              <div className="archive-foot">
                <span>
                  <HardDrive size={16} />
                  {fmtBytes(stats.storage_bytes)} stored
                </span>
                {ragStatus && (
                  <span>
                    <Bot size={16} />
                    {ragStatus.rag.llm === "extractive-fallback" ? "Grounded search ready" : "AI answers ready"}
                  </span>
                )}
              </div>
            </SectionCard>
          </div>

          <div className="dashboard-lower-grid">
            <SectionCard className="recent-card" ariaLabel="Recent documents">
              <div className="section-heading">
                <div>
                  <span className="section-kicker">Latest activity</span>
                  <h2>Recent documents</h2>
                </div>
                <button className="text-button" onClick={() => navigate("/repository")}>
                  View repository <ArrowRight size={16} />
                </button>
              </div>

              {recentDocuments.length ? (
                <div className="document-rows">
                  {recentDocuments.map((document) => (
                    <motion.button
                      layout="position"
                      className="document-row"
                      key={document.id}
                      onClick={() => navigate(`/documents/${document.id}`)}
                      whileTap={{ scale: 0.995 }}
                    >
                      <span className="file-symbol">
                        <FileText size={18} />
                      </span>
                      <span className="document-name">
                        <strong>{document.title}</strong>
                        <small>{document.doc_class || "Uncategorized"}</small>
                      </span>
                      <StatusPill tone={documentTone(document.status)}>{document.status}</StatusPill>
                      <span className="document-updated">{formatUpdated(document.created_at)}</span>
                      <ArrowRight size={16} aria-hidden="true" />
                    </motion.button>
                  ))}
                </div>
              ) : (
                <div className="inline-empty">Upload a document to begin building the archive.</div>
              )}
            </SectionCard>

            <SectionCard className="quick-actions-card" ariaLabel="Quick actions">
              <div className="section-heading">
                <div>
                  <span className="section-kicker">Shortcuts</span>
                  <h2>Quick actions</h2>
                </div>
              </div>
              <div className="quick-actions">
                <button onClick={() => navigate("/ask")}>
                  <span><Bot size={18} /></span>
                  Ask a question
                  <ArrowRight size={16} />
                </button>
                {can("CREATE") && (
                  <>
                    <button onClick={() => navigate("/upload")}>
                      <span><UploadCloud size={18} /></span>
                      Upload files
                      <ArrowRight size={16} />
                    </button>
                    <button onClick={() => navigate("/upload")}>
                      <span><FolderInput size={18} /></span>
                      Import a folder
                      <ArrowRight size={16} />
                    </button>
                  </>
                )}
                <button onClick={() => navigate("/search")}>
                  <span><Search size={18} /></span>
                  Search the archive
                  <ArrowRight size={16} />
                </button>
              </div>
            </SectionCard>
          </div>
        </>
      )}
    </div>
  );
}
