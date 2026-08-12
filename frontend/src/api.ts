// Typed API client for the XENIUS DocVault backend.
// Uses the Vite dev proxy (/api -> :8000), so requests are same-origin.

const TOKEN_KEY = "docvault_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(t: string | null) {
  if (t) localStorage.setItem(TOKEN_KEY, t);
  else localStorage.removeItem(TOKEN_KEY);
}

export interface Me {
  id: number;
  username: string;
  name: string;
  email: string;
  roles: string[];
  permissions: string[];
}

export interface AdminUser {
  id: number;
  username: string;
  name: string;
  email: string;
  status: "active" | "suspended";
  roles: string[];
}

export interface DocClass {
  id: number;
  name: string;
  parent_id: number | null;
}

export interface RbacMatrix {
  permissions: string[];
  roles: Record<string, string[]>;
}

export interface AccessRule {
  id: number;
  principal_type: "USER" | "GROUP";
  principal_id: number;
  permission: string;
  scope_type: "GLOBAL" | "CABINET" | "FOLDER" | "DOC";
  scope_id: number | null;
  effect: "ALLOW" | "DENY";
  inherits: boolean;
  is_active: boolean;
  expires_at: string | null;
  reason: string | null;
  created_by: number;
  created_at: string;
}

export interface EffectiveAccess {
  user_id: number;
  permission: string;
  scope_type: string;
  scope_id: number | null;
  allowed: boolean;
  reason_code: string;
  matched_rule: {
    id: number;
    principal_type: string;
    principal_id: number;
    effect: string;
    inherits: boolean;
    reason: string | null;
    scope_type: string;
    scope_id: number | null;
  } | null;
  policy_version: number;
  ancestry: Array<{ scope_type: string; scope_id: number | null }>;
}

export interface UserDocRule {
  rule_id: number;
  doc_id: number;
  doc_title: string;
  doc_class: string | null;
  effect: "ALLOW" | "DENY";
  permission: string;
  reason: string | null;
  is_active: boolean;
  created_at: string;
}

export interface AllDocRule extends UserDocRule {
  user_id: number;
  user_name: string;
  user_username: string;
}

export interface AllGroupDocRule extends UserDocRule {
  group_id: number;
  group_name: string;
}

export interface RoleGroupMap {
  role_name: string;
  group_id: number;
}

export interface DocSummary {
  id: number;
  title: string;
  folder_id: number;
  doc_class: string | null;
  class_confidence: number | null;
  status: string;
  ocr_status: string;
  ocr_confidence: number | null;
  page_count: number;
  size: number | null;
  created_at: string;
  updated_at: string;
}

export interface DocDetail extends DocSummary {
  content_hash: string;
  language: string;
  ocr_text: string;
  metadata: { key: string; value: string; confidence: number | null }[];
  versions: {
    id: number;
    version_no: number;
    filename: string;
    content_type: string;
    size: number;
    checksum: string;
    created_at: string;
  }[];
  is_duplicate_of: number | null;
}

