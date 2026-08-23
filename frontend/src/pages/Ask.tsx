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
  getToken,
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
  runMeta?: Record<string, { model_id: string; display_version: string; status?: string; metrics?: AskRunMetrics; passedFrom?: string[] }>;
  toolEvents?: Record<string, { tool: string; label: string; summary?: string; status?: string; running: boolean; code?: string }[]>;
  artifacts?: Record<string, { id: string; name: string; mime: string; size: number }[]>;
  chosenProvider?: string;
  picked?: Record<string, boolean>;
  evidence?: { sources: string[]; routes: { kind: string; label: string }[]; grounding: string; drive_files: string[] };
  startedAt?: number;
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
  const [reasoningChoice, setReasoningChoice] = useState<Record<string, string>>({});
  const [askUsage, setAskUsage] = useState<Awaited<ReturnType<typeof api.getAskUsage>> | null>(null);
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
    api.getAskUsage().then(setAskUsage).catch(() => {});
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
  const [artifactPreview, setArtifactPreview] = useState<{ url: string; name: string; mime: string } | null>(null);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [comparisonLog, setComparisonLog] = useState<Awaited<ReturnType<typeof api.getConversationRuns>>["runs"] | null>(null);
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
    api.getAskUsage().then(setAskUsage).catch(() => {});
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

  function orderedConversations(): { conv: ConversationSummary; depth: number }[] {
    const byParent = new Map<string, ConversationSummary[]>();
    const byId = new Map(conversations.map((c) => [c.id, c]));
    const roots: ConversationSummary[] = [];
    for (const c of conversations) {
      const parent = (c.parent_ids ?? [])[0];
      if (parent && byId.has(parent)) {
        (byParent.get(parent) ?? byParent.set(parent, []).get(parent)!).push(c);
      } else {
        roots.push(c);
      }
    }
    const out: { conv: ConversationSummary; depth: number }[] = [];
    const visit = (c: ConversationSummary, depth: number) => {
      out.push({ conv: c, depth });
      for (const child of byParent.get(c.id) ?? []) visit(child, depth + 1);
    };
    roots.forEach((r) => visit(r, 0));
    return out;
  }

  async function selectConversation(id: string) {
    setActiveConvId(id);
    setBusy(true);
    try {
      const detail = await api.getConversation(id);
      // Branches carry their own model set (default: only the continued model);
      // root conversations reset to every provider enabled.
      if (detail.enabled_models && detail.enabled_models.length > 0) {
        const set = new Map(detail.enabled_models.map((m) => [m.provider, m]));
        setProviderEnabled(Object.fromEntries(modelRegistry.map((p) => [p.provider, set.has(p.provider)])));
        setModelChoice((cur) => {
          const next = { ...cur };
          set.forEach((m, prov) => { if (m.model_id) next[prov] = m.model_id; });
          return next;
        });
        setReasoningChoice((cur) => {
          const next = { ...cur };
          set.forEach((m, prov) => { next[prov] = m.reasoning ?? "none"; });
          return next;
        });
      } else {
        setProviderEnabled(Object.fromEntries(modelRegistry.map((p) => [p.provider, true])));
      }
      setActiveScope(detail.active_scope || { documents: [], classes: [] });
      setCompanyKbEnabled(detail.company_kb_enabled ?? true);
      setGoogleDriveEnabled(detail.google_drive_enabled ?? false);

      // Rebuild turns: pair user/assistant messages, then rehydrate the full
      // comparison grid (cards, tools, artifacts, metrics) from persisted runs.
      let runsByTurn = new Map<string, Awaited<ReturnType<typeof api.getConversationRuns>>["runs"]>();
      try {
        for (const r of (await api.getConversationRuns(id)).runs) {
          const key = r.turn_message_id || "";
          if (!runsByTurn.has(key)) runsByTurn.set(key, []);
          runsByTurn.get(key)!.push(r);
        }
      } catch { /* runs unavailable — degrade to plain history */ }

      const reconstructed: Turn[] = [];
      const msgs = detail.messages || [];
      for (let i = 0; i < msgs.length; i++) {
        if (msgs[i].role === "user") {
          const userMsg = msgs[i];
          const nextMsg = msgs[i + 1]?.role === "assistant" ? msgs[i + 1] : null;
          const turn: Turn = {
            id: userMsg.id,
            question: userMsg.content,
            startedAt: userMsg.created_at ? Date.parse(userMsg.created_at) : undefined,
            evidence: (userMsg as any).evidence ?? undefined,
            response: {
              question: userMsg.content,
              answer: nextMsg?.content ?? "",
              mode: "rag",
              model: null,
              needs_clarification: false,
              scoped_document_id: null,
              citations: [],
              candidates: [],
              images: [],
              conversation_id: id,
            } as any,
          };
          const runs = runsByTurn.get(userMsg.id) ?? [];
          if (runs.length > 0) {
            turn.providerChunks = {};
            turn.runMeta = {};
            turn.toolEvents = {};
            turn.artifacts = {};
            for (const r of runs) {
              // Later runs of the same provider (regenerate/second pass) win.
              turn.providerChunks[r.provider] = r.body;
              turn.runMeta[r.provider] = {
                model_id: r.model_id,
                display_version: r.display_version,
                status: r.status,
                metrics: r.metrics,
                passedFrom: r.passed_from?.length ? r.passed_from : undefined,
              };
              turn.toolEvents[r.provider] = (r.tool_events ?? [])
                .filter((t) => t.type === "tool_result")
                .map((t) => ({ tool: t.tool, label: t.label || t.tool, summary: t.summary, status: t.status, running: false }));
              turn.artifacts[r.provider] = r.artifacts ?? [];
              if (r.selected) turn.chosenProvider = r.provider;
            }
            if (!turn.chosenProvider && nextMsg && (nextMsg as any).provider) {
              turn.chosenProvider = (nextMsg as any).provider;
            }
          }
          reconstructed.push(turn);
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

  async function openArtifact(a: { id: string; name: string; mime: string }) {
    try {
      const headers = new Headers();
      const token = getToken();
      if (token) headers.set("Authorization", `Bearer ${token}`);
      const res = await fetch(`/api/v1/search/ask/artifacts/${encodeURIComponent(a.id)}/preview`, { headers });
      if (!res.ok) throw new Error(`Preview failed (${res.status})`);
      const blob = await res.blob();
      // SECURITY: never window.open a blob of model-generated HTML — a blob URL
      // inherits the app origin. Render inside a sandboxed iframe (opaque
      // origin, no storage/cookie access) instead.
      setArtifactPreview({ url: URL.createObjectURL(blob), name: a.name, mime: a.mime });
    } catch (err) {
      console.error(err);
    }
  }

  function closeArtifactPreview() {
    if (artifactPreview) URL.revokeObjectURL(artifactPreview.url);
    setArtifactPreview(null);
  }

  async function copyText(key: string, text: string) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    setCopiedKey(key);
    setTimeout(() => setCopiedKey((cur) => (cur === key ? null : cur)), 1600);
  }

  function providerColor(provider: string): string | undefined {
    return modelRegistry.find((p) => p.provider === provider)?.color;
  }

  async function rerunModels(turn: Turn, targets: string[]) {
    const convId = turn.response?.conversation_id || activeConvId;
    if (!convId || busy || targets.length === 0) return;
    setBusy(true);
    setTurns((current) =>
      current.map((t) => {
        if (t.id !== turn.id) return t;
        const next = { ...t, pending: true };
        for (const target of targets) {
          next.providerChunks = { ...next.providerChunks, [target]: "" };
          next.toolEvents = { ...next.toolEvents, [target]: [] };
          next.artifacts = { ...next.artifacts, [target]: [] };
          const prev = next.runMeta?.[target];
          next.runMeta = { ...next.runMeta, [target]: { model_id: prev?.model_id ?? "", display_version: prev?.display_version ?? "", metrics: undefined, status: undefined } };
        }
        return next;
      })
    );
    try {
      const res = await api.askStream(
        turn.question, null, undefined, convId, companyKbEnabled, googleDriveEnabled,
        targets.map((t) => ({ provider: t, model_id: modelChoice[t] ?? null, reasoning: reasoningChoice[t] ?? null })),
        null, true
      );
      const reader = res.body?.getReader();
      if (!reader) throw new Error("No readable stream");
      const decoder = new TextDecoder();
      let buffer = "";
      let done = false;
      while (!done) {
        const { done: rDone, value } = await reader.read();
        if (rDone) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed.startsWith("data: ")) continue;
          let data: any;
          try { data = JSON.parse(trimmed.substring(6)); } catch { continue; }
          if (data.type === "done") { done = true; break; }
          const target = data.provider;
          if (!target || !targets.includes(target)) continue;
          setTurns((current) =>
            current.map((t) => {
              if (t.id !== turn.id) return t;
              const next = { ...t };
              if (data.type === "chunk") {
                next.providerChunks = { ...next.providerChunks, [target]: (next.providerChunks?.[target] ?? "") + data.chunk };
              } else if (data.type === "run_started") {
                next.runMeta = { ...next.runMeta, [target]: { model_id: data.model_id || "", display_version: data.display_version || "" } };
              } else if (data.type === "run_completed") {
                next.runMeta = { ...next.runMeta, [target]: { ...(next.runMeta?.[target] as any), status: data.status, metrics: data.metrics } };
              } else if (data.type === "tool_started") {
                next.toolEvents = { ...next.toolEvents, [target]: [...(next.toolEvents?.[target] ?? []), { tool: data.tool, label: data.label || data.tool, running: true }] };
              } else if (data.type === "tool_result") {
                const list = [...(next.toolEvents?.[target] ?? [])];
                const open = [...list].reverse().find((x) => x.running && x.tool === data.tool);
                if (open) { open.running = false; open.summary = data.summary; open.status = data.status; }
                next.toolEvents = { ...next.toolEvents, [target]: list };
              } else if (data.type === "artifact") {
                next.artifacts = { ...next.artifacts, [target]: [...(next.artifacts?.[target] ?? []), { id: data.id, name: data.name, mime: data.mime, size: data.size }] };
              }
              return next;
            })
          );
        }
      }
    } catch (err) {
      console.error(err);
    } finally {
      setTurns((current) => current.map((t) => (t.id === turn.id ? { ...t, pending: false } : t)));
      setBusy(false);
      api.getAskUsage().then(setAskUsage).catch(() => {});
    }
  }

  async function continueWith(turn: Turn, provider: string, content: string) {
    const convId = turn.response?.conversation_id || activeConvId;
    if (!convId) return;
    // Merge-branch (spec §2.1): picked answers + the continued one form the sources.
    const pickedIds = Object.keys(turn.picked ?? {}).filter((k) => turn.picked![k] && k !== provider);
    const sources = [
      { provider, model_id: turn.runMeta?.[provider]?.model_id ?? null, content },
      ...pickedIds
        .map((p) => ({ provider: p, model_id: turn.runMeta?.[p]?.model_id ?? null, content: turn.providerChunks?.[p] ?? "" }))
        .filter((p) => p.content),
    ];
    try {
      // The branch defaults to ONLY the continued model(s): one response per
      // question until the user re-enables more models.
      const branchModels = sources.map((src) => ({
        provider: src.provider,
        model_id: src.model_id ?? modelChoice[src.provider] ?? null,
        reasoning: reasoningChoice[src.provider] ?? null,
      }));
      const res = await api.branchConversation(convId, sources, branchModels);
      setTurns((current) =>
        current.map((t) =>
          t.id === turn.id ? { ...t, chosenProvider: provider, picked: {}, response: { ...t.response!, answer: content } } : t
        )
      );
      const convList = await api.listConversations();
      setConversations(convList);
      await selectConversation(res.conversation_id);
    } catch (err) {
      console.error(err);
    }
  }

  async function secondPass(turn: Turn, target: string) {
    const convId = turn.response?.conversation_id || activeConvId;
    if (!convId || busy) return;
    const pickedIds = Object.keys(turn.picked ?? {}).filter((k) => turn.picked![k]);
    const passed = pickedIds
      .map((p) => ({ provider: p, model_id: turn.runMeta?.[p]?.model_id ?? null, content: turn.providerChunks?.[p] ?? "" }))
      .filter((p) => p.content);
    if (passed.length === 0) return;
    setBusy(true);
    // Reset the target card and stream the re-run into it.
    setTurns((current) =>
      current.map((t) =>
        t.id === turn.id
          ? {
              ...t,
              picked: {},
              providerChunks: { ...t.providerChunks, [target]: "" },
              runMeta: { ...t.runMeta, [target]: { model_id: "", display_version: "", passedFrom: pickedIds } },
              toolEvents: { ...t.toolEvents, [target]: [] },
              artifacts: { ...t.artifacts, [target]: [] },
            }
          : t
      )
    );
    try {
      const res = await api.askStream(
        turn.question, null, undefined, convId, companyKbEnabled, googleDriveEnabled,
        [{ provider: target, model_id: modelChoice[target] ?? null, reasoning: reasoningChoice[target] ?? null }],
        passed
      );
      const reader = res.body?.getReader();
      if (!reader) throw new Error("No readable stream");
      const decoder = new TextDecoder();
      let buffer = "";
      let done = false;
      while (!done) {
        const { done: rDone, value } = await reader.read();
        if (rDone) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed.startsWith("data: ")) continue;
          let data: any;
          try { data = JSON.parse(trimmed.substring(6)); } catch { continue; }
          if (data.type === "done") { done = true; break; }
          if (data.provider !== target) continue;
          setTurns((current) =>
            current.map((t) => {
              if (t.id !== turn.id) return t;
              const next = { ...t };
              if (data.type === "chunk") {
                next.providerChunks = { ...next.providerChunks, [target]: (next.providerChunks?.[target] ?? "") + data.chunk };
              } else if (data.type === "run_started") {
                next.runMeta = { ...next.runMeta, [target]: { ...(next.runMeta?.[target] as any), model_id: data.model_id || "", display_version: data.display_version || "", passedFrom: pickedIds } };
              } else if (data.type === "run_completed") {
                next.runMeta = { ...next.runMeta, [target]: { ...(next.runMeta?.[target] as any), status: data.status, metrics: data.metrics } };
              } else if (data.type === "tool_started") {
                const list = [...(next.toolEvents?.[target] ?? [])];
                list.push({ tool: data.tool, label: data.label || data.tool, running: true });
                next.toolEvents = { ...next.toolEvents, [target]: list };
              } else if (data.type === "tool_result") {
                const list = [...(next.toolEvents?.[target] ?? [])];
                const open = [...list].reverse().find((x) => x.running && x.tool === data.tool);
                if (open) { open.running = false; open.summary = data.summary; open.status = data.status; }
                next.toolEvents = { ...next.toolEvents, [target]: list };
              } else if (data.type === "artifact") {
                next.artifacts = { ...next.artifacts, [target]: [...(next.artifacts?.[target] ?? []), { id: data.id, name: data.name, mime: data.mime, size: data.size }] };
              }
              return next;
            })
          );
        }
      }
    } catch (err) {
      console.error(err);
    } finally {
      setBusy(false);
    }
  }

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

    setTurns((current) => [...current, { id, question: normalized, pending: true, providerChunks: {}, startedAt: Date.now() }]);

    try {
      const modelsPayload = modelRegistry.length > 0
        ? modelRegistry
            .filter((p) => providerEnabled[p.provider] !== false)
            .map((p) => ({ provider: p.provider, model_id: modelChoice[p.provider] ?? null, reasoning: reasoningChoice[p.provider] ?? null }))
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
      let toolEvents: NonNullable<Turn["toolEvents"]> = {};
      let artifacts: NonNullable<Turn["artifacts"]> = {};

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
                if (data.evidence) {
                  setTurns((current) =>
                    current.map((turn) => (turn.id === id ? { ...turn, evidence: data.evidence } : turn))
                  );
                }
                if (data.active_scope && typeof data.active_scope === "object" && Array.isArray(data.active_scope.documents)) {
                  setActiveScope(data.active_scope);
                }
                if (data.conversation_id && !activeConvId) setActiveConvId(data.conversation_id);
                {
                  const runProviders = Array.isArray(data.run_providers) && data.run_providers.length > 0
                    ? data.run_providers
                    : (Array.isArray(data.providers) ? data.providers : []);
                  runProviders.forEach((p: string) => {
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
              } else if (data.type === "tool_started" && data.provider) {
                const list = toolEvents[data.provider] ?? (toolEvents[data.provider] = []);
                list.push({ tool: data.tool, label: data.label || data.tool, running: true });
                setTurns((current) =>
                  current.map((turn) =>
                    turn.id === id ? { ...turn, toolEvents: { ...toolEvents } } : turn
                  )
                );
              } else if (data.type === "tool_progress" && data.provider) {
                const list = toolEvents[data.provider] ?? (toolEvents[data.provider] = []);
                const open = [...list].reverse().find((t) => t.running && t.tool === data.tool);
                if (open) {
                  open.code = ((open.code ?? "") + (data.text ?? "")).slice(-2000);
                  setTurns((current) =>
                    current.map((turn) =>
                      turn.id === id ? { ...turn, toolEvents: { ...toolEvents } } : turn
                    )
                  );
                }
              } else if (data.type === "tool_result" && data.provider) {
                const list = toolEvents[data.provider] ?? (toolEvents[data.provider] = []);
                const open = [...list].reverse().find((t) => t.running && t.tool === data.tool);
                if (open) { open.running = false; open.summary = data.summary; open.status = data.status; }
                else list.push({ tool: data.tool, label: data.label || data.tool, summary: data.summary, status: data.status, running: false });
                setTurns((current) =>
                  current.map((turn) =>
                    turn.id === id ? { ...turn, toolEvents: { ...toolEvents } } : turn
                  )
                );
              } else if (data.type === "artifact" && data.provider) {
                (artifacts[data.provider] ?? (artifacts[data.provider] = [])).push({ id: data.id, name: data.name, mime: data.mime, size: data.size });
                setTurns((current) =>
                  current.map((turn) =>
                    turn.id === id ? { ...turn, artifacts: { ...artifacts } } : turn
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
      api.getAskUsage().then(setAskUsage).catch(() => {});
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
              orderedConversations().map(({ conv, depth }) => (
                <div
                  key={conv.id}
                  className={`ask-conv-item ${conv.id === activeConvId ? "is-active" : ""}`}
                  onClick={() => selectConversation(conv.id)}
                  style={depth > 0 ? { marginLeft: `${Math.min(depth, 4) * 14}px` } : undefined}
                >
                  <div className="ask-conv-title" title={conv.title}>
                    {depth > 0 ? (
                      <span style={{ flexShrink: 0, color: "var(--brand)", fontSize: "13px" }}>↳</span>
                    ) : (
                      <span style={{ flexShrink: 0, color: "var(--text-muted)", fontSize: "12px" }}>☐</span>
                    )}
                    <span>
                      {conv.title || "Untitled conversation"}
                      {depth > 0 && (conv.branched_from?.length ?? 0) > 0 && (
                        <span style={{ display: "block", fontSize: "9px", letterSpacing: "0.08em", color: "var(--text-muted)", fontFamily: "IBM Plex Mono, monospace", textTransform: "uppercase" }}>
                          continued from {conv.branched_from!.map((b) => b.provider).filter(Boolean).join(" + ")}
                        </span>
                      )}
                    </span>
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
                        <div>
                          {turn.question}
                          {(() => {
                            const n = Object.keys(turn.providerChunks ?? {}).filter((k) => k !== "Notice").length;
                            if (n === 0) return null;
                            const when = turn.startedAt
                              ? new Date(turn.startedAt).toLocaleString(undefined, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).toUpperCase()
                              : "";
                            const state = turn.pending
                              ? `RUNNING ${n} MODEL${n > 1 ? "S" : ""} IN PARALLEL`
                              : `${n} MODEL${n > 1 ? "S" : ""} RUN${turn.chosenProvider ? " · 1 SELECTED" : ""}`;
                            return (
                              <div style={{ fontFamily: "IBM Plex Mono, monospace", fontSize: "0.66rem", letterSpacing: "0.06em", color: "var(--text-muted)", marginTop: "0.25rem", fontWeight: 400 }}>
                                {when && `${when} · `}{state}
                              </div>
                            );
                          })()}
                        </div>
                      </div>

                      <div className="assistant-message">
                        <span className="assistant-avatar"><Bot size={18} /></span>
                        <div className="assistant-card">
                          {turn.evidence && (turn.evidence.grounding || turn.evidence.sources.length > 0 || (turn.evidence.routes?.[0]?.label ?? "") !== "0 passages") && (
                            <div style={{ border: "1px solid var(--line)", borderRadius: "9px", padding: "0.65rem 0.8rem", marginBottom: "0.8rem", background: "var(--surface-50)" }}>
                              <div style={{ fontFamily: "IBM Plex Mono, monospace", fontSize: "0.64rem", letterSpacing: "0.14em", color: "var(--text-muted)" }}>EVIDENCE PACK</div>
                              <div style={{ display: "flex", flexWrap: "wrap", gap: "0.4rem", marginTop: "0.45rem", alignItems: "center" }}>
                                {turn.evidence.sources.map((src) => (
                                  <span key={src} style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem", border: "1px solid var(--line)", background: "var(--surface, #fff)", borderRadius: "7px", padding: "0.25rem 0.55rem", fontSize: "0.74rem", fontWeight: 500 }}>
                                    <span style={{ color: "var(--brand)" }}>▣</span> {src}
                                  </span>
                                ))}
                                {turn.evidence.routes.map((r, i) => (
                                  <span key={i} style={{ border: "1px solid var(--line)", borderRadius: "999px", padding: "0.2rem 0.6rem", fontSize: "0.68rem", fontWeight: 600, color: "var(--brand)" }}>
                                    <span style={{ fontFamily: "IBM Plex Mono, monospace", opacity: 0.75, marginRight: "0.3rem" }}>{r.kind}</span>{r.label}
                                  </span>
                                ))}
                                {turn.evidence.drive_files.map((f) => (
                                  <span key={f} style={{ border: "1px solid var(--line)", borderRadius: "7px", padding: "0.25rem 0.55rem", fontSize: "0.74rem" }}>☁ {f}</span>
                                ))}
                              </div>
                              {turn.evidence.grounding && (
                                <details style={{ marginTop: "0.5rem" }}>
                                  <summary style={{ cursor: "pointer", fontFamily: "IBM Plex Mono, monospace", fontSize: "0.64rem", letterSpacing: "0.12em", color: "var(--text-muted)" }}>
                                    RAG CONTEXT · sent to every enabled model
                                  </summary>
                                  <div style={{ fontSize: "0.8rem", lineHeight: 1.55, color: "var(--text-muted)", marginTop: "0.4rem" }}>{turn.evidence.grounding}…</div>
                                  <div style={{ fontFamily: "IBM Plex Mono, monospace", fontSize: "0.62rem", color: "var(--text-muted)", marginTop: "0.35rem" }}>
                                    Identical context per model — retrieval only, no direct store access.
                                  </div>
                                </details>
                              )}
                            </div>
                          )}
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
                                  <>
                                  {(() => {
                                    const ids = Object.keys(turn.providerChunks!).filter((k) => k !== "Notice");
                                    if (ids.length === 0) return null;
                                    const allPicked = ids.every((k) => turn.picked?.[k]);
                                    return (
                                      <div style={{ display: "flex", alignItems: "center", gap: "0.6rem", marginTop: "0.9rem" }}>
                                        <span style={{ fontFamily: "IBM Plex Mono, monospace", fontSize: "0.66rem", letterSpacing: "0.14em", color: "var(--text-muted)" }}>
                                          {ids.length} ANSWER{ids.length > 1 ? "S" : ""}
                                        </span>
                                        <span style={{ flex: 1 }} />
                                        {!turn.pending && (
                                          <>
                                            <button
                                              type="button"
                                              className="button is-small"
                                              onClick={() =>
                                                setTurns((current) =>
                                                  current.map((t) =>
                                                    t.id === turn.id
                                                      ? { ...t, picked: allPicked ? {} : Object.fromEntries(ids.map((k) => [k, true])) }
                                                      : t
                                                  )
                                                )
                                              }
                                            >
                                              {allPicked ? "Clear picks" : "Pick all"}
                                            </button>
                                            <button type="button" className="button is-small" onClick={() => rerunModels(turn, ids)}>
                                              ↻ Re-run all
                                            </button>
                                          </>
                                        )}
                                      </div>
                                    );
                                  })()}
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
                                        draggable={!turn.pending && provider !== "Notice"}
                                        onDragStart={(e) => { e.dataTransfer.setData("text/x-docvault-provider", provider); e.dataTransfer.effectAllowed = "move"; }}
                                        onDragOver={(e) => { e.preventDefault(); }}
                                        onDrop={(e) => {
                                          e.preventDefault();
                                          const from = e.dataTransfer.getData("text/x-docvault-provider");
                                          if (from && from !== provider) {
                                            setTurns((current) => current.map((t) => (t.id === turn.id ? { ...t, picked: { [from]: true } } : t)));
                                            setTimeout(() => secondPass({ ...turn, picked: { [from]: true } }, provider), 0);
                                          }
                                        }}
                                        style={{
                                          border: "1px solid var(--line)",
                                          borderTop: `3px solid ${providerColor(provider) ?? "var(--line)"}`,
                                          cursor: turn.pending ? undefined : "grab",
                                          minWidth: 0,
                                          maxWidth: "100%",
                                          overflow: "hidden",
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
                                        {(turn.runMeta?.[provider]?.passedFrom?.length ?? 0) > 0 && (
                                          <div style={{ marginBottom: "0.5rem", fontSize: "0.68rem", letterSpacing: "0.08em", textTransform: "uppercase", fontFamily: "IBM Plex Mono, monospace", color: "var(--brand)", background: "var(--surface-100, rgba(37,99,235,0.07))", border: "1px dashed var(--brand)", borderRadius: "6px", padding: "0.3rem 0.5rem" }}>
                                            Passed from {turn.runMeta![provider].passedFrom!.join(" + ")}
                                          </div>
                                        )}
                                        {(turn.toolEvents?.[provider]?.length ?? 0) > 0 && (
                                          <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem", marginBottom: "0.6rem" }}>
                                            {turn.toolEvents![provider].map((t, ti) => (
                                              <div key={ti} style={{ fontSize: "0.72rem", fontFamily: "IBM Plex Mono, monospace", color: t.status === "error" ? "var(--danger, #b3261e)" : "var(--text-muted)", background: "var(--surface-100, rgba(0,0,0,0.03))", border: "1px solid var(--line)", borderRadius: "6px", padding: "0.28rem 0.5rem", minWidth: 0, maxWidth: "100%", overflow: "hidden" }}>
                                                <div style={{ display: "flex", alignItems: "center", gap: "0.45rem" }}>
                                                  <span>{t.tool === "search_documents" ? "⌕" : t.tool === "execute_python" ? "▶" : "≣"}</span>
                                                  <span style={{ fontWeight: 600 }}>{t.label}</span>
                                                  <span style={{ marginLeft: "auto", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "60%" }}>{t.running ? (t.code ? "writing code…" : "running…") : t.summary}</span>
                                                </div>
                                                {t.running && t.code && (
                                                  <div
                                                    style={{
                                                      marginTop: "0.3rem",
                                                      height: "3.2em",
                                                      overflow: "hidden",
                                                      display: "flex",
                                                      flexDirection: "column",
                                                      justifyContent: "flex-end",
                                                      whiteSpace: "pre",
                                                      minWidth: 0,
                                                      maxWidth: "100%",
                                                      opacity: 0.9,
                                                      WebkitMaskImage: "linear-gradient(to bottom, transparent 0%, black 45%, black 80%, rgba(0,0,0,0.35) 100%)",
                                                      maskImage: "linear-gradient(to bottom, transparent 0%, black 45%, black 80%, rgba(0,0,0,0.35) 100%)",
                                                    }}
                                                  >
                                                    {t.code.replace(/\\n/g, "\n").split("\n").filter(Boolean).slice(-3).map((ln, li) => (
                                                      <div key={li} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "100%" }}>{ln.slice(0, 300)}</div>
                                                    ))}
                                                  </div>
                                                )}
                                              </div>
                                            ))}
                                          </div>
                                        )}
                                        {(turn.artifacts?.[provider]?.length ?? 0) > 0 && (
                                          <div style={{ display: "flex", flexWrap: "wrap", gap: "0.4rem", marginBottom: "0.6rem" }}>
                                            {turn.artifacts![provider].map((a) => (
                                              <button
                                                key={a.id}
                                                type="button"
                                                onClick={() => openArtifact(a)}
                                                title={`${a.name} · ${(a.size / 1024).toFixed(1)} KB`}
                                                style={{ display: "flex", alignItems: "center", gap: "0.4rem", border: "1px solid var(--brand)", background: "var(--surface-50)", color: "var(--brand)", borderRadius: "7px", padding: "0.3rem 0.6rem", fontSize: "0.76rem", fontWeight: 600, cursor: "pointer" }}
                                              >
                                                <span>▤</span>
                                                <span style={{ maxWidth: "11rem", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{a.name}</span>
                                                <span style={{ fontWeight: 400, color: "var(--text-muted)" }}>Open</span>
                                              </button>
                                            ))}
                                          </div>
                                        )}
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
                                          <div style={{ marginTop: "1rem", display: "flex", gap: "0.5rem", alignItems: "center" }}>
                                            <button
                                              type="button"
                                              className="button is-small"
                                              title="Copy answer"
                                              onClick={() => copyText(`${turn.id}:${provider}`, chunk)}
                                            >
                                              {copiedKey === `${turn.id}:${provider}` ? "✓ copied" : "⧉ copy"}
                                            </button>
                                            <label style={{ display: "flex", alignItems: "center", gap: "0.3rem", fontSize: "0.75rem", color: "var(--text-muted)", cursor: "pointer", userSelect: "none" }}>
                                              <input
                                                type="checkbox"
                                                checked={!!turn.picked?.[provider]}
                                                onChange={(e) =>
                                                  setTurns((current) =>
                                                    current.map((t) =>
                                                      t.id === turn.id ? { ...t, picked: { ...t.picked, [provider]: e.target.checked } } : t
                                                    )
                                                  )
                                                }
                                              />
                                              pick
                                            </label>
                                            <button
                                              type="button"
                                              className="button primary is-small"
                                              style={{ flex: 1, fontWeight: 600 }}
                                              onClick={() => continueWith(turn, provider, chunk)}
                                            >
                                              Continue with {provider}
                                            </button>
                                            <button
                                              type="button"
                                              className="button is-small"
                                              title={`Regenerate ${provider}`}
                                              onClick={() => rerunModels(turn, [provider])}
                                            >
                                              ↻
                                            </button>
                                          </div>
                                        )}
                                      </div>
                                    ))}
                                  </div>
                                  {(() => {
                                    const pickedIds = Object.keys(turn.picked ?? {}).filter((k) => turn.picked![k]);
                                    if (turn.pending || pickedIds.length === 0) return null;
                                    const targets = Object.keys(turn.providerChunks ?? {}).filter(
                                      (t) => t !== "Notice" && !pickedIds.includes(t)
                                    );
                                    return (
                                      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginTop: "0.7rem", flexWrap: "wrap", fontSize: "0.8rem" }}>
                                        <span style={{ color: "var(--text-muted)" }}>
                                          {pickedIds.length} picked ({pickedIds.join(", ")}) —
                                        </span>
                                        <span style={{ color: "var(--text-muted)", fontSize: "0.7rem", width: "100%", order: 99 }}>
                                          Tip: you can also drag a card onto another model to pass its answer over.
                                        </span>
                                        {targets.map((t) => (
                                          <button key={t} type="button" className="button is-small" onClick={() => secondPass(turn, t)}>
                                            Send to {t}
                                          </button>
                                        ))}
                                        <button
                                          type="button"
                                          className="button is-small"
                                          onClick={() =>
                                            setTurns((current) => current.map((x) => (x.id === turn.id ? { ...x, picked: {} } : x)))
                                          }
                                        >
                                          Clear
                                        </button>
                                      </div>
                                    );
                                  })()}
                                </>
                                ) : (
                                  <>
                                    {turn.chosenProvider && (
                                      <div style={{ fontSize: "0.85rem", color: "var(--text-muted)", marginBottom: "0.5rem", display: "flex", gap: "0.7rem", alignItems: "center" }}>
                                        <span>
                                          Answer selected from: <strong style={{ textTransform: "capitalize" }}>{turn.chosenProvider}</strong>
                                        </span>
                                        {turn.runMeta?.[turn.chosenProvider]?.metrics && (
                                          <span style={{ fontFamily: "IBM Plex Mono, monospace", fontSize: "0.72rem" }}>
                                            ${turn.runMeta[turn.chosenProvider].metrics!.cost_usd.toFixed(3)} · {(turn.runMeta[turn.chosenProvider].metrics!.latency_ms / 1000).toFixed(1)}s · {(turn.runMeta[turn.chosenProvider].metrics!.tokens_in + turn.runMeta[turn.chosenProvider].metrics!.tokens_out).toLocaleString()} tok
                                          </span>
                                        )}
                                        {(() => {
                                          const others = Object.keys(turn.providerChunks ?? {}).filter((k) => k !== "Notice" && k !== turn.chosenProvider);
                                          return others.length > 0 ? <span style={{ fontSize: "0.75rem" }}>{others.join(" and ")} answers archived</span> : null;
                                        })()}
                                        <button
                                          type="button"
                                          onClick={async () => {
                                            const convId = turn.response?.conversation_id || activeConvId;
                                            if (!convId) return;
                                            try { setComparisonLog((await api.getConversationRuns(convId)).runs); } catch (e) { console.error(e); }
                                          }}
                                          style={{ border: "none", background: "none", color: "var(--brand)", cursor: "pointer", fontSize: "0.8rem", padding: 0, textDecoration: "underline" }}
                                        >
                                          View comparison log
                                        </button>
                                        <button
                                          type="button"
                                          title="Copy selected answer"
                                          onClick={() => copyText(`${turn.id}:accepted`, turn.response?.answer || turn.providerChunks?.[turn.chosenProvider!] || "")}
                                          style={{ border: "none", background: "none", color: "var(--brand)", cursor: "pointer", fontSize: "0.8rem", padding: 0, textDecoration: "underline" }}
                                        >
                                          {copiedKey === `${turn.id}:accepted` ? "✓ copied" : "⧉ copy"}
                                        </button>
                                      </div>
                                    )}
                                    {turn.chosenProvider && (turn.artifacts?.[turn.chosenProvider]?.length ?? 0) > 0 && (
                                      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.4rem", marginBottom: "0.6rem" }}>
                                        {turn.artifacts![turn.chosenProvider].map((a) => (
                                          <button
                                            key={a.id}
                                            type="button"
                                            onClick={() => openArtifact(a)}
                                            title={`${a.name} · ${(a.size / 1024).toFixed(1)} KB`}
                                            style={{ display: "flex", alignItems: "center", gap: "0.4rem", border: "1px solid var(--brand)", background: "var(--surface-50)", color: "var(--brand)", borderRadius: "7px", padding: "0.3rem 0.6rem", fontSize: "0.76rem", fontWeight: 600, cursor: "pointer" }}
                                          >
                                            <span>▤</span>
                                            <span style={{ maxWidth: "14rem", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{a.name}</span>
                                            <span style={{ fontWeight: 400, color: "var(--text-muted)" }}>Open</span>
                                          </button>
                                        ))}
                                      </div>
                                    )}
                                    {turn.chosenProvider && (turn.toolEvents?.[turn.chosenProvider]?.length ?? 0) > 0 && (
                                      <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem", marginBottom: "0.5rem" }}>
                                        {turn.toolEvents![turn.chosenProvider].map((t, ti) => (
                                          <div key={ti} style={{ display: "flex", alignItems: "center", gap: "0.45rem", fontSize: "0.72rem", fontFamily: "IBM Plex Mono, monospace", color: "var(--text-muted)", background: "var(--surface-100, rgba(0,0,0,0.03))", border: "1px solid var(--line)", borderRadius: "6px", padding: "0.28rem 0.5rem" }}>
                                            <span>{t.tool === "search_documents" ? "⌕" : t.tool === "execute_python" ? "▶" : "≣"}</span>
                                            <span style={{ fontWeight: 600 }}>{t.label}</span>
                                            <span style={{ marginLeft: "auto" }}>{t.summary}</span>
                                          </div>
                                        ))}
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
              {askUsage && (
                <span
                  title={`Today: ${askUsage.usage.runs} runs · ${askUsage.usage.sandbox_execs} sandbox execs (max ${askUsage.limits.daily_sandbox_execs || "∞"}) · ${askUsage.limits.max_concurrent_runs || "∞"} concurrent`}
                  style={{
                    marginLeft: "auto", order: 99, fontSize: "0.68rem", fontFamily: "IBM Plex Mono, monospace",
                    color: askUsage.limits.daily_cost_usd > 0 && askUsage.usage.cost_usd >= askUsage.limits.daily_cost_usd ? "var(--danger, #b3261e)" : "var(--text-muted)",
                    border: "1px solid var(--line)", borderRadius: "999px", padding: "0.18rem 0.6rem",
                  }}
                >
                  today ${askUsage.usage.cost_usd.toFixed(2)}
                  {askUsage.limits.daily_cost_usd > 0 && ` / $${askUsage.limits.daily_cost_usd.toFixed(2)}`}
                  {" · "}
                  {(askUsage.usage.tokens / 1000).toFixed(1)}k tok
                  {askUsage.limits.daily_tokens > 0 && ` / ${(askUsage.limits.daily_tokens / 1000).toFixed(0)}k`}
                </span>
              )}
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
                    {(() => {
                      const ver = p.versions.find((v) => v.model_id === (modelChoice[p.provider] ?? "")) ?? p.versions.find((v) => v.default);
                      const levels = ver?.reasoning_levels ?? [];
                      if (levels.length === 0) return null;
                      return (
                        <select
                          value={reasoningChoice[p.provider] ?? "none"}
                          disabled={!enabled}
                          onChange={(e) => setReasoningChoice((cur) => ({ ...cur, [p.provider]: e.target.value }))}
                          title="Reasoning effort"
                          style={{ border: "none", background: "transparent", fontSize: "0.7rem", fontFamily: "IBM Plex Mono, monospace", color: "var(--text-muted)", cursor: "pointer" }}
                        >
                          {levels.map((l) => (
                            <option key={l} value={l}>{l === "none" ? "no reasoning" : `reason: ${l}`}</option>
                          ))}
                        </select>
                      );
                    })()}
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

      {comparisonLog && (
        <div
          onClick={() => setComparisonLog(null)}
          style={{ position: "fixed", inset: 0, background: "rgba(13,27,47,0.55)", zIndex: 90, display: "flex", alignItems: "center", justifyContent: "center", padding: "3vh 3vw" }}
        >
          <div onClick={(e) => e.stopPropagation()} style={{ background: "var(--surface, #fff)", borderRadius: "12px", width: "min(880px, 94vw)", maxHeight: "84vh", display: "flex", flexDirection: "column", overflow: "hidden", boxShadow: "0 24px 64px rgba(0,0,0,0.35)" }}>
            <div style={{ display: "flex", alignItems: "center", padding: "0.7rem 1rem", borderBottom: "1px solid var(--line)" }}>
              <strong style={{ fontSize: "0.95rem" }}>Comparison log</strong>
              <span style={{ marginLeft: "0.6rem", fontSize: "0.72rem", color: "var(--text-muted)", fontFamily: "IBM Plex Mono, monospace" }}>{comparisonLog.length} runs</span>
              <button type="button" className="button is-small" style={{ marginLeft: "auto" }} onClick={() => setComparisonLog(null)}>Close</button>
            </div>
            <div style={{ overflowY: "auto", padding: "0.9rem 1rem", display: "flex", flexDirection: "column", gap: "0.8rem" }}>
              {comparisonLog.map((r) => (
                <div key={r.id} style={{ border: `1px solid ${r.selected ? "var(--brand)" : "var(--line)"}`, borderRadius: "9px", padding: "0.7rem 0.85rem" }}>
                  <div style={{ display: "flex", gap: "0.6rem", alignItems: "baseline", flexWrap: "wrap", fontSize: "0.78rem", fontFamily: "IBM Plex Mono, monospace", color: "var(--text-muted)" }}>
                    <strong style={{ color: "var(--text)", textTransform: "capitalize", fontSize: "0.85rem" }}>{r.provider}</strong>
                    <span>{r.display_version || r.model_id}</span>
                    {r.reasoning !== "none" && <span>reason:{r.reasoning}</span>}
                    {r.selected && <span style={{ color: "var(--brand)", fontWeight: 700 }}>SELECTED</span>}
                    {r.passed_from.length > 0 && <span>passed from {r.passed_from.join("+")}</span>}
                    {r.status === "error" && <span style={{ color: "var(--danger, #b3261e)" }}>error</span>}
                    <span style={{ marginLeft: "auto" }}>
                      {((r.metrics?.tokens_in ?? 0) + (r.metrics?.tokens_out ?? 0)).toLocaleString()} tok · ${(r.metrics?.cost_usd ?? 0).toFixed(4)} · {((r.metrics?.latency_ms ?? 0) / 1000).toFixed(1)}s
                    </span>
                  </div>
                  {r.tool_events.filter((t) => t.type === "tool_result").length > 0 && (
                    <div style={{ marginTop: "0.35rem", fontSize: "0.72rem", fontFamily: "IBM Plex Mono, monospace", color: "var(--text-muted)" }}>
                      {r.tool_events.filter((t) => t.type === "tool_result").map((t, i) => (
                        <span key={i} style={{ marginRight: "0.8rem" }}>{t.label}: {t.summary}</span>
                      ))}
                    </div>
                  )}
                  <div style={{ marginTop: "0.45rem", fontSize: "0.85rem", lineHeight: 1.5, maxHeight: "9rem", overflowY: "auto", whiteSpace: "pre-wrap" }}>{r.body}</div>
                  {r.artifacts.length > 0 && (
                    <div style={{ display: "flex", gap: "0.4rem", marginTop: "0.45rem", flexWrap: "wrap" }}>
                      {r.artifacts.map((a) => (
                        <button key={a.id} type="button" onClick={() => openArtifact(a)} style={{ border: "1px solid var(--brand)", color: "var(--brand)", background: "none", borderRadius: "6px", padding: "0.2rem 0.5rem", fontSize: "0.72rem", cursor: "pointer" }}>
                          ▤ {a.name}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {artifactPreview && (
        <div
          onClick={closeArtifactPreview}
          style={{ position: "fixed", inset: 0, background: "rgba(13,27,47,0.55)", zIndex: 90, display: "flex", alignItems: "center", justifyContent: "center", padding: "3vh 3vw" }}
        >
          <div onClick={(e) => e.stopPropagation()} style={{ background: "var(--surface, #fff)", borderRadius: "12px", width: "min(960px, 94vw)", height: "min(80vh, 900px)", display: "flex", flexDirection: "column", overflow: "hidden", boxShadow: "0 24px 64px rgba(0,0,0,0.35)" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "0.6rem", padding: "0.6rem 0.9rem", borderBottom: "1px solid var(--line)" }}>
              <span style={{ fontWeight: 600, fontSize: "0.9rem", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{artifactPreview.name}</span>
              <span style={{ fontSize: "0.72rem", color: "var(--text-muted)", fontFamily: "IBM Plex Mono, monospace" }}>{artifactPreview.mime} · sandboxed preview</span>
              <a href={artifactPreview.url} download={artifactPreview.name} className="button is-small" style={{ marginLeft: "auto" }}>Download</a>
              <button type="button" className="button is-small" onClick={closeArtifactPreview}>Close</button>
            </div>
            <div style={{ flex: 1, minHeight: 0, background: "#fff" }}>
              {artifactPreview.mime.startsWith("image/") ? (
                <img src={artifactPreview.url} alt={artifactPreview.name} style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain", display: "block", margin: "0 auto" }} />
              ) : (
                <iframe
                  title={artifactPreview.name}
                  src={artifactPreview.url}
                  sandbox="allow-scripts"
                  style={{ width: "100%", height: "100%", border: "none" }}
                />
              )}
            </div>
          </div>
        </div>
      )}

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
