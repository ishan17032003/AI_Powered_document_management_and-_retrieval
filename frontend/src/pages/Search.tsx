import { ChangeEvent, FormEvent, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AnimatePresence, motion } from "motion/react";
import {
  ArrowRight,
  FileSearch,
  FileText,
  Image as ImageIcon,
  Layers3,
  PanelsTopLeft,
  Search as SearchIcon,
  Sparkles,
  UploadCloud,
} from "lucide-react";
import {
  api,
  ImageSearchResponse,
  SearchResponse,
  VisualSearchResponse,
} from "../api";
import { EmptyState, PageHeader, SectionCard, StatusPill } from "../components/ui";

type SearchMode =
  | "keyword"
  | "semantic"
  | "text_to_image"
  | "text_to_page"
  | "image";

const SEARCH_MODES: Array<{
  value: SearchMode;
  label: string;
  icon: typeof SearchIcon;
}> = [
  { value: "keyword", label: "Exact words", icon: SearchIcon },
  { value: "semantic", label: "Meaning", icon: Sparkles },
  { value: "text_to_image", label: "Images", icon: ImageIcon },
  { value: "text_to_page", label: "Pages", icon: PanelsTopLeft },
  { value: "image", label: "Visual upload", icon: UploadCloud },
];

function isTextVisualMode(mode: SearchMode): mode is "text_to_image" | "text_to_page" {
  return mode === "text_to_image" || mode === "text_to_page";
}

function visualModeLabel(mode: "text_to_image" | "text_to_page" | "image_to_image" | "hybrid") {
  if (mode === "text_to_image") return "Text → image assets";
  if (mode === "text_to_page") return "Text → visual pages";
  if (mode === "image_to_image") return "Image → similar assets";
  return "Hybrid visual search";
}

