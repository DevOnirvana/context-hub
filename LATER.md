# Later (ideas parked; design is frozen for the demo)

- UPI use case: deliberate forgetting (claim expires after reversal completes) shown live with the clock.
- Per-customer encryption keys; erasure by per-customer key destruction (also covers event erasure, which append-only grants make impossible for hub_app).
- Policy decisions via a dedicated policy engine.
- Hub exposed as a network service.
- Versioned agent instructions in LangSmith Context Hub.
- Merchant-name allowlist for the sensitive-data check: in the Step 5 recorded run, both the extractor and the keyword backstop flagged "Apollo Hospital Pharmacy" as health, so a legitimate duplicate-charge claim is lost (SPEC 17.17).
