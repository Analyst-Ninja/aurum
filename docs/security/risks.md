# Security Review: `main`

Reviewed local `main` at commit `b084152`.

## Summary

Overall risk is **High**.

The biggest concerns are:

1. RDS/Postgres is publicly reachable by default.
2. `main` is not branch-protected.
3. The `Bootstrap AWS` workflow can assume the admin AWS role without the new `apply` approval gate.
4. GitHub environment protections exist, but they are weaker than ideal.
5. The AWS deploy role has full `AdministratorAccess`.

## Findings

### Critical: RDS Is Internet-Reachable by Default

Evidence:

- `db_publicly_accessible = true`
- `db_ingress_cidrs = ["0.0.0.0/0"]`
- Postgres ingress is applied on port `5432`

Impact:

Anyone on the internet can reach the database endpoint. The database password becomes the only meaningful protection.

Recommendation:

- Set `db_publicly_accessible = false`
- Set `db_ingress_cidrs = []`
- Use SSM port forwarding, VPN/Tailscale, or a temporary `/32` CIDR for operator access
- Rotate the DB password if this has already been live with `0.0.0.0/0`

Severity: **Critical**

---

### High: `main` Branch Is Not Protected

Evidence:

GitHub reported:

```text
Branch not protected
````

Impact:

Any account or token with write access can push directly to `main`. Since deployment runs from `main`, this is a direct path to changing AWS infrastructure.

Recommendation:

Enable branch protection or a repository ruleset:

- Require pull requests before merge
- Require status checks
- Block force pushes
- Block branch deletion
- Restrict who can push to `main`

Severity: **High**

***

### High: Bootstrap Workflow Can Bypass Apply Approval

Evidence:

`Bootstrap AWS` can assume the same AWS deploy role using OIDC, but it does not use the `apply` environment approval gate.

Impact:

The `apply` workflow now requires approval, but `Bootstrap AWS` can still assume the admin role from `main`.

Recommendation:

- Add an environment gate to `Bootstrap AWS`
- Use either `environment: apply` or a separate `bootstrap` environment
- Require reviewers
- Restrict the environment to `main`

Severity: **High**

***

### High: GitHub Environment Rules Are Weak

Observed settings:

- Required reviewers exist
- Self-review is allowed
- Admin bypass is allowed
- No deployment branch policy is configured

Impact:

The approval gate helps, but it is weaker than expected. A compromised admin or self-approved deployment can still proceed.

Recommendation:

- Enable prevent self-review
- Disable admin bypass if possible
- Restrict environment deployments to `main`
- Keep `apply` and `destroy` as separate environments

Severity: **High**

***

### Medium: AWS Deploy Role Has AdministratorAccess

Evidence:

The GitHub Actions role is attached to:

```
arn:aws:iam::aws:policy/AdministratorAccess
```

Impact:

If GitHub Actions/OIDC is abused, the blast radius is the full AWS account.

Recommendation:

- Short term: keep only if GitHub controls are hardened
- Medium term: replace with a scoped Terraform deploy policy
- Add alerting for `AssumeRoleWithWebIdentity` on `aurum-github-actions`

Severity: **Medium**

***

### Medium: MCP Falls Back to Owner DB Credentials

Evidence:

If `AURUM_MCP_USERNAME` is missing, MCP falls back to `AURUM_USERNAME` and `AURUM_PASSWORD`.

Impact:

The MCP server is intended to use a read-only role. Falling back to owner credentials weakens that boundary.

Recommendation:

- Fail closed if `AURUM_MCP_USERNAME` or `AURUM_MCP_PASSWORD` is missing
- Do not fall back to owner credentials

Severity: **Medium**

## Recommended Priority

1. Lock down RDS public access.
2. Protect `main`.
3. Add an approval environment to `Bootstrap AWS`.
4. Harden `apply` and `destroy` environment settings.
5. Reduce AWS deploy role permissions.
6. Remove MCP owner-credential fallback.

## Final Assessment

The project has some good controls already: pinned GitHub Actions, OIDC instead of long-lived AWS keys, environment approvals for apply/destroy, SSM parameters for secrets, encrypted storage, no MCP ingress, and read-only MCP query enforcement.

The main issue is that a few high-impact controls are currently incomplete. Fixing RDS exposure and GitHub branch/environment protections should be treated as urgent.