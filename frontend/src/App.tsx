import { Link, Navigate, Route, Routes } from "react-router-dom";
import EmailListPage from "./pages/EmailListPage";
import EmailDetailPage from "./pages/EmailDetailPage";
import { ReviewerInput } from "./components/ReviewerInput";

export default function App() {
  return (
    <>
      <header className="app-header">
        <h1>
          <Link to="/emails">FOIA Redaction Review</Link>
        </h1>
        <ReviewerInput />
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Navigate to="/emails" replace />} />
          <Route path="/emails" element={<EmailListPage />} />
          <Route path="/emails/:id" element={<EmailDetailPage />} />
          <Route
            path="*"
            element={<p className="muted">404 — page not found.</p>}
          />
        </Routes>
      </main>
    </>
  );
}
