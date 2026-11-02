# Der Supervisor übergibt BUILD_FROM mit seinem eigenen Basis-Image (ohne npm). Es wird hier
# bewusst NICHT verwendet: das offizielle Node-Image bringt Node.js und npm mit.
FROM node:20-alpine

# Version des Pakets swissgroceries-mcp (wird beim Bauen EINMAL installiert, nicht bei jedem Start)
ARG MCP_VERSION=0.12.0

WORKDIR /app
RUN npm install --omit=dev --no-audit --no-fund @nicktcode/swissgroceries-mcp@${MCP_VERSION} \
 && npm cache clean --force
COPY server.mjs /app/server.mjs

ENV NODE_ENV=production \
    MCP_SCRIPT=/app/node_modules/@nicktcode/swissgroceries-mcp/dist/index.js
EXPOSE 8080
CMD ["node", "/app/server.mjs"]
