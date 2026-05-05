import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import EmailListPage from "./pages/EmailListPage";
import EmailDetailPage from "./pages/EmailDetailPage";
import LoginPage from "./pages/LoginPage";
import CasesPage from "./pages/CasesPage";
import NewCasePage from "./pages/NewCasePage";
import CaseDetailPage from "./pages/CaseDetailPage";
import SearchPage from "./pages/SearchPage";
import PersonsPage from "./pages/PersonsPage";
import ExportsPage from "./pages/ExportsPage";
import AuditPage from "./pages/AuditPage";
import { AuthProvider, RequireAuth, useAuth } from "./auth";

// Bump on every UI commit so operators can confirm via the header
// pill that their docker rebuild and browser cache actually picked
// up the new bundle. If you see an older tag than the one in
// ``git log`` for this file, you're on stale code — hard-refresh
// (Ctrl+Shift+R) or clear the browser's site cache.
const UI_BUILD_TAG = "ui-2026-05-05-build-pill";

const NAV = [
  { to: "/cases", label: "Cases" },
  { to: "/emails", label: "Emails" },
  { to: "/search", label: "Search" },
  { to: "/persons", label: "Persons" },
  { to: "/exports", label: "Exports" },
  { to: "/audit", label: "Audit" },
];

function Header() {
  const { user, logout } = useAuth();
  return (
    <header className="app-header">
      <div className="header-left">
        <h1>Paperclip</h1>
        {user ? (
          <nav className="top-nav">
            {NAV.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                className={({ isActive }) => (isActive ? "active" : "")}
              >
                {n.label}
              </NavLink>
            ))}
          </nav>
        ) : null}
      </div>
      <span
        title={
          "Bundle version. If this doesn't match the latest tag in " +
          "git log src/App.tsx, the browser is showing a cached old " +
          "bundle — Ctrl+Shift+R."
        }
        style={{
          fontSize: 10,
          color: "#888",
          background: "#f4f4f4",
          padding: "2px 8px",
          borderRadius: 10,
          marginRight: 12,
          fontFamily: "monospace",
        }}
      >
        {UI_BUILD_TAG}
      </span>
      {user ? (
        <div className="user-badge">
          <span className="user-name">{user.display_name ?? user.username}</span>
          <button onClick={logout} className="link-button">
            Sign out
          </button>
        </div>
      ) : null}
    </header>
  );
}

function Shell() {
  return (
    <>
      <Header />
      <main>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route
            path="/"
            element={<Navigate to="/cases" replace />}
          />
          <Route
            path="/cases"
            element={
              <RequireAuth>
                <CasesPage />
              </RequireAuth>
            }
          />
          <Route
            path="/cases/new"
            element={
              <RequireAuth>
                <NewCasePage />
              </RequireAuth>
            }
          />
          <Route
            path="/cases/:id"
            element={
              <RequireAuth>
                <CaseDetailPage />
              </RequireAuth>
            }
          />
          <Route
            path="/cases/:id/emails"
            element={
              <RequireAuth>
                <EmailListPage />
              </RequireAuth>
            }
          />
          <Route
            path="/emails"
            element={
              <RequireAuth>
                <EmailListPage />
              </RequireAuth>
            }
          />
          <Route
            path="/emails/:id"
            element={
              <RequireAuth>
                <EmailDetailPage />
              </RequireAuth>
            }
          />
          <Route
            path="/search"
            element={
              <RequireAuth>
                <SearchPage />
              </RequireAuth>
            }
          />
          <Route
            path="/persons"
            element={
              <RequireAuth>
                <PersonsPage />
              </RequireAuth>
            }
          />
          <Route
            path="/exports"
            element={
              <RequireAuth>
                <ExportsPage />
              </RequireAuth>
            }
          />
          <Route
            path="/audit"
            element={
              <RequireAuth>
                <AuditPage />
              </RequireAuth>
            }
          />
          <Route
            path="*"
            element={<p className="muted">404 — page not found.</p>}
          />
        </Routes>
      </main>
    </>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <Shell />
    </AuthProvider>
  );
}
