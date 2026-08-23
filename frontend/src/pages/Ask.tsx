import { Children, FormEvent, ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import {
  ArrowRight,
  Bot,
  Check,
  ChevronLeft,
  Cloud,
  Database,
  FileText,
  Folder,
  Image as ImageIcon,
  MessageSquare,
  Plus,
  Send,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import ReactMarkdown, { type Components } from "react-markdown";
import {
  ActiveScopeInfo,
  api,
  AskResponse,
  ConversationSummary,
  DocSummary,
  GoogleDriveStatus,
  VisualSearchHit,
  AskProviderInfo,
  AskRunMetrics,
} from "../api";
import { PageHeader, StatusPill } from "../components/ui";

interface Turn {
  id: string;
  question: string;
  response?: AskResponse;
  error?: string;
  pending?: boolean;
  providerChunks?: Record<string, string>;
  runMeta?: Record<string, { model_id: string; display_version: string; status?: string; metrics?: AskRunMetrics }>;
  chosenProvider?: string;
}

type MentionView = "none" | "choose_type" | "doc_list" | "class_list";

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

function uniqueCitations(citations?: AskResponse["citations"]) {
  if (!Array.isArray(citations)) return [];
  const seen = new Set<number>();
  return citations.filter((citation) => {
    if (!citation || citation.document_id == null) return false;
    if (seen.has(citation.document_id)) return false;
    seen.add(citation.document_id);
    return true;
  });
}

export default function Ask() {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [activeConvId, setActiveConvId] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);

  // Active retrieval scope (document & class filters)
  const [activeScope, setActiveScope] = useState<ActiveScopeInfo>({ documents: [], classes: [] });

  // Source selection states: by default Company KB is enabled; at least one must be selected
  const [modelRegistry, setModelRegistry] = useState<AskProviderInfo[]>([]);
  const [providerEnabled, setProviderEnabled] = useState<Record<string, boolean>>({});
  const [modelChoice, setModelChoice] = useState<Record<string, string>>({});
  const [companyKbEnabled, setCompanyKbEnabled] = useState(true);
  const [googleDriveEnabled, setGoogleDriveEnabled] = useState(false);
  const [driveStatus, setDriveStatus] = useState<GoogleDriveStatus>({ connected: false, email: null });

  useEffect(() => {
    api.getAskModels().then((r) => {
      setModelRegistry(r.providers);
      setProviderEnabled((cur) => {
        const next = { ...cur };
        r.providers.forEach((p) => { if (next[p.provider] === undefined) next[p.provider] = true; });
        return next;
      });
      setModelChoice((cur) => {
        const next = { ...cur };
        r.providers.forEach((p) => {
          if (!next[p.provider]) {
            const def = p.versions.find((v) => v.default) ?? p.versions[0];
            if (def) next[p.provider] = def.model_id;
          }
        });
        return next;
      });
    }).catch(() => {});
  }, []);

  // Available documents and classes for @ mentions
  const [availableDocs, setAvailableDocs] = useState<DocSummary[]>([]);
  const [availableClasses, setAvailableClasses] = useState<string[]>([]);

  // @ Mention Autocomplete state
  const [mentionView, setMentionView] = useState<MentionView>("none");
  const [mentionFilter, setMentionFilter] = useState("");
  const [mentionSelectedIndex, setMentionSelectedIndex] = useState(0);
  const [mentionTokenStart, setMentionTokenStart] = useState(-1);
  const inputRef = useRef<HTMLInputElement>(null);

  const [previewUrls, setPreviewUrls] = useState<Record<number, string>>({});
  const [zoomed, setZoomed] = useState<VisualSearchHit | null>(null);
  const requestedPreviews = useRef(new Set<number>());
  const createdPreviewUrls = useRef<string[]>([]);
  const threadRef = useRef<HTMLDivElement>(null);
  const reduceMotion = useReducedMotion();

  // Load Google Drive connection status
  useEffect(() => {
    api.googleDriveStatus().then(setDriveStatus).catch(() => {});
  }, []);

  // Load available documents and classes for @ mention autocomplete
  useEffect(() => {
    api.listDocuments().then((docs) => {
      setAvailableDocs(docs);
      const classes = new Set<string>();
      docs.forEach((d) => {
        if (d.doc_class) classes.add(d.doc_class);
      });
      api.listDocClasses().then((clsList) => {
        clsList.forEach((c) => classes.add(c.name));
        setAvailableClasses(Array.from(classes).sort());
      }).catch(() => {
        setAvailableClasses(Array.from(classes).sort());
      });
    }).catch(() => {});
  }, []);


  // Listen for OAuth completion from popup
  useEffect(() => {
    const handleOAuthMsg = (evt: MessageEvent) => {
      if (evt.data && evt.data.type === "GOOGLE_DRIVE_OAUTH" && evt.data.success) {
        api.googleDriveStatus().then(setDriveStatus).catch(() => {});
      }
    };
    window.addEventListener("message", handleOAuthMsg);
    return () => window.removeEventListener("message", handleOAuthMsg);
  }, []);


  // Load conversations on mount
  useEffect(() => {
    loadConversations();
  }, []);

  async function loadConversations() {
    try {
      const convList = await api.listConversations();
      setConversations(convList);
      if (convList.length > 0 && !activeConvId) {
        selectConversation(convList[0].id);
      }
    } catch {
      // Degrades gracefully in offline or stateless mode
    }
  }

  async function selectConversation(id: string) {
    setActiveConvId(id);
    setBusy(true);
    try {
      const detail = await api.getConversation(id);
      setActiveScope(detail.active_scope || { documents: [], classes: [] });
      setCompanyKbEnabled(detail.company_kb_enabled ?? true);
      setGoogleDriveEnabled(detail.google_drive_enabled ?? false);

      // Map conversation messages into turns
      const reconstructed: Turn[] = [];
      const msgs = detail.messages || [];
      for (let i = 0; i < msgs.length; i++) {
        if (msgs[i].role === "user") {
          const userMsg = msgs[i];
          const nextMsg = msgs[i + 1]?.role === "assistant" ? msgs[i + 1] : null;
          reconstructed.push({
            id: userMsg.id,
            question: userMsg.content,
            response: nextMsg
              ? {
                  question: userMsg.content,
                  answer: nextMsg.content,
                  mode: "rag",
                  model: null,
                  needs_clarification: false,
                  scoped_document_id: null,
                  citations: [],
                  candidates: [],
                  images: [],
                }
              : undefined,
          });
          if (nextMsg) i++;
        }
      }
      setTurns(reconstructed);
    } catch (err) {
      console.warn("Failed to load conversation details", err);
    } finally {
      setBusy(false);
    }
  }

  async function handleNewConversation() {
    try {
      const newConv = await api.createConversation({
        company_kb_enabled: true,
        google_drive_enabled: false,
      });
      setConversations((prev) => [newConv, ...prev]);
      setActiveConvId(newConv.id);
      setActiveScope({ documents: [], classes: [] });
      setCompanyKbEnabled(true);
      setGoogleDriveEnabled(false);
      setTurns([]);
    } catch {
      // In stateless fallback
      setActiveConvId(null);
      setActiveScope({ documents: [], classes: [] });
      setTurns([]);
    }
  }

  async function handleDeleteConversation(convId: string, event: React.MouseEvent) {
    event.stopPropagation();
    try {
      await api.deleteConversation(convId);
      const remaining = conversations.filter((c) => c.id !== convId);
      setConversations(remaining);
      if (activeConvId === convId) {
        if (remaining.length > 0) {
          selectConversation(remaining[0].id);
        } else {
          setActiveConvId(null);
          setTurns([]);
          setActiveScope({ documents: [], classes: [] });
        }
      }
    } catch (err) {
      console.warn("Failed to delete conversation", err);
    }
  }

  async function handleClearScope() {
    if (activeConvId) {
      try {
        await api.clearConversationScope(activeConvId);
      } catch {}
    }
    setActiveScope({ documents: [], classes: [] });
  }

  function removeDocScope(docId: number) {
    setActiveScope((prev) => ({
      ...prev,
      documents: prev.documents.filter((d) => d.document_id !== docId),
    }));
  }

  function removeClassScope(classId: number) {
    setActiveScope((prev) => ({
      ...prev,
      classes: prev.classes.filter((c) => c.class_id !== classId),
    }));
  }

  // Source toggle handlers ensuring at least one is selected
  function toggleCompanyKb() {
    if (companyKbEnabled && !googleDriveEnabled) {
      // Cannot turn off the only active source
      return;
    }
    setCompanyKbEnabled((v) => !v);
  }

  function toggleGoogleDrive() {
    if (!driveStatus.connected) {
      alert("Please connect your Google Drive account in the profile menu (bottom left) first.");
      return;
    }
    if (googleDriveEnabled && !companyKbEnabled) {
      // Cannot turn off the only active source
      return;
    }
    setGoogleDriveEnabled((v) => !v);
  }

  // Auto-scroll thread
  useEffect(() => {
    const thread = threadRef.current;
    if (!thread) return;
    thread.scrollTo({
      top: thread.scrollHeight,
      behavior: reduceMotion ? "auto" : "smooth",
    });
  }, [turns, reduceMotion]);

  // Image preview fetching
  useEffect(() => {
    const assetIds = turns
      .flatMap((turn) => turn.response?.images || [])
      .map((hit) => hit.asset_id)
      .filter((assetId) => !requestedPreviews.current.has(assetId));
    if (!assetIds.length) return;
    assetIds.forEach((assetId) => requestedPreviews.current.add(assetId));
    void Promise.all(
      assetIds.map(async (assetId) => {
        try {
          const url = URL.createObjectURL(await api.visualPreview(assetId));
          createdPreviewUrls.current.push(url);
          setPreviewUrls((current) => ({ ...current, [assetId]: url }));
        } catch {
          // Preview may disappear
        }
      })
    );
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

  async function run(value: string) {
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

    setTurns((current) => [...current, { id, question: normalized, pending: true, providerChunks: {} }]);

    try {
      const modelsPayload = modelRegistry.length > 0
        ? modelRegistry
            .filter((p) => providerEnabled[p.provider] !== false)
            .map((p) => ({ provider: p.provider, model_id: modelChoice[p.provider] ?? null }))
        : null;
      const res = await api.askStream(
        normalized,
        null,
        historyPayload.length > 0 ? historyPayload : undefined,
        activeConvId,
        companyKbEnabled,
        googleDriveEnabled,
        modelsPayload
      );
      
      const reader = res.body?.getReader();
      if (!reader) throw new Error("No readable stream");
      
      const decoder = new TextDecoder();
      let responseMeta: any = null;
      let chunks: Record<string, string> = {};
      let runMeta: Turn["runMeta"] = {};

      let buffer = "";
      let isDone = false;
      while (!isDone) {
        const { done, value } = await reader.read();
        if (done) break;
        
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          const trimmed = line.trim();
          if (trimmed.startsWith("data: ")) {
            const dataStr = trimmed.substring(6).trim();
            if (dataStr === "[DONE]") { isDone = true; break; }
            try {
              const data = JSON.parse(dataStr);
              if (data.type === "done") { isDone = true; break; }
              if (data.type === "meta") {
                responseMeta = data;
                if (data.active_scope && typeof data.active_scope === "object" && Array.isArray(data.active_scope.documents)) {
                  setActiveScope(data.active_scope);
                }
                if (data.conversation_id && !activeConvId) setActiveConvId(data.conversation_id);
                if (Array.isArray(data.providers) && data.providers.length > 0) {
                  data.providers.forEach((p: string) => {
                    if (chunks[p] === undefined) chunks[p] = "";
                  });
                }
                setTurns((current) =>
                  current.map((turn) =>
                    turn.id === id ? { 
                      ...turn, 
                      providerChunks: { ...chunks },
                      response: { ...data, answer: "" }
                    } : turn
                  )
                );
              } else if (data.type === "run_started" && data.provider) {
                runMeta![data.provider] = { model_id: data.model_id || "", display_version: data.display_version || "" };
                if (chunks[data.provider] === undefined) chunks[data.provider] = "";
                setTurns((current) =>
                  current.map((turn) =>
                    turn.id === id ? { ...turn, providerChunks: { ...chunks }, runMeta: { ...runMeta } } : turn
                  )
                );
              } else if (data.type === "run_completed" && data.provider) {
                runMeta![data.provider] = {
                  ...(runMeta![data.provider] || { model_id: data.model_id || "", display_version: "" }),
                  status: data.status,
                  metrics: data.metrics,
                };
                setTurns((current) =>
                  current.map((turn) =>
                    turn.id === id ? { ...turn, runMeta: { ...runMeta } } : turn
                  )
                );
              } else if (data.chunk !== undefined && data.provider) {
                chunks[data.provider] = (chunks[data.provider] || "") + data.chunk;
                setTurns((current) =>
                  current.map((turn) =>
                    turn.id === id ? { 
                      ...turn, 
                      providerChunks: { ...chunks },
                      response: responseMeta ? { ...responseMeta, answer: "" } : { citations: [], answer: "" }
                    } : turn
                  )
                );
              }
            } catch (e) {
              console.warn("Error parsing stream chunk:", e);
            }
          }
        }
      }

      setTurns((current) =>
        current.map((turn) => {
          if (turn.id !== id) return turn;
          const hasChunks = Object.values(chunks).some((c) => c && c.trim().length > 0);
          return {
            ...turn,
            pending: false,
            providerChunks: { ...chunks },
            response: {
              ...(turn.response || responseMeta || { citations: [] }),
              answer: turn.response?.answer || (hasChunks ? "" : "No response was generated by the configured LLM models."),
            },
          };
        })
      );
      api.listConversations().then(setConversations).catch(() => {});
    } catch (err: any) {
      setTurns((current) =>
        current.map((turn) => turn.id === id ? { id, question: normalized, error: err.message || "Search failed." } : turn)
      );
    } finally {
      setBusy(false);
    }
  }

  // ── @ Mention Detection & Autocomplete Handlers ────────────────────────────

  function detectMention(text: string, cursor: number) {
    const beforeCursor = text.slice(0, cursor);
    const atIndex = beforeCursor.lastIndexOf("@");
    if (atIndex === -1) {
      setMentionView("none");
      return;
    }

    // Ensure @ is at start of string or preceded by whitespace
    if (atIndex > 0 && !/\s/.test(beforeCursor[atIndex - 1])) {
      setMentionView("none");
      return;
    }

    const token = beforeCursor.slice(atIndex);

    // If the mention is already closed by '}' between '@' and cursor, do not show popup
    if (token.includes("}")) {
      setMentionView("none");
      return;
    }

    setMentionTokenStart(atIndex);

    // If token is doc mention e.g. "@doc:" or "@doc:{..."
    if (/^@doc:/i.test(token)) {
      const query = token.replace(/^@doc:\{?/i, "").trim().toLowerCase();
      setMentionView("doc_list");
      setMentionFilter(query);
      setMentionSelectedIndex(0);
    }
    // If token is class mention e.g. "@class:" or "@class:{..."
    else if (/^@class:/i.test(token)) {
      const query = token.replace(/^@class:\{?/i, "").trim().toLowerCase();
      setMentionView("class_list");
      setMentionFilter(query);
      setMentionSelectedIndex(0);
    }
    // Category picker (@, @d, @c, etc.) - only if no whitespace yet
    else if (!/\s/.test(token)) {
      const query = token.slice(1).trim().toLowerCase();
      setMentionView("choose_type");
      setMentionFilter(query);
      setMentionSelectedIndex(0);
    } else {
      setMentionView("none");
    }
  }


  function handleInputChange(e: React.ChangeEvent<HTMLInputElement>) {
    const val = e.target.value;
    setQuestion(val);
    const cursor = e.target.selectionStart ?? val.length;
    detectMention(val, cursor);
  }

  const categoryOptions = [
    { id: "doc", label: "Document", syntax: "@doc:", icon: FileText, hint: "Scope query to a specific document" },
    { id: "class", label: "Class", syntax: "@class:", icon: Folder, hint: "Scope query to a document category" },
  ].filter((c) => !mentionFilter || c.label.toLowerCase().includes(mentionFilter) || c.id.toLowerCase().includes(mentionFilter));

  const filteredDocs = availableDocs.filter((d) =>
    !mentionFilter ||
    d.title.toLowerCase().includes(mentionFilter) ||
    (d.doc_class && d.doc_class.toLowerCase().includes(mentionFilter))
  );

  const filteredClasses = availableClasses.filter((c) =>
    !mentionFilter || c.toLowerCase().includes(mentionFilter)
  );

  function insertMentionText(inserted: string) {
    if (mentionTokenStart === -1) return;
    const cursor = inputRef.current?.selectionStart ?? question.length;
    const before = question.slice(0, mentionTokenStart);
    const after = question.slice(cursor);
    const newText = `${before}${inserted}${after}`;
    setQuestion(newText);
    setMentionView("none");
    setTimeout(() => {
      if (inputRef.current) {
        inputRef.current.focus();
        const newPos = before.length + inserted.length;
        inputRef.current.setSelectionRange(newPos, newPos);
      }
    }, 10);
  }

  function selectCategory(id: "doc" | "class") {
    if (id === "doc") {
      setMentionView("doc_list");
      setMentionFilter("");
      setMentionSelectedIndex(0);
    } else {
      setMentionView("class_list");
      setMentionFilter("");
      setMentionSelectedIndex(0);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (mentionView === "none") return;

    if (e.key === "ArrowDown") {
      e.preventDefault();
      const count =
        mentionView === "choose_type"
          ? categoryOptions.length
          : mentionView === "doc_list"
          ? filteredDocs.length
          : filteredClasses.length;
      if (count > 0) {
        setMentionSelectedIndex((prev) => (prev + 1) % count);
      }
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      const count =
        mentionView === "choose_type"
          ? categoryOptions.length
          : mentionView === "doc_list"
          ? filteredDocs.length
          : filteredClasses.length;
      if (count > 0) {
        setMentionSelectedIndex((prev) => (prev - 1 + count) % count);
      }
    } else if (e.key === "Enter" || e.key === "Tab") {
      if (mentionView === "choose_type" && categoryOptions.length > 0) {
        e.preventDefault();
        selectCategory((categoryOptions[mentionSelectedIndex]?.id as "doc" | "class") || "doc");
      } else if (mentionView === "doc_list" && filteredDocs.length > 0) {
        e.preventDefault();
        const doc = filteredDocs[mentionSelectedIndex];
        if (doc) insertMentionText(`@doc:{${doc.title}} `);
      } else if (mentionView === "class_list" && filteredClasses.length > 0) {
        e.preventDefault();
        const cls = filteredClasses[mentionSelectedIndex];
        if (cls) insertMentionText(`@class:{${cls}} `);
      }
    } else if (e.key === "Escape") {
      e.preventDefault();
      setMentionView("none");
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (mentionView !== "none") {
      setMentionView("none");
    }
    const normalized = question.trim();
    if (!normalized) return;
    setQuestion("");
    run(normalized);
  }


  const samples = useMemo(() => {
    const docName = availableDocs[0]?.title || "TARRIF (1).pdf";
    const className = availableClasses[0] || "Policies";
    return [
      {
        title: "Tariff & Billing Rates",
        prompt: `@doc:{${docName}} What are the key billing slabs, fixed charges, and tax terms?`,
      },
      {
        title: "Google Drive QA Tracker",
        prompt: "What are the critical open bugs and issue summaries reported for Salesbot?",
      },
      {
        title: "Category Policy Scope",
        prompt: `@class:{${className}} Summarize the core compliance rules and operational requirements`,
      },
    ];
  }, [availableDocs, availableClasses]);


  return (
    <div className="ask-page">
      <PageHeader
        eyebrow="Grounded in your archive & connected sources"
        title="Ask DocVault"
        description="Ask questions and get verified answers with citations from documents you are authorized to view and connected cloud drives."
        actions={
          <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
            <StatusPill tone="success">
              <span className="status-dot" /> Sources strictly verified
            </StatusPill>
          </div>
        }
      />

      <div className="ask-container">
        {/* Left Sidebar: Conversations */}
        <aside className="ask-sidebar" aria-label="Conversation history">
          <div className="ask-sidebar-head">
            <button
              type="button"
              className="button primary is-small new-chat-button"
              onClick={handleNewConversation}
            >
              <Plus size={15} />
              <span>New Conversation</span>
            </button>
          </div>

          <div className="ask-sidebar-list">
            {conversations.length === 0 ? (
              <div style={{ padding: "16px 8px", fontSize: "12px", color: "var(--muted)", textAlign: "center" }}>
                No past conversations
              </div>
            ) : (
              conversations.map((conv) => (
                <div
                  key={conv.id}
                  className={`ask-conv-item ${conv.id === activeConvId ? "is-active" : ""}`}
                  onClick={() => selectConversation(conv.id)}
                >
                  <div className="ask-conv-title" title={conv.title}>
                    <MessageSquare size={14} style={{ flexShrink: 0 }} />
                    <span>{conv.title || "Untitled conversation"}</span>
                  </div>
                  <button
                    type="button"
                    className="ask-conv-del"
                    onClick={(e) => handleDeleteConversation(conv.id, e)}
                    title="Delete conversation"
                    aria-label="Delete conversation"
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              ))
            )}
          </div>
        </aside>

        {/* Main Workspace */}
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
                <p>Ask about a document, compare records, query Google Drive, or request a summary.</p>
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
                          {turn.pending && !turn.response && (
                            <div className="answer-loading">
                              <span>Searching authorized documents & sources</span>
                              <i /><i /><i />
                            </div>
                          )}
                          {turn.error && <div className="notice is-danger">{turn.error}</div>}
                          {turn.response && (
                            <>
                              {/* Company KB Sources - unchanged original design */}
                              {(turn.response.citations?.length ?? 0) > 0 && turn.response.mode !== "notfound" && (
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

                              {/* Google Drive Sources */}
                              {turn.response.drive_sources && turn.response.drive_sources.length > 0 && (
                                <div className="answer-sources" style={{ borderTop: turn.response.citations.length > 0 ? "1px solid var(--line)" : "none", paddingTop: turn.response.citations.length > 0 ? "8px" : "0" }}>
                                  <span>Google Drive Files ({turn.response.drive_sources.length})</span>
                                  <div>
                                    {turn.response.drive_sources.map((ds, idx) => (
                                      <a
                                        key={ds.id || idx}
                                        href={ds.webViewLink || "#"}
                                        target="_blank"
                                        rel="noopener noreferrer"
                                        style={{ borderColor: "rgba(16, 185, 129, 0.4)", color: "#34d399", background: "rgba(16, 185, 129, 0.08)" }}
                                      >
                                        <Cloud size={14} />
                                        {ds.name || "Drive Document"}
                                      </a>
                                    ))}
                                  </div>
                                </div>
                              )}

                              <div className="answer-body">
                                {(!turn.chosenProvider && turn.providerChunks && Object.keys(turn.providerChunks).length > 0) ? (
                                  <div
                                    className="llm-grid"
                                    style={{
                                      display: "grid",
                                      gridTemplateColumns:
                                        Object.keys(turn.providerChunks).length === 1
                                          ? "1fr"
                                          : Object.keys(turn.providerChunks).length === 2
                                          ? "repeat(2, 1fr)"
                                          : "repeat(auto-fit, minmax(300px, 1fr))",
                                      gap: "1rem",
                                      marginTop: "1rem",
                                    }}
                                  >
                                    {Object.entries(turn.providerChunks).map(([provider, chunk]) => (
                                      <div
                                        key={provider}
                                        className="llm-grid-cell"
                                        style={{
                                          border: "1px solid var(--line)",
                                          padding: "1.1rem",
                                          borderRadius: "10px",
                                          background: "var(--surface-50)",
                                          display: "flex",
                                          flexDirection: "column",
                                          boxShadow: "0 2px 8px rgba(0,0,0,0.03)",
                                        }}
                                      >
                                        <div
                                          style={{
                                            fontWeight: 600,
                                            fontSize: "0.95rem",
                                            marginBottom: "0.75rem",
                                            textTransform: "capitalize",
                                            color: "var(--brand)",
                                            display: "flex",
                                            alignItems: "center",
                                            justifyContent: "space-between",
                                            borderBottom: "1px solid var(--line)",
                                            paddingBottom: "0.4rem",
                                          }}
                                        >
                                          <span>
                                            {provider}
                                            {turn.runMeta?.[provider]?.display_version && (
                                              <span style={{ marginLeft: "0.45rem", fontSize: "0.7rem", letterSpacing: "0.06em", textTransform: "uppercase", color: "var(--text-muted)", fontWeight: 500 }}>
                                                {turn.runMeta[provider].display_version}
                                              </span>
                                            )}
                                          </span>
                                          {turn.pending && (
                                            <span style={{ fontSize: "0.75rem", color: "var(--text-muted)", fontWeight: "normal" }}>
                                              Streaming...
                                            </span>
                                          )}
                                        </div>
                                        <div style={{ flex: 1, overflowY: "auto", minHeight: "80px", maxHeight: "350px", fontSize: "0.92rem", lineHeight: "1.6" }}>
                                          {chunk ? (
                                            <AnswerMarkdown text={chunk} />
                                          ) : (
                                            <span style={{ color: "var(--text-muted)", fontStyle: "italic" }}>Waiting for response...</span>
                                          )}
                                        </div>
                                        {turn.runMeta?.[provider]?.metrics && (
                                          <div style={{ marginTop: "0.7rem", paddingTop: "0.5rem", borderTop: "1px dashed var(--line)", display: "flex", gap: "0.6rem", fontSize: "0.72rem", fontFamily: "IBM Plex Mono, monospace", color: "var(--text-muted)" }}>
                                            <span>{(turn.runMeta[provider].metrics!.tokens_in + turn.runMeta[provider].metrics!.tokens_out).toLocaleString()} tok{turn.runMeta[provider].metrics!.tokens_estimated ? "~" : ""}</span>
                                            <span>·</span>
                                            <span>${turn.runMeta[provider].metrics!.cost_usd.toFixed(4)}</span>
                                            <span>·</span>
                                            <span>{(turn.runMeta[provider].metrics!.latency_ms / 1000).toFixed(1)}s</span>
                                          </div>
                                        )}
                                        {!turn.pending && chunk && provider !== "Notice" && (
                                          <button
                                            type="button"
                                            className="button primary is-small"
                                            style={{ marginTop: "1rem", width: "100%", fontWeight: 600 }}
                                            onClick={async () => {
                                              if (!turn.response?.conversation_id) return;
                                              try {
                                                await api.selectAnswer(
                                                  turn.response.conversation_id, chunk, provider,
                                                  turn.runMeta?.[provider]?.model_id ?? null,
                                                  turn.runMeta?.[provider]?.metrics ?? null
                                                );
                                                setTurns((current) =>
                                                  current.map((t) =>
                                                    t.id === turn.id
                                                      ? { ...t, chosenProvider: provider, response: { ...t.response!, answer: chunk } }
                                                      : t
                                                  )
                                                );
                                              } catch (err) {
                                                console.error(err);
                                              }
                                            }}
                                          >
                                            Select {provider}
                                          </button>
                                        )}
                                      </div>
                                    ))}
                                  </div>
                                ) : (
                                  <>
                                    {turn.chosenProvider && (
                                      <div style={{ fontSize: "0.85rem", color: "var(--text-muted)", marginBottom: "0.5rem" }}>
                                        Answer selected from: <strong style={{ textTransform: "capitalize" }}>{turn.chosenProvider}</strong>
                                      </div>
                                    )}
                                    {turn.response?.answer ? (
                                      <AnswerMarkdown text={turn.response.answer} />
                                    ) : (
                                      turn.pending && (
                                        <div className="answer-loading" style={{ margin: "1rem 0" }}>
                                          <span>Generating answer from available LLM models</span>
                                          <i /><i /><i />
                                        </div>
                                      )
                                    )}
                                  </>
                                )}
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
                                    : turn.response.mode === "gdrive"
                                    ? "Grounded answer from Google Drive files"
                                    : turn.response.mode === "combined"
                                    ? "Synthesized answer from Company KB and Google Drive"
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

          {/* Models bar: enable/disable providers and pick versions for the next question */}
          {modelRegistry.length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "0.5rem", padding: "0.45rem 0.15rem" }}>
              <span style={{ fontSize: "0.68rem", fontFamily: "IBM Plex Mono, monospace", letterSpacing: "0.12em", color: "var(--text-muted)" }}>MODELS</span>
              {modelRegistry.map((p) => {
                const enabled = providerEnabled[p.provider] !== false;
                return (
                  <div
                    key={p.provider}
                    style={{
                      display: "flex", alignItems: "center", gap: "0.4rem",
                      border: `1px solid ${enabled ? p.color : "var(--line)"}`,
                      borderRadius: "999px", padding: "0.18rem 0.55rem",
                      opacity: enabled ? 1 : 0.55, background: "var(--surface-50)",
                    }}
                  >
                    <button
                      type="button"
                      onClick={() => setProviderEnabled((cur) => ({ ...cur, [p.provider]: !enabled }))}
                      title={enabled ? `Disable ${p.display_name}` : `Enable ${p.display_name}`}
                      style={{ border: "none", background: "none", cursor: "pointer", fontWeight: 600, fontSize: "0.78rem", color: enabled ? p.color : "var(--text-muted)", padding: 0 }}
                    >
                      {p.display_name}
                    </button>
                    <select
                      value={modelChoice[p.provider] ?? ""}
                      disabled={!enabled || p.versions.length < 2}
                      onChange={(e) => setModelChoice((cur) => ({ ...cur, [p.provider]: e.target.value }))}
                      style={{ border: "none", background: "transparent", fontSize: "0.72rem", fontFamily: "IBM Plex Mono, monospace", color: "var(--text-muted)", cursor: "pointer", maxWidth: "9.5rem" }}
                    >
                      {p.versions.map((v) => (
                        <option key={v.model_id} value={v.model_id}>{v.display_version}</option>
                      ))}
                    </select>
                  </div>
                );
              })}
            </div>
          )}

          {/* Scope Bar & Source Selector Toolbar above the Composer */}
          <div className="ask-scope-toolbar">
            <div className="ask-scope-chips">
              {(activeScope?.documents || []).map((doc) => (
                <span key={doc.document_id} className="scope-chip doc-chip">
                  <FileText size={13} style={{ flexShrink: 0 }} />
                  <span className="scope-chip-label" title={doc.title}>{doc.title}</span>
                  <button
                    type="button"
                    onClick={() => removeDocScope(doc.document_id)}
                    title="Remove document filter"
                    aria-label="Remove document filter"
                  >
                    <X size={12} />
                  </button>
                </span>
              ))}

              {(activeScope?.classes || []).map((cls) => (
                <span key={cls.class_id} className="scope-chip class-chip">
                  <Folder size={13} style={{ flexShrink: 0 }} />
                  <span className="scope-chip-label" title={cls.class_name}>{cls.class_name}</span>
                  <button
                    type="button"
                    onClick={() => removeClassScope(cls.class_id)}
                    title="Remove class filter"
                    aria-label="Remove class filter"
                  >
                    <X size={12} />
                  </button>
                </span>
              ))}

              {googleDriveEnabled && (
                <span className="scope-chip drive-chip">
                  <Cloud size={13} style={{ flexShrink: 0 }} />
                  <span className="scope-chip-label">Drive Active</span>
                </span>
              )}

              {((activeScope?.documents?.length ?? 0) > 0 || (activeScope?.classes?.length ?? 0) > 0) && (
                <button type="button" className="scope-clear-btn" onClick={handleClearScope}>
                  Clear filters
                </button>
              )}
            </div>

            {/* Source Selector Toggles */}
            <div className="ask-source-toggles">
              <button
                type="button"
                className={`source-toggle-btn ${companyKbEnabled ? "is-active" : ""}`}
                onClick={toggleCompanyKb}
                title={
                  !googleDriveEnabled
                    ? "Company KB is active (cannot disable when Drive is off)"
                    : "Toggle Company Knowledge Base"
                }
              >
                <Database size={13} />
                <span>Company KB</span>
                {companyKbEnabled && <Check size={12} />}
              </button>

              <button
                type="button"
                className={`source-toggle-btn ${googleDriveEnabled ? "is-active" : ""} ${!driveStatus.connected ? "is-disabled" : ""}`}
                onClick={toggleGoogleDrive}
                title={
                  !driveStatus.connected
                    ? "Connect Google Drive in the user profile menu (bottom left)"
                    : !companyKbEnabled
                    ? "Google Drive is active (cannot disable when KB is off)"
                    : "Toggle Google Drive source"
                }
              >
                <Cloud size={13} />
                <span>Google Drive</span>
                {googleDriveEnabled && <Check size={12} />}
                {!driveStatus.connected && <span className="source-badge">Connect in Profile</span>}
              </button>
            </div>
          </div>

          {/* Composer Form with @ Mention Autocomplete Popup */}
          <div className="ask-composer-wrapper">
            {mentionView !== "none" && (
              <div className="ask-mention-popup">
                <div className="ask-mention-header">
                  {mentionView === "choose_type" ? (
                    <span>Choose Scope Type</span>
                  ) : mentionView === "doc_list" ? (
                    <>
                      <button
                        type="button"
                        className="ask-mention-header-back"
                        onClick={() => setMentionView("choose_type")}
                      >
                        <ChevronLeft size={13} /> Back
                      </button>
                      <span>Select Document ({filteredDocs.length})</span>
                    </>
                  ) : (
                    <>
                      <button
                        type="button"
                        className="ask-mention-header-back"
                        onClick={() => setMentionView("choose_type")}
                      >
                        <ChevronLeft size={13} /> Back
                      </button>
                      <span>Select Class ({filteredClasses.length})</span>
                    </>
                  )}
                </div>

                <ul className="ask-mention-list" role="listbox">
                  {mentionView === "choose_type" &&
                    categoryOptions.map((opt, idx) => {
                      const Icon = opt.icon;
                      const isSelected = idx === mentionSelectedIndex;
                      return (
                        <li
                          key={opt.id}
                          className={`ask-mention-item ${isSelected ? "is-selected" : ""}`}
                          onClick={() => selectCategory(opt.id as "doc" | "class")}
                          onMouseEnter={() => setMentionSelectedIndex(idx)}
                          role="option"
                          aria-selected={isSelected}
                        >
                          <div className={`ask-mention-icon-box ${opt.id === "doc" ? "ask-mention-icon-doc" : "ask-mention-icon-class"}`}>
                            <Icon size={15} />
                          </div>
                          <div className="ask-mention-content">
                            <span className="ask-mention-title">{opt.label}</span>
                            <span className="ask-mention-subtitle">{opt.hint}</span>
                          </div>
                          <span className="ask-mention-badge">{opt.syntax}</span>
                        </li>
                      );
                    })}

                  {mentionView === "doc_list" && (
                    filteredDocs.length === 0 ? (
                      <div className="ask-mention-empty">No documents found matching "{mentionFilter}"</div>
                    ) : (
                      filteredDocs.map((doc, idx) => {
                        const isSelected = idx === mentionSelectedIndex;
                        return (
                          <li
                            key={doc.id}
                            className={`ask-mention-item ${isSelected ? "is-selected" : ""}`}
                            onClick={() => insertMentionText(`@doc:{${doc.title}} `)}
                            onMouseEnter={() => setMentionSelectedIndex(idx)}
                            role="option"
                            aria-selected={isSelected}
                          >
                            <div className="ask-mention-icon-box ask-mention-icon-doc">
                              <FileText size={15} />
                            </div>
                            <div className="ask-mention-content">
                              <span className="ask-mention-title" title={doc.title}>{doc.title}</span>
                              <span className="ask-mention-subtitle">{doc.doc_class || "Unclassified"}</span>
                            </div>
                            <span className="ask-mention-badge">DOC #{doc.id}</span>
                          </li>
                        );
                      })
                    )
                  )}

                  {mentionView === "class_list" && (
                    filteredClasses.length === 0 ? (
                      <div className="ask-mention-empty">No classes found matching "{mentionFilter}"</div>
                    ) : (
                      filteredClasses.map((cls, idx) => {
                        const isSelected = idx === mentionSelectedIndex;
                        const docCount = availableDocs.filter((d) => d.doc_class === cls).length;
                        return (
                          <li
                            key={cls}
                            className={`ask-mention-item ${isSelected ? "is-selected" : ""}`}
                            onClick={() => insertMentionText(`@class:{${cls}} `)}
                            onMouseEnter={() => setMentionSelectedIndex(idx)}
                            role="option"
                            aria-selected={isSelected}
                          >
                            <div className="ask-mention-icon-box ask-mention-icon-class">
                              <Folder size={15} />
                            </div>
                            <div className="ask-mention-content">
                              <span className="ask-mention-title">{cls}</span>
                              <span className="ask-mention-subtitle">{docCount} document{docCount === 1 ? "" : "s"}</span>
                            </div>
                            <span className="ask-mention-badge">CLASS</span>
                          </li>
                        );
                      })
                    )
                  )}
                </ul>
              </div>
            )}

            <form className="ask-composer" onSubmit={submit}>
              <label>
                <span className="sr-only">Ask a question about your documents</span>
                <input
                  ref={inputRef}
                  value={question}
                  onChange={handleInputChange}
                  onKeyDown={handleKeyDown}
                  placeholder="Ask a question, or use @ to filter by Document or Class..."
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
              <small>Answers can only use documents and sources you have verified permission to access.</small>
            </form>
          </div>

        </section>
      </div>

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