export interface SearchHit {
  document_id: number;
  title: string;
  doc_class: string | null;
  snippet: string;
  score: number;
}
export interface SearchResponse {
  query: string;
  mode: string;
  count: number;
  hits: SearchHit[];
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface AskResponse {
  question: string;
  answer: string;
  mode: string; // claude | extractive | clarify
  model: string | null;
  needs_clarification: boolean;
  scoped_document_id: number | null;
  citations: { index: number; document_id: number; title: string }[];
  candidates: { document_id: number; title: string }[];
  images: VisualSearchHit[];
}

export interface ImportResult {
  path: string;
  imported: number;
  duplicates: number;
  skipped: number;
  errors: number;
  items: { filename: string; status: string; document_id: number | null; detail: string | null }[];
}

export interface UploadResult {
  id: number;
  job_id: string | null;
  version_id: number | null;
  title: string;
  status: string;
  folder_id: number;
  ocr_status: string;
  doc_class: string | null;
  duplicate_of: number | null;
  pipeline: Record<string, unknown>;
}

export interface IngestionStatus {
  id: string;
  document_id: number | null;
  version_id: number | null;
  state: string;
  stage: string;
  stage_version: string;
  attempt_count: number;
  retryable: boolean;
  cancellable: boolean;
  terminal: boolean;
  next_attempt_at: string | null;
  error_code: string | null;
}

export interface ImageSearchResponse {
  mode: "image_to_image";
  count: number;
  hits: Array<Record<string, unknown>>;
  provider: string;
  audit: Record<string, unknown>;
  ephemeral: true;
}

export interface VisualSearchHit {
  asset_id: number;
  document_id: number;
  version_id: number;
  title: string;
  asset_type: "PAGE" | "IMAGE" | "REGION" | "THUMBNAIL";
  result_type: "page" | "image";
  page_number: number | null;
  content_type: string;
  snippet: string;
  score: number;
  matched_lanes: string[];
  preview_available: boolean;
}

export interface VisualSearchResponse {
  query: string;
  mode: "text_to_image" | "text_to_page" | "image_to_image" | "hybrid";
  count: number;
  hits: VisualSearchHit[];
  provider: string;
  degraded: boolean;
}

export interface Stats {
  total_documents: number;
  processing: number;
  needs_review: number;
  storage_bytes: number;
  open_duplicate_groups: number;
  duplicate_members: number;
  engine: Record<string, boolean>;
}

export interface RagStatus {
  status: string;
  rag: {
    llm: string;
    model: string | null;
    search: {
      qdrant: boolean;
      embedding_model: string | null;
      reranker_model: string | null;
      fts5_fallback: boolean;
    };
    okf: { enabled: boolean; entry_count: number };
    docling: boolean;
  };
  models_ready: {
    embedding: boolean;
    reranker: boolean;
    qdrant: boolean;
  };
  ocr_engines: Record<string, boolean>;
}

export interface DupGroup {
  id: number;
  similarity_type: string;
  primary_document_id: number;
  resolved: boolean;
  members: { document_id: number; title: string; similarity_score: number }[];
}

export interface AuditRow {
  id: number;
  actor_name: string;
  action: string;
  object_type: string;
  object_id: string;
  ip: string;
  timestamp: string;
  details: string;
}

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function req<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const headers = new Headers(opts.headers || {});
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(`/api/v1${path}`, { ...opts, headers });
  if (res.status === 401) {
    setToken(null);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export const api = {
  async login(username: string, password: string): Promise<string> {
    const body = new URLSearchParams({ username, password });
    const res = await fetch("/api/v1/auth/login", { method: "POST", body });
    if (!res.ok) {
      const b = await res.json().catch(() => ({}));
      throw new ApiError(
        res.status,
        b.message || b.detail || b.code || "Login failed",
      );
    }
    const data = await res.json();
    setToken(data.access_token);
    return data.access_token;
  },
  me: () => req<Me>("/auth/me"),
  adminUsers: () => req<AdminUser[]>("/admin/users"),
  rbacMatrix: () => req<RbacMatrix>("/admin/rbac-matrix"),
  createUser: (payload: { username: string; name: string; email: string; password: string; role: string }) =>
    req<AdminUser>("/admin/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  setUserStatus: (userId: number, status: "active" | "suspended") =>
    req<AdminUser>(`/admin/users/${userId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    }),
  assignRole: (userId: number, role: string) =>
    req<AdminUser>(`/admin/users/${userId}/role`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role }),
    }),
  listAccessRules: (scope: "DOC" | "FOLDER" | "CABINET", scopeId: number) =>
    req<AccessRule[]>(`/access/${scope}/${scopeId}/rules`),
  grantAccessRule: (
    scope: "DOC" | "FOLDER" | "CABINET",
    scopeId: number,
    payload: { principal_type: "USER" | "GROUP"; principal_id: number; permission: string; effect: "ALLOW" | "DENY"; inherits: boolean; reason?: string },
  ) =>
    req<AccessRule>(`/access/${scope}/${scopeId}/rules`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  revokeAccessRule: (ruleId: number) =>
    req<AccessRule>(`/access/rules/${ruleId}`, { method: "DELETE" }),
  explainAccess: (userId: number, scopeId: number) =>
    req<EffectiveAccess>(`/access/effective?user_id=${userId}&permission=VIEW&scope=DOC&scope_id=${scopeId}`),
  listUserDocRules: (userId: number) =>
    req<UserDocRule[]>(`/access/user/${userId}/doc-rules`),
  listAllDocRules: () =>
    req<AllDocRule[]>(`/access/all-doc-rules`),
  listAllGroupDocRules: () =>
    req<AllGroupDocRule[]>(`/access/all-group-doc-rules`),
  getRoleGroups: () => req<RoleGroupMap[]>("/admin/role-groups"),
  stats: () => req<Stats>("/admin/stats"),
  ragStatus: () => req<RagStatus>("/status"),
  listDocuments: () => req<DocSummary[]>("/documents"),
  listTrash: () => req<DocSummary[]>("/documents/trash"),
  getDocument: (id: number) => req<DocDetail>(`/documents/${id}`),
  deleteDocument: (id: number) => req<void>(`/documents/${id}`, { method: "DELETE" }),
  restoreDocument: (id: number) => req<DocSummary>(`/documents/${id}/restore`, { method: "POST" }),
  purgeDocument: (id: number) => req<void>(`/documents/${id}/purge`, { method: "DELETE" }),
  emptyTrash: () => req<void>("/documents/trash/empty", { method: "DELETE" }),
  upload: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return req<UploadResult>("/documents", { method: "POST", body: fd });
  },
  importFolder: (path: string, recursive = true) =>
    req<ImportResult>("/documents/import-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, recursive }),
    }),
  search: (q: string) => req<SearchResponse>(`/search?q=${encodeURIComponent(q)}`),
  semantic: (q: string) =>
    req<SearchResponse>("/search/semantic", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ q, limit: 20 }),
    }),
  imageSearch: (image: File, limit = 20) => {
    const fd = new FormData();
    fd.append("image", image);
    return req<ImageSearchResponse>(`/search/image?limit=${limit}`, {
      method: "POST",
      body: fd,
    });
  },
  visualSearch: (
    q: string,
    mode: "text_to_image" | "text_to_page" | "hybrid" = "text_to_image",
    limit = 20,
  ) =>
    req<VisualSearchResponse>("/search/visual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ q, mode, limit }),
    }),
  visualImageSearch: (
    image: File,
    mode: "image_to_image" | "hybrid" = "image_to_image",
    limit = 20,
  ) => {
    const fd = new FormData();
    fd.append("image", image);
    return req<VisualSearchResponse>(
      `/search/visual/image?mode=${encodeURIComponent(mode)}&limit=${limit}`,
      { method: "POST", body: fd },
    );
  },
  visualPreview: async (assetId: number): Promise<Blob> => {
    const headers = new Headers();
    const token = getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const res = await fetch(`/api/v1/search/visual-assets/${assetId}/preview`, { headers });
    if (!res.ok) throw new ApiError(res.status, "Visual preview unavailable");
    return res.blob();
  },
  ingestionStatus: (jobId: string) => req<IngestionStatus>(`/ingestions/${encodeURIComponent(jobId)}`),
  retryIngestion: (jobId: string) =>
    req<IngestionStatus>(`/ingestions/${encodeURIComponent(jobId)}/retry`, { method: "POST" }),
  cancelIngestion: (jobId: string) =>
    req<IngestionStatus>(`/ingestions/${encodeURIComponent(jobId)}/cancel`, { method: "POST" }),
  ask: (question: string, document_id?: number, history?: ChatMessage[]) =>
    req<AskResponse>("/search/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, document_id: document_id ?? null, history: history ?? null }),
    }),
  duplicates: () => req<DupGroup[]>("/duplicates"),
  resolveDup: (groupId: number, primary: number) =>
    req<DupGroup>(`/duplicates/${groupId}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ primary_document_id: primary, action: "keep_primary" }),
    }),
  audit: () => req<AuditRow[]>("/audit?limit=200"),
  contentUrl: (id: number) => `/api/v1/documents/${id}/content`,
  async downloadDocument(docId: number, filename: string): Promise<void> {
    const headers = new Headers();
    const token = getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    
    const res = await fetch(`/api/v1/documents/${docId}/content`, { headers });
    if (!res.ok) {
      if (res.status === 401) setToken(null);
      throw new ApiError(res.status, "Failed to download document");
    }
    
    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);
    const a = window.document.createElement("a");
    a.href = url;
    a.download = filename;
    window.document.body.appendChild(a);
    a.click();
    window.document.body.removeChild(a);
    window.URL.revokeObjectURL(url);
  },
  listDocClasses: () => req<DocClass[]>("/admin/doc-classes"),
  reclassifyDocument: (docId: number, classId: number | null, className?: string) =>
    req<DocSummary>(`/admin/documents/${docId}/class`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(
        className ? { class_name: className } : { class_id: classId }
      ),
    }),
};

export { ApiError };
