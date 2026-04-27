import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import EmailListPage from "./pages/EmailListPage";
import EmailDetailPage from "./pages/EmailDetailPage";
import ImportPage from "./pages/ImportPage";
import SearchPage from "./pages/SearchPage";
import PersonsPage from "./pages/PersonsPage";
import ExportsPage from "./pages/ExportsPage";
import AuditPage from "./pages/AuditPage";
import { ReviewerInput } from "./components/ReviewerInput";

const NAV = [
  { to: "/imports", label: "Import" },
  { to: "/emails", label: "Emails" },
  { to: "/search", label: "Search" },
  { to: "/persons", label: "Persons" },
  { to: "/exports", label: "Exports" },
  { to: "/audit", label: "Audit" },
];

export default function App() {
  return (
    <>
      <header className="app-header">
        <div className="header-left">
          <h1>FOIA Redaction Review</h1>
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
        </div>
        <ReviewerInput />
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Navigate to="/imports" replace />} />
          <Route path="/imports" element={<ImportPage />} />
          <Route path="/emails" element={<EmailListPage />} />
          <Route path="/emails/:id" element={<EmailDetailPage />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/persons" element={<PersonsPage />} />
          <Route path="/exports" element={<ExportsPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route
            path="*"
            element={<p className="muted">404 — page not found.</p>}
          />
        </Routes>
      </main>
    </>
  );
}
