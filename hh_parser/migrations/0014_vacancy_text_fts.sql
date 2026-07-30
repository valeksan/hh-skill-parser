CREATE VIRTUAL TABLE IF NOT EXISTS vacancy_text_fts USING fts5(
    title,
    description_text,
    content='vacancy_snapshots',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS vacancy_text_fts_ai AFTER INSERT ON vacancy_snapshots BEGIN
    INSERT INTO vacancy_text_fts(rowid, title, description_text)
    VALUES (new.id, new.title, new.description_text);
END;

CREATE TRIGGER IF NOT EXISTS vacancy_text_fts_ad AFTER DELETE ON vacancy_snapshots BEGIN
    INSERT INTO vacancy_text_fts(vacancy_text_fts, rowid, title, description_text)
    VALUES ('delete', old.id, old.title, old.description_text);
END;

CREATE TRIGGER IF NOT EXISTS vacancy_text_fts_au AFTER UPDATE OF title, description_text ON vacancy_snapshots BEGIN
    INSERT INTO vacancy_text_fts(vacancy_text_fts, rowid, title, description_text)
    VALUES ('delete', old.id, old.title, old.description_text);
    INSERT INTO vacancy_text_fts(rowid, title, description_text)
    VALUES (new.id, new.title, new.description_text);
END;

INSERT INTO vacancy_text_fts(rowid, title, description_text)
SELECT id, title, description_text FROM vacancy_snapshots;
