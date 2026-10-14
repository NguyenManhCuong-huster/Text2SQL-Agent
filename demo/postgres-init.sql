-- Read-only role used by Text2SQL Studio in the demo. Default privileges cover tables created later by the seeder.
CREATE ROLE analyst LOGIN PASSWORD 'analyst';
GRANT CONNECT ON DATABASE shopdb TO analyst;
GRANT USAGE ON SCHEMA public TO analyst;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON TABLES TO analyst;
