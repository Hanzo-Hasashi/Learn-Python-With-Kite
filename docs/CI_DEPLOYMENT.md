# CI and production deployment

## Continuous integration

The `CI` workflow runs on pull requests targeting `main` and pushes to `main`.
Its single required check, `CI / quality`, runs the complete Python unittest
suite, Node proxy lifecycle tests, Python compilation, JavaScript syntax checks,
YAML/JSON validation, and a whitespace check. It requires no deployment or
application secrets.

The `main` branch requires `CI / quality` to pass before updates can be merged.
If GitHub Actions is unavailable, the required check intentionally blocks
merges rather than treating an unstarted or skipped check as success.

## Vercel frontend

The frontend uses Vercel's native Git integration; no competing Vercel CLI
workflow or deployment token is configured. GitHub reported a successful
`Vercel` deployment status for the current production commit during this audit.
The production health-check workflow can separately confirm that the public
frontend responds, but that availability check does not establish which commit
is deployed. Use the Vercel status on the commit to verify commit association.

The Vercel project's dashboard settings, including its root directory, were
not accessible for inspection in this repository session. Keep the frontend
project configured for the `web` directory as documented in the Render
deployment guide.

## Render backend

The `kite-backend` Render service is linked to this repository's `main` branch,
but its current `autoDeploy` setting is **Off**. Its live commit predates the
account-deletion reconciliation migration. The current backend performs
fail-closed startup schema validation, so enabling deployment before an
authorized maintainer applies and verifies the required migration could make a
new deployment fail its health check. This work does not run migrations or
change the production service setting.

After the migration has been handled through the approved production process,
an authorized maintainer can enable Render's native **After CI Checks Pass**
auto-deploy setting. Configure build filters for backend-relevant paths so
unrelated frontend-only or documentation changes do not redeploy the backend.
Render's configured `/health` endpoint is the deployment liveness probe; it
does not query the database.

The manual `Production Health Checks` workflow checks the Render `/health`
endpoint and the Vercel homepage with bounded retries suitable for cold starts.
It checks availability only and does not prove that either provider deployed a
particular commit. The `/health/ready` endpoint performs a database query and
is intentionally not called by this workflow.

## External blockers observed during this audit

- GitHub Actions runs were blocked before runner startup because the GitHub
  account was locked due to a billing issue. Restore Actions billing to obtain
  hosted CI results.
- Render automatic deployment remains disabled until the production migration
  prerequisite is satisfied and the service is deliberately switched to
  **After CI Checks Pass**.
