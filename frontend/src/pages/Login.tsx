import { useState } from "react";
import { motion } from "motion/react";
import { ArrowRight, Check, LockKeyhole, Search, ShieldCheck, UserRound } from "lucide-react";
import { useAuth } from "../auth";
import Brand from "../components/Brand";
import ThemeToggle from "../components/ThemeToggle";

const DEMO_ROLES = [
  ["admin", "Full workspace"],
  ["contributor", "Capture and edit"],
  ["viewer", "View documents"],
  ["auditor", "Audit access"],
] as const;

export default function Login() {
  const { login } = useAuth();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      await login(username, password);
    } catch (err: any) {
      setError(err.message || "The username or password was not recognized.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-screen">
      <motion.section
        className="login-story"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.28 }}
      >
        <Brand />
        <div className="login-story-copy">
          <span className="eyebrow is-light">Secure document intelligence</span>
          <h1>Every document.<br />One trusted record.</h1>
          <p>
            Capture, understand, find, and govern business documents from one accountable workspace.
          </p>
        </div>

        <div className="login-archive" aria-hidden="true">
          <div className="login-archive-stack is-contract"><span /></div>
          <div className="login-archive-stack is-invoice"><span /></div>
          <div className="login-archive-stack is-report"><span /></div>
          <div className="login-archive-shelf" />
        </div>

        <div className="login-assurances">
          <span><ShieldCheck size={17} /> Role-based access</span>
          <span><Search size={17} /> Grounded document search</span>
          <span><Check size={17} /> Immutable activity history</span>
        </div>
      </motion.section>

      <section className="login-panel">
        <ThemeToggle className="login-theme-toggle" />
        <motion.form
          className="login-form"
          onSubmit={submit}
          initial={{ opacity: 0, x: 18 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ type: "spring", stiffness: 360, damping: 34, delay: 0.04 }}
        >
          <div className="login-form-heading">
            <span className="eyebrow">DocVault workspace</span>
            <h2>Welcome back</h2>
            <p>Sign in to continue to your document archive.</p>
          </div>

          <label className="field">
            <span>Username</span>
            <span className="input-shell">
              <UserRound size={18} aria-hidden="true" />
              <input
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                autoComplete="username"
                required
              />
            </span>
          </label>

          <label className="field">
            <span>Password</span>
            <span className="input-shell">
              <LockKeyhole size={18} aria-hidden="true" />
              <input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
                required
              />
            </span>
          </label>

          {error && <div className="notice is-danger" role="alert">{error}</div>}

          <motion.button
            className="button primary login-submit"
            disabled={busy}
            whileTap={busy ? undefined : { scale: 0.985 }}
          >
            <span>{busy ? "Signing in…" : "Sign in"}</span>
            {!busy && <ArrowRight size={18} />}
          </motion.button>

          <div className="demo-accounts">
            <div className="demo-heading">
              <span>Demo access</span>
              <small>Select a role to fill the username</small>
            </div>
            <div className="demo-grid">
              {DEMO_ROLES.map(([account, role]) => (
                <button
                  type="button"
                  key={account}
                  onClick={() => {
                    setUsername(account);
                    setPassword("");
                  }}
                >
                  <strong>{account}</strong>
                  <small>{role}</small>
                </button>
              ))}
            </div>
            <p className="login-help">
              Select a role to fill the username, then enter the password
              provisioned by the backend.
            </p>
          </div>
        </motion.form>
      </section>
    </main>
  );
}
