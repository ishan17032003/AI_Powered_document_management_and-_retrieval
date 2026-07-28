import { Link } from "react-router-dom";

export default function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <Link className={`brand-lockup ${compact ? "is-compact" : ""}`} to="/" aria-label="XENIUS DocVault home">
      <span className="brand-mark" aria-hidden="true">
        <span />
        <span />
        <span />
      </span>
      {!compact && (
        <span className="brand-copy">
          <strong>XENIUS</strong>
          <span>DOCVAULT</span>
          <small>Document intelligence</small>
        </span>
      )}
    </Link>
  );
}
