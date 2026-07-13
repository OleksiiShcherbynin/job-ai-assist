# Security & Threat Model

## Overview

`job-ai-assist` is a Jupyter-notebook-driven Python application that scrapes job postings from Profesia.sk, extracts structured data via LLM (Google Gemini), and matches them against a user's resume. It operates entirely offline except for external API calls (HTTP scraping, LLM inference). No web server, no user-facing API.

## Threat Model (STRIDE-Lite)

| Threat | Surface | Risk | Mitigation |
|--------|---------|------|-----------|
| **Prompt Injection** | LLM prompts embed untrusted scraped job text and resume directly as instructions | HIGH | System role enforces data boundary; scraped content fenced in XML tags; untrusted text clamped to 20k chars; suspicious delimiters stripped |
| **Supply Chain (Vulnerable Dependencies)** | No dependency pinning; unknown CVE exposure | MEDIUM | All main deps pinned in requirements.txt; CI audits with pip-audit; pre-commit runs bandit (SAST) |
| **Scraped Redirect to Off-Site** | `follow_redirects=True` on URLs from scraped href attributes without host restriction | LOW | Redirect target URLs restricted to profesia.sk via hostname allowlist |
| **Path Traversal in Resume Loading** | Function accepts caller-supplied filepath with no validation | LOW (mitigated by hardcoded usage) | Path must be under data/; .pdf extension enforced; resolved and validated before opening |
| **PII Exposure (Resume Data)** | Resume PDF and extracted contact info (phone, email) handled locally; sent to Google Gemini API | MEDIUM | Local data gitignored after cleanup; resume kept off-disk by default; transparently flows through LLM provider; pre-commit nbstripout prevents saved notebook outputs from leaking PII |
| **Secrets Exposure** | API key in plaintext .env on disk | LOW | .env gitignored (never committed); no keys in source; local file only; `.env.example` template provided |

## Security Controls in Place

### Code-Level
- **No dangerous patterns:** No eval, exec, os.system, subprocess.shell=True, pickle, yaml.load, or shell injection vectors.
- **TLS verification:** All outbound HTTPS enforced (httpx default verify=True).
- **Timeout protection:** 30s timeout on HTTP and LLM calls.
- **Input validation:** Page bounds checked (start_page, stop_page); score clamped 0-100; file paths validated.
- **LLM output schema:** Pydantic models constrain extraction and scoring outputs; strict=False with defense-in-depth clamp.

### DevOps & CI
- **SAST:** bandit scans Python code for security anti-patterns.
- **SCA (Software Composition Analysis):** pip-audit audits dependencies for known CVEs.
- **Secret scanning:** gitleaks detects hardcoded secrets in git history and diffs.
- **Pre-commit hooks:** Enforces clean notebook outputs (nbstripout), files-only checks (no large binaries), trailing whitespace removal.
- **Dependency pinning:** All package versions locked for reproducibility and auditability.

### Data Handling
- **Resume storage:** Kept local under `data/` (gitignored); never committed to public repo.
- **PII in transit:** Resume text sent to Google Gemini API (US, Google's ToS apply); no PII in logs or notebooks (nbstripout).
- **Scraped vacancy content:** Treated as untrusted; fenced and never allowed as LLM instructions.

## What is Intentionally NOT Addressed

- **Multi-user access control:** This is a single-user CLI tool; no auth/authz.
- **Database security:** No database — all data in-memory or JSON files.
- **API rate limiting on Profesia:** Tool includes request throttling (0.5s delay) for politeness, not security.
- **Privacy policy:** PII processing (resume → Gemini) is a feature; user is responsible for consenting to data flows.

## Responsible Disclosure

If you discover a security vulnerability, please email it privately (do not open a public GitHub issue):

- Email: shcherbininvg@gmail.com
- Expected response time: 48-72 hours.

Do not publicly disclose until a patch is available.

## Data Flows

### Resume → LLM
1. Local PDF file (`data/CV_*.pdf`) read via pypdf → extracted text.
2. Text sent to Google Gemini API (instruction: extract profile structure).
3. Response parsed into Pydantic `CandidateProfile` model.
4. Returned to notebook (no persistence; printed to stdout only, now redacted via nbstripout).

### Profesia.sk Scrape → LLM
1. HTTP GET to Profesia listing (allowlisted host: www.profesia.sk); parse vacancy links.
2. 0.5s throttle between detail-page fetches (politeness).
3. Extract job posting text; store in cache (`fixtures/profesia_vacancies.json`).
4. Send each vacancy to Gemini scoring API with system role + fenced untrusted data.
5. Return match scores (0-100) and reasons; filter/sort locally.

### Environment & Keys
- `GOOGLE_API_KEY` loaded from `.env` file (developer's machine only).
- Never logged, never committed, rotated after public history exposure.

## Compliance & Standards

- **OWASP LLM Top 10:** Mitigates LLM01 (Prompt Injection), LLM10 (Model Theft / PII Leakage).
- **OWASP Top 10 (2021):** No instances of A01 (Broken Access Control), A03 (Injection), A05 (Broken Access Control), A06 (Vulnerable Components).
- **CWE:** Prevents CWE-22 (Path Traversal), CWE-78 (OS Command Injection), CWE-94 (Code Injection).

## Version History

- **v1.0 (2026-07-13):** Security hardening — prompt injection mitigation, dependency audit, SAST/SCA CI, PII cleanup.
