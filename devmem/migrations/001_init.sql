CREATE TABLE projects (
  id serial PRIMARY KEY,
  slug text UNIQUE NOT NULL,           -- taskflow, keepchat-test...
  root_path text,
  git_remote text,
  created_at timestamptz DEFAULT now()
);
CREATE TABLE tasks (
  id serial PRIMARY KEY,
  project_id int REFERENCES projects(id),
  title text NOT NULL,
  status text NOT NULL DEFAULT 'open',  -- open | in_progress | done | dropped
  plan text,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);
CREATE TABLE entries (
  id bigserial PRIMARY KEY,
  project_id int REFERENCES projects(id),
  task_id int REFERENCES tasks(id),
  kind text NOT NULL,          -- decision | step_summary | fact | lesson | todo | handoff | imported
  content text NOT NULL,
  status text NOT NULL DEFAULT 'active',  -- active | superseded
  superseded_by bigint REFERENCES entries(id),
  refs jsonb DEFAULT '[]',     -- файлы, коммиты, URL
  commit_sha text,             -- на каком коммите это было верно
  source text NOT NULL,        -- agent | user | import
  source_path text,            -- для import
  content_hash text,           -- дедупликация импорта
  client text,                 -- roo | cursor | continue | opencode
  model text,
  embedding vector(768),
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
  created_at timestamptz DEFAULT now()
);
CREATE INDEX ON entries USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON entries USING gin (tsv);
CREATE INDEX ON entries (project_id, status, kind);
CREATE UNIQUE INDEX ON entries (project_id, content_hash) WHERE content_hash IS NOT NULL;
