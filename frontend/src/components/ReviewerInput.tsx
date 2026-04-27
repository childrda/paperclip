import { useEffect, useState } from "react";

const STORAGE_KEY = "foia.reviewer";

export function getReviewer(): string {
  return localStorage.getItem(STORAGE_KEY) ?? "";
}

export function ReviewerInput() {
  const [name, setName] = useState<string>(() => getReviewer());

  useEffect(() => {
    if (name) localStorage.setItem(STORAGE_KEY, name);
    else localStorage.removeItem(STORAGE_KEY);
  }, [name]);

  return (
    <div className="reviewer">
      <label htmlFor="reviewer-id">Reviewer:</label>
      <input
        id="reviewer-id"
        type="text"
        placeholder="your name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        autoComplete="off"
      />
    </div>
  );
}
