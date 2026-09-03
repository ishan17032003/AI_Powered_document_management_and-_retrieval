import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft,
  Download,
  FileText,
  Film,
  History,
  Image as ImageIcon,
  Languages,
  Music,
  ScanText,
  Tag,
  Trash2,
} from "lucide-react";
import { api, DocDetail } from "../api";
import { useAuth } from "../auth";
import {
  ConfirmDialog,
  LoadingState,
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

function fmtBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export default function DocumentDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { can } = useAuth();
  const [document, setDocument] = useState<DocDetail | null>(null);
  const [mediaUrl, setMediaUrl] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    api
      .getDocument(Number(id))
      .then(setDocument)
      .catch((err) => setError(err.message));
  }, [id]);

  useEffect(() => {
    if (!document) return;
    const filename = document.title.toLowerCase();
    const extension = filename.slice(filename.lastIndexOf("."));
    const shouldLoadMedia = [
      ".mp4", ".avi", ".mov", ".mkv", ".webm",
      ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac",
      ".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp",
    ].includes(extension);

    if (shouldLoadMedia) {
      api.fetchBlob(document.id)
        .then((res) => setMediaUrl(res.url))
        .catch(() => setMediaUrl(null));
    }
  }, [document?.id]);

  async function remove() {
    if (!document) return;
    setDeleting(true);
    try {
      await api.deleteDocument(document.id);
      navigate("/repository");
    } catch (err: any) {
      setError(err.message || "The document could not be deleted.");
      setDeleting(false);
      setConfirmOpen(false);
    }
  }

  if (error && !document) {
    return (
      <SectionCard>
        <div className="notice is-danger">{error}</div>
        <button className="button secondary" onClick={() => navigate("/repository")}>
          <ArrowLeft size={17} /> Back to repository
        </button>
      </SectionCard>
    );
  }

  if (!document) return <LoadingState label="Opening document record…" />;

  return (
    <div>
      <button className="back-link" onClick={() => navigate(-1)}>
        <ArrowLeft size={17} /> Back
      </button>

      <PageHeader
        eyebrow={`Document ${document.id}`}
        title={document.title}
        description={
          <span className="document-status-line">
            <StatusPill tone={statusTone(document.status)}>{document.status}</StatusPill>
            <StatusPill tone={statusTone(document.ocr_status)}>Capture: {document.ocr_status}</StatusPill>
            {document.is_duplicate_of && (
              <StatusPill tone="warning">Matches document #{document.is_duplicate_of}</StatusPill>
            )}
          </span>
        }
        actions={
          <>
            {can("DOWNLOAD") && (
              <button 
                className="button secondary" 
                onClick={async () => {
                  try {
                    await api.downloadDocument(document.id, document.versions?.[0]?.filename || "document");
                  } catch (err: any) {
                    setError(err.message ?? "Could not download document.");
                  }
                }}
              >
                <Download size={17} /> Download
              </button>
            )}
            {can("DELETE") && (
              <button className="button danger-subtle" onClick={() => setConfirmOpen(true)}>
                <Trash2 size={17} /> Delete
              </button>
            )}
          </>
        }
      />

      {error && <div className="notice is-danger">{error}</div>}

      <div className="document-detail-grid">
        <SectionCard className="metadata-card">
          <div className="section-heading">
            <div>
              <span className="section-kicker">Recognized fields</span>
              <h2>Document details</h2>
            </div>
            <Tag size={19} />
          </div>

          <dl className="metadata-grid">
            <div>
              <dt>Class</dt>
              <dd>
                {document.doc_class || "Uncategorized"}
                {document.class_confidence != null && (
                  <small>{Math.round(document.class_confidence * 100)}% confidence</small>
                )}
              </dd>
            </div>
            <div>
              <dt>Language</dt>
              <dd><Languages size={15} /> {document.language || "Unknown"}</dd>
            </div>
            <div>
              <dt>Pages</dt>
              <dd>{document.page_count}</dd>
            </div>
            <div>
              <dt>Capture confidence</dt>
              <dd>
                {document.ocr_confidence != null
                  ? `${Math.round(document.ocr_confidence * 100)}%`
                  : "Not available"}
              </dd>
            </div>
            {document.metadata.map((item) => (
              <div key={item.key}>
                <dt>{item.key}</dt>
                <dd>{item.value}</dd>
              </div>
            ))}
          </dl>

          <div className="hash-block">
            <span>SHA-256 fingerprint</span>
            <code>{document.content_hash}</code>
          </div>
        </SectionCard>

        <SectionCard className="version-card">
          <div className="section-heading">
            <div>
              <span className="section-kicker">Stored revisions</span>
              <h2>Version history</h2>
            </div>
            <History size={19} />
          </div>

          <div className="version-list">
            {document.versions.map((version) => (
              <div key={version.id} className="version-row">
                <span className="version-number">v{version.version_no}</span>
                <span className="version-copy">
                  <strong>{version.filename}</strong>
                  <small>{version.content_type} · {fmtBytes(version.size)}</small>
                </span>
                <time>{new Date(version.created_at).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" })}</time>
              </div>
            ))}
          </div>
        </SectionCard>
      </div>

      {(() => {
        const ext = (document.title || "").toLowerCase().slice((document.title || "").lastIndexOf("."));
        const isVideo = [".mp4", ".avi", ".mov", ".mkv", ".webm"].includes(ext);
        const isAudio = [".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"].includes(ext);
        const isImage = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp"].includes(ext);
        if (!(isVideo || isAudio || isImage) || !mediaUrl) return null;
        return (
          <SectionCard className="media-preview-card">
            <div className="section-heading">
              <div>
                <span className="section-kicker">Media playback & preview</span>
                <h2>{isVideo ? "Video Player" : isAudio ? "Audio Player" : "Image Preview"}</h2>
              </div>
              {isVideo ? <Film size={20} /> : isAudio ? <Music size={20} /> : <ImageIcon size={20} />}
            </div>
            <div className="media-player-container" style={{ display: "flex", justifyContent: "center", padding: "1rem 0" }}>
              {isVideo && (
                <video
                  controls
                  src={mediaUrl}
                  style={{ maxWidth: "100%", maxHeight: "480px", borderRadius: "8px" }}
                />
              )}
              {isAudio && (
                <audio
                  controls
                  src={mediaUrl}
                  style={{ width: "100%", maxWidth: "600px" }}
                />
              )}
              {isImage && (
                <img
                  src={mediaUrl}
                  alt={document.title}
                  style={{ maxWidth: "100%", maxHeight: "500px", objectFit: "contain", borderRadius: "8px" }}
                />
              )}
            </div>
          </SectionCard>
        );
      })()}

      <SectionCard className="extracted-text-card">
        <div className="section-heading">
          <div>
            <span className="section-kicker">Searchable content</span>
            <h2>Extracted text</h2>
          </div>
          <ScanText size={20} />
        </div>
        {document.ocr_text ? (
          <pre className="ocr-text">{document.ocr_text}</pre>
        ) : (
          <div className="inline-empty">
            <FileText size={18} /> No searchable text was extracted from this document.
          </div>
        )}
      </SectionCard>

      <ConfirmDialog
        open={confirmOpen}
        title="Delete this document?"
        description={`“${document.title}” and all stored versions will be removed. This action cannot be undone.`}
        confirmLabel="Delete document"
        onConfirm={remove}
        onCancel={() => setConfirmOpen(false)}
        busy={deleting}
      />
    </div>
  );
}
