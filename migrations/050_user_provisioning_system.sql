-- Migration: User provisioning system (schema-per-user architecture)
-- Date: 2026-05-27
-- Purpose: Create public.users and public.user_provisioning tables for multi-tenant schema support

-- Users table: canonical user records (lives in 'public' schema)
CREATE TABLE IF NOT EXISTS public.users (
    user_id UUID PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE, -- "christopher", "marla", etc. — used to derive schema name
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_email ON public.users (email);
CREATE INDEX IF NOT EXISTS idx_users_slug ON public.users (slug);

-- User provisioning status table: tracks schema creation for each user
CREATE TABLE IF NOT EXISTS public.user_provisioning (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL UNIQUE,
    schema_name TEXT NOT NULL UNIQUE, -- "faultline_christopher", etc.
    status TEXT NOT NULL DEFAULT 'provisioning', -- provisioning | ready | error
    error_message TEXT, -- set if status='error'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ready_at TIMESTAMPTZ, -- set when status='ready'
    FOREIGN KEY (user_id) REFERENCES public.users(user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_user_provisioning_status ON public.user_provisioning (status);
CREATE INDEX IF NOT EXISTS idx_user_provisioning_schema ON public.user_provisioning (schema_name);

-- Migrations log: track which migrations have been applied to each schema
CREATE TABLE IF NOT EXISTS public.migrations_log (
    id SERIAL PRIMARY KEY,
    schema_name TEXT NOT NULL, -- e.g., "faultline_christopher"
    migration_name TEXT NOT NULL, -- e.g., "001_create_facts.sql"
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (schema_name, migration_name)
);

CREATE INDEX IF NOT EXISTS idx_migrations_log_schema ON public.migrations_log (schema_name);
