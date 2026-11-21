# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately through GitHub: **Security → Report a vulnerability** on
[this repository](https://github.com/pratheek888/mcp-devops-agents/security/advisories/new).
Include what you found, how to reproduce it, and the impact you expect.

You can expect an acknowledgement within 3 working days and a fix or mitigation plan within 14 days
for confirmed issues. Credit is given in the release notes unless you prefer otherwise.

## Supported versions

Only the latest release on `main` receives security fixes.

## How this repository protects itself

- Dependabot keeps dependencies and GitHub Actions up to date (`.github/dependabot.yml`)
- CI scans for committed secrets with Gitleaks on every push
- OpenSSF Scorecard runs weekly and publishes results (`.github/workflows/scorecard.yaml`)
- `main` is protected: changes land through pull requests with passing CI
