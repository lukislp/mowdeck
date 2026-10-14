# Security policy

MowDeck is currently a private repository. There is no public disclosure process yet.

- Report suspected vulnerabilities privately to the repository owner (do not open a public issue).
- The security baseline is the OWASP ASVS (Application Security Verification Standard); each phase gate reviews the relevant chapters.
- Secrets never go into the repository or into container images. Configuration and secrets are passed through environment variables or secret files at runtime. `.env` files are git-ignored; only `.env.example` with placeholder values is committed.
- Tenant isolation is the main risk of the platform: every service ships tests that request foreign-account data and expect rejection.
