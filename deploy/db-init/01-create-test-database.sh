#!/bin/sh
# Creates the database integration tests use (AGENTS §4). Runs once, on first
# initialisation of the data volume.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
	CREATE DATABASE assistant_test OWNER "$POSTGRES_USER";
SQL
