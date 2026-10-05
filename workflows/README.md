# Dify Chatflow

`dify-chatflow.yml` is **exported from a running Dify instance** after it was tested there (spec §6.2), not hand-written. Secrets are not included.

- Dify: self-hosted 1.17.1 (docker compose), DSL version 0.7.0
- Model: DeepSeek `deepseek-v4-flash` via the `langgenius/deepseek` plugin 0.0.24, temperature 0.2
- Prompt versions recorded with each answer: `gen-v1`, and `gen-v1-fix` for the one fix attempt

## Flow

```
Start → build request (code) → POST /api/retrieve → parse (code)
  ├─ error (proxy 503 = service unreachable; service JSON error with request_id) → message
  ├─ no_evidence → "no relevant material" message
  └─ ok → LLM (JSON: status, answer_text, cited handles C1..Cn)
            ├─ model error (fail-branch) → message
            └─ build payload (code: strip <think>, map handles to chunk ids, renumber) → POST /api/answers → check (code)
                  ├─ 200 → answer text + citation links built by the backend
                  ├─ 422 → one fix attempt with the validator's reason → POST /api/answers again
                  │        ├─ 200 → answer
                  │        └─ rejected again → "did not pass citation checks, not shown"
                  └─ other error → message
```

Request bodies are built with `json.dumps` in code nodes, never by pasting the question into a JSON template, so quotes and newlines in a question cannot break or alter the request.

## Set up

The web app and service API serve the **published** version only. After importing or editing, publish the app. Dify's displayed URLs omit the port unless `APP_WEB_URL` and `SERVICE_API_URL` are set in its `.env` (here `http://localhost:8090`).

1. Start the retrieval service (see the repository README / `docs/progress.md`) and build an index.
2. In Dify, import `dify-chatflow.yml` (Studio → Import DSL).
3. Install the DeepSeek plugin and add your own API key under Settings → Model Provider.
4. In the app's environment variables, set `DKA_SERVICE_TOKEN` to the same value as `DKA_SERVICE_TOKEN` in this repository's `.env`. `DKA_BASE_URL` defaults to `http://host.docker.internal:8077`.
5. Dify sends HTTP requests through its SSRF proxy, which blocks private addresses. Allow only this host, in Dify's `docker/.env`:

   ```
   SSRF_PROXY_ALLOW_PRIVATE_DOMAINS=host.docker.internal
   ```

   Do not open whole private ranges. The retrieval service can stay bound to `127.0.0.1`: Docker Desktop forwards `host.docker.internal` to it.

## Behaviour of Dify 1.17.1 that shaped this flow

- **The fail branch discards the response.** On `error_strategy: fail-branch`, a node's outputs are replaced by `error_message` and `error_type`; `status_code` and `body` are gone. Any non-2xx response also counts as a failure once an error strategy is set. So the HTTP nodes have no fail branch: they always hand `status_code` and `body` to a code node, which needs the 422 reason for the fix attempt. Only the two LLM nodes use a fail branch.
- **"Service down" arrives as a 503, not a connection error.** Requests go through the SSRF proxy (squid), which answers with its own HTML 503 (`X-Squid-Error: ERR_CONNECT_FAIL`) when it cannot reach the service. The parse node tells this apart from the service's own JSON 503 by the body.
- **A 401 or 403 is reported as an SSRF block.** Dify treats any 401/403 response that came through squid as "blocked by SSRF protection" and fails the node. A wrong `DKA_SERVICE_TOKEN` therefore shows up as an SSRF error, not as 401. If you see that message while the allowlist above is in place, check the token first.
- **Reasoning output.** `deepseek-v4-flash` emits a `<think>` block before the answer, and the reasoning may contain braces. The LLM nodes use `reasoning_format: separated`, and the payload code node also drops everything up to `</think>` before parsing.

## Tested on 2026-10-05

Draft runs against the real service, index and model (`docs/progress.md` has the details):

| Case | Result |
| --- | --- |
| Three seed questions, one with no material (Kafka), one ambiguous | All answered or marked insufficient evidence, citations validated on the first submission |
| Retrieval service stopped | "cannot reach the retrieval service" message |
| Service up without an index | `NO_ACTIVE_INDEX` with request_id |
| Wrong token | Dify reports an SSRF block (see above); nothing is shown as an answer |
| Model error (out-of-range temperature) | "model returned no result" message |
| Exported DSL re-imported as a new app | Imported without warnings; nodes and edges identical; with the token set, it answered Q1 and passed validation on the first submission |
| Published web app (`/chat/<code>`) | Answers; in one run the first submission listed no citations and the fix attempt recovered it |
