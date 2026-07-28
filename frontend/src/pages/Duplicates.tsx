import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AnimatePresence, motion } from "motion/react";
import { CopyCheck, FileStack, FileText, Star } from "lucide-react";
import { api, DupGroup } from "../api";
import { useAuth } from "../auth";
import { ConfirmDialog, EmptyState, PageHeader, SectionCard, StatusPill } from "../components/ui";

type PendingResolution = {
  group: DupGroup;
  primaryId: number;
  title: string;
};

export default function Duplicates() {
  const { can } = useAuth();
  const [groups, setGroups] = useState<DupGroup[]>([]);
  const [error, setError] = useState("");
  const [pending, setPending] = useState<PendingResolution | null>(null);
  const [resolving, setResolving] = useState(false);

  function load() {
    api.duplicates().then(setGroups).catch((err) => setError(err.message));
  }

  useEffect(load, []);

  async function resolve() {
    if (!pending) return;
    setResolving(true);
    setError("");
    try {
      await api.resolveDup(pending.group.id, pending.primaryId);
      setPending(null);
      load();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setResolving(false);
    }
  }

  return (
    <div>
      <PageHeader
        eyebrow="One canonical record"
        title="Duplicate review"
        description="Compare identical files and choose the document that should remain primary."
        actions={<StatusPill tone={groups.length ? "warning" : "success"}>{groups.length} open groups</StatusPill>}
      />

      {error && <div className="notice is-danger">{error}</div>}

      {groups.length === 0 ? (
        <SectionCard>
          <EmptyState
            icon={CopyCheck}
            title="No duplicate conflicts"
            description="Every stored document currently has a clear canonical source."
          />
        </SectionCard>
      ) : (
        <div className="duplicate-groups">
          <AnimatePresence initial={false}>
            {groups.map((group) => (
              <motion.div
                key={group.id}
                layout
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0, height: 0 }}
              >
                <SectionCard className="duplicate-card">
                  <div className="duplicate-heading">
                    <span className="duplicate-symbol"><FileStack size={21} /></span>
                    <div>
                      <span className="section-kicker">Conflict group {group.id}</span>
                      <h2>{group.members.length} identical documents</h2>
                    </div>
                    <StatusPill tone="warning">{group.similarity_type}</StatusPill>
                  </div>

                  <div className="duplicate-members">
                    {group.members.map((member) => {
                      const isPrimary = member.document_id === group.primary_document_id;
                      return (
                        <div className="duplicate-member" key={member.document_id}>
                          <span className="file-symbol"><FileText size={18} /></span>
                          <span className="duplicate-document">
                            <Link to={`/documents/${member.document_id}`}>{member.title}</Link>
                            <small>{Math.round(member.similarity_score * 100)}% content match</small>
                          </span>
                          {isPrimary ? (
                            <StatusPill tone="success"><Star size={12} /> Current primary</StatusPill>
                          ) : (
                            <span className="muted-label">Duplicate copy</span>
                          )}
                          {can("DELETE") && (
                            <button
                              className="button secondary compact"
                              onClick={() =>
                                setPending({
                                  group,
                                  primaryId: member.document_id,
                                  title: member.title,
                                })
                              }
                            >
                              Keep this file
                            </button>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </SectionCard>
              </motion.div>
            ))}
          </AnimatePresence>
        </div>
      )}

      <ConfirmDialog
        open={!!pending}
        title="Make this the primary document?"
        description={
          pending
            ? `“${pending.title}” will remain and the other copies in this group will be removed.`
            : ""
        }
        confirmLabel="Resolve duplicates"
        onConfirm={resolve}
        onCancel={() => setPending(null)}
        busy={resolving}
        tone="primary"
      />
    </div>
  );
}
