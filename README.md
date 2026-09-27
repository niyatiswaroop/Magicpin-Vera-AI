# Vera — magicpin AI Challenge submission

**Approach:** a deterministic composer with a provenance-tracked fact layer, not a prompt around an LLM.

## How it works

```
4 contexts ──► build_factset() ──► kind spec ──► assemble ──► validate ──► message
               facts + provenance   26+ kinds    hook /        hard rules
                                                 lever / CTA
```

**1. Facts carry provenance.** `build_factset()` extracts ~50 citable values per composition, each with the path it came from (`2,410` ← `merchant.performance.views`). The composer can only slot in something it actually read, so **fabrication is structurally impossible** rather than merely discouraged. Hallucination is the rubric's heaviest penalty (§11) and the whole point of the adaptive-injection phase, so I made it unreachable instead of unlikely.

**2. Dispatch covers 31 trigger kinds.** All 26 present in the generated dataset, plus 5 more from §4.3 that the generator didn't emit but the harness may push live. Each spec declares its *why-now*, an ordered anchor list, which compulsion levers to fire, and its CTA shape. Hook templates use an optional-clause syntax (`[[ ... {fact} ]]`) that drops silently when a fact is absent — necessary because **75 of 100 triggers carry `{"placeholder": true}`** and 14 of the 30 graded pairs are that sparse case.

**3. The validator is a gate, not a lint.** Every body is checked for URLs, category taboos, single-CTA shape, CTA-lands-last, generic-discount framing, preambles, re-introductions, leaked internals, language match, and a **closed-world number check** — every digit must trace to a value in the pushed contexts. Two strictness levels: whole-context for deterministic output, facts-actually-supplied for LLM output.

**4. Multi-turn is rules, not a model.** Auto-reply detection, intent-pivot routing and graceful exit are pattern-recognition, answered in microseconds with no risk of a model talking itself into another qualifying question. Detection is keyed **per merchant across conversations**, because the harness sends its four identical auto-replies under four different `conversation_id`s.

## Tradeoffs

**No LLM in the shipped path.** I wired a local open-source model (qwen2.5:7b via Ollama, no API key, no metered spend) as an optional phrasing layer and measured it against the deterministic output. It never fabricated — the strict gate held — but it consistently *compressed away the parts that score*: it deleted whole offers of work ("Confirm with a YES please."), dropped the peer-scope attribution that made stats credible, and reduced "which of the three usual causes" to "the usual causes". A no-material-loss guard now rejects those rewrites, and the layer ships off. **The deterministic path isn't a fallback; it measured better.** A frontier model would likely beat it on prose variety — that's the trade I made, and it's reversible via `VERA_USE_LLM=1`.

**Determinism via construction, not temperature.** The brief suggests `temperature=0`, but current frontier models reject the parameter outright (400 on Opus 5 / Sonnet 5). Deterministic assembly needs no sampling controls at all; the optional local path pins `temperature: 0` + fixed `seed` and disk-caches by input hash.

**Restraint over coverage.** Suppression keys, a cross-conversation repetition guard, and a 12s internal budget (the bundled simulator's client timeout is 15s, not the advertised 30s) mean the bot returns `{"actions": []}` rather than sending something weak.

**Quoting real discount offers.** §11 penalises generic discount framing, but a merchant's own offer may *be* "Flat 30% OFF". Service@price is preferred wherever available; quoting a real title is allowed with a warning, inventing one is an error.

## What additional context would have helped most

1. **Populated trigger payloads.** Three-quarters are placeholders. Every fact in a `perf_dip` payload is a sentence the message could have earned; without them I derive the moved metric from `delta_7d`, which is honest but thinner.
2. **Locality-level peer stats.** `peer_stats` is category-wide, so the strongest social-proof line in the brief — *"3 dentists in your locality did Y this month"* — is unavailable without inventing it. This is the single biggest cap on the lever the brief names as production Vera's largest miss.
3. **Reply-level engagement outcomes.** `conversation_history` carries engagement tags but not which *phrasings* earned replies. With that I could rank lever combinations empirically instead of by judgement.
4. **Authoritative CTA/URL policy.** §5.4 permits URLs; `api-call-examples.md` F.4 calls them a hard fail at −3. I follow the stricter rule and never emit one.
5. **Merchant-level language signal.** `identity.languages` says which languages exist, not which the merchant *writes in*. `customer.language_pref` does this properly; the merchant equivalent would remove a guess.

## Running it

```bash
pip install fastapi "uvicorn[standard]" pydantic httpx
uvicorn bot:app --host 0.0.0.0 --port 8080

python test_contract.py        # 57 HTTP-contract checks vs api-call-examples.md
python test_conversations.py   # 39 multi-turn checks vs §12 + Phase 4 scenarios
python eval_harness.py lint    # offline rubric proxies, zero cost
python make_submission.py      # regenerate submission.jsonl
```

`compose(category, merchant, trigger, customer)` in `composer.py` is standalone and importable — no server needed.

**Files:** `bot.py` (5 endpoints + teardown) · `composer.py` (`compose`) · `conversation_handlers.py` (`respond`) · `facts.py` (provenance + validator) · `kinds.py` (31 specs) · `voice.py` (per-category voice) · `llm.py` (optional local model) · `store.py` (versioned context state)
