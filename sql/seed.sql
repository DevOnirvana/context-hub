-- Seed data: only the customer and the policy rules (SPEC 17.4).
-- Cases are created by scenario steps as the simulated timeline runs. Synthetic data only.

INSERT INTO customer_context.customers (customer_id, display_name) VALUES
    ('CUST-001', 'Riya Menon');

-- Access rules (SPEC 9). NULL means wildcard; most specific match wins; ties go to the stricter decision.
-- The role rules name sensitivity 'normal' on purpose: with a NULL there, a 3-column role rule
-- would outrank the 1-column restricted-deny rule and let restricted units through.
INSERT INTO policy.rules (agent_role, purpose, case_type, sensitivity, decision) VALUES
    ('payments_agent', 'payments_servicing',     'card_dispute',     'normal',     'allow'),
    ('payments_agent', 'payments_servicing',     'upi_failed_debit', 'normal',     'allow'),
    ('credit_agent',   'card_dispute_servicing', 'card_dispute',     'normal',     'allow'),
    -- The credit agent may know a payments case exists, never what is in it.
    ('credit_agent',   'card_dispute_servicing', 'upi_failed_debit', 'normal',     'existence_only'),
    -- Restricted units (e.g. a hardship marker) are denied to any agent unless a more specific rule allows.
    (NULL,             NULL,                     NULL,               'restricted', 'deny');
