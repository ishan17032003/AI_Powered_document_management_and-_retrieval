import { FormEvent, ReactNode, useEffect, useState } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { AnimatePresence, motion } from "motion/react";
import {
  Bell,
  Bot,
  ChevronUp,
  Copy,
  FileSearch,
  FolderArchive,
  LayoutDashboard,
  Menu,
  Plus,
  ScrollText,
  Search,
  ShieldCheck,
  Trash2,
  UploadCloud,
  X,
} from "lucide-react";
import { useAuth } from "../auth";
import Brand from "./Brand";
import ThemeToggle from "./ThemeToggle";

const links = [
  { to: "/", label: "Dashboard", perm: null, icon: LayoutDashboard },
  { to: "/repository", label: "Repository", perm: "VIEW", icon: FolderArchive },
  { to: "/search", label: "Search", perm: "VIEW", icon: FileSearch },
  { to: "/ask", label: "Ask AI", perm: "VIEW", icon: Bot },
  { to: "/upload", label: "Capture", perm: "CREATE", icon: UploadCloud },
  { to: "/duplicates", label: "Duplicates", perm: "VIEW", icon: Copy },
  { to: "/trash", label: "Trash Bin", perm: "VIEW", icon: Trash2 },
  { to: "/audit", label: "Audit trail", perm: "VIEW_AUDIT", icon: ScrollText },
  { to: "/admin", label: "Users & access", perm: "ADMIN", icon: ShieldCheck },
];

function initials(name?: string) {
  if (!name) return "PA";
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length > 1) return `${words[0][0]}${words[1][0]}`.toUpperCase();
  return name.slice(0, 2).toUpperCase();
}

export default function Layout({ children }: { children: ReactNode }) {
  const { user, logout, can } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [isMobile, setIsMobile] = useState(() => window.matchMedia("(max-width: 959px)").matches);
  const [globalQuery, setGlobalQuery] = useState("");

  useEffect(() => {
    const media = window.matchMedia("(max-width: 959px)");
    const update = () => setIsMobile(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    setMobileOpen(false);
    setProfileOpen(false);
    window.scrollTo({ top: 0 });
  }, [location.pathname]);

  function runGlobalSearch(event: FormEvent) {
    event.preventDefault();
    const query = globalQuery.trim();
    if (!query) return;
    navigate(`/search?q=${encodeURIComponent(query)}`);
  }

  const sidebar = (
    <motion.aside
      className="sidebar"
      initial={false}
      animate={isMobile ? { x: mobileOpen ? 0 : "-102%" } : { x: 0 }}
      transition={{ type: "spring", stiffness: 430, damping: 38 }}
      aria-label="Primary navigation"
    >
      <div className="sidebar-head">
        <Brand />
        <button className="icon-button sidebar-close" onClick={() => setMobileOpen(false)} aria-label="Close navigation">
          <X size={20} />
        </button>
      </div>
      <nav className="nav">
        <div className="nav-label">Workspace</div>
        {links
          .filter((link) => !link.perm || can(link.perm))
          .map((link) => {
            const Icon = link.icon;
            return (
              <NavLink key={link.to} to={link.to} end={link.to === "/"}>
                {({ isActive }) => (
                  <>
                    {isActive && <motion.span className="nav-index" layoutId="nav-index" />}
                    <Icon className="nav-icon" size={19} strokeWidth={1.8} aria-hidden="true" />
                    <span>{link.label}</span>
                  </>
                )}
              </NavLink>
            );
          })}
      </nav>
      <div className="sidebar-spacer" />
      <div className="sidebar-user">
        <button
          className="user-summary"
          onClick={() => setProfileOpen((open) => !open)}
          aria-expanded={profileOpen}
        >
          <span className="avatar">{initials(user?.name)}</span>
          <span className="user-copy">
            <strong>{user?.name}</strong>
            <small>{user?.roles.join(", ")}</small>
          </span>
          <ChevronUp className={profileOpen ? "" : "is-collapsed"} size={17} />
        </button>
        <AnimatePresence initial={false}>
          {profileOpen && (
            <motion.div
              className="user-menu"
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
            >
              <button onClick={logout}>Sign out</button>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </motion.aside>
  );

  return (
    <div className="app-shell">
      {sidebar}
      <AnimatePresence>
        {isMobile && mobileOpen && (
          <motion.button
            className="sidebar-scrim"
            aria-label="Close navigation"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={() => setMobileOpen(false)}
          />
        )}
      </AnimatePresence>

      <div className="workspace">
        <header className="topbar">
          <div className="topbar-leading">
            <button className="icon-button menu-button" onClick={() => setMobileOpen(true)} aria-label="Open navigation">
              <Menu size={21} />
            </button>
            <div className="mobile-brand">
              <Brand compact />
            </div>
          </div>

          <form className="global-search" onSubmit={runGlobalSearch}>
            <Search size={19} aria-hidden="true" />
            <label className="sr-only" htmlFor="global-search">Search documents, people, or content</label>
            <input
              id="global-search"
              value={globalQuery}
              onChange={(event) => setGlobalQuery(event.target.value)}
              placeholder="Search documents, people or content"
            />
            <span className="search-hint" aria-hidden="true">↵</span>
          </form>

          <div className="topbar-actions">
            <ThemeToggle />
            {can("VIEW_AUDIT") && (
              <button className="icon-button" onClick={() => navigate("/audit")} aria-label="Open audit activity">
                <Bell size={19} />
              </button>
            )}
            {can("CREATE") && (
              <button className="button primary upload-button" onClick={() => navigate("/upload")}>
                <Plus size={18} />
                <span>Upload files</span>
              </button>
            )}
            <span className="avatar topbar-avatar" title={user?.name}>
              {initials(user?.name)}
            </span>
          </div>
        </header>

        <main className="workspace-content">{children}</main>
      </div>
    </div>
  );
}
