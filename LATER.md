# Later (ideas parked; design is frozen for the demo)

- UPI use case: deliberate forgetting (claim expires after reversal completes) shown live with the clock.
- Per-customer encryption keys; erasure by per-customer key destruction (also covers event erasure, which append-only grants make impossible for hub_app).
- Policy decisions via a dedicated policy engine.
- Hub exposed as a network service.
- Versioned agent instructions in LangSmith Context Hub.
- Merchant-name allowlist for the sensitive-data check: in the Step 5 recorded run, both the extractor and the keyword backstop flagged "Apollo Hospital Pharmacy" as health, so a legitimate duplicate-charge claim is lost (SPEC 17.17).
- Preference to instruction mapping: `reply_instructions` always adds "avoid banking terms" for any explanation-style value, so a customer who asks for banking terms (rehearsal round 2, l) would get contradictory instructions. Map allowed values to instructions in code instead of inserting free text.
- Prune orphaned `.llm_cache/` entries (requests no test or rehearsal makes any more) with a key-tracing test run.
