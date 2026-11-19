# Agri Stack Composite console (agri-composite-ui)

A read-only web console for the Agri Stack use-case composite service: what the composite is
configured with, the published use cases (sources, consent scopes, output fields), the
registries it reads from and their data scopes, the partners granted it (use-case policies) and their keys
in Partner Management, and the calls it has served.

Built with Next.js 16 (standalone output), React 19, next-intl, Tailwind 4 and lucide-react, on
the same shell as the OpenG2P Master Data UI (branding, layout, IAM login and permissions).

## How it works

- **Sign-in** goes through the OpenG2P IAM staff portal API: `/api/login` starts the IAM
  authentication transaction, `/api/me` reads the logged-in user, `/api/permissions` reads the
  user's permissions for `APPLICATION_MNEMONIC`, `/api/logout` ends the session.
- **Data** comes from the composite's admin API. The browser calls `/api/admin/<path>`; the
  Next.js server forwards it as `GET <BACKEND_URL>/composite/v1/admin/<path>` with the user's
  IAM session (Bearer token, auth cookies, CSRF token) and relays refreshed cookies back. Only
  the admin paths below are forwarded.
- **Access** needs the IAM permission `composite:view` (roles `AGRI_COMPOSITE_VIEWER` or
  `AGRI_COMPOSITE_ADMIN`). Without it the console shows "Access denied"; the backend enforces
  the same permission. An expired session (401) sends the user back to the IAM login.

## Backend it needs

The composite API (`agri-composite-api`) with its admin endpoints and IAM session auth enabled:

| Page | Endpoint |
| --- | --- |
| Overview | `GET /composite/v1/admin/overview` |
| Use cases | `GET /composite/v1/admin/use-cases`, `GET /composite/v1/admin/use-cases/{ref}` |
| Registries | `GET /composite/v1/admin/registries`, `GET /composite/v1/admin/registries/{id}/data-scopes` |
| Partners | `GET /composite/v1/admin/partners` |
| Activity | `GET /composite/v1/admin/activity?limit&offset&partner&use_case&outcome` |

An IAM instance with the `agri-composite` application (permissions `composite:view`,
`composite:manage`) and a login provider is needed for sign-in.

## Environment

All variables are read at request time (pod env), so one image serves every environment.

| Variable | Description | Example |
| --- | --- | --- |
| `BACKEND_URL` | Composite API base URL | `http://agri-composite-api` |
| `IAM_URL` | OpenG2P IAM staff portal API base URL | `https://iam.example.org` |
| `LOGIN_PROVIDER_ID` | IAM login provider ID | `1` |
| `APPLICATION_MNEMONIC` | IAM application whose permissions are read (default `agri-composite`) | `agri-composite` |
| `COOKIE_DOMAIN` | Parent domain auth cookies are rewritten to (optional) | `.example.org` |
| `DEFAULT_LOCALE` | Default UI locale (optional, default `en`) | `en` |

See `.env.example`.

## Develop

```bash
cp .env.example .env.local   # then edit
npm install && npm run dev   # http://localhost:3000
npm run lint
npm run build
```

Code layout: `src/app` (pages under `[locale]/`, API routes under `api/`), `src/features/<name>`
(`api.ts`, `hooks.ts`, `types.ts`, `components/`) for overview, use-cases, registries, partners
and activity, shared UI in `src/components`, auth and permissions in `src/context`. Text is in
`locales/en.json`; to add a language, add `locales/<locale>.json` and the locale to
`src/i18n/routing.ts`.

## Docker

The image is built from the `composite/` folder of the agri-stack repo:

```bash
docker build -f composite/docker/agri-composite-ui/Dockerfile composite
```

It listens on port 3000 (`node server.js`).
