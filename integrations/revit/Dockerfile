FROM node:22-alpine
WORKDIR /app
COPY --chmod=644 hub.mjs common.mjs publications.mjs publications-aps.mjs publication-server.mjs ./
USER node
ENV HOST=0.0.0.0 PORT=9091 REVIT_REGISTRY=/data/registry.json
HEALTHCHECK --interval=15s --timeout=5s --start-period=5s CMD node -e "fetch('http://127.0.0.1:9091/health').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"
CMD ["node", "hub.mjs"]
