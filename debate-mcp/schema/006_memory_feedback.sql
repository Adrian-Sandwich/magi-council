-- Calificación humana de la memoria que vio el consejo en una decisión:
-- ¿sirvió o no? Es el conjunto "calificado a mano" que pedía el roadmap de
-- memoria (docs/memory-evolution.md, paso 6), recogido con el uso en vez de
-- etiquetar aparte. `sources` es la foto de los nodos del grafo que se
-- mostraron (decisions.minority_report->'memory_sources' en ese momento).
CREATE TABLE memory_feedback (
    id bigserial PRIMARY KEY,
    decision_id bigint NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    round integer NOT NULL,
    useful boolean NOT NULL,
    note text NOT NULL DEFAULT '',
    sources jsonb NOT NULL DEFAULT '[]'::jsonb,
    message_id bigint NOT NULL REFERENCES messages(id),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX memory_feedback_decision ON memory_feedback(decision_id, id);
