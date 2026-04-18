-- 021_widen_vault_pseudonym.sql
--
-- The original schema (007_pii_vault.sql) declared
--     pseudonym VARCHAR(20)
-- which is too narrow for the format emitted by pseudonymize_text():
--     [<ENTITY_TYPE>:<8-hex-chars>]
-- Even `[PERSON:12345678]` is exactly 17 chars, but `[IBAN_CODE:12345678]`
-- is 20, `[EMAIL_ADDRESS:12345678]` is 24, `[DE_DATE_OF_BIRTH:12345678]` is 27.
-- Widen to 64 to accommodate any entity type (built-in or custom recognizer).
-- Idempotent: only alters if the column still has its original length.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'pii_vault'
           AND table_name = 'pseudonym_mapping'
           AND column_name = 'pseudonym'
           AND character_maximum_length < 64
    ) THEN
        ALTER TABLE pii_vault.pseudonym_mapping
            ALTER COLUMN pseudonym TYPE VARCHAR(64);
    END IF;
END
$$;
