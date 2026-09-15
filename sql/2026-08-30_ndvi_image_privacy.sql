-- =====================================================================
-- NDVI imagery: private buckets + tenant-scoped read      (2026-08-30)
-- Project: qfklkkzxemsbeniyugiz
--
-- WHY
-- Both ndvi buckets are public = true today, with a blanket
-- "Public read access to ndvi-thumbnails [SELECT roles={public}]"
-- policy. Anyone holding a land UUID can fetch any tenant's field
-- imagery without authenticating - a DPDP Act 2023 exposure for
-- farmer-linked geospatial data.
--
-- v3.1 writes one PNG per OBSERVATION at
--     {tenant_id}/{land_id}/{acquisition_date}_{scene_id}.png
-- so the FIRST path segment is the tenant. That is what makes a
-- private bucket enforceable: the SELECT policy authorises on
-- (storage.foldername(name))[1] against has_tenant_access(uuid), the
-- same function that already guards public.ndvi_data.
--
-- The pipeline is unaffected - it writes with the service role, which
-- keeps its existing "Service role can manage ..." ALL policy.
--
-- BREAKING, AND DELIBERATE: existing public URLs stop resolving.
--   * 2,481 legacy v1 ndvi_data rows hold absolute public URLs to 73
--     objects last written 2026-06-10. Those images are from a
--     superseded pipeline and were already shown beside mismatched
--     metrics; they are not worth keeping reachable unauthenticated.
--   * The farmer app must mint a signed URL for anything that is not
--     already an http(s) URL. Ship the NDVIMapView change together
--     with this migration.
--
-- Apply AFTER the v3.1 pipeline is deployed, so new images exist.
-- =====================================================================

BEGIN;

-- 1. Close the buckets.
UPDATE storage.buckets SET public = false
 WHERE id IN ('ndvi-thumbnails', 'ndvi-rasters');

-- 2. Drop the blanket anonymous read.
DROP POLICY IF EXISTS "Public read access to ndvi-thumbnails" ON storage.objects;
DROP POLICY IF EXISTS "Public read access to ndvi-rasters"    ON storage.objects;

-- 3. Tenant-scoped read. A signed URL can only be minted by a caller
--    that passes this policy, so authorisation happens once, here.
--    Legacy objects written as "{land_id}.png" have no tenant segment,
--    so segment 1 is not a uuid - they fail the cast and are therefore
--    unreachable, which is the intended outcome for superseded imagery.
DROP POLICY IF EXISTS "Tenant read ndvi-thumbnails" ON storage.objects;
CREATE POLICY "Tenant read ndvi-thumbnails" ON storage.objects
  FOR SELECT TO authenticated
  USING (
    bucket_id = 'ndvi-thumbnails'
    AND (storage.foldername(name))[1] ~ '^[0-9a-fA-F-]{36}$'
    AND public.has_tenant_access(((storage.foldername(name))[1])::uuid)
  );

DROP POLICY IF EXISTS "Tenant read ndvi-rasters" ON storage.objects;
CREATE POLICY "Tenant read ndvi-rasters" ON storage.objects
  FOR SELECT TO authenticated
  USING (
    bucket_id = 'ndvi-rasters'
    AND (storage.foldername(name))[1] ~ '^[0-9a-fA-F-]{36}$'
    AND public.has_tenant_access(((storage.foldername(name))[1])::uuid)
  );

-- 4. The pipeline's service-role ALL policies are left untouched.

COMMENT ON COLUMN public.ndvi_data.image_url IS
  'v3.1+: storage PATH inside the private ndvi-thumbnails bucket ({tenant_id}/{land_id}/{date}_{scene_id}.png) - NOT a URL, because a signed URL expires. Consumers mint a signed URL on read. Legacy v1 rows hold absolute public URLs; treat a value starting with http as legacy.';

COMMIT;

-- =====================================================================
-- VERIFICATION (read-only)
-- =====================================================================
-- SELECT id, public FROM storage.buckets WHERE id LIKE 'ndvi%';
--   -- expect public = false for both
--
-- SELECT policyname, cmd, roles FROM pg_policies
--  WHERE schemaname='storage' AND tablename='objects' AND policyname ILIKE '%ndvi%';
--   -- expect: Tenant read ndvi-rasters, Tenant read ndvi-thumbnails (SELECT,
--   --         authenticated) + the two service_role ALL policies. No {public}.
--
-- After the next pipeline run:
-- SELECT count(*) FILTER (WHERE image_url IS NOT NULL) AS with_image,
--        count(*) AS total
--   FROM ndvi_data WHERE spatial_stat_method = 'fractional_coverage_v3';
--
-- SELECT left(image_url, 80) FROM ndvi_data
--  WHERE spatial_stat_method='fractional_coverage_v3' AND image_url IS NOT NULL LIMIT 3;
--   -- expect paths like "<tenant-uuid>/<land-uuid>/2026-08-21_S2C_....png"
--   -- NOT anything beginning with https://