export default function Search() {
  const [searchParams, setSearchParams] = useSearchParams();
  const initialQuery = searchParams.get("q") || "";
  const [query, setQuery] = useState(initialQuery);
  const [mode, setMode] = useState<SearchMode>("keyword");
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [visualResult, setVisualResult] = useState<VisualSearchResponse | null>(null);
  const [imageResult, setImageResult] = useState<ImageSearchResponse | null>(null);
  const [imageFile, setImageFile] = useState<File | null>(null);
  const [previewUrls, setPreviewUrls] = useState<Record<number, string>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const initialSearchStarted = useRef(false);

  useEffect(() => {
    let disposed = false;
    const created: string[] = [];
    setPreviewUrls({});
    const assetIds = [
      ...(imageResult?.hits || []),
      ...(visualResult?.hits || []),
    ]
      .map((hit) => hit.asset_id)
      .filter((value): value is number => typeof value === "number");

    void Promise.all(assetIds.map(async (assetId) => {
      try {
        const url = URL.createObjectURL(await api.visualPreview(assetId));
        created.push(url);
        if (!disposed) setPreviewUrls((current) => ({ ...current, [assetId]: url }));
      } catch {
        // A preview can disappear between search authorization and hydration.
      }
    }));
    return () => {
      disposed = true;
      created.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [imageResult, visualResult]);

  async function execute(value: string, searchMode = mode) {
    const normalized = value.trim();
    if (!normalized) return;
    setBusy(true);
    setError("");
    try {
      if (isTextVisualMode(searchMode)) {
        setVisualResult(await api.visualSearch(normalized, searchMode));
        setResult(null);
        setImageResult(null);
      } else {
        const response =
          searchMode === "keyword"
            ? await api.search(normalized)
            : await api.semantic(normalized);
        setResult(response);
        setVisualResult(null);
        setImageResult(null);
      }
    } catch (err: any) {
      setError(err.message || "The archive could not be searched.");
    } finally {
      setBusy(false);
    }
  }

  async function executeImage(file: File) {
    setBusy(true);
    setError("");
    try {
      setVisualResult(await api.visualImageSearch(file));
      setResult(null);
      setImageResult(null);
    } catch (err: any) {
      setError(err.message || "The image query could not be processed.");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (!initialQuery || initialSearchStarted.current || mode === "image") return;
    initialSearchStarted.current = true;
    execute(initialQuery, "keyword");
  }, [initialQuery]);

  async function run(event: FormEvent) {
    event.preventDefault();
    if (mode === "image") {
      if (imageFile) await executeImage(imageFile);
      return;
    }
    const normalized = query.trim();
    if (!normalized) return;
    setSearchParams({ q: normalized });
    await execute(normalized);
  }

  function selectMode(nextMode: SearchMode) {
    setMode(nextMode);
    setResult(null);
    setVisualResult(null);
    setImageResult(null);
    setError("");
  }

  return (
    <div>
      <PageHeader
        eyebrow="Across every document"
        title="Search the archive"
        description="Search exact language, meaning, image assets, or rendered pages. Every result is filtered to what you can view."
      />

      <SectionCard className="search-composer">
        <div className="segmented-control" aria-label="Search mode">
          {SEARCH_MODES.map(({ value, label, icon: Icon }) => (
            <button
              type="button"
              key={value}
              className={mode === value ? "is-active" : ""}
              onClick={() => selectMode(value)}
              aria-pressed={mode === value}
            >
              {mode === value && <motion.span className="segment-indicator" layoutId="search-mode" />}
              <Icon size={16} />
              <span>{label}</span>
            </button>
          ))}
        </div>

        <form className="search-composer-form" onSubmit={run}>
          {mode === "image" ? (
            <label className="image-query-field">
              <UploadCloud size={21} aria-hidden="true" />
              <span>
                <strong>{imageFile ? imageFile.name : "Choose an image to search"}</strong>
                <small>Processed ephemerally; query bytes are not stored</small>
              </span>
              <input
                type="file"
                accept="image/png,image/jpeg,image/webp,image/gif"
                aria-label="Choose an image for visual search"
                onChange={(event: ChangeEvent<HTMLInputElement>) => setImageFile(event.target.files?.[0] || null)}
              />
            </label>
          ) : (
            <label className="search-field is-large">
              {isTextVisualMode(mode) ? <Layers3 size={21} /> : <SearchIcon size={21} />}
              <span className="sr-only">Search the document archive</span>
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={
                  mode === "keyword"
                    ? "Search a phrase, filename, or reference number"
                    : mode === "semantic"
                      ? "Describe what you need to find"
                      : mode === "text_to_image"
                        ? "Describe an image, figure, or visual detail"
                        : "Describe a page, table, chart, or layout"
                }
              />
            </label>
          )}
          <motion.button
            className="button primary"
            disabled={busy || (mode === "image" ? !imageFile : !query.trim())}
            whileTap={busy ? undefined : { scale: 0.98 }}
          >
            {busy ? "Searching…" : "Search"}
          </motion.button>
        </form>
        {isTextVisualMode(mode) || mode === "image" ? (
          <p className="search-mode-note">
            {mode === "text_to_image"
              ? "Matches indexed OCR, captions, and descriptions for authorized images and figures."
              : mode === "text_to_page"
                ? "Matches indexed OCR and page text for authorized rendered document pages."
                : "Finds visually similar authorized assets. The uploaded query is processed in memory and discarded."}
          </p>
        ) : null}
      </SectionCard>

      {error && <div className="notice is-danger">{error}</div>}

      <AnimatePresence mode="wait">
        {mode === "image" && imageResult ? (
          <motion.section className="search-results" key="image-results" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            <div className="results-heading">
              <div>
                <span className="eyebrow">Ephemeral image search</span>
                <h2>{imageResult.count ? `${imageResult.count} visual results` : "No visual results"}</h2>
              </div>
              <StatusPill tone={imageResult.provider === "disabled" ? "warning" : "success"}>
                {imageResult.provider === "disabled" ? "Visual provider unavailable" : "Visual search ready"}
              </StatusPill>
            </div>
            {imageResult.count && imageResult.hits.length ? (
              <div className="visual-result-list">
                {imageResult.hits.map((hit, index) => {
                  const documentId = typeof hit.document_id === "number" ? hit.document_id : null;
                  const assetId = typeof hit.asset_id === "number" ? hit.asset_id : null;
                  const title = typeof hit.title === "string" ? hit.title : `Visual result ${index + 1}`;
                  return (
                    <article className="visual-result-card" key={`${assetId || documentId || "result"}-${index}`}>
                      {assetId && previewUrls[assetId] ? <img src={previewUrls[assetId]} alt="" loading="lazy" /> : <span className="visual-result-placeholder"><ImageIcon size={22} /></span>}
                      <div>
                        {documentId ? <Link to={`/documents/${documentId}`}>{title}</Link> : <strong>{title}</strong>}
                        <small>Authorized visual match</small>
                      </div>
                    </article>
                  );
                })}
              </div>
            ) : (
              <SectionCard>
                <EmptyState
                  icon={ImageIcon}
                  title={imageResult.count ? "Visual results are ready" : "No visual matches yet"}
                  description={imageResult.provider === "disabled"
                    ? "The local visual provider is disabled. Your image was validated and discarded without being stored."
                    : "Results are authorized per document and previews are issued only through scoped links."}
                />
              </SectionCard>
            )}
          </motion.section>
        ) : visualResult ? (
          <motion.section className="search-results" key={`${visualResult.query}-${visualResult.mode}`} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            <div className="results-heading">
              <div>
                <span className="eyebrow">{visualModeLabel(visualResult.mode)}</span>
                <h2>
                  {visualResult.query === "[uploaded image]"
                    ? `${visualResult.count} ${visualResult.count === 1 ? "similar visual" : "similar visuals"}`
                    : `${visualResult.count} ${visualResult.count === 1 ? "visual result" : "visual results"} for “${visualResult.query}”`}
                </h2>
              </div>
              <StatusPill tone={visualResult.degraded ? "warning" : "success"}>
                {visualResult.provider === "siglip2"
                    ? "Semantic visual index"
                    : visualResult.provider === "visual_text"
                      ? "OCR / caption index"
                    : visualResult.provider === "sql_exact"
                      ? "Exact visual match"
                      : visualResult.provider === "siglip2_policy_blocked"
                        ? "Authorization scope too large"
                      : "Visual search unavailable"}
              </StatusPill>
            </div>
            {visualResult.count ? (
              <div className="visual-result-list visual-result-list-wide">
                {visualResult.hits.map((hit, index) => (
                  <motion.article
                    className="visual-result-card visual-result-card-wide"
                    key={`${hit.asset_id}-${hit.result_type}`}
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: Math.min(index * 0.035, 0.18), opacity: { duration: 0.18 } }}
                  >
                    {previewUrls[hit.asset_id] ? <img src={previewUrls[hit.asset_id]} alt="" loading="lazy" /> : <span className="visual-result-placeholder"><ImageIcon size={22} /></span>}
                    <div className="visual-result-copy">
                      <div className="visual-result-meta">
                        <StatusPill tone="info">{hit.result_type === "page" ? "Page" : "Image"}</StatusPill>
                        {hit.page_number && <span>Page {hit.page_number}</span>}
                      </div>
                      <Link to={`/documents/${hit.document_id}`}>{hit.title}</Link>
                      <p>{hit.snippet || "No extracted text preview is available for this visual."}</p>
                      <small>Authorized preview · relevance {Math.round(hit.score * 100)}%</small>
                    </div>
                    <Link className="result-open" to={`/documents/${hit.document_id}`} aria-label={`Open ${hit.title}`}>
                      <ArrowRight size={18} />
                    </Link>
                  </motion.article>
                ))}
              </div>
            ) : (
              <SectionCard>
                <EmptyState
                  icon={isTextVisualMode(mode) && mode === "text_to_page" ? PanelsTopLeft : ImageIcon}
                  title="No visual matches"
                  description={visualResult.degraded
                    ? visualResult.provider === "siglip2_policy_blocked"
                      ? "This search is bounded until a security-domain authorization projection is available."
                      : visualResult.query === "[uploaded image]"
                      ? "Semantic image search is not enabled in this backend profile. The image was validated and discarded without being stored."
                      : "Visual retrieval is not enabled in this backend profile."
                    : "Try a more specific visual detail, document title, or OCR phrase."}
                />
              </SectionCard>
            )}
          </motion.section>
        ) : result ? (
          <motion.section
            className="search-results"
            key={`${result.query}-${result.mode}`}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
          >
            <div className="results-heading">
              <div>
                <span className="eyebrow">{result.mode.startsWith("semantic") ? "Meaning search" : "Exact-word search"}</span>
                <h2>{result.count} {result.count === 1 ? "result" : "results"} for “{result.query}”</h2>
              </div>
            </div>

            {result.count ? (
              <div className="result-list">
                {result.hits.map((hit, index) => (
                  <motion.article
                    className="result-card"
                    key={hit.document_id}
                    initial={{ opacity: 0, x: 10 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: Math.min(index * 0.035, 0.18), opacity: { duration: 0.18 } }}
                  >
                    <span className="file-symbol"><FileText size={18} /></span>
                    <div className="result-content">
                      <div className="result-title">
                        <Link to={`/documents/${hit.document_id}`}>{hit.title}</Link>
                        <StatusPill>{hit.doc_class || "Uncategorized"}</StatusPill>
                      </div>
                      <div
                        className="result-snippet"
                        dangerouslySetInnerHTML={{ __html: hit.snippet }}
                      />
                    </div>
                    <Link className="result-open" to={`/documents/${hit.document_id}`} aria-label={`Open ${hit.title}`}>
                      <ArrowRight size={18} />
                    </Link>
                  </motion.article>
                ))}
              </div>
            ) : (
              <SectionCard>
                <EmptyState
                  icon={FileSearch}
                  title="No matching documents"
                  description="Try fewer terms, a filename, or switch between exact words and meaning."
                />
              </SectionCard>
            )}
          </motion.section>
        ) : null}
      </AnimatePresence>
    </div>
  );
}
