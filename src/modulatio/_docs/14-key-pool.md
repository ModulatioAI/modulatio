# API key pool

Your API keys belong to the **provider**, not to any one model. They sit in a single **shared floating pool**, and by default every model on that provider draws from it: each request rotates to the next key (so a swarm of producers spreads its load instead of hammering one key), and a rate-limit fails over to the next key.

That's the whole simple path — add keys, your models use them. The one optional lever is **pinning**: pin a key to a specific model when you want that model's spend isolated for a budget. A pinned key serves only its model(s) and **leaves the pool**.

This page is the architectural deep-dive: how the pool works, why it's metered by key, and how pinning gives you isolation without giving up the simple default. To just turn it on, jump to [Managing keys](#managing-keys).

---

## The mental model

A provider has a **base env var** (`GEMINI_API_KEY`) and may hold any number of numbered keys behind it (`GEMINI_API_KEY_2`, `_3`, … — no cap), each with an optional label:

```
GEMINI_API_KEY      ← key #1  (label: "main")
GEMINI_API_KEY_2    ← key #2  (label: "backup")
GEMINI_API_KEY_3    ← key #3  (label: "images")   ← pinned to the image model
```

Every **set, unpinned** key is in the shared pool. A model that uses the pool stores only the base name plus a `pool` flag; it never stores a key value (see [The security keel](#the-security-keel)).

> These are all **your own** keys on **your own** account. Pooling uses the headroom you legitimately have — it is not a way to manufacture more. See [The ethics line](#the-ethics-line).

---

## How it works

### Rotation (throughput) — the default

A model on the pool picks the next key in round-robin order over the set, unpinned keys, on **every** request:

```
request 1 → GEMINI_API_KEY      (#1)
request 2 → GEMINI_API_KEY_2    (#2)
request 3 → GEMINI_API_KEY      (#1)   ← wraps around (the unpinned keys)
```

Three unpinned keys give a model roughly three times the per-minute budget of one. The rotation happens **per request, at call time** — not once when the model is wired — because Modulatio builds one runner per model and reuses it; choosing the key at construction would pin it for the runner's life.

### Failover (resilience)

If a request returns a rate-limit (`HTTP 429`), the pool rotates to the next key and retries, bounded by the number of keys in the pool. If every key is throttled, the original error is re-raised — the pool never swallows a genuine failure. A provider with one key, or a model on a single pinned key, behaves as before: a `429` raises immediately, no added retry.

### Pinning (isolation) — the optional lever

Pin a key to one or more models and two things happen:

1. those models use **only** that key (no rotation), and
2. the key **leaves the shared pool** — no other model can spend it.

That's what makes a budget honest. Pin a key to your image model and the vendor's meter on that key *is* your image-generation budget, because nothing else ever touches it.

> A pinned key is removed from the pool entirely. If you pin **every** key a provider has, the shared pool is empty — and a model still set to use the pool will **refuse to dispatch** with a clear "no unpinned key" error rather than quietly borrow a pinned key. Keep at least one key on the pool, or pin the pool models too.

---

## Why it's metered by key, not in the router

The natural question: why doesn't Modulatio track token/cost budgets centrally? Because the **provider is already the authoritative meter**, and it meters per key.

A second accounting system in the router would duplicate — and inevitably disagree with — the numbers the vendor bills you on. Instead, Modulatio leans on the meter that already exists and makes **keys the accounting buckets**: pin a key to a model (or a few related models) and that key's vendor-side usage *is* that work's spend. You read it in the provider's own dashboard — no reconciliation, no drift. The router stays thin: it rotates the pool and respects pins; it does not pretend to be a billing system.

Rotation and pinning aren't opposed — they're the two ends of one mechanism. Unpinned keys pool for throughput; a pinned key isolates for metering. The default leans on throughput; you reach for a pin only when you want a budget.

---

## The ethics line

Pooling is for **your own legitimately obtained keys**. If a provider meters its free tier per key (some do), pooling several of your own keys uses headroom you actually have.

What Modulatio does **not** do — and will not help you do — is multiply throwaway *accounts* to dodge a provider's limits. That violates essentially every provider's terms of service and gets the offending user banned. The product makes legitimate free inference easy and lets you pool your own keys; it ships no account-multiplication tooling, by design.

---

## Managing keys

Everything lives in the **Configuration tab** of the TUI (`modulatio-tui`), MODELS side.

### Adding a model

Pick the provider, authenticate, pick the model, register. For an API-key provider:

- If the provider **already has a pooled key**, there's nothing to enter — the model uses the shared pool. Just continue.
- If it has **no key yet**, paste the first one; it joins the pool.
- You can optionally add another key to the pool here too.

### Providers & keys (the standalone manager)

Below the model list is a **PROVIDERS & KEYS** section — a key manager that doesn't need a model. Select a provider and drill into its keys; each is shown by number and label (and whether it's `[shared pool]` or `[pinned → …]`), **never the value**. From here you can:

- **Add key** — paste a new key; it joins the shared pool with an optional label.
- **Remove key** — purge a key from Modulatio entirely (value from the vault and the environment, plus its label and any pins). If the key was pinned to a model, that model is automatically put back on the shared pool, so a removal never leaves a model pointing at a dead key.

Outside-service API keys (the **SERVICES** section just below — image / video / speech / research services) ride the **same numbered-slot pool**, keyed by each service's env var and managed with the same companion. See [The SERVICES pool](/reference/tools/#the-services-pool).

### Pinning a key to a model

Select a model and choose **Pin key**. Pick a key to pin it to that model (it leaves the pool, isolating its spend), or choose **Use pool** to put the model back on the shared pool. Pinning is the one advanced lever — you only meet it if you go looking for it.

---

## The security keel

A model preset stores a **reference**, never a value:

- a pooled model carries `auth_config: { env_var: "GEMINI_API_KEY", pool: true }`; a pinned model carries `auth_config: { env_var: "GEMINI_API_KEY_3" }` — a name, not a secret;
- key values live in your vault's secret store (`0600`), read from the environment at call time;
- labels (`key_labels.json`) and pins (`key_pins.json`, `{env_var: [model, …]}`) store labels and model references only — never values.

`model_presets.add_preset` actively **rejects** any attempt to put a raw secret field (`key`, `api_key`, `token`, …) into a preset's `auth_config`. A key value cannot end up in `model_presets.json`, the catalog, the labels file, the pins file, or a log, by construction.

---

## Honest caveats

- **Rotation under heavy concurrency is approximate.** The round-robin cursor is best-effort and not locked, so under concurrent producers two requests can occasionally pick the same key. That's harmless skew, not corruption — the provider meters each key regardless, and the cursor can never index out of range as the pool grows or shrinks.
- **The tool-using path rotates but does not yet add 429 *failover*.** Tool-loop producers get per-request key rotation; the explicit rotate-and-retry-on-429 loop currently runs on the single-shot completion path. Rotation alone already spreads load so a key is far less likely to hit its limit; dedicated chat-path failover is planned.
- **Pin everything and the pool is empty.** A model still on the pool then fails closed (a clear "no unpinned key" error) rather than borrowing a pinned key — by design, so a budget is never silently contaminated.
- **Pooling is for one account's keys.** It does not, and is not meant to, aggregate keys across multiple accounts — see [the ethics line](#the-ethics-line).

---

## See also

- [Providers & models](/concepts/providers/) — how model entries and auth work.
- [Architecture: sandbox + tool execution](/architecture/sandbox/) — where runners sit in the call path.
