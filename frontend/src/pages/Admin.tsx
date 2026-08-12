import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronUp,
  FileText,
  Lock,
  Plus,
  Search,
  Shield,
  ShieldCheck,
  UserPlus,
  Users,
  X,
} from "lucide-react";
import {
  api,
  AdminUser,
  AllDocRule,
  DocSummary,
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

const ROLE_COLORS: Record<string, { bg: string; border: string; text: string }> = {
  "Super Admin":     { bg: "rgba(247,123,90,0.12)",  border: "rgba(247,123,90,0.45)",  text: "#f77b5a" },
  "Administrator":   { bg: "rgba(118,146,255,0.12)", border: "rgba(118,146,255,0.45)", text: "#7692ff" },
  "Records Manager": { bg: "rgba(167,139,250,0.12)", border: "rgba(167,139,250,0.45)", text: "#a78bfa" },
  "Contributor":     { bg: "rgba(68,199,168,0.12)",  border: "rgba(68,199,168,0.45)",  text: "#44c7a8" },
  "Reviewer":        { bg: "rgba(224,162,74,0.12)",  border: "rgba(224,162,74,0.45)",  text: "#e0a24a" },
  "Viewer":          { bg: "rgba(98,181,229,0.12)",  border: "rgba(98,181,229,0.45)",  text: "#62b5e5" },
  "Auditor":         { bg: "rgba(226,126,183,0.12)", border: "rgba(226,126,183,0.45)", text: "#e27eb7" },
  "Guest":           { bg: "rgba(130,147,168,0.12)", border: "rgba(130,147,168,0.45)", text: "#8293a8" },
};

const ROLE_ORDER = [
  "Super Admin", "Administrator", "Records Manager", "Contributor",
  "Reviewer", "Viewer", "Auditor", "Guest",
];

const LOCKED_ROLES = new Set(["Super Admin", "Administrator"]);

function isLockedUser(user: AdminUser): boolean {
  return user.roles.some((r) => LOCKED_ROLES.has(r));
}

type DocEntry = {
  doc: DocSummary;
  // role = from role bucket only, no explicit user rule
  // user-allow = explicit USER ALLOW override
  // user-deny = explicit USER DENY override (blocks role access)
  // implicit = SA/Admin full access
  source: "role" | "user-allow" | "user-deny" | "implicit";
  rule: AllDocRule | null;
};

export default function Admin() {
  const { can, user: me } = useAuth();
  const isSuperAdmin = !!me?.roles.includes("Super Admin");

  const [users, setUsers] = useState<AdminUser[]>([]);
  const [documents, setDocuments] = useState<DocSummary[]>([]);
  const [matrix, setMatrix] = useState<RbacMatrix | null>(null);
  const [allRules, setAllRules] = useState<AllDocRule[]>([]);
  const [roleGroups, setRoleGroups] = useState<Record<string, number>>({});
  const [groupRules, setGroupRules] = useState<import("../api").AllGroupDocRule[]>([]);

  type AcmMode = "user" | "role";
  const [acmMode, setAcmMode] = useState<AcmMode>("user");

  const [selectedRole, setSelectedRole] = useState<string>("");
  const [roleAddDocId, setRoleAddDocId] = useState<string>("");
  const [roleBusy, setRoleBusy] = useState("");

  const [expandedUserId, setExpandedUserId] = useState<string>("");
  const [userSearch, setUserSearch] = useState("");
  const [matrixLoading, setMatrixLoading] = useState(false);
  const [createFormOpen, setCreateFormOpen] = useState(false);
  const [createForm, setCreateForm] = useState({
    username: "", name: "", email: "", password: "", role: "Viewer",
  });
  const [docGrantBusy, setDocGrantBusy] = useState<Record<number, string>>({});
  const [docSearch, setDocSearch] = useState("");
  const [docSearchFocused, setDocSearchFocused] = useState(false);
  const docSearchRef = useRef<HTMLDivElement>(null);

  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  const roles = useMemo(() => Object.keys(matrix?.roles || {}), [matrix]);

  async function loadAllRules() {
    setMatrixLoading(true);
    try {
      const data = await api.listAllDocRules();
      setAllRules(data.filter((r) => r.is_active));
    } catch (err: any) {
      setError(err.message || "Could not load access matrix.");
    } finally {
      setMatrixLoading(false);
    }
  }

  useEffect(() => {
    if (!can("ADMIN")) return;
    Promise.all([
      api.adminUsers(),
      api.rbacMatrix(),
      api.listDocuments(),
      api.listAllDocRules(),
      api.getRoleGroups(),
      api.listAllGroupDocRules(),
    ])
      .then(([nextUsers, nextMatrix, nextDocuments, nextRules, nextRoleGroups, nextGroupRules]) => {
        setUsers(nextUsers);
        setMatrix(nextMatrix);
        setDocuments(nextDocuments);
        setAllRules(nextRules.filter((r) => r.is_active));
        const groupMap: Record<string, number> = {};
        for (const rg of nextRoleGroups) groupMap[rg.role_name] = rg.group_id;
        setRoleGroups(groupMap);
        setGroupRules(nextGroupRules.filter((r) => r.is_active));
        const firstRole = ROLE_ORDER.find((r) => nextMatrix.roles[r]);
        if (firstRole) setSelectedRole(firstRole);
        if (nextMatrix.roles.Viewer) setCreateForm((c) => ({ ...c, role: "Viewer" }));
      })
      .catch((err: any) => setError(err.message || "RBAC data could not be loaded."));
  }, [can]);

  async function createUser(event: FormEvent) {
    event.preventDefault();
    setBusy("create");
    setError("");
    try {
      const created = await api.createUser(createForm);
      setUsers((c) => [...c, created].sort((a, b) => a.username.localeCompare(b.username)));
      setCreateForm({ username: "", name: "", email: "", password: "", role: roles[0] || "Viewer" });
      setCreateFormOpen(false);
    } catch (err: any) {
      const msg = err.message || "";
      if (msg === "USER_ALREADY_EXISTS") setError("A user with that username or email already exists.");
      else if (msg === "PROVISION_PASSWORD_POLICY") setError("Password does not meet the complexity requirements.");
      else if (msg === "PROVISION_USERNAME_INVALID") setError("Username is invalid. Must be lowercase alphanumeric (3-80 chars).");
      else setError(msg || "User could not be created.");
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

  async function grantDocToUser(userId: number, docId: number, effect: "ALLOW" | "DENY") {
    setDocGrantBusy((prev) => ({ ...prev, [docId]: effect }));
    setError("");
    try {
      await api.grantAccessRule("DOC", docId, {
        principal_type: "USER",
        principal_id: userId,
        permission: "VIEW",
        effect,
        inherits: false,
        reason: effect === "ALLOW" ? "User-level document access grant" : "User-level document access deny",
      });
      await loadAllRules();
    } catch (err: any) {
      setError(err.message || "Could not update document access.");
    } finally {
      setDocGrantBusy((prev) => ({ ...prev, [docId]: "" }));
    }
  }

  async function revokeUserDocRule(ruleId: number, docId: number) {
    setDocGrantBusy((prev) => ({ ...prev, [docId]: "revoke" }));
    setError("");
    try {
      await api.revokeAccessRule(ruleId);
      setAllRules((prev) => prev.filter((r) => r.rule_id !== ruleId));
    } catch (err: any) {
      setError(err.message || "Could not revoke access.");
    } finally {
      setDocGrantBusy((prev) => ({ ...prev, [docId]: "" }));
    }
  }

  const roleDocMap = useMemo((): Map<number, { doc_title: string; doc_class: string | null; rule_id: number }> => {
    const map = new Map<number, { doc_title: string; doc_class: string | null; rule_id: number }>();
    if (selectedRole && LOCKED_ROLES.has(selectedRole)) {
      for (const doc of documents) {
        map.set(doc.id, { doc_title: doc.title, doc_class: doc.doc_class, rule_id: -1 });
      }
      return map;
    }
    if (!selectedRole || !roleGroups[selectedRole]) return map;
    const groupId = roleGroups[selectedRole];
    for (const rule of groupRules) {
      if (rule.is_active && rule.effect === "ALLOW" && rule.group_id === groupId) {
        map.set(rule.doc_id, { doc_title: rule.doc_title, doc_class: rule.doc_class, rule_id: rule.rule_id });
      }
    }
    return map;
  }, [groupRules, selectedRole, roleGroups, documents]);

  async function addDocToRole() {
    if (!selectedRole || !roleAddDocId || !roleGroups[selectedRole]) return;
    setRoleBusy("add");
    setError("");
    try {
      const groupId = roleGroups[selectedRole];
      await api.grantAccessRule("DOC", Number(roleAddDocId), {
        principal_type: "GROUP",
        principal_id: groupId,
        permission: "VIEW",
        effect: "ALLOW",
        inherits: false,
        reason: `Role-level grant: ${selectedRole}`,
      });
      setRoleAddDocId("");
      const nextGroupRules = await api.listAllGroupDocRules();
      setGroupRules(nextGroupRules.filter((r) => r.is_active));
    } catch (err: any) {
      setError(err.message || "Could not grant document to role.");
    } finally {
      setRoleBusy("");
    }
  }

  async function revokeDocFromRole(docId: number) {
    setRoleBusy(`revoke-${docId}`);
    setError("");
    try {
      const entry = roleDocMap.get(docId);
      if (!entry) return;
      await api.revokeAccessRule(entry.rule_id);
      setGroupRules((prev) => prev.filter((r) => r.rule_id !== entry.rule_id));
    } catch (err: any) {
      setError(err.message || "Could not revoke document from role.");
    } finally {
      setRoleBusy("");
    }
  }

  const expandedUser = useMemo(
    () => users.find((u) => u.id === Number(expandedUserId)),
    [users, expandedUserId]
  );

  const expandedUserRoleDocIds = useMemo(() => {
    if (!expandedUser) return new Set<number>();
    const docIds = new Set<number>();
    for (const roleName of expandedUser.roles) {
      const groupId = roleGroups[roleName];
      if (!groupId) continue;
      for (const rule of groupRules) {
        if (rule.group_id === groupId && rule.is_active && rule.effect === "ALLOW") {
          docIds.add(rule.doc_id);
        }
      }
    }
    return docIds;
  }, [expandedUser, roleGroups, groupRules]);

  const expandedUserDocs = useMemo((): DocEntry[] => {
    if (!expandedUser) return [];
    // SA/Admin → all docs as implicit, deduplicated by doc.id
    if (isLockedUser(expandedUser)) {
      const seen = new Set<number>();
      return documents.filter((doc) => { if (seen.has(doc.id)) return false; seen.add(doc.id); return true; })
        .map((doc) => ({ doc, source: "implicit", rule: null }));
    }
    // Build explicit rule map: DENY wins over ALLOW
    const explicitByDocId = new Map<number, AllDocRule>();
    for (const rule of allRules) {
      if (rule.user_id === expandedUser.id && rule.is_active) {
        const existing = explicitByDocId.get(rule.doc_id);
        if (!existing || rule.effect === "DENY") {
          explicitByDocId.set(rule.doc_id, rule);
        }
      }
    }
    const result: DocEntry[] = [];
    const seen = new Set<number>(); // deduplicate by doc.id
    for (const doc of documents) {
      if (seen.has(doc.id)) continue;
      seen.add(doc.id);
      const explicit = explicitByDocId.get(doc.id);
      const inRole = expandedUserRoleDocIds.has(doc.id);
      if (explicit?.effect === "DENY") {
        // Show user-denied docs so admins can restore them
        result.push({ doc, source: "user-deny", rule: explicit });
      } else if (explicit?.effect === "ALLOW") {
        result.push({ doc, source: "user-allow", rule: explicit });
      } else if (inRole) {
        result.push({ doc, source: "role", rule: null });
      }
    }
    return result;
  }, [expandedUser, expandedUserRoleDocIds, allRules, documents]);

  const expandedUserNotAccessibleDocs = useMemo(() => {
    // Exclude docs that are: accessible (role/user-allow) OR actively denied (user-deny)
    // user-deny docs appear in the panel with a Restore button so don't offer them in search
    const handledIds = new Set(expandedUserDocs.map((e) => e.doc.id));
    // Also deduplicate the full docs list before filtering
    const seen = new Set<number>();
    return documents.filter((d) => {
      if (seen.has(d.id)) return false;
      seen.add(d.id);
      return !handledIds.has(d.id);
    });
  }, [expandedUserDocs, documents]);

  const docSearchResults = useMemo(() => {
    const q = docSearch.trim().toLowerCase();
    if (!q) return [];
    return expandedUserNotAccessibleDocs.filter(
      (d) => d.title.toLowerCase().includes(q) || (d.doc_class?.toLowerCase().includes(q) ?? false)
    );
  }, [docSearch, expandedUserNotAccessibleDocs]);

  const filteredUsers = useMemo(() => {
    const q = userSearch.trim().toLowerCase();
    if (!q) return users;
    return users.filter(
      (u) =>
        u.name.toLowerCase().includes(q) ||
        u.username.toLowerCase().includes(q) ||
        u.email.toLowerCase().includes(q) ||
        u.roles.some((r) => r.toLowerCase().includes(q))
    );
  }, [users, userSearch]);

  const roleGrantedDocIds = useMemo(() => new Set(roleDocMap.keys()), [roleDocMap]);
  const roleAvailableDocs = useMemo(
    () => documents.filter((d) => !roleGrantedDocIds.has(d.id)),
    [documents, roleGrantedDocIds]
  );

  if (!can("ADMIN")) {
    return (
      <div className="notice is-danger">
        Administrator permission is required to manage users and file access.
      </div>
    );
  }

  const rc = selectedRole ? (ROLE_COLORS[selectedRole] ?? ROLE_COLORS["Administrator"]) : null;
  const isRoleLocked = selectedRole ? LOCKED_ROLES.has(selectedRole) : false;

  const pwdChecks = {
    length: createForm.password.length >= 16,
    classes: [
      /[a-z]/.test(createForm.password),
      /[A-Z]/.test(createForm.password),
      /\d/.test(createForm.password),
      /[^a-zA-Z0-9]/.test(createForm.password),
    ].filter(Boolean).length >= 3,
    noUsername: !createForm.username || !createForm.password.toLowerCase().includes(createForm.username.toLowerCase()),
    noDocvault: !createForm.password.toLowerCase().includes("docvault"),
  };
  const pwdValid = Object.values(pwdChecks).every(Boolean);
  const usernameValid = /^[a-z][a-z0-9._-]{2,79}$/.test(createForm.username);

  return (
    <div className="admin-page">
      <PageHeader
        eyebrow="Identity and authorization"
        title="Users & access"
        description="Roles describe what a user can do. Access rules decide which files they can see."
        actions={<StatusPill tone="success"><ShieldCheck size={14} /> Policy enforced at the API</StatusPill>}
      />

      {error && <div className="notice is-danger" role="alert">{error}</div>}

      <SectionCard className="acm-card acm-card-full">

        <div className="acm-header-row">
          <div className="acm-header-left">
            <span className="section-kicker">
              {acmMode === "role" ? "Role-based document control" : "User-based document control"}
            </span>
            <h2><Lock size={19} /> Access control matrix</h2>
          </div>
          <div className="acm-mode-toggle">
            <button
              id="acm-mode-role"
              className={`acm-mode-btn${acmMode === "role" ? " is-active" : ""}`}
              onClick={() => setAcmMode("role")}
            >
              <Shield size={14} /> Role Mode
            </button>
            <button
              id="acm-mode-user"
              className={`acm-mode-btn${acmMode === "user" ? " is-active" : ""}`}
              onClick={() => setAcmMode("user")}
            >
              <Users size={14} /> User Mode
            </button>
          </div>
        </div>

        {/* ── ROLE MODE ─────────────────────────────────────────────────── */}
        {acmMode === "role" && (
          <div className="acm-role-mode-body">
            <p className="admin-help" style={{ margin: "10px 0 0" }}>
              Role mode controls which documents are visible to each <strong>role</strong>.
              Granting a document here applies to all users holding that role.
              <strong> Super Admin</strong> and <strong>Administrator</strong> have implicit full access that cannot be changed.
            </p>

            <div className="acm-role-chips">
              {ROLE_ORDER.filter((r) => matrix?.roles[r]).map((role) => {
                const c = ROLE_COLORS[role];
                const userCount = users.filter((u) => u.roles.includes(role)).length;
                return (
                  <button
                    key={role}
                    className={`acm-role-chip${selectedRole === role ? " is-active" : ""}`}
                    style={selectedRole === role ? { background: c.bg, borderColor: c.border, color: c.text } : {}}
                    onClick={() => setSelectedRole(role)}
                  >
                    <span className="acm-role-chip-name">{role}</span>
                    {LOCKED_ROLES.has(role) && <Lock size={9} style={{ opacity: 0.55 }} />}
                    <span
                      className="acm-role-chip-count"
                      style={selectedRole === role ? { background: c.border, color: "#fff" } : {}}
                    >
                      {userCount}
                    </span>
                  </button>
                );
              })}
            </div>

            {!selectedRole && (
              <p className="admin-empty" style={{ marginTop: 14 }}>Select a role above to manage its document bucket.</p>
            )}

            {selectedRole && (
              <div className="acm-role-panel">
                <div className="acm-role-panel-header">
                  <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                    <span className="acm-role-badge" style={rc ? { background: rc.bg, borderColor: rc.border, color: rc.text } : {}}>
                      {selectedRole}
                    </span>
                    <span className="acm-role-doc-count">{roleDocMap.size} doc{roleDocMap.size !== 1 ? "s" : ""}</span>
                    {isRoleLocked && <span className="acm-lock-tag"><Lock size={10} /> Implicit full access</span>}
                  </div>
                  {!isRoleLocked && (
                    <div className="acm-add-doc-control">
                      <select className="acm-add-doc-select" value={roleAddDocId} onChange={(e) => setRoleAddDocId(e.target.value)}>
                        <option value="">Add document to this role…</option>
                        {roleAvailableDocs.map((d) => (
                          <option key={d.id} value={d.id}>#{d.id} · {d.title}</option>
                        ))}
                      </select>
                      <button className="button primary compact" onClick={addDocToRole} disabled={!roleAddDocId || roleBusy === "add"}>
                        <Plus size={13} />{roleBusy === "add" ? "Granting…" : "Grant to role"}
                      </button>
                    </div>
                  )}
                </div>

                {isRoleLocked && (
                  <div className="acm-locked-notice">
                    <ShieldCheck size={15} />
                    <span><strong>{selectedRole}</strong> has implicit full access to all documents. This cannot be modified.</span>
                  </div>
                )}

                {matrixLoading ? (
                  <p className="admin-empty" style={{ marginTop: 14 }}>Loading…</p>
                ) : !isRoleLocked && roleDocMap.size === 0 ? (
                  <p className="admin-empty" style={{ marginTop: 14 }}>
                    No documents granted to <strong>{selectedRole}</strong> yet. Use the control above.
                  </p>
                ) : (
                  <div className="acm-doc-grid">
                    {Array.from(roleDocMap.entries()).map(([docId, entry]) => (
                      <div
                        key={docId}
                        className={`acm-doc-card${isRoleLocked ? " is-locked" : ""}`}
                        style={rc && !isRoleLocked ? { borderColor: rc.border } : {}}
                      >
                        <div className="acm-doc-card-icon" style={rc ? { color: rc.text } : {}}><FileText size={24} /></div>
                        <div className="acm-doc-card-body">
                          <span className="acm-doc-card-title" title={entry.doc_title}>{entry.doc_title}</span>
                          {entry.doc_class && <span className="acm-doc-card-class">{entry.doc_class}</span>}
                          <span className="acm-doc-card-meta">{isRoleLocked ? "Implicit full access" : "Role Bucket"}</span>
                        </div>
                        {isRoleLocked
                          ? <span className="acm-doc-card-lock"><Lock size={12} /></span>
                          : isSuperAdmin && (
                            <button
                              className="acm-doc-card-revoke"
                              title={`Remove from ${selectedRole}`}
                              disabled={roleBusy === `revoke-${docId}`}
                              onClick={() => void revokeDocFromRole(docId)}
                            >
                              <X size={13} />
                            </button>
                          )
                        }
                      </div>
                    ))}
                  </div>
                )}

                {matrix && matrix.roles[selectedRole] && (
                  <details className="acm-role-caps" style={{ marginTop: 14 }}>
                    <summary>Capability set for {selectedRole}</summary>
                    <div className="acm-role-perms">
                      {matrix.roles[selectedRole].map((perm) => (
                        <span key={perm} className="perm-badge">{perm}</span>
                      ))}
                    </div>
                  </details>
                )}
              </div>
            )}
          </div>
        )}

        {/* ── USER MODE ─────────────────────────────────────────────────── */}
        {acmMode === "user" && (
          <div className="acm-user-mode-body">

            {/* Top bar */}
            <div className="acm-user-topbar">
              <div className="acm-user-search-wrap">
                <Search size={13} />
                <input
                  className="acm-user-search"
                  type="text"
                  placeholder="Search users by name, username, email or role…"
                  value={userSearch}
                  onChange={(e) => setUserSearch(e.target.value)}
                />
                {userSearch && (
                  <button className="acm-search-clear" onClick={() => setUserSearch("")}><X size={12} /></button>
                )}
              </div>
              <button className="button primary compact" onClick={() => setCreateFormOpen((v) => !v)} id="acm-create-user-btn">
                <UserPlus size={14} />{createFormOpen ? "Close form" : "New user"}
              </button>
            </div>

            {/* Create user form */}
            {createFormOpen && (
              <form className="acm-create-form" onSubmit={createUser}>
                <div className="acm-sub-heading" style={{ marginBottom: 10 }}>
                  <UserPlus size={15} style={{ opacity: 0.6 }} />
                  <span className="section-kicker">Create new user</span>
                </div>
                <div className="acm-create-form-grid">
                  <label className="field">
                    <span>Username</span>
                    <input
                      value={createForm.username}
                      onChange={(e) => setCreateForm({ ...createForm, username: e.target.value.toLowerCase() })}
                      required minLength={3} placeholder="e.g. j.smith" autoCapitalize="none"
                    />
                    {createForm.username && !usernameValid && (
                      <small style={{ color: "var(--danger)", marginTop: 3 }}>
                        Must start with a lowercase letter, 3-80 chars, alphanumeric/dot/dash/underscore only.
                      </small>
                    )}
                  </label>
                  <label className="field">
                    <span>Full name</span>
                    <input value={createForm.name} onChange={(e) => setCreateForm({ ...createForm, name: e.target.value })} required placeholder="John Smith" />
                  </label>
                  <label className="field">
                    <span>Email address</span>
                    <input type="email" value={createForm.email} onChange={(e) => setCreateForm({ ...createForm, email: e.target.value })} required placeholder="john@company.com" />
                  </label>
                  <label className="field">
                    <span>Temporary password</span>
                    <input type="password" value={createForm.password} onChange={(e) => setCreateForm({ ...createForm, password: e.target.value })} required minLength={16} placeholder="At least 16 characters" />
                  </label>
                  <label className="field">
                    <span>Initial role</span>
                    <select value={createForm.role} onChange={(e) => setCreateForm({ ...createForm, role: e.target.value })}>
                      {ROLE_ORDER.filter((r) => matrix?.roles[r]).map((role) => (
                        <option key={role}>{role}</option>
                      ))}
                    </select>
                  </label>
                </div>
                {createForm.password && (
                  <div className="acm-pwd-checklist">
                    <strong>Password requirements</strong>
                    <ul>
                      <li className={pwdChecks.length ? "ok" : ""}>At least 16 characters</li>
                      <li className={pwdChecks.classes ? "ok" : ""}>3+ character classes (uppercase, lowercase, number, symbol)</li>
                      <li className={pwdChecks.noUsername ? "ok" : "fail"}>Does not contain your username</li>
                      <li className={pwdChecks.noDocvault ? "ok" : "fail"}>Does not contain "docvault"</li>
                    </ul>
                  </div>
                )}
                <div className="acm-create-form-actions">
                  <button className="button primary" disabled={busy === "create" || !usernameValid || !pwdValid || !createForm.name || !createForm.email}>
                    <UserPlus size={15} />{busy === "create" ? "Creating…" : "Create user"}
                  </button>
                  <button type="button" className="button" onClick={() => setCreateFormOpen(false)}>Cancel</button>
                </div>
              </form>
            )}

            {/* Expandable user list */}
            <div className="acm-expandable-user-list">
              {filteredUsers.length === 0 ? (
                <p className="admin-empty" style={{ marginTop: 14 }}>No users match your search.</p>
              ) : (
                filteredUsers.map((item) => {
                  const isExpanded = expandedUserId === String(item.id);
                  const locked = isLockedUser(item);

                  return (
                    <div key={item.id} className={`acm-user-expand-row${isExpanded ? " is-expanded" : ""}`}>
                      {/* User row header */}
                      <div
                        className="acm-user-expand-header"
                        onClick={() => {
                          setExpandedUserId(isExpanded ? "" : String(item.id));
                          setDocSearch("");
                        }}
                      >
                        <span className="avatar is-small">{item.name.slice(0, 2).toUpperCase()}</span>
                        <span className="admin-user-copy">
                          <strong>{item.name}</strong>
                          <small>{item.username} · {item.email}</small>
                          <span className="acm-user-roles">
                            {item.roles.length > 0
                              ? item.roles.map((r) => {
                                  const c = ROLE_COLORS[r];
                                  return (
                                    <span key={r} className="acm-role-pill" style={c ? { background: c.bg, borderColor: c.border, color: c.text } : {}}>{r}</span>
                                  );
                                })
                              : <span className="acm-role-pill is-empty">No role assigned</span>
                            }
                          </span>
                        </span>
                        <span className="admin-user-actions" onClick={(e) => e.stopPropagation()}>
                          <StatusPill tone={toneForStatus(item.status)}>{item.status}</StatusPill>
                          <select aria-label={`Assign role to ${item.name}`} value="" onChange={(e) => void assignRole(item.id, e.target.value)} disabled={busy === `role-${item.id}`}>
                            <option value="">Assign role…</option>
                            {ROLE_ORDER.filter((r) => matrix?.roles[r]).map((role) => (
                              <option key={role} value={role}>{role}</option>
                            ))}
                          </select>
                          <button className="text-button compact" onClick={() => void setStatus(item)} disabled={busy === `status-${item.id}`}>
                            {item.status === "active" ? "Suspend" : "Reactivate"}
                          </button>
                        </span>
                        <span className="acm-expand-chevron">
                          {isExpanded ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
                        </span>
                      </div>

                      {/* Expanded document panel */}
                      {isExpanded && (
                        <div className="acm-user-doc-panel">
                          {locked ? (
                            <div className="acm-locked-notice">
                              <ShieldCheck size={16} />
                              <div>
                                <strong>{item.name}</strong> has implicit full access to all {documents.length} documents
                                via <strong>{item.roles.filter((r) => LOCKED_ROLES.has(r)).join(" / ")}</strong>.
                                <br />
                                <small style={{ opacity: 0.7 }}>Document access cannot be modified for privileged roles.</small>
                              </div>
                            </div>
                          ) : (
                            <>
                              {/* Grant search */}
                              <div className="acm-doc-grant-bar" ref={docSearchRef}>
                                <div className="acm-doc-grant-input-wrap">
                                  <Search size={13} />
                                  <input
                                    className="acm-doc-grant-input"
                                    type="text"
                                    placeholder="Search or click to see all available documents…"
                                    value={docSearch}
                                    onChange={(e) => setDocSearch(e.target.value)}
                                    onFocus={() => setDocSearchFocused(true)}
                                    onBlur={() => setTimeout(() => setDocSearchFocused(false), 150)}
                                    autoComplete="off"
                                  />
                                  {docSearch && (
                                    <button className="acm-search-clear" onClick={() => setDocSearch("")}><X size={12} /></button>
                                  )}
                                </div>
                                {/* Show dropdown on focus (all docs) or while typing (filtered) */}
                                {docSearchFocused && (
                                  <div className="acm-doc-search-dropdown">
                                    {(() => {
                                      const q = docSearch.trim().toLowerCase();
                                      const items = q
                                        ? expandedUserNotAccessibleDocs.filter(
                                            (d) =>
                                              d.title.toLowerCase().includes(q) ||
                                              (d.doc_class?.toLowerCase().includes(q) ?? false)
                                          )
                                        : expandedUserNotAccessibleDocs;
                                      return items.length > 0 ? (
                                        items.map((doc) => (
                                          <button
                                            key={doc.id}
                                            className="acm-doc-search-item"
                                            disabled={!!docGrantBusy[doc.id]}
                                            onMouseDown={(e) => e.preventDefault()} // keep focus on input
                                            onClick={() => {
                                              void grantDocToUser(item.id, doc.id, "ALLOW");
                                              setDocSearch("");
                                              setDocSearchFocused(false);
                                            }}
                                          >
                                            <FileText size={13} />
                                            <span className="acm-doc-search-title">{doc.title}</span>
                                            {doc.doc_class && <span className="acm-doc-search-class">{doc.doc_class}</span>}
                                            <span className="acm-doc-search-add"><Plus size={12} /> Grant</span>
                                          </button>
                                        ))
                                      ) : (
                                        <p className="acm-doc-search-empty">
                                          {q ? "No matching documents, or all are already granted." : "All documents are already accessible to this user."}
                                        </p>
                                      );
                                    })()}
                                  </div>
                                )}
                              </div>

                              {/* Accessible docs list */}
                              <div className="acm-user-accessible-docs">
                                {matrixLoading ? (
                                  <p className="admin-empty" style={{ padding: "14px" }}>Loading…</p>
                                ) : expandedUserDocs.length === 0 ? (
                                  <div className="acm-panel-empty">
                                    <FileText size={28} style={{ opacity: 0.2 }} />
                                    <p>No documents accessible to <strong>{item.name}</strong>.</p>
                                    <small>Use the search bar above to grant access to documents.</small>
                                  </div>
                                ) : (
                                  <>
                                    <div className="acm-user-doc-list-header">
                                      <span>Document</span>
                                      <span>Class</span>
                                      <span>Source</span>
                                      <span>Actions</span>
                                    </div>
                                    {expandedUserDocs.map(({ doc, source, rule }) => {
                                      const revoking   = docGrantBusy[doc.id] === "revoke";
                                      const denying    = docGrantBusy[doc.id] === "DENY";
                                      const restoring  = docGrantBusy[doc.id] === "revoke";
                                      return (
                                        <div key={doc.id} className={`acm-user-doc-row source-${source}`}>
                                          <span className="acm-user-doc-name">
                                            <FileText size={12} style={{ opacity: 0.45, flexShrink: 0 }} />
                                            <span title={doc.title}>{doc.title}</span>
                                          </span>
                                          <span className="acm-user-doc-class">
                                            {doc.doc_class ?? <em style={{ opacity: 0.3 }}>—</em>}
                                          </span>
                                          <span className="acm-user-doc-source">
                                            {source === "role" && (
                                              <span className="acm-source-badge role"><Shield size={10} /> Role</span>
                                            )}
                                            {source === "user-allow" && (
                                              <span className="acm-source-badge user"><Check size={10} /> User</span>
                                            )}
                                            {source === "user-deny" && (
                                              <span className="acm-source-badge denied"><X size={10} /> Denied</span>
                                            )}
                                          </span>
                                          <span className="acm-user-doc-actions">
                                            {/* User-level explicit allow → Remove button */}
                                            {source === "user-allow" && rule && (
                                              <button className="udoc-btn revoke" disabled={revoking} onClick={() => void revokeUserDocRule(rule.rule_id, doc.id)}>
                                                <X size={11} />{revoking ? "…" : "Remove"}
                                              </button>
                                            )}
                                            {/* Role-level source → SA can deny for this specific user */}
                                            {source === "role" && (
                                              isSuperAdmin ? (
                                                <button
                                                  className="udoc-btn deny-user"
                                                  disabled={denying}
                                                  title="Block this document for this user only (role access still applies to others)"
                                                  onClick={() => void grantDocToUser(item.id, doc.id, "DENY")}
                                                >
                                                  <X size={11} />{denying ? "…" : "Deny for user"}
                                                </button>
                                              ) : (
                                                <span className="acm-role-locked-label"><Lock size={10} /> Role level</span>
                                              )
                                            )}
                                            {/* User-deny → show Restore button to revoke the deny rule */}
                                            {source === "user-deny" && rule && (
                                              <button
                                                className="udoc-btn restore"
                                                disabled={restoring}
                                                title="Remove the deny override — user regains role-level access"
                                                onClick={() => void revokeUserDocRule(rule.rule_id, doc.id)}
                                              >
                                                <Check size={11} />{restoring ? "…" : "Restore"}
                                              </button>
                                            )}
                                          </span>
                                        </div>
                                      );
                                    })}
                                  </>
                                )}
                              </div>
                            </>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })
              )}
            </div>

            {/* Role capability guide */}
            {matrix && (
              <details className="role-matrix" style={{ marginTop: 20 }}>
                <summary>Role capability guide</summary>
                <div className="role-matrix-list">
                  {ROLE_ORDER.filter((r) => matrix.roles[r]).map((role) => {
                    const c = ROLE_COLORS[role];
                    return (
                      <div key={role}>
                        <strong style={c ? { color: c.text } : {}}>{role}</strong>
                        <span>{matrix.roles[role].join(" · ")}</span>
                      </div>
                    );
                  })}
                </div>
              </details>
            )}
          </div>
        )}
      </SectionCard>
    </div>
  );
}
