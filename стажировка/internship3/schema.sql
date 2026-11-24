-- Idempotent schema: safe to run on every start (db.init_db does exactly that).

-- Internship Task 1: videos
CREATE TABLE IF NOT EXISTS videos (
    id SERIAL PRIMARY KEY,
    company_name TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL,          -- absolute path to the file on disk
    video_url TEXT NOT NULL,          -- public URL to the video
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Internship Task 3: companies
CREATE TABLE IF NOT EXISTS companies (
    id SERIAL PRIMARY KEY,
    company_name TEXT NOT NULL UNIQUE,
    language TEXT,
    country TEXT,
    postcode TEXT,
    city TEXT,
    street TEXT,
    house_no TEXT,
    web TEXT,                         -- company home page URL
    status INTEGER,                   -- NULL = not yet processed (see status.py)
    impressum_url TEXT,               -- resolved Impressum/Imprint URL
    legal_name TEXT,                  -- extracted company name
    postal_address TEXT,              -- extracted postal address
    handelsregister TEXT,             -- trade registry number (HRB/HRA)
    vat_number TEXT,                  -- VAT id (USt-IdNr., DE...)
    phone TEXT,
    email TEXT,
    responsible_person TEXT,          -- e.g. Geschäftsführer / responsible editor
    raw_json JSONB,                   -- full structured DeepSeek response
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- the enrichment script selects by status
CREATE INDEX IF NOT EXISTS companies_status_idx ON companies (status);
