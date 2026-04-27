FOIA Redaction Tool — Phase-Based Build Prompt (Container-Aware)
Role & Objective

You are a senior full-stack engineer building a local-first FOIA redaction system for K–12 school districts.

This system must:

Be legally defensible
Be non-destructive
Be fully auditable
Work offline by default
Be easy to deploy across multiple districts

You will build this system in strict phases.

Critical Rule

Do NOT build future phases early.
Each phase must be:

Fully working
Tested
Documented
Clean and maintainable
Architecture Overview
Stack
Backend: Python + FastAPI
Frontend: React + TypeScript
Database: SQLite (with FTS5)
OCR: Tesseract
Document conversion: LibreOffice (headless)
PII detection: Microsoft Presidio
PDF export: WeasyPrint
Deployment Requirement (Global)

The system must be container-ready and deployable via Docker Compose.

Rules
Use environment variables for all configuration
No hardcoded paths
Dependencies must be explicitly defined
Code must run locally without Docker during early phases
Structure must allow seamless containerization later
Containerization Strategy
Phase 1–2: Local development only (container-ready structure)
Phase 3–5: Add backend Dockerfile
Phase 6+: Full Docker Compose stack (backend + frontend)
Phase 1 — Ingestion Engine (Foundation)
Goal

Build a standalone ingestion system for .mbox files.

Features
Parse emails:
body (text + HTML → sanitized)
headers (from, to, cc, subject, date)
Extract attachments:
PDF
images
nested .eml
Strip:
JavaScript
tracking pixels
external references
Storage

SQLite database:

emails
attachments
raw_content
Interface

CLI only:

python ingest.py --file sample.mbox
Constraints
No UI
No redaction
No AI
Deliverables
Working ingestion pipeline
Database schema
Unit tests
Sample dataset
Phase 2 — Attachment Processing + OCR
Goal

Convert all attachments into searchable text.

Features
PDF text extraction
OCR for scanned PDFs and images
Office documents → PDF via LibreOffice
Store extracted text in DB
Requirements
Track source relationships
Log failures cleanly
Deliverables
Processing pipeline
Error handling
Tests with mixed file types
Phase 3 — PII Detection Engine
Goal

Detect sensitive information with high recall

Features
Integrate Microsoft Presidio
Detect:
SSN
DOB
email
phone
Add configurable custom recognizers:
student IDs
district-specific identifiers
Output

Store:

entity type
start/end position
confidence score
Deliverables
Detection service
YAML-configurable patterns
Evaluation harness (precision/recall)
Rule

Missing PII is unacceptable.

Phase 4 — Entity Resolution
Goal

Unify identities across the dataset

Features
Link identities using:
email addresses
names
signatures
Build a person table
Map people to occurrences
Deliverables
Entity resolution logic
Merge strategy
Manual override (CLI)
Phase 5 — Backend API
Goal

Expose system data via API

Build

FastAPI endpoints:

list emails
view email
view attachments
view PII detections
Features
pagination
filtering
full-text search (FTS5)
Deliverables
API server
OpenAPI documentation
Phase 6 — Redaction System (Core Legal Layer)
Goal

Implement non-destructive redactions

Rules
Source content is immutable
Redactions stored as spans:
document_id
start/end
exemption_code
reviewer_id
Deliverables
Redaction schema
CRUD API
Validation rules
Phase 7 — Minimal UI
Goal

Provide a usable review interface

Build

React frontend:

email list
email viewer
highlight detected PII
accept/reject redactions
Requirements
simple, clear UX
no over-engineering
Phase 8 — PDF Export
Goal

Generate legally usable output

Features
burned-in redactions
black boxes
Bates numbering
exemption codes
redaction log (PDF + CSV)
Deliverables
Export service
Sample output
Phase 9 — Audit Logging
Goal

Full traceability

Track
ingestion
detection
redactions
edits
exports
Requirements
immutable logs
timestamp + actor
Phase 10 — Optional AI QA Layer
Goal

Assist reviewers without replacing them

Features
Pluggable AI backend:
local (Ollama)
OpenAI / Azure / Anthropic
AI returns:
flagged risks
AI NEVER auto-redacts
Deliverables
AI interface
config toggles
per-case override
Configuration System

All district-specific settings must live in a single YAML file:

district name
email domains
ID patterns
exemption codes
Bates prefix
AI settings

No code changes required per district.

Evaluation Requirement

Build evaluation alongside detection:

synthetic K–12 dataset generator
labeled PII
precision/recall metrics
Final Rules
Local-first by default
Human approval required for all redactions
No destructive edits
Keep configuration simple
Avoid unnecessary complexity