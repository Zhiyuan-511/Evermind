# ═══════════════════════════════════════════
# Evermind Frontend — Next.js Multi-Stage Build
# ═══════════════════════════════════════════

# Stage 1: Build
FROM node:20-alpine AS builder
WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci

COPY . .

# Backend API & WebSocket URLs (resolved at build time for Next.js)
ENV NEXT_PUBLIC_API_URL=http://localhost:8765
ENV NEXT_PUBLIC_WS_URL=ws://localhost:8765/ws

RUN npm run build

# Stage 2: Production
FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

# Copy only what's needed to run
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

EXPOSE 3000

CMD ["node", "server.js"]
