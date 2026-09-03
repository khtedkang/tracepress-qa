# Contributing

Tracepress QA is a compact demonstration, so changes should stay legible, deterministic, and dependency-free.

## Workflow

1. Create an issue describing the publishing risk or quality gate being addressed.
2. Add or update a failing `unittest` case before changing behavior.
3. Keep fixtures synthetic and free of names, internal paths, credentials, client material, or proprietary text.
4. Run the complete local check:

   ```bash
   python -m unittest discover -s tests -v
   python -m publishing_qa build --workspace sample --output sample/output
   python -m publishing_qa verify --output sample/output
   ```

5. Explain user-facing changes in `CHANGELOG.md`.

## Design guidelines

- Prefer a small explicit validator to a hidden heuristic.
- Emit actionable messages with a stable check code.
- Treat malformed, missing, or out-of-scope input as a blocking error.
- Keep generated output byte-for-byte deterministic for the same input.
- Add no network dependency to the build or test path.

By contributing, you agree that your contribution may be distributed under the MIT License.
