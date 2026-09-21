# gptgrep-jev

A Rust client for Jev's structured Decisions endpoint on OpenRouter. It uses the process's `OPENROUTER_API_KEY` and does not read credential files. The default model selector is `typesafe/jev-1.13`. OpenRouter may resolve that selector to a dated revision; responses preserve the returned model identifier.

```rust,no_run
use gptgrep_jev::{Candidate, JevClient};

# async fn example() -> anyhow::Result<()> {
let client = JevClient::from_env(None)?;
let result = client.rerank(
    "What is the retention period?",
    &[Candidate {
        id: "handbook:page-8:block-3".into(),
        text: "Records are retained for seven years after termination.".into(),
    }],
).await?;
assert_eq!(result.rankings[0].id, "handbook:page-8:block-3");
# Ok(())
# }
```

`rerank` asks one Score question per candidate, using the same four descriptive relevance levels. Its output score is the expected level divided by three, in 0..1. It is a comparable relevance measure, not a probability of answer correctness. A missing `confidence` remains `None`; missing usage remains JSON null. Results sort by descending score, then candidate ID. Empty input, duplicate candidate IDs, and oversized batches are errors.

`decide(state, questions)` accepts Noul, Choice and Score questions as `serde_json::Value`. State and instructions must be text, objects or arrays. Noul criteria, when supplied, need both true/false descriptions. Choice accepts 2–255 options; Score accepts 2–10 levels. These local constraints follow the useful vendor contract even where the gateway schema is looser. Responses use the public `DecisionAnswer` enum.

Both calls have a 20-second request timeout, a 64-question cap, a 64 KiB serialized input cap and a 1 MiB response cap. These are local application bounds, not token counts or a billing guarantee. The client makes no application retries, follows no redirects and explicitly disables provider fallback. Callers own shortlisting, further batches, budget scheduling and any explicit local fallback.

Response validation rejects missing, unexpected or duplicate answer IDs, duplicate JSON object keys, wrong answer types, non-finite/out-of-range numbers, invalid choices, inconsistent probability keys and malformed usage. Optional gateway confidence/distributions/legend stay optional; supplied values are validated. No provider body, query, excerpt or credential is included in error text. After a transport failure, usage may be unknown.

`JevClient::new(api_key, model)` supports an explicitly supplied key. `with_endpoint(api_key, model, endpoint)` supports compatible HTTPS endpoints and loopback HTTP test servers; it rejects URL credentials, query strings and fragments.

The wire contract was checked against [OpenRouter's Decisions schema](https://openrouter.ai/openapi.json), the [Jev model page](https://openrouter.ai/typesafe/jev-1.13), and [TypeSafe's API documentation](https://docs.typesafe.ai/api.md). Jev is a text-only decision model; it does not generate text. Unit and loopback transport tests require no API key and make no paid model calls.

```sh
cargo test -p gptgrep-jev
```
