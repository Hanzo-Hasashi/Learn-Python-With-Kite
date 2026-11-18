$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$envFile = Join-Path $repoRoot "tests\postgres-test.env"
$composeFile = Join-Path $repoRoot "compose.postgres-test.yaml"
$envValues = @{}

foreach ($line in Get-Content -LiteralPath $envFile) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith("#")) {
        continue
    }

    $separator = $trimmed.IndexOf("=")
    if ($separator -lt 1) {
        throw "Invalid entry in tests\postgres-test.env."
    }
    $key = $trimmed.Substring(0, $separator)
    $value = $trimmed.Substring($separator + 1)
    if ($envValues.ContainsKey($key)) {
        throw "Duplicate key in tests\postgres-test.env: $key"
    }
    $envValues[$key] = $value
}

$expectedKeys = @(
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "KITE_TEST_RUNTIME_PASSWORD"
)
$actualKeySet = (@($envValues.Keys | Sort-Object) -join ",")
$expectedKeySet = (@($expectedKeys | Sort-Object) -join ",")
if ($actualKeySet -ne $expectedKeySet) {
    throw "tests\postgres-test.env must contain only the four documented test settings."
}
if (
    $envValues["POSTGRES_USER"] -ne "kite_test_owner" -or
    $envValues["POSTGRES_DB"] -ne "kite_integration_test"
) {
    throw "The PostgreSQL test owner and database names do not match the safety guard."
}
foreach ($key in @("POSTGRES_PASSWORD", "KITE_TEST_RUNTIME_PASSWORD")) {
    $password = $envValues[$key]
    if (
        $password.Length -lt 24 -or
        $password -notmatch "^[A-Za-z0-9_-]+$" -or
        $password -match "^(replace|change|example|placeholder|your[-_ ])"
    ) {
        throw "Set $key to a unique URL-safe test-only password of at least 24 characters."
    }
}
if ($envValues["POSTGRES_PASSWORD"] -eq $envValues["KITE_TEST_RUNTIME_PASSWORD"]) {
    throw "Owner and runtime PostgreSQL test passwords must be different."
}

$ownerUrl = "postgresql+psycopg://$($envValues['POSTGRES_USER']):$($envValues['POSTGRES_PASSWORD'])@127.0.0.1:55432/kite_integration_test?sslmode=disable"
$runtimeUrl = "postgresql+psycopg://kite_runtime:$($envValues['KITE_TEST_RUNTIME_PASSWORD'])@127.0.0.1:55432/kite_integration_test?sslmode=disable"

Set-Location $repoRoot
Remove-Item Env:RENDER -ErrorAction SilentlyContinue
Remove-Item Env:VERCEL -ErrorAction SilentlyContinue
Remove-Item Env:VERCEL_ENV -ErrorAction SilentlyContinue
Remove-Item Env:SUPABASE_URL -ErrorAction SilentlyContinue
Remove-Item Env:SUPABASE_ANON_KEY -ErrorAction SilentlyContinue

$env:KITE_POSTGRES_INTEGRATION_TESTS = "1"
$env:KITE_TEST_SAFETY_MARKER = "KITE_DISPOSABLE_POSTGRES_TEST_ONLY"
$env:KITE_ENV = "development"
$env:KITE_TEST_DATABASE_URL = $ownerUrl
$env:KITE_TEST_RUNTIME_DATABASE_URL = $runtimeUrl
$env:KITE_TEST_RUNTIME_PASSWORD = $envValues["KITE_TEST_RUNTIME_PASSWORD"]
$env:DATABASE_URL = $runtimeUrl
$env:MIGRATION_DATABASE_URL = $ownerUrl
$env:KITE_SECRET_KEY = "postgres-integration-test-secret-key-only"
$env:KITE_ALLOW_LOCAL_AUTH = "true"
$env:RUNNER_SHARED_SECRET = "postgres-integration-test-runner-key"
$env:SUPABASE_URL = ""
$env:SUPABASE_ANON_KEY = ""

& docker compose --env-file $envFile -f $composeFile exec -T postgres-test `
    pg_isready -U $envValues["POSTGRES_USER"] -d $envValues["POSTGRES_DB"]
if ($LASTEXITCODE -ne 0) {
    throw "The isolated PostgreSQL test service is not ready. Start it with the documented Compose command."
}

$python = if ($env:VIRTUAL_ENV) {
    Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
} else {
    Join-Path $repoRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python environment not found. Activate it or create .venv before running the PostgreSQL suite."
}

& $python -m unittest discover -s tests -p postgres_integration_suite.py -v
exit $LASTEXITCODE
