# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's private vulnerability reporting:
https://github.com/lukislp/mowdeck/security/advisories/new

Do not open a public issue for security problems. You will get an acknowledgement within a few
days and a fix or mitigation plan before any public disclosure. A report that involves a mower
manufacturer's cloud or firmware is passed on to the manufacturer only with your consent.

## Supported versions

Only the latest release and the `main` branch receive security fixes.

## Security baseline

- The baseline is the OWASP ASVS (Application Security Verification Standard); each phase gate reviews the relevant chapters.
- Secrets never go into the repository or into container images. Configuration and secrets are passed through environment variables or secret files at runtime. `.env` files are git-ignored; only `.env.example` with placeholder values is committed.
- Tenant isolation is the main risk of the platform: every service ships tests that request foreign-account data and expect rejection.
