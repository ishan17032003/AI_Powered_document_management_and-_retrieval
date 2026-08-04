import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  Check,
  FileText,
  KeyRound,
  Lock,
  Search,
  ShieldCheck,
  UserPlus,
  Users,
  X,
} from "lucide-react";
import {
  api,
  AccessRule,
  AdminUser,
  AllDocRule,
  DocSummary,
  EffectiveAccess,
  RbacMatrix,
} from "../api";
import { useAuth } from "../auth";
import { PageHeader, SectionCard, StatusPill, StatusTone } from "../components/ui";

function toneForStatus(status: string): StatusTone {
  return status === "active" || status === "ALLOW"
    ? "success"
    : status === "suspended" || status === "DENY"
    ? "danger"
    : "neutral";
}

export default function Admin() {
  const { can, user: me } = useAuth();
  const isSuperAdmin = !!me?.roles.includes("Super Admin");

  const [users, setUsers] = useState<AdminUser[]>([]);
  const [documents, setDocuments] = useState<DocSummary[]>([]);
  const [matrix, setMatrix] = useState<RbacMatrix | null>(null);

  // Access-control matrix
  const [allRules, setAllRules] = useState<AllDocRule[]>([]);
  const [selectedAcmUserId, setSelectedAcmUserId] = useState<string>("");
  const [matrixLoading, setMatrixLoading] = useState(false);

  // Legacy doc-centric grant panel
  const [selectedUserId, setSelectedUserId] = useState("");
  const [selectedDocumentId, setSelectedDocumentId] = useState("");
  const [rules, setRules] = useState<AccessRule[]>([]);
  const [effective, setEffective] = useState<EffectiveAccess | null>(null);

  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [createForm, setCreateForm] = useState({
    username: "", name: "", email: "", password: "", role: "Viewer",
  });
  const [accessForm, setAccessForm] = useState({ effect: "ALLOW" as "ALLOW" | "DENY", reason: "" });

  const roles = useMemo(() => Object.keys(matrix?.roles || {}), [matrix]);
  const selectedUser = users.find((item) => item.id === Number(selectedUserId));
  const selectedDocument = documents.find((item) => item.id === Number(selectedDocumentId));


  async function loadAllRules() {
    setMatrixLoading(true);
    try {
      const data = await api.listAllDocRules();
      // Only keep active rules as per user request to not show history
      setAllRules(data.filter((r) => r.is_active));
    } catch (err: any) {
      setError(err.message || "Could not load access matrix.");
    } finally {
      setMatrixLoading(false);
    }
  }

  async function loadAccess(documentId = selectedDocumentId) {
    if (!documentId) return;
    setEffective(null);
    try {
      const nextRules = await api.listAccessRules("DOC", Number(documentId));
      // Only show active rules in the legacy panel
      setRules(nextRules.filter((r) => r.is_active !== false));
      if (selectedUserId)
        setEffective(await api.explainAccess(Number(selectedUserId), Number(documentId)));
    } catch (err: any) {
      setError(err.message || "Access rules could not be loaded.");
    }
  }

  useEffect(() => {
    if (!can("ADMIN")) return;
    Promise.all([api.adminUsers(), api.rbacMatrix(), api.listDocuments(), api.listAllDocRules()])
      .then(([nextUsers, nextMatrix, nextDocuments, nextRules]) => {
        setUsers(nextUsers);
        setMatrix(nextMatrix);
        setDocuments(nextDocuments);
        setAllRules(nextRules);
        if (nextUsers[0]) setSelectedUserId(String(nextUsers[0].id));
        if (nextDocuments[0]) setSelectedDocumentId(String(nextDocuments[0].id));
        if (nextMatrix.roles.Viewer) setCreateForm((c) => ({ ...c, role: "Viewer" }));
      })
      .catch((err: any) => setError(err.message || "RBAC data could not be loaded."));
  }, [can]);

  useEffect(() => {
    if (selectedDocumentId) void loadAccess(selectedDocumentId);
  }, [selectedDocumentId, selectedUserId]);

  async function createUser(event: FormEvent) {
    event.preventDefault();
    setBusy("create");
    setError("");
    try {
      const created = await api.createUser(createForm);
      setUsers((c) => [...c, created].sort((a, b) => a.username.localeCompare(b.username)));
      setSelectedUserId(String(created.id));
      setCreateForm({ username: "", name: "", email: "", password: "", role: roles[0] || "Viewer" });
    } catch (err: any) {
      setError(err.message || "User could not be created.");
    } finally {
      setBusy("");
    }
  }

  async function assignRole(userId: number, role: string) {
    if (!role) return;
    setBusy(`role-${userId}`);
    setError("");
    try {
      const updated = await api.assignRole(userId, role);
      setUsers((c) => c.map((item) => (item.id === updated.id ? updated : item)));
    } catch (err: any) {
      setError(err.message || "Role could not be assigned.");
    } finally {
      setBusy("");
    }
  }

  async function setStatus(item: AdminUser) {
    const nextStatus = item.status === "active" ? "suspended" : "active";
    setBusy(`status-${item.id}`);
    setError("");
    try {
      const updated = await api.setUserStatus(item.id, nextStatus);
      setUsers((c) => c.map((u) => (u.id === updated.id ? updated : u)));
    } catch (err: any) {
      setError(err.message || "User status could not be changed.");
    } finally {
      setBusy("");
    }
  }

  async function grantAccess(event: FormEvent) {
    event.preventDefault();
    if (!selectedUser || !selectedDocument) return;
    setBusy("access");
    setError("");
    try {
      await api.grantAccessRule("DOC", selectedDocument.id, {
        principal_type: "USER",
        principal_id: selectedUser.id,
        permission: "VIEW",
        effect: accessForm.effect,
        inherits: false,
        reason: accessForm.reason || "Administrator-managed file access",
      });
      setAccessForm((c) => ({ ...c, reason: "" }));
      await loadAccess();
      await loadAllRules();
    } catch (err: any) {
      setError(err.message || "Access rule could not be saved.");
    } finally {
      setBusy("");
    }
  }

  async function revokeRule(rule: AccessRule) {
    setBusy(`revoke-${rule.id}`);
    setError("");
    try {
      await api.revokeAccessRule(rule.id);
      // Remove immediately from local state
      setRules((prev) => prev.filter((r) => r.id !== rule.id));
      // Also refresh the matrix
      void loadAllRules();
    } catch (err: any) {
      setError(err.message || "Access rule could not be revoked.");
    } finally {
      setBusy("");
    }
  }

  // Revoke from the access matrix — removes row immediately from UI
  async function revokeMatrixRule(ruleId: number) {
    setBusy(`mx-${ruleId}`);
    setError("");
    try {
      await api.revokeAccessRule(ruleId);
      // Remove immediately from local state so the row disappears
      setAllRules((prev) => prev.filter((r) => r.rule_id !== ruleId));
    } catch (err: any) {
      setError(err.message || "Access rule could not be revoked.");
    } finally {
      setBusy("");
    }
  }

  if (!can("ADMIN")) {
    return <div className="notice is-danger">Administrator permission is required to manage users and file access.</div>;
  }

  const userRules = useMemo(() => {
    if (!selectedAcmUserId) return [];
    return allRules.filter((r) => r.user_id.toString() === selectedAcmUserId && r.effect === "ALLOW" && r.is_active);
  }, [allRules, selectedAcmUserId]);

  return (
    <div className="admin-page">
      <PageHeader
        eyebrow="Identity and authorization"
        title="Users & access"
        description="Roles describe what a user can do. Access rules decide which files they can see."
        actions={<StatusPill tone="success"><ShieldCheck size={14} /> Policy enforced at the API</StatusPill>}
      />

      {error && <div className="notice is-danger" role="alert">{error}</div>}

      {/* ── ACCESS CONTROL MATRIX ─────────────────────────────────────────────── */}
      <SectionCard className="acm-card">
        <div className="section-heading">
          <div>
            <span className="section-kicker">User specific</span>
            <h2><Lock size={19} /> Access control matrix</h2>
          </div>
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <span className="section-count">{userRules.length}</span>
            <div className="acm-search-wrap">
              <Users size={13} style={{ opacity: 0.5, marginLeft: 8 }} />
              <select
                className="acm-search"
                style={{ appearance: "none", paddingLeft: 28 }}
                value={selectedAcmUserId}
                onChange={(e) => setSelectedAcmUserId(e.target.value)}
              >
                <option value="">Select a user...</option>
                {users.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.name} ({u.username})
                  </option>
                ))}
              </select>
            </div>
          </div>
        </div>

        {matrixLoading ? (
          <p className="admin-empty">Loading…</p>
        ) : !selectedAcmUserId ? (
          <p className="admin-empty">Please select a user to view their permitted documents.</p>
        ) : userRules.length === 0 ? (
          <p className="admin-empty">This user has no permitted documents.</p>
        ) : (
          <div className="acm-table">
            <div className="acm-header">
              <span>Document</span>
              <span>Class</span>
              <span>Permission</span>
              <span>Effect</span>
              <span>Reason</span>
              <span>Date</span>
              {isSuperAdmin && <span />}
            </div>

            <div className="acm-user-group">
              {/* Rule rows for this user */}
              {userRules.map((rule) => (
                <div
                  key={rule.rule_id}
                  className="acm-row"
                >
                  <span className="acm-doc">
                    <FileText size={12} style={{ flexShrink: 0, opacity: 0.5 }} />
                    <span>{rule.doc_title}</span>
                  </span>
                  <span className="acm-class">
                    {rule.doc_class ?? <em style={{ opacity: 0.4 }}>—</em>}
                  </span>
                  <span>
                    <span className="perm-badge">{rule.permission}</span>
                  </span>
                  <span>
                    <StatusPill tone={toneForStatus(rule.effect)}>{rule.effect}</StatusPill>
                  </span>
                  <span className="acm-reason">
                    {rule.reason || <em style={{ opacity: 0.35 }}>—</em>}
                  </span>
                  <span className="acm-date">
                    {new Date(rule.created_at).toLocaleDateString()}
                  </span>
                  {isSuperAdmin && (
                    <span>
                      <button
                        className="icon-button danger-on-hover"
                        onClick={() => void revokeMatrixRule(rule.rule_id)}
                        aria-label={`Revoke access to ${rule.doc_title}`}
                        disabled={busy === `mx-${rule.rule_id}`}
                        title="Delete access"
                      >
                        <X size={13} />
                      </button>
                    </span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </SectionCard>

      <div className="admin-grid" style={{ marginTop: 12 }}>
        {/* ── Users panel ────────────────────────────────────────────── */}
        <SectionCard className="admin-users-card">
          <div className="section-heading">
            <div>
              <span className="section-kicker">Identity lifecycle</span>
              <h2><Users size={20} /> Users</h2>
            </div>
            <span className="section-count">{users.length}</span>
          </div>

          <form className="admin-create-form" onSubmit={createUser}>
            <div className="admin-form-grid">
              <label className="field"><span>Username</span><input value={createForm.username} onChange={(e) => setCreateForm({ ...createForm, username: e.target.value })} required minLength={3} /></label>
              <label className="field"><span>Name</span><input value={createForm.name} onChange={(e) => setCreateForm({ ...createForm, name: e.target.value })} required /></label>
              <label className="field"><span>Email</span><input type="email" value={createForm.email} onChange={(e) => setCreateForm({ ...createForm, email: e.target.value })} required /></label>
              <label className="field"><span>Temporary password</span><input type="password" value={createForm.password} onChange={(e) => setCreateForm({ ...createForm, password: e.target.value })} required minLength={16} placeholder="At least 16 characters" /></label>
              <label className="field"><span>Initial role</span><select value={createForm.role} onChange={(e) => setCreateForm({ ...createForm, role: e.target.value })}>{roles.map((role) => <option key={role}>{role}</option>)}</select></label>
            </div>
            <button className="button primary" disabled={busy === "create"}><UserPlus size={16} />{busy === "create" ? "Creating…" : "Create user"}</button>
          </form>

          <div className="admin-user-list">
            {users.map((item) => (
              <div className="admin-user-row" key={item.id}>
                <span className="avatar is-small">{item.name.slice(0, 2).toUpperCase()}</span>
                <span className="admin-user-copy">
                  <strong>{item.name}</strong>
                  <small>{item.username} · {item.email}</small>
                  <span className="role-list">{item.roles.join(" · ") || "No role"}</span>
                </span>
                <span className="admin-user-actions">
                  <StatusPill tone={toneForStatus(item.status)}>{item.status}</StatusPill>
                  <select aria-label={`Assign role to ${item.name}`} value="" onChange={(e) => void assignRole(item.id, e.target.value)} disabled={busy === `role-${item.id}`}>
                    <option value="">Assign role…</option>
                    {roles.map((role) => <option key={role} value={role}>{role}</option>)}
                  </select>
                  <button className="text-button compact" onClick={() => void setStatus(item)} disabled={busy === `status-${item.id}`}>
                    {item.status === "active" ? "Suspend" : "Reactivate"}
                  </button>
                </span>
              </div>
            ))}
          </div>
          <p className="admin-help">Role assignment is additive. Use "Grant file access" below for document-level visibility.</p>
          {matrix && (
            <details className="role-matrix">
              <summary>Role capability guide</summary>
              <div className="role-matrix-list">
                {Object.entries(matrix.roles).map(([role, permissions]) => (
                  <div key={role}><strong>{role}</strong><span>{permissions.join(" · ")}</span></div>
                ))}
              </div>
            </details>
          )}
        </SectionCard>

        {/* ── Grant file access ────────────────────────────────────────── */}
        <SectionCard className="admin-access-card">
          <div className="section-heading">
            <div>
              <span className="section-kicker">Resource authorization</span>
              <h2><Lock size={19} /> Grant file access</h2>
            </div>
            <StatusPill tone="info">DOC scope</StatusPill>
          </div>

          <div className="admin-form-grid access-select-grid">
            <label className="field"><span>User</span><select value={selectedUserId} onChange={(e) => setSelectedUserId(e.target.value)}>{users.map((item) => <option key={item.id} value={item.id}>{item.name} ({item.username})</option>)}</select></label>
            <label className="field"><span>File</span><select value={selectedDocumentId} onChange={(e) => setSelectedDocumentId(e.target.value)}>{documents.map((item) => <option key={item.id} value={item.id}>#{item.id} · {item.title}</option>)}</select></label>
          </div>

          <form className="access-grant-form" onSubmit={grantAccess}>
            <label className="field"><span>Decision</span><select value={accessForm.effect} onChange={(e) => setAccessForm({ ...accessForm, effect: e.target.value as "ALLOW" | "DENY" })}><option value="ALLOW">Allow view</option><option value="DENY">Deny view</option></select></label>
            <label className="field"><span>Reason (optional)</span><input value={accessForm.reason} onChange={(e) => setAccessForm({ ...accessForm, reason: e.target.value })} placeholder="Why this file is shared" maxLength={1000} /></label>
            <button className="button primary" disabled={!selectedUser || !selectedDocument || busy === "access"}><KeyRound size={16} />{busy === "access" ? "Saving…" : "Save access"}</button>
          </form>

          {selectedUser && selectedDocument && effective && (
            <div className={`effective-access ${effective.allowed ? "is-allowed" : "is-denied"}`}>
              {effective.allowed ? <Check size={17} /> : <X size={17} />}
              <span>
                <strong>{selectedUser.name} {effective.allowed ? "can see" : "cannot see"} this file</strong>
                <small>{effective.reason_code.replaceAll("_", " ").toLowerCase()} · policy revision {effective.policy_version}</small>
              </span>
            </div>
          )}

          <div className="access-rules-heading">
            <span>Active rules on {selectedDocument ? `#${selectedDocument.id} · ${selectedDocument.title}` : "selected file"}</span>
            <small>{rules.length} rule{rules.length === 1 ? "" : "s"}</small>
          </div>
          <div className="access-rule-list">
            {rules.length ? rules.map((rule) => (
              <div className="access-rule-row" key={rule.id}>
                <StatusPill tone={toneForStatus(rule.effect)}>{rule.effect}</StatusPill>
                <span>
                  <strong>{rule.principal_type} #{rule.principal_id}</strong>
                  <small>{rule.permission} · {rule.reason || "No reason recorded"}</small>
                </span>
                <button
                  className="icon-button danger-on-hover"
                  onClick={() => void revokeRule(rule)}
                  aria-label={`Revoke rule ${rule.id}`}
                  disabled={busy === `revoke-${rule.id}`}
                >
                  <X size={15} />
                </button>
              </div>
            )) : <p className="admin-empty">No active rules for this file. The effective decision may still come from folder/cabinet/global rules.</p>}
          </div>
        </SectionCard>
      </div>
    </div>
  );
}
