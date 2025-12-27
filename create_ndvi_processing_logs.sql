-- =====================================================
-- NDVI Processing Logs Table
-- =====================================================
-- This table tracks all NDVI processing steps for observability
-- Run this in Supabase SQL Editor if the table doesn't exist

CREATE TABLE IF NOT EXISTS ndvi_processing_logs (
    -- Primary key
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Processing step tracking
    processing_step TEXT NOT NULL,  -- e.g., 'PROCESS_START', 'PROCESS_END', 'PROCESS_ERROR', 'PROCESS_SKIPPED'
    step_status TEXT NOT NULL,      -- e.g., 'started', 'completed', 'failed', 'skipped'
    
    -- Multi-tenant support
    tenant_id UUID NOT NULL,
    land_id UUID,                   -- NULL for global steps
    
    -- Satellite data reference
    satellite_tile_id TEXT,         -- Sentinel scene ID (optional)
    
    -- Timing
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER,            -- Processing duration in milliseconds
    
    -- Error tracking
    error_message TEXT,
    error_details JSONB,
    
    -- Additional metadata
    metadata JSONB DEFAULT '{}'::jsonb,
    
    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =====================================================
-- Indexes for Performance
-- =====================================================

-- Index for querying by land
CREATE INDEX IF NOT EXISTS idx_ndvi_logs_land_id 
ON ndvi_processing_logs(land_id);

-- Index for querying by tenant
CREATE INDEX IF NOT EXISTS idx_ndvi_logs_tenant_id 
ON ndvi_processing_logs(tenant_id);

-- Index for querying by processing step
CREATE INDEX IF NOT EXISTS idx_ndvi_logs_step 
ON ndvi_processing_logs(processing_step);

-- Index for querying by status
CREATE INDEX IF NOT EXISTS idx_ndvi_logs_status 
ON ndvi_processing_logs(step_status);

-- Index for time-based queries
CREATE INDEX IF NOT EXISTS idx_ndvi_logs_created_at 
ON ndvi_processing_logs(created_at DESC);

-- Composite index for common queries
CREATE INDEX IF NOT EXISTS idx_ndvi_logs_tenant_land_created 
ON ndvi_processing_logs(tenant_id, land_id, created_at DESC);

-- =====================================================
-- Row Level Security (RLS)
-- =====================================================

-- Enable RLS
ALTER TABLE ndvi_processing_logs ENABLE ROW LEVEL SECURITY;

-- Policy: Allow service role full access (for pipeline)
CREATE POLICY IF NOT EXISTS "Service role has full access to ndvi_processing_logs"
ON ndvi_processing_logs
FOR ALL
TO service_role
USING (true)
WITH CHECK (true);

-- Policy: Tenants can only view their own logs
CREATE POLICY IF NOT EXISTS "Tenants can view their own ndvi_processing_logs"
ON ndvi_processing_logs
FOR SELECT
TO authenticated
USING (tenant_id = auth.uid());

-- =====================================================
-- Comments for Documentation
-- =====================================================

COMMENT ON TABLE ndvi_processing_logs IS 
'Tracks NDVI processing pipeline execution steps for observability and debugging';

COMMENT ON COLUMN ndvi_processing_logs.processing_step IS 
'Processing step name: PROCESS_START, PROCESS_END, PROCESS_ERROR, PROCESS_SKIPPED';

COMMENT ON COLUMN ndvi_processing_logs.step_status IS 
'Step execution status: started, completed, failed, skipped';

COMMENT ON COLUMN ndvi_processing_logs.duration_ms IS 
'Processing duration in milliseconds (NULL if not completed)';
