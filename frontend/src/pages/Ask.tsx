import { Children, FormEvent, ReactNode, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { ArrowRight, Bot, FileText, Image as ImageIcon, Plus, Send, Sparkles, X } from "lucide-react";
import ReactMarkdown, { type Components } from "react-markdown";
import { api, AskResponse, VisualSearchHit } from "../api";
import { PageHeader, StatusPill } from "../components/ui";

interface Turn {
  id: string;
  question: string;
  response?: AskResponse;
  error?: string;
  pending?: boolean;
}

function AnswerText({ text }: { text: string }) {
  const parts = text.split(/(\[\d+\])/g);
  return (
    <>
      {parts.map((part, index) =>
        /^\[\d+\]$/.test(part) ? (
          <span key={`${part}-${index}`} className="citation-marker">{part}</span>
        ) : (
          <span key={index}>{part}</span>
        )
      )}
    </>
  );
}

// Wrap plain-text markdown children so [1]-style citations keep their chip styling.
function withCitations(children: ReactNode): ReactNode {
  return Children.map(children, (child) =>
    typeof child === "string" ? <AnswerText text={child} /> : child
  );
}

const answerMarkdownComponents: Components = {
  p: ({ children }) => <p>{withCitations(children)}</p>,
  li: ({ children }) => <li>{withCitations(children)}</li>,
  strong: ({ children }) => <strong>{withCitations(children)}</strong>,
  em: ({ children }) => <em>{withCitations(children)}</em>,
  // Answers are untrusted model output: render links as plain text.
  a: ({ children }) => <>{withCitations(children)}</>,
};

function AnswerMarkdown({ text }: { text: string }) {
  return <ReactMarkdown components={answerMarkdownComponents}>{text}</ReactMarkdown>;
}

function uniqueCitations(citations: AskResponse["citations"]) {
  const seen = new Set<number>();
  return citations.filter((citation) => {
    if (seen.has(citation.document_id)) return false;
    seen.add(citation.document_id);
    return true;
  });
}

const STORAGE_KEY = "docvault_ask_history";

export default function Ask() {
  const [turns, setTurns] = useState<Turn[]>(() => {
    try {
      const saved = sessionStorage.getItem(STORAGE_KEY);
      return saved ? JSON.parse(saved) : [];
    } catch {
      return [];
    }
  });
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [previewUrls, setPreviewUrls] = useState<Record<number, string>>({});
  const [zoomed, setZoomed] = useState<VisualSearchHit | null>(null);
  const requestedPreviews = useRef(new Set<number>());
  const createdPreviewUrls = useRef<string[]>([]);
  const threadRef = useRef<HTMLDivElement>(null);
  const reduceMotion = useReducedMotion();

  useEffect(() => {
    try {
      const toSave = turns
        .filter((turn) => !turn.pending && turn.response)
        .map((turn) => ({
          id: turn.id,
          question: turn.question,
          response: turn.response,
        }));
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(toSave));
    } catch {}
  }, [turns]);

  useEffect(() => {
    const thread = threadRef.current;
    if (!thread) return;
    thread.scrollTo({
      top: thread.scrollHeight,
      behavior: reduceMotion ? "auto" : "smooth",
    });
  }, [turns, reduceMotion]);

  useEffect(() => {
    const assetIds = turns
      .flatMap((turn) => turn.response?.images || [])
      .map((hit) => hit.asset_id)
      .filter((assetId) => !requestedPreviews.current.has(assetId));
    if (!assetIds.length) return;
    assetIds.forEach((assetId) => requestedPreviews.current.add(assetId));
    void Promise.all(assetIds.map(async (assetId) => {
      try {
        const url = URL.createObjectURL(await api.visualPreview(assetId));
        createdPreviewUrls.current.push(url);
        setPreviewUrls((current) => ({ ...current, [assetId]: url }));
      } catch {
        // A preview can disappear between search authorization and hydration.
      }
    }));
  }, [turns]);

  useEffect(() => () => {
    createdPreviewUrls.current.forEach((url) => URL.revokeObjectURL(url));
  }, []);

  useEffect(() => {
    if (!zoomed) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setZoomed(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [zoomed]);

  function clearHistory() {
    setTurns([]);
    try {
      sessionStorage.removeItem(STORAGE_KEY);
    } catch {}
  }

  async function run(value: string, documentId?: number) {
    const normalized = value.trim();
    if (!normalized || busy) return;
    const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    setBusy(true);

    const historyPayload = turns
      .filter((turn) => !turn.pending && turn.response?.answer)
      .flatMap((turn) => [
        { role: "user" as const, content: turn.question },
        { role: "assistant" as const, content: turn.response!.answer },
      ]);

    setTurns((current) => [...current, { id, question: normalized, pending: true }]);

    try {
      const response = await api.ask(
        normalized,
        documentId,
        historyPayload.length > 0 ? historyPayload : undefined
      );
      setTurns((current) =>
        current.map((turn) =>
          turn.id === id ? { id, question: normalized, response } : turn
        )
      );
    } catch (err: any) {
      setTurns((current) =>
        current.map((turn) =>
          turn.id === id
            ? { id, question: normalized, error: err.message || "The archive could not be searched." }
            : turn
        )
      );
    } finally {
      setBusy(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const normalized = question.trim();
    if (!normalized) return;
    setQuestion("");
    run(normalized);
  }

  const samples = [
    {
      title: "Find a commitment",
      prompt: "What are the key terms in the contract?",
    },
    {
      title: "Collect related files",
      prompt: "Show me all invoices",
    },
    {
      title: "Brief the archive",
      prompt: "Summarize all uploaded documents",
    },
  ];

  return (
    <div className="ask-page">
      <PageHeader
        eyebrow="Grounded in your archive"
        title="Ask DocVault"
        description="Get one concise answer from the text in documents you are allowed to view. Context is preserved across your conversation turns."
        actions={
          <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
            {turns.length > 0 && (
              <button
                type="button"
                className="button secondary is-small"
                onClick={clearHistory}
                style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem" }}
              >
                <Plus size={15} />
                New Conversation
              </button>
            )}
            <StatusPill tone="success"><span className="status-dot" /> Sources included</StatusPill>
          </div>
        }
      />

      <section className="ask-workspace">
        <div className="ask-thread" ref={threadRef} aria-live="polite">
          {turns.length === 0 ? (
            <motion.div
              className="ask-empty"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ duration: 0.24 }}
            >
              <div className="ask-orbit" aria-hidden="true">
                <span><Sparkles size={23} /></span>
                <i />
                <i />
              </div>
              <h2>What do you need from the archive?</h2>
              <p>Ask about a document, compare records, or request a summary.</p>
              <div className="prompt-grid">
                {samples.map((sample) => (
                  <button key={sample.prompt} onClick={() => run(sample.prompt)}>
                    <span>
                      <small>{sample.title}</small>
                      <strong>{sample.prompt}</strong>
                    </span>
                    <ArrowRight size={17} />
                  </button>
                ))}
              </div>
            </motion.div>
          ) : (
            <div className="conversation">
              <AnimatePresence initial={false}>
                {turns.map((turn) => (
                  <motion.div
                    className="conversation-turn"
                    key={turn.id}
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                  >
                    <div className="user-message">
                      <span className="avatar is-small">PA</span>
                      <div>{turn.question}</div>
                    </div>

                    <div className="assistant-message">
                      <span className="assistant-avatar"><Bot size={18} /></span>
                      <div className="assistant-card">
                        {turn.pending && (
                          <div className="answer-loading">
                            <span>Searching your documents</span>
                            <i /><i /><i />
                          </div>
                        )}
                        {turn.error && <div className="notice is-danger">{turn.error}</div>}
                        {turn.response && (
                          <>
                            {turn.response.citations.length > 0 && turn.response.mode !== "notfound" && (
                              <div className="answer-sources">
                                <span>Sources</span>
                                <div>
                                  {uniqueCitations(turn.response.citations).map((citation) => (
                                    <Link key={citation.document_id} to={`/documents/${citation.document_id}`}>
                                      <FileText size={14} />
                                      {citation.title}
                                    </Link>
                                  ))}
                                </div>
                              </div>
                            )}
                            <div className="answer-body">
                              <AnswerMarkdown text={turn.response.answer} />
                            </div>
                            {(turn.response.images?.length ?? 0) > 0 && (
                              <div className="answer-images">
                                {turn.response.images.map((hit) => (
                                  <button
                                    key={hit.asset_id}
                                    type="button"
                                    className="answer-image"
                                    title={hit.title}
                                    onClick={() => setZoomed(hit)}
                                  >
                                    {previewUrls[hit.asset_id] ? (
                                      <img src={previewUrls[hit.asset_id]} alt={hit.title} loading="lazy" />
                                    ) : (
                                      <span className="answer-image-placeholder"><ImageIcon size={18} /></span>
                                    )}
                                    <small>{hit.page_number ? `${hit.title} — p.${hit.page_number}` : hit.title}</small>
                                  </button>
                                ))}
                              </div>
                            )}
                            {turn.response.mode !== "notfound" && (
                              <div className="answer-foot">
                                <Sparkles size={14} />
                                {turn.response.mode === "extractive"
                                  ? "Grounded extract from your documents"
                                  : "Grounded answer with document citations"}
                              </div>
                            )}
                          </>
                        )}
                      </div>
                    </div>
                  </motion.div>
                ))}
              </AnimatePresence>
            </div>
          )}
        </div>

        <form className="ask-composer" onSubmit={submit}>
          <label>
            <span className="sr-only">Ask a question about your documents</span>
            <input
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="Ask a question about your documents"
              disabled={busy}
            />
          </label>
          <motion.button
            className="button primary send-button"
            disabled={busy || !question.trim()}
            whileTap={busy ? undefined : { scale: 0.96 }}
            aria-label="Ask DocVault"
          >
            <Send size={18} />
          </motion.button>
          <small>Answers can only use documents you have permission to view.</small>
        </form>
      </section>

      <AnimatePresence>
        {zoomed && (
          <motion.div
            className="dialog-backdrop image-lightbox"
            role="dialog"
            aria-modal="true"
            aria-label={zoomed.title}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={() => setZoomed(null)}
          >
            <figure onClick={(event) => event.stopPropagation()}>
              <button
                type="button"
                className="icon-button dialog-close"
                onClick={() => setZoomed(null)}
                aria-label="Close image"
              >
                <X size={18} />
              </button>
              {previewUrls[zoomed.asset_id] ? (
                <img src={previewUrls[zoomed.asset_id]} alt={zoomed.title} />
              ) : (
                <span className="answer-image-placeholder"><ImageIcon size={28} /></span>
              )}
              <figcaption>
                <span>{zoomed.page_number ? `${zoomed.title} — p.${zoomed.page_number}` : zoomed.title}</span>
                <Link to={`/documents/${zoomed.document_id}`} onClick={() => setZoomed(null)}>
                  Open document
                </Link>
              </figcaption>
            </figure>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
