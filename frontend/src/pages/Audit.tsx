import { useEffect, useMemo, useState } from "react";
import { motion } from "motion/react";
import { Activity, Search, ScrollText } from "lucide-react";
import { api, AuditRow } from "../api";
import { EmptyState, PageHeader, SectionCard, StatusPill } from "../components/ui";

function presentAction(action: string) {
  return action
    .toLowerCase()
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

export default function Audit() {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");

  useEffect(() => {
    api.audit().then(setRows).catch((err) => setError(err.message));
  }, []);

  const shown = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((row) =>
      [row.action, row.actor_name, row.object_type, row.object_id]
        .filter(Boolean)
        .some((value) => value.toLowerCase().includes(needle))
    );
  }, [filter, rows]);

  return (
    <div>
      <PageHeader
        eyebrow="Accountable by design"
        title="Audit trail"
        description="A chronological record of document and workspace activity."
        actions={
          <StatusPill tone="success">
            <Activity size={13} /> Immutable history
          </StatusPill>
        }
      />

      {error && <div className="notice is-danger">{error}</div>}

      <SectionCard className="audit-card">
        <div className="collection-toolbar">
          <label className="search-field">
            <Search size={18} aria-hidden="true" />
            <span className="sr-only">Filter audit trail</span>
            <input
              placeholder="Filter by action, person, or object"
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
            />
          </label>
          <span className="result-count">{shown.length} events</span>
        </div>

        {shown.length ? (
          <div className="table-scroll">
            <table className="data-table responsive-table audit-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Person</th>
                  <th>Action</th>
                  <th>Object</th>
                  <th>Location</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((row, index) => (
                  <motion.tr
                    key={row.id}
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    transition={{ delay: Math.min(index * 0.015, 0.16) }}
                  >
                    <td data-label="Time" className="table-date">
                      {new Date(row.timestamp).toLocaleString([], {
                        month: "short",
                        day: "numeric",
                        hour: "numeric",
                        minute: "2-digit",
                      })}
                    </td>
                    <td data-label="Person">
                      <span className="person-cell">
                        <span className="avatar is-tiny">{row.actor_name.slice(0, 2).toUpperCase()}</span>
                        {row.actor_name}
                      </span>
                    </td>
                    <td data-label="Action"><StatusPill>{presentAction(row.action)}</StatusPill></td>
                    <td data-label="Object">
                      <span className="object-cell">
                        <strong>{row.object_type || "Workspace"}</strong>
                        {row.object_id && <small>#{row.object_id}</small>}
                      </span>
                    </td>
                    <td data-label="Location" className="mono-cell">{row.ip || "Local"}</td>
                    <td data-label="Details">
                      <code className="details-cell" title={row.details}>{row.details || "—"}</code>
                    </td>
                  </motion.tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            icon={filter ? Search : ScrollText}
            title={filter ? "No matching activity" : "No activity recorded"}
            description={
              filter
                ? "Try a broader person, action, or object."
                : "New document and workspace actions will appear here."
            }
            action={
              filter ? (
                <button className="button secondary" onClick={() => setFilter("")}>Clear filter</button>
              ) : undefined
            }
          />
        )}
      </SectionCard>
    </div>
  );
}
