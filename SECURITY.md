# Security and responsible use

ProspectPilot is a research/portfolio project. Its guardrails are part of the design:

- **No real email.** `agents/mailer.py` refuses every SMTP host except the local Mailpit sandbox
  unless `ALLOW_REAL_SEND=true` *and* the CLI's `--i-understand` flag are both set. Please keep it
  that way; real cold outreach has legal requirements (CAN-SPAM, GDPR, opt-out handling) this
  project does not implement.
- **Allowed sources only.** HN (Algolia API), GitHub REST API, public company pages (robots.txt
  respected, 1 req/s per domain) and CSV files you own. Do not add scrapers for sites whose terms
  forbid it (e.g. LinkedIn).
- **No SMTP probing** of third-party mail servers for verification.
- **Secrets** live in `.env` (gitignored) locally and in AWS Secrets Manager when deployed.

## Reporting a vulnerability

Please open a private security advisory on GitHub
(Security → Advisories → "Report a vulnerability") rather than a public issue.
