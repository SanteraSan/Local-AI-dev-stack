-- Runs once, on first start with an empty data volume.
CREATE DATABASE litellm;
CREATE DATABASE devmem;

\connect devmem
CREATE EXTENSION IF NOT EXISTS vector;
