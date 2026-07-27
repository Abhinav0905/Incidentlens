# Security Policy

IncidentLens may process sensitive operational telemetry.

## Never submit

- API keys
- database passwords
- customer data
- proprietary logs
- production architecture details
- private incident transcripts

## Reporting a vulnerability

Do not open a public issue for security vulnerabilities.

Contact the maintainer privately through the security contact listed in the repository profile.

Include:

- affected version
- reproduction steps
- impact
- suggested remediation, when available

## Safe deployment guidance

- run behind authentication
- use read-only telemetry credentials
- redact secrets before analysis
- encrypt stored artifacts
- enforce retention limits
- require human approval before operational changes
