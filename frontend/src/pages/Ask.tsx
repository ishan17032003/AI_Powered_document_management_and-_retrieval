import { FormEvent, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { ArrowRight, Bot, FileText, Send, Sparkles } from "lucide-react";
import { api, AskResponse } from "../api";
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

function uniqueCitations(citations: AskResponse["citations"]) {
  const seen = new Set<number>();
  return citations.filter((citation) => {
    if (seen.has(citation.document_id)) return false;
    seen.add(citation.document_id);
    return true;
  });
}

export default function Ask() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const threadRef = useRef<HTMLDivElement>(null);
  const reduceMotion = useReducedMotion();

  useEffect(() => {
    const thread = threadRef.current;
    if (!thread) return;
    thread.scrollTo({
      top: thread.scrollHeight,
      behavior: reduceMotion ? "auto" : "smooth",
    });
  }, [turns, reduceMotion]);

  async function run(value: string, documentId?: number) {
    const normalized = value.trim();
    if (!normalized || busy) return;
    const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    setBusy(true);
    setTurns((current) => [...current, { id, question: normalized, pending: true }]);

    try {
      const response = await api.ask(normalized, documentId);
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
        description="Get one concise answer from the text in documents you are allowed to view. For visual assets or rendered pages, use Search → Images or Pages."
        actions={<StatusPill tone="success"><span className="status-dot" /> Sources included</StatusPill>}
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
                              <AnswerText text={turn.response.answer} />
                            </div>
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
    </div>
  );
}
