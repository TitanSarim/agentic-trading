-- Phase 5: LLM decision journal enrichment (input hash + validated JSON).

ALTER TABLE llm_decisions ADD COLUMN input_hash TEXT;
ALTER TABLE llm_decisions ADD COLUMN validated_json TEXT;
