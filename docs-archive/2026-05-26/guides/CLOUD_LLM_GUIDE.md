# Enterprise Cloud LLM Integration & Ollama Independence Guide

This guide details how to configure **AIFactory** to run completely on cloud-hosted enterprise APIs (Google AI Studio, AWS Bedrock, and GCP Vertex AI), bypassing local hardware dependencies like Ollama and avoiding system-level CLI binaries.

---

## 1. Native Google AI Studio (Gemini) Integration

We have introduced the `studio:` model prefix to route Gemini processing loops directly to Google AI Studio's native OpenAI-compatible API. This eliminates the need for local Ollama instances or system-level `gemini` CLI subprocess binaries.

### How it Works
When a model starts with the `studio:` prefix, AIFactory automatically:
1. Resolves the canonical provider to `"openai-compatible"`.
2. Strips the prefix to pass the pure model ID to the API.
3. Automatically sets the connection base URL to the official Google AI Studio endpoint (`https://generativelanguage.googleapis.com/v1beta/openai`).
4. Extracts your key from `GOOGLE_API_KEY` or `GEMINI_API_KEY`.

### Configuration Steps
Add the following to your `apps/backend/.env` file:
```env
# Google AI Studio API Key (obtain from Google AI Studio / Google Cloud)
GOOGLE_API_KEY=AIzaSyD...
```

### Usage
Simply select or pass any model prefixed with `studio:` during task or spec creation. E.g.:
```bash
# Run a spec using Gemini 2.5 Flash for review and QA
python run.py --spec 001 --model studio:gemini-2.5-flash

# Run with Gemini 2.5 Pro
python run.py --spec 001 --model studio:gemini-2.5-pro
```

---

## 2. AWS Bedrock & GCP Vertex AI (Claude) Integration

AIFactory coordinates autonomous coding agent loops using the **Claude Agent SDK** (`claude-agent-sdk`). The SDK executes inside a sandboxed subprocess and captures standard environment variables. By leveraging a high-performance routing proxy like **LiteLLM**, you can direct all SDK traffic to AWS Bedrock or GCP Vertex AI seamlessly.

### How it Works
The Claude Agent SDK respects the `ANTHROPIC_BASE_URL` environment variable. When set, all planning and coding session API calls are redirected to your proxy.

### Setup using LiteLLM

1. Install LiteLLM:
   ```bash
   pip install litellm[proxy]
   ```

2. Create a `config.yaml` for LiteLLM:
   ```yaml
   model_list:
     - model_name: claude-3-5-sonnet-20241022
       litellm_params:
         model: bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0
         aws_access_key_id: "os.environ/AWS_ACCESS_KEY_ID"
         aws_secret_access_key: "os.environ/AWS_SECRET_ACCESS_KEY"
         aws_region_name: "us-east-1"
   ```

3. Start the LiteLLM Proxy server (runs on `http://localhost:4000` by default):
   ```bash
   litellm --config config.yaml
   ```

4. Configure AIFactory to route to the proxy:
   In your `apps/backend/.env` file, specify:
   ```env
   # Redirect SDK traffic to local LiteLLM proxy
   ANTHROPIC_BASE_URL=http://localhost:4000/v1
   # A dummy key is required by the SDK client even when utilizing Bedrock/Vertex
   ANTHROPIC_API_KEY=dummy-secret-key-for-sdk
   ```

Now, all Phase 1/Phase 2 coder and planner agent tasks are transparently executed via AWS Bedrock Claude!

---

## 3. Graphiti Memory System Cloud Mapping

The **Graphiti** memory system (`apps/backend/integrations/graphiti/`) builds the semantic knowledge graph and extracts persistent session insights. Out-of-the-box, Graphiti supports native cloud-hosted providers so you never need local Ollama embeddings.

### Google AI Studio Configuration
To run Graphiti entirely on Google AI Studio embeddings and models:
```env
GRAPHITI_ENABLED=true
GRAPHITI_LLM_PROVIDER=google
GRAPHITI_EMBEDDER_PROVIDER=google
GOOGLE_API_KEY=your-api-key
```

### Anthropic & OpenAI Combination
To use Anthropic Sonnet for knowledge graph extraction and OpenAI for fast semantic embedding:
```env
GRAPHITI_ENABLED=true
GRAPHITI_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=your-anthropic-key

GRAPHITI_EMBEDDER_PROVIDER=openai
OPENAI_API_KEY=your-openai-key
```

---

## 4. Fail-Safe Ollama Discovery

If local Ollama is not running, AIFactory used to experience a startup delay of 5+ seconds due to blocking socket connection attempts. 

We have reduced the discovery timeout to **1 second** and wrapped all internal API requests in robust `try-except` blocks. If Ollama is offline:
- The detector fails silently and instantly.
- The web server and CLI startup without any lag.
- AIFactory runs completely on your cloud-hosted configurations without attempting local hardware fallback.
