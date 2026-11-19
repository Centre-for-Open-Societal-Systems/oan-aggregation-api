# Agri Stack Composite console (read-only admin UI) — Next.js standalone image.
# Build context: the composite/ folder of the agri-stack repo (same as the API image), e.g.
#   docker build -f composite/docker/agri-composite-ui/Dockerfile composite
# Sources are in composite/ui. Runtime settings (BACKEND_URL, IAM_URL, LOGIN_PROVIDER_ID,
# APPLICATION_MNEMONIC, COOKIE_DOMAIN, DEFAULT_LOCALE) are read from the pod env at request
# time, so one image serves every environment.
FROM node:20-alpine AS base
WORKDIR /app

FROM base AS builder
# Install dependencies first so this layer is cached unless the lockfile changes.
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
# Then copy the rest of the source and produce the standalone build
# (next.config.ts sets `output: "standalone"`).
COPY ui/ ./
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM base AS runner

ENV NODE_ENV=production
ENV PORT=3000
ENV HOSTNAME=0.0.0.0
ENV NEXT_TELEMETRY_DISABLED=1

RUN addgroup --system --gid 1001 nodejs \
    && adduser --system --uid 1001 nextjs

# Standalone output already includes a fully traced node_modules; ownership is set at copy time.
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000

CMD ["node", "server.js"]
