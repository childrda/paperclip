import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { ApiError } from "../api";
import { useAuth } from "../auth";

export default function LoginPage() {
  const { user, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const fallback = (location.state as { from?: string } | null)?.from || "/cases";

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (user) {
    navigate(fallback, { replace: true });
    return null;
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await login(username.trim(), password);
      navigate(fallback, { replace: true });
    } catch (e) {
      // Server returns a generic error string; we don't try to be clever.
      const msg =
        e instanceof ApiError ? "Sign in failed. Check username and password." : String(e);
      setError(msg);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-frame">
      <form onSubmit={submit} className="login-card">
        <h1>Paperclip</h1>
        <p className="muted">FOIA review for K–12 districts.</p>

        <label>
          Username
          <input
            type="text"
            autoComplete="username"
            autoFocus
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            disabled={busy}
            required
          />
        </label>
        <label>
          Password
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={busy}
            required
          />
        </label>
        {error ? <div className="error">{error}</div> : null}
        <button type="submit" disabled={busy || !username || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <p className="muted small">
          Sign in with your district directory credentials. Access is
          limited to members of the configured FOIA security group.
        </p>
      </form>
    </div>
  );
}
