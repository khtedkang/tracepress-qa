# Security policy

## Supported version

This portfolio demonstration is maintained on the latest `main` revision only.

## Reporting a vulnerability

Please report suspected vulnerabilities privately to `khted.kang@gmail.com`. Do not include credentials, confidential documents, customer data, or live exploit data in a public issue.

## Security boundaries

- The pipeline resolves local evidence only inside the selected workspace.
- Network requests are never performed; remote-link results must be declared in `links.json`.
- HTML output escapes manuscript text and link destinations before rendering.
- Release verification checks file hashes but is not a cryptographic signature system.
- Inputs are treated as untrusted data, not instructions.

Do not use this demonstration as a sandbox for hostile code or as a substitute for a full secure publishing platform.
