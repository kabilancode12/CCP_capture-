-- 024_decision_prompt: keep the developer's prompt with each decision.
--
-- A decision starts with something the developer asked. The prompt that was current at the first
-- evidence line of the decision is stored here (its full text, its time and its transcript entry),
-- so a decision can always be read together with what was asked, even after the index is rebuilt.
-- Filled when decisions are written, and now for the decisions that exist.

ALTER TABLE decision
    ADD COLUMN prompt_text TEXT,
    ADD COLUMN prompt_at   TIMESTAMPTZ,
    ADD COLUMN prompt_uuid TEXT;

UPDATE decision d SET prompt_text = p.text, prompt_at = p.occurred_at, prompt_uuid = p.entry_uuid
  FROM (
    SELECT d2.decision_id,
           (SELECT e.entry_uuid || E'\x1f' || e.occurred_at::text || E'\x1f' ||
                   COALESCE(e.payload #>> '{message,content}', e.payload #>> '{message,content,0,text}', '')
              FROM event e
             WHERE e.session_id = d2.session_id AND e.event_type = 'prompt' AND e.actor = 'human'
               AND NOT e.is_abandoned
               AND (e.raw_object_key, e.raw_line_number) <= (first_ev.key, first_ev.line)
             ORDER BY e.raw_object_key DESC, e.raw_line_number DESC LIMIT 1) AS packed
      FROM decision d2
      CROSS JOIN LATERAL (
        SELECT x ->> 'key' AS key, (x ->> 'line')::int AS line
          FROM jsonb_array_elements(d2.evidence_lines) x
         ORDER BY x ->> 'key', (x ->> 'line')::int LIMIT 1) first_ev
  ) q
  CROSS JOIN LATERAL (
    SELECT split_part(q.packed, E'\x1f', 1) AS entry_uuid,
           split_part(q.packed, E'\x1f', 2)::timestamptz AS occurred_at,
           substr(q.packed, length(split_part(q.packed, E'\x1f', 1)) + length(split_part(q.packed, E'\x1f', 2)) + 3) AS text
  ) p
 WHERE d.decision_id = q.decision_id AND q.packed IS NOT NULL;
