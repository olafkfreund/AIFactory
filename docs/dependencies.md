# Dependencies

> Auto-generated from the project manifests by `scripts/generate-techdocs-deps.py` (run in CI). Do not edit by hand.

AIFactory pins **17** backend, **37** web-server, and **49** frontend runtime dependencies.

## Backend (Python) — `apps/backend/requirements.txt`

| Package | Version |
|---------|---------|
| `anthropic` | `>=0.84.0` |
| `bashlex` | `>=0.18` |
| `boto3` | `>=1.43.0` |
| `claude-agent-sdk` | `>=0.1.16` |
| `google-generativeai` | `>=0.8.0` |
| `graphiti-core` | `>=0.5.0; python_version >= "3.12"` |
| `hvac` | `>=2.3.0` |
| `jinja2` | `>=3.1.0` |
| `kubernetes-asyncio` | `>=32.0` |
| `pydantic` | `>=2.0.0` |
| `python-dotenv` | `>=1.0.0` |
| `real_ladybug` | `>=0.13.0; python_version >= "3.12"` |
| `tomli` | `>=2.0.0; python_version < "3.11"` |
| `tree-sitter` | `>=0.21.0` |
| `tree-sitter-javascript` | `>=0.21.0` |
| `tree-sitter-python` | `>=0.21.0` |
| `tree-sitter-typescript` | `>=0.21.0` |

## Web server (Python) — `apps/web-server/requirements.txt`

| Package | Version |
|---------|---------|
| `aiofiles` | `>=23.0.0` |
| `aiosqlite` | `>=0.20.0` |
| `alembic` | `>=1.13.0` |
| `asyncpg` | `>=0.30.0` |
| `authlib` | `>=1.3.0` |
| `azure-identity` | `>=1.20.0` |
| `azure-keyvault-keys` | `>=4.10.0` |
| `bashlex` | `>=0.18` |
| `boto3` | `>=1.35.0` |
| `email-validator` | `>=2.0.0` |
| `fastapi` | `>=0.109.0` |
| `gitpython` | `>=3.1.0` |
| `google-cloud-kms` | `>=3.0.0` |
| `google-crc32c` | `>=1.5.0` |
| `httpx` | `>=0.27.0` |
| `hvac` | `>=2.3.0` |
| `itsdangerous` | `>=2.2.0` |
| `kubernetes-asyncio` | `>=32.0` |
| `opentelemetry-api` | `>=1.27` |
| `opentelemetry-exporter-otlp` | `>=1.27` |
| `opentelemetry-instrumentation-asyncpg` | `>=0.48b0` |
| `opentelemetry-instrumentation-fastapi` | `>=0.48b0` |
| `opentelemetry-instrumentation-httpx` | `>=0.48b0` |
| `opentelemetry-instrumentation-redis` | `>=0.48b0` |
| `opentelemetry-instrumentation-sqlalchemy` | `>=0.48b0` |
| `opentelemetry-sdk` | `>=1.27` |
| `prometheus-fastapi-instrumentator` | `>=7.0.0` |
| `psutil` | `>=5.9.0` |
| `ptyprocess` | `>=0.7.0` |
| `pydantic` | `>=2.0.0` |
| `pydantic-settings` | `>=2.0.0` |
| `python-dotenv` | `>=1.0.0` |
| `python-multipart` | `>=0.0.6` |
| `python3-saml` | `>=1.16` |
| `redis` | `>=5.0` |
| `structlog` | `>=24.4.0` |
| `websockets` | `>=12.0` |

## Frontend (npm) — `apps/frontend-web/package.json`

### Runtime

| Package | Version |
|---------|---------|
| `@dnd-kit/core` | `^6.3.1` |
| `@dnd-kit/sortable` | `^10.0.0` |
| `@dnd-kit/utilities` | `^3.2.2` |
| `@fontsource/hanken-grotesk` | `^5.2.8` |
| `@fontsource/jetbrains-mono` | `^5.2.8` |
| `@monaco-editor/react` | `^4.6.0` |
| `@radix-ui/react-alert-dialog` | `^1.1.15` |
| `@radix-ui/react-checkbox` | `^1.1.4` |
| `@radix-ui/react-collapsible` | `^1.1.3` |
| `@radix-ui/react-dialog` | `^1.1.15` |
| `@radix-ui/react-dropdown-menu` | `^2.1.16` |
| `@radix-ui/react-popover` | `^1.1.15` |
| `@radix-ui/react-progress` | `^1.1.8` |
| `@radix-ui/react-radio-group` | `^1.3.8` |
| `@radix-ui/react-scroll-area` | `^1.2.10` |
| `@radix-ui/react-select` | `^2.2.6` |
| `@radix-ui/react-separator` | `^1.1.8` |
| `@radix-ui/react-slot` | `^1.2.4` |
| `@radix-ui/react-switch` | `^1.2.6` |
| `@radix-ui/react-tabs` | `^1.1.13` |
| `@radix-ui/react-toast` | `^1.2.15` |
| `@radix-ui/react-tooltip` | `^1.2.8` |
| `@tailwindcss/typography` | `^0.5.19` |
| `@tanstack/react-virtual` | `^3.13.13` |
| `@xterm/addon-fit` | `^0.11.0` |
| `@xterm/addon-serialize` | `^0.14.0` |
| `@xterm/addon-web-links` | `^0.12.0` |
| `@xterm/addon-webgl` | `^0.19.0` |
| `@xterm/xterm` | `^6.0.0` |
| `class-variance-authority` | `^0.7.1` |
| `clsx` | `^2.1.1` |
| `highlight.js` | `^11.11.1` |
| `i18next` | `^25.7.3` |
| `lucide-react` | `^0.562.0` |
| `motion` | `^12.23.26` |
| `react` | `^19.2.3` |
| `react-dom` | `^19.2.3` |
| `react-i18next` | `^16.5.0` |
| `react-markdown` | `^10.1.0` |
| `react-resizable-panels` | `^4.2.0` |
| `react-router-dom` | `^7.1.0` |
| `rehype-highlight` | `^7.0.2` |
| `rehype-raw` | `^7.0.0` |
| `rehype-sanitize` | `^6.0.0` |
| `remark-gfm` | `^4.0.1` |
| `tailwind-merge` | `^3.4.0` |
| `uuid` | `^13.0.0` |
| `zod` | `^4.2.1` |
| `zustand` | `^5.0.9` |

### Dev

| Package | Version |
|---------|---------|
| `@playwright/test` | `^1.55.0` |
| `@tailwindcss/postcss` | `^4.1.17` |
| `@testing-library/jest-dom` | `^6.9.1` |
| `@testing-library/react` | `^16.3.1` |
| `@types/highlight.js` | `^9.12.4` |
| `@types/node` | `^25.0.0` |
| `@types/react` | `^19.2.7` |
| `@types/react-dom` | `^19.2.3` |
| `@types/uuid` | `^10.0.0` |
| `@vitejs/plugin-react` | `^5.1.2` |
| `autoprefixer` | `^10.4.22` |
| `jsdom` | `^27.4.0` |
| `postcss` | `^8.5.6` |
| `tailwindcss` | `^4.1.17` |
| `tsx` | `^4.20.0` |
| `typescript` | `^5.9.3` |
| `vite` | `^7.2.7` |
| `vitest` | `^4.0.16` |

