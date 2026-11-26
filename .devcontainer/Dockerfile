# The workspace container that Codespaces or VS Code attaches to: git, a shell and the `vscode`
# user from the Dev Containers base image, plus the same Node.js, Bun and uv as the app images and
# CI, so the checks run here too. The app itself runs in the services of ../compose.yaml.
FROM mcr.microsoft.com/devcontainers/base:2-trixie@sha256:41ff7ce12ec45f2704f27a6e091831186ad922ec8112e6339425bfefdf5a70f9

# Keep in step with deploy/docker/Dockerfile.web (node:24, oven/bun:1.4.0) and
# deploy/docker/Dockerfile.api (uv 0.11.17). uv installs Python 3.14 from .python-version on demand.
COPY --from=node:24-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=oven/bun:1.4.0 /usr/local/bin/bun /usr/local/bin/bun
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /usr/local/bin/
RUN ln -s bun /usr/local/bin/bunx
