#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [switch]$NoAutostart,
    [double]$Delay = 0
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerDir = $ScriptDir
$Base      = $BaseUrl.TrimEnd('/')
$Py        = Join-Path $ServerDir 'venv\Scripts\python.exe'
if (-not (Test-Path $Py)) { $Py = 'python' }
# Inspect whatever database the server is actually using: SWAPIFY_DB_PATH points
# it elsewhere in deployment (and when running the suite against a scratch copy),
# and a DB dump of a different file than the one under test proves nothing.
$Db        = if ($env:SWAPIFY_DB_PATH) { $env:SWAPIFY_DB_PATH } else { Join-Path $ServerDir 'swapify.db' }
$OutLog    = Join-Path $ServerDir '.test_server.out.log'
$ErrLog    = Join-Path $ServerDir '.test_server.err.log'
$script:Pass = 0
$script:Fail = 0
$script:ServerProc = $null
$script:Token = ''
$script:UserId = 0
$script:LastBody = ''

# UTF-8 so AI answers / product names render correctly.
# NOTE: use a BOM-less UTF-8 for $OutputEncoding — a BOM would be prepended
# when passing args to python and break it (U+FEFF SyntaxError).
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
$script:DbqScript = Join-Path ([System.IO.Path]::GetTempPath()) "swapify_dbq_$PID.py"
$script:ImgDir    = Join-Path $ServerDir '.test_images'   # throwaway upload test images
$script:HasCurl   = [bool](Get-Command curl.exe -ErrorAction SilentlyContinue)

# ---- helpers ----------------------------------------------------------------
function Write-Banner($t) {
    Write-Host ""
    Write-Host ("=" * 78) -ForegroundColor Magenta
    Write-Host "  $t"      -ForegroundColor Magenta
    Write-Host ("=" * 78) -ForegroundColor Magenta
}

function Write-Section($n, $t) {
    Write-Host ""
    Write-Host ("-" * 78)   -ForegroundColor Cyan
    Write-Host "  [$n]  $t"  -ForegroundColor Cyan
    Write-Host ("-" * 78)   -ForegroundColor Cyan
}

# pretty-print a JSON string; if $Head > 0 and it's an array, show only first N
function Show-Json($content, $head = 0) {
    if ([string]::IsNullOrWhiteSpace($content)) { Write-Host "(empty body)"; return }
    try { $o = $content | ConvertFrom-Json } catch { Write-Host $content; return }
    if ($head -gt 0 -and $o -is [System.Array]) {
        $take = [Math]::Min($head, $o.Count)
        Write-Host "[array with $($o.Count) items - showing first $take]" -ForegroundColor DarkGray
        ($o[0..($take - 1)] | ConvertTo-Json -Depth 20)
    } else {
        ($o | ConvertTo-Json -Depth 20)
    }
}

# core request runner. Stores raw body in $script:LastBody.
# -Expect defaults to '2' (any 2xx = pass); pass '4' for tests that are meant to
# return a client error (e.g. rating validation) so they still count as a pass.
function Invoke-Api {
    param(
        [string]$Method,
        [string]$Path,
        [string]$Data = $null,
        [switch]$Auth,
        [int]$Head = 0,
        [string]$Expect = '2'
    )
    $url = "$Base$Path"
    $headers = @{}
    $shown = ">> $Method $url"
    if ($Auth) { $headers['Authorization'] = "Bearer $script:Token"; $shown += "   [Auth: Bearer <TOKEN>]" }
    Write-Host $shown -ForegroundColor DarkGray
    if ($Data) { Write-Host "   body: $Data" -ForegroundColor DarkGray }

    $code = 0
    $content = ''
    try {
        $params = @{
            Uri             = $url
            Method          = $Method
            Headers         = $headers
            TimeoutSec      = 60
            UseBasicParsing = $true
        }
        if ($Data) { $params['Body'] = $Data; $params['ContentType'] = 'application/json' }
        $resp = Invoke-WebRequest @params
        $code = [int]$resp.StatusCode
        $content = $resp.Content
    } catch {
        $err = $_
        $r = $err.Exception.Response
        # status code (works for both PS 5.1 WebException and PS7 HttpResponseException)
        if ($r) {
            try {
                if ($r.StatusCode -is [System.Net.HttpStatusCode]) { $code = [int]$r.StatusCode }
                elseif ($null -ne $r.StatusCode.value__) { $code = [int]$r.StatusCode.value__ }
            } catch { $code = 0 }
        }
        # body: PS7 puts it in ErrorDetails.Message; PS5.1 needs the response stream
        if ($err.ErrorDetails -and $err.ErrorDetails.Message) {
            $content = $err.ErrorDetails.Message
        } elseif ($r -and ($r | Get-Member -Name GetResponseStream -MemberType Method)) {
            try {
                $sr = New-Object System.IO.StreamReader($r.GetResponseStream())
                $content = $sr.ReadToEnd(); $sr.Close()
            } catch { $content = $err.Exception.Message }
        } else {
            $content = $err.Exception.Message
        }
    }

    $col = 'Green'
    if     ($code -ge 400 -and $code -lt 500) { $col = 'Yellow' }
    elseif ($code -lt 200 -or  $code -ge 500) { $col = 'Red' }
    Write-Host "HTTP $code" -ForegroundColor $col
    Show-Json $content $Head

    # A status matching the expected prefix (default 2xx) is a pass. Tests that
    # deliberately expect a client error pass -Expect '4'. Matched as an anchored
    # regex rather than a literal prefix so a test with two acceptable outcomes can
    # say so: -Expect '[24]' passes on either, which is what a lookup whose honest
    # answer may legitimately be "no source publishes this" (404) needs.
    if (("$code") -match "^$Expect") { $script:Pass++ } else { $script:Fail++ }
    $script:LastBody = $content
    $script:LastCode = $code
    if ($Delay -gt 0) { Start-Sleep -Seconds $Delay }
}

# Admin request runner (Feature 3): like Invoke-Api but sends the X-Admin-Token
# header instead of a bearer token.
function Invoke-Admin {
    param([string]$Method, [string]$Path, [string]$Data = $null,
          [string]$AdminToken, [string]$Expect = '2')
    $url = "$Base$Path"
    Write-Host ">> $Method $url   [X-Admin-Token: <ADMIN_TOKEN>]" -ForegroundColor DarkGray
    $headers = @{ 'X-Admin-Token' = $AdminToken }
    $code = 0; $content = ''
    try {
        $params = @{ Uri = $url; Method = $Method; Headers = $headers; TimeoutSec = 60; UseBasicParsing = $true }
        if ($Data) { $params['Body'] = $Data; $params['ContentType'] = 'application/json' }
        $resp = Invoke-WebRequest @params
        $code = [int]$resp.StatusCode; $content = $resp.Content
    } catch {
        $err = $_; $r = $err.Exception.Response
        if ($r) { try { if ($r.StatusCode -is [System.Net.HttpStatusCode]) { $code = [int]$r.StatusCode } elseif ($null -ne $r.StatusCode.value__) { $code = [int]$r.StatusCode.value__ } } catch { $code = 0 } }
        if ($err.ErrorDetails -and $err.ErrorDetails.Message) { $content = $err.ErrorDetails.Message } else { $content = $err.Exception.Message }
    }
    $col = 'Green'
    if     ($code -ge 400 -and $code -lt 500) { $col = 'Yellow' }
    elseif ($code -lt 200 -or  $code -ge 500) { $col = 'Red' }
    Write-Host "HTTP $code" -ForegroundColor $col
    Show-Json $content
    if (("$code").StartsWith($Expect)) { $script:Pass++ } else { $script:Fail++ }
    $script:LastBody = $content
}

# run a SQL query against swapify.db via python (no SQLite module needed).
# The program is written to a temp .py file (ASCII, no BOM) and executed with
# args — piping it via stdin would prepend a BOM that python rejects.
function Invoke-DbQuery($sql) {
    if (-not (Test-Path $script:DbqScript)) {
        @'
import sys, sqlite3
db, sql = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db); con.row_factory = sqlite3.Row
try:
    for r in con.execute(sql):
        print("   " + " | ".join(f"{k}={r[k]}" for k in r.keys()))
except Exception as e:
    print("   (query error:", e, ")")
con.close()
'@ | Out-File -FilePath $script:DbqScript -Encoding ascii
    }
    & $Py $script:DbqScript $Db $sql
}

function Get-Count($table) {
    $out = Invoke-DbQuery "SELECT COUNT(*) AS c FROM $table"
    if ($out -match 'c=(\d+)') { return [int]$Matches[1] } else { return 0 }
}

# multipart/form-data upload runner (Task 2C). PowerShell 5.1 has no
# `Invoke-WebRequest -Form`, so uploads go through curl.exe (present on Win10+).
# The file path is passed with forward slashes, which curl.exe opens fine.
function Invoke-Upload {
    param(
        [string]$Path, [string]$Barcode, [string]$File, [string]$ContentType,
        [switch]$Auth, [string]$Expect = '2'
    )
    $url = "$Base$Path"
    Write-Host ">> POST $url   [multipart: barcode=$Barcode, file=$(Split-Path -Leaf $File);type=$ContentType]" -ForegroundColor DarkGray
    if (-not $script:HasCurl) {
        Write-Host "curl.exe not found - skipping upload test." -ForegroundColor Yellow
        return
    }
    $fwd = $File -replace '\\', '/'
    $curlArgs = @('-s', '-S', '-m', '60', '-w', "`n__HTTP__%{http_code}",
                  '-F', "barcode=$Barcode", '-F', "file=@$fwd;type=$ContentType")
    if ($Auth) { $curlArgs += @('-H', "Authorization: Bearer $script:Token") }
    $curlArgs += $url
    $raw = (& curl.exe @curlArgs) -join "`n"
    $code = 0; $body = $raw
    if ($raw -match '(?s)^(.*?)\r?\n?__HTTP__(\d+)\s*$') { $body = $Matches[1]; $code = [int]$Matches[2] }
    $col = 'Green'
    if     ($code -ge 400 -and $code -lt 500) { $col = 'Yellow' }
    elseif ($code -lt 200 -or  $code -ge 500) { $col = 'Red' }
    Write-Host "HTTP $code" -ForegroundColor $col
    Show-Json $body
    if (("$code").StartsWith($Expect)) { $script:Pass++ } else { $script:Fail++ }
    $script:LastBody = $body
}

# Gzip check (Task 1D): passes when the server returns Content-Encoding: gzip for
# a large response requested with Accept-Encoding: gzip.
function Test-Gzip {
    param([string]$Path)
    $url = "$Base$Path"
    Write-Host ">> GET $url   [Accept-Encoding: gzip]" -ForegroundColor DarkGray
    if (-not $script:HasCurl) { Write-Host "curl.exe not found - skipping gzip test." -ForegroundColor Yellow; return }
    $headers = & curl.exe -s -H 'Accept-Encoding: gzip' -D - -o NUL $url
    $val = ''
    foreach ($h in $headers) { if ($h -match '^\s*content-encoding:\s*(.+?)\s*$') { $val = $Matches[1].ToLower() } }
    if ($val -eq 'gzip') {
        Write-Host "Content-Encoding: gzip  (response compressed)" -ForegroundColor Green
        $script:Pass++
    } else {
        Write-Host "Expected gzip, got: '$val'" -ForegroundColor Red
        $script:Fail++
    }
}

# image_url presence check on the last response (Task 2B). -Mode 'array' checks a
# list of results; 'field' checks a single object. Passes when every item has a
# non-empty image_url.
function Test-ImageUrl {
    param([string]$Mode = 'array')
    try { $o = $script:LastBody | ConvertFrom-Json } catch { Write-Host "   (unparseable body)" -ForegroundColor Red; $script:Fail++; return }
    $items = if ($o -is [System.Array]) { $o } else { @($o) }
    if ($items.Count -eq 0) { Write-Host "   (no items to check)" -ForegroundColor DarkGray; return }
    $missing = @()
    for ($i = 0; $i -lt $items.Count; $i++) { if (-not $items[$i].image_url) { $missing += $i } }
    if ($missing.Count -gt 0) {
        Write-Host "   MISSING image_url on items: $($missing -join ',')" -ForegroundColor Red
        $script:Fail++
    } else {
        Write-Host "   image_url present on all $($items.Count) item(s), e.g. $($items[0].image_url)" -ForegroundColor Green
        $script:Pass++
    }
}

# Tally a non-HTTP assertion (value equality) against the pass/fail counters.
function Assert-Equal {
    param([string]$Label, $Actual, $Expected)
    if ("$Actual" -eq "$Expected") {
        Write-Host "  PASS  $Label (got '$Actual')" -ForegroundColor Green
        $script:Pass++
    } else {
        Write-Host "  FAIL  $Label (got '$Actual', expected '$Expected')" -ForegroundColor Red
        $script:Fail++
    }
}

# Number of items in the last response: a bare JSON array, or the `results`
# array inside a ?meta=true envelope.
function Get-ResultCount {
    try { $o = $script:LastBody | ConvertFrom-Json } catch { return -1 }
    if ($null -eq $o) { return -1 }
    if ($o -is [System.Array]) { return $o.Count }
    if ($o.PSObject.Properties.Name -contains 'results') { return @($o.results).Count }
    return -1
}

# Assert the last /chat reply did not leak the attached product's context into an
# answer that had nothing to do with that product.
function Assert-NoProductLeak {
    param([string]$Label)
    try { $o = $script:LastBody | ConvertFrom-Json } catch { Write-Host "  FAIL  $Label (unparseable)" -ForegroundColor Red; $script:Fail++; return }
    $r = ("" + $o.response).ToLower()
    $leaked = @()
    foreach ($w in @('coca', 'cola', 'score of', '/10')) { if ($r.Contains($w)) { $leaked += $w } }
    if ($leaked.Count -gt 0) {
        Write-Host "  FAIL  $Label - reply leaked product context: $($leaked -join ', ')" -ForegroundColor Red
        $script:Fail++
    } else {
        Write-Host "  PASS  $Label - reply stays on topic" -ForegroundColor Green
        $script:Pass++
    }
}

# Generate throwaway test images: a valid 1x1 PNG, a text file with a .png name
# (rejected on content), and a >2 MB file (rejected on size).
function New-TestImages {
    if (-not (Test-Path $script:ImgDir)) { New-Item -ItemType Directory -Path $script:ImgDir -Force | Out-Null }
    $gen = Join-Path ([System.IO.Path]::GetTempPath()) "swapify_mkimg_$PID.py"
    @'
import sys, struct, zlib, os
d = sys.argv[1]
def chunk(t, x):
    c = t + x
    return struct.pack('>I', len(x)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
png = (b'\x89PNG\r\n\x1a\n'
       + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
       + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00'))
       + chunk(b'IEND', b''))
open(os.path.join(d, 'valid.png'), 'wb').write(png)
open(os.path.join(d, 'not_image.png'), 'wb').write(b'this is plain text, definitely not an image')
open(os.path.join(d, 'too_big.png'), 'wb').write(png + b'\x00' * (2 * 1024 * 1024 + 64))
print('test images ready in', d)
'@ | Out-File -FilePath $gen -Encoding ascii
    & $Py $gen $script:ImgDir
    Remove-Item $gen -Force -ErrorAction SilentlyContinue
}

function Test-Health {
    try { Invoke-RestMethod -Uri "$Base/health" -TimeoutSec 3 -UseBasicParsing | Out-Null; return $true }
    catch { return $false }
}

# =============================================================================
Write-Banner "SWAPIFY API TEST SUITE (PowerShell)"
Write-Host "Base URL : $Base"
Write-Host "Python   : $Py"
Write-Host "Database : $Db"

# ---- make sure the server is running ----------------------------------------
if (Test-Health) {
    Write-Host "Server already running." -ForegroundColor Green
} elseif ($NoAutostart) {
    Write-Host "Server not reachable at $Base and -NoAutostart set. Start it first:" -ForegroundColor Red
    Write-Host "   cd server\src; ..\venv\Scripts\python.exe -m uvicorn app:app --port 8000"
    exit 1
} else {
    Write-Host "Server not running - starting it..." -ForegroundColor Yellow
    $srcDir = Join-Path $ServerDir 'src'
    $script:ServerProc = Start-Process -FilePath $Py `
        -ArgumentList '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', '8000' `
        -WorkingDirectory $srcDir -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog
    $up = $false
    for ($i = 0; $i -lt 50; $i++) {
        if (Test-Health) { $up = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if ($up) {
        Write-Host "Server is up (pid $($script:ServerProc.Id))." -ForegroundColor Green
    } else {
        Write-Host "Server failed to start. Last error-log lines:" -ForegroundColor Red
        if (Test-Path $ErrLog) { Get-Content $ErrLog -Tail 30 }
        if ($script:ServerProc) { Stop-Process -Id $script:ServerProc.Id -Force -ErrorAction SilentlyContinue }
        exit 1
    }
}

# DB baseline (to prove writes later)
$baseUsers   = Get-Count 'users'
$baseScans   = Get-Count 'scan_history'
$baseReports = Get-Count 'missing_reports'

# =============================================================================
#  TEST DATA
# =============================================================================
$stamp    = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$Email    = "tester_$stamp@example.com"
$Username = "tester_$stamp"
$Password = "Passw0rd!"

# real barcodes present in swapify.db
$BcUnhealthy = "8901491101837"   # Lay's Classic Salted
$BcCola      = "8901058000532"   # Coca-Cola - the product that leaked into off-topic chat replies
$BcHealthy   = "8908013479122"   # The Whole Truth protein bar
$BcBar       = "8906127540016"   # Farmley Datebites (protein_bar -> has same-cat alternatives)
$BcBar2      = "8904335602385"   # Yoga bar protein bar
$BcSauce     = "8901595862962"   # Ching's Schezwan Chutney (sauce -> no cross-cat noodles!)
$BcOff       = "3017620422003"   # Nutella -> NOT in DB, tests Open Food Facts fallback

$chatSource = "unknown"

try {
    # -------------------------------------------------------------------------
    Write-Section 0 "HEALTH CHECK  (GET /health)"
    Invoke-Api GET "/health"

    Write-Section "0b" "PRODUCT COUNT  (GET /product-count)  ->  live curated count + coverage (Task 3)"
    Invoke-Api GET "/product-count"

    Write-Section 1 "REGISTER USER  (POST /register)  ->  writes users table"
    $regBody = @{ email = $Email; username = $Username; password = $Password } | ConvertTo-Json -Compress
    Invoke-Api POST "/register" $regBody

    Write-Section 2 "LOGIN  (POST /login)  ->  returns JWT access_token"
    $loginBody = @{ email = $Email; password = $Password } | ConvertTo-Json -Compress
    Invoke-Api POST "/login" $loginBody
    try { $script:Token = ($script:LastBody | ConvertFrom-Json).access_token } catch { $script:Token = '' }
    if ($script:Token) {
        Write-Host ("Got token: {0}...({1} chars)" -f $script:Token.Substring(0, [Math]::Min(32, $script:Token.Length)), $script:Token.Length) -ForegroundColor Green
    } else {
        Write-Host "NO TOKEN - authenticated tests below will fail." -ForegroundColor Red
    }

    Write-Section 3 "PROFILE  (GET /profile)  [auth]"
    Invoke-Api GET "/profile" -Auth
    try { $script:UserId = ($script:LastBody | ConvertFrom-Json).id } catch { $script:UserId = 0 }
    Write-Host "user_id = $($script:UserId)" -ForegroundColor Blue

    Write-Section 4 "PRODUCT LOOKUP (local DB)  (GET /product/{barcode})  [auth -> records scan]"
    Write-Host "# Scanning 3 products while authenticated so they land in scan_history" -ForegroundColor DarkGray
    Invoke-Api GET "/product/$BcUnhealthy" -Auth
    Invoke-Api GET "/product/$BcHealthy"   -Auth
    Invoke-Api GET "/product/$BcBar"       -Auth

    Write-Section 5 "PRODUCT LOOKUP (Open Food Facts fallback)  (GET /product/{barcode})"
    Write-Host "# $BcOff is NOT in the local DB -> server fetches live from Open Food Facts" -ForegroundColor DarkGray
    Invoke-Api GET "/product/$BcOff" -Auth

    Write-Section 6 "HEALTH SCORE v1  (GET /score/{barcode})"
    Invoke-Api GET "/score/$BcUnhealthy"

    Write-Section 7 "HEALTH SCORE v2  (GET /v2/score/{barcode})  [personalized when auth]"
    Invoke-Api GET "/v2/score/$BcHealthy" -Auth

    Write-Section 8 "BETTER ALTERNATIVES  (GET /similar/{barcode})  [personalized]"
    Invoke-Api GET "/similar/$BcBar" -Auth
    Write-Host "   All alternatives above must share the SAME category as the scanned product (Task 2)." -ForegroundColor DarkGray

    Write-Section "8b" "BETTER ALTERNATIVES - category match (Task 2)  (GET /similar/{sauce})  ->  NO noodles"
    Write-Host "   Schezwan Chutney (category 'sauce') must NOT return Maggi (noodles). No same-" -ForegroundColor DarkGray
    Write-Host "   category peer -> the correct answer is an empty list, never a cross-category grab-bag." -ForegroundColor DarkGray
    Invoke-Api GET "/similar/$BcSauce"

    Write-Section 9 "SET PREFERENCES  (POST /preferences)  [auth]  ->  writes user_preferences"
    $prefBody = @{ preferences = @{ high_protein = $true; low_sugar = $true; vegan = $false } } | ConvertTo-Json -Compress
    Invoke-Api POST "/preferences" $prefBody -Auth

    Write-Section 10 "GET PREFERENCES  (GET /preferences)  [auth]"
    Invoke-Api GET "/preferences" -Auth

    Write-Section 11 "UPDATE PREFERENCES (alias)  (POST /update-preferences)  [auth]"
    $updBody = @{ low_sodium = $true; high_fiber = $true } | ConvertTo-Json -Compress
    Invoke-Api POST "/update-preferences" $updBody -Auth

    Write-Section 12 "BETTER ALTERNATIVES - re-ranked by NEW preferences  (GET /similar/{barcode})"
    Invoke-Api GET "/similar/$BcBar" -Auth

    Write-Section 13 "ADD FAVORITE  (POST /favorites)  [auth]  ->  writes favorites"
    $favBody = @{ barcode = $BcHealthy } | ConvertTo-Json -Compress
    Invoke-Api POST "/favorites" $favBody -Auth

    Write-Section 14 "LIST FAVORITES  (GET /favorites)  [auth]"
    Invoke-Api GET "/favorites" -Auth

    Write-Section 15 "REMOVE FAVORITE  (DELETE /favorites/{barcode})  [auth]"
    Invoke-Api DELETE "/favorites/$BcHealthy" -Auth

    Write-Section 16 "SCAN HISTORY  (GET /history)  [auth]  <- proves scans from step 4 saved"
    Invoke-Api GET "/history" -Auth

    Write-Section "16b" "ISSUE 2 - NO 'UNKNOWN PRODUCT' ANYWHERE  (/history, /favorites, /search)"
    Write-Host "  A scanned product we hold a name for must never render as 'Unknown Product'" -ForegroundColor Blue
    Write-Host "  (the reported 5-Star bug: scan_history stored the real name, /history ignored it)" -ForegroundColor Blue
    $placeholders = @('', 'unknown', 'unknown product', 'n/a', 'none', 'null')

    Invoke-Api POST "/activity" (@{ action_type = "scan"; barcode = $BcHealthy } | ConvertTo-Json -Compress) -Auth
    Invoke-Api GET "/history" -Auth
    try { $h = $script:LastBody | ConvertFrom-Json } catch { $h = @() }
    $histBad = @(@($h) | Where-Object { $placeholders -contains ("" + $_.product_name).Trim().ToLower() }).Count
    Assert-Equal "no history row renders as 'Unknown Product'" $histBad 0

    Invoke-Api POST "/favorites" (@{ barcode = $BcBar } | ConvertTo-Json -Compress) -Auth
    Invoke-Api GET "/favorites" -Auth
    try { $f = $script:LastBody | ConvertFrom-Json } catch { $f = @() }
    $favBad = @(@($f) | Where-Object { $placeholders -contains ("" + $_.product_name).Trim().ToLower() }).Count
    Assert-Equal "no favorite renders as 'Unknown Product'" $favBad 0
    Invoke-Api DELETE "/favorites/$BcBar" -Auth

    # Whole-catalogue sweep: every search result must carry a real name.
    Invoke-Api GET "/search?limit=300&external=false"
    try { $s = $script:LastBody | ConvertFrom-Json } catch { $s = @() }
    $rows = if ($s -is [array]) { $s } elseif ($s.results) { @($s.results) } else { @() }
    $nameBad = @($rows | Where-Object { $placeholders -contains ("" + $_.name).Trim().ToLower() }).Count
    Write-Host "  placeholder names / products checked: $nameBad/$($rows.Count)" -ForegroundColor Blue
    Assert-Equal "every curated product carries a real name" $nameBad 0

    # The specific product from the report.
    Invoke-Api GET "/search?q=star&external=false&limit=10"
    try { $s2 = $script:LastBody | ConvertFrom-Json } catch { $s2 = @() }
    $starRows = if ($s2 -is [array]) { $s2 } elseif ($s2.results) { @($s2.results) } else { @() }
    Write-Host "  '5 star' search -> $(($starRows | ForEach-Object { $_.name }) -join ' | ')" -ForegroundColor Blue
    $starOk = @($starRows | Where-Object { ("" + $_.name).ToLower() -like '*star*' }).Count -gt 0
    Assert-Equal "5 Star chocolate resolves by name (Issue 2)" $starOk $true

    Write-Section 17 "WEEKLY SUMMARY  (GET /weekly-summary)  [auth]"
    Invoke-Api GET "/weekly-summary" -Auth

    Write-Section 18 "MONTHLY REPORT  (GET /monthly-report)  [auth]"
    Invoke-Api GET "/monthly-report" -Auth

    Write-Section 19 "RECENT SCANS (in-memory)  (GET /recent)"
    Invoke-Api GET "/recent"

    Write-Section 20 "COMPARE TWO PRODUCTS  (GET /compare/{b1}/{b2})"
    Invoke-Api GET "/compare/$BcUnhealthy/$BcHealthy"

    Write-Section 21 "COMPARE MULTIPLE (2-4)  (POST /compare-multiple)"
    $cmpBody = @{ barcodes = @($BcBar, $BcHealthy, $BcBar2, $BcOff) } | ConvertTo-Json -Compress
    Invoke-Api POST "/compare-multiple" $cmpBody -Auth

    Write-Section 22 "OFFLINE PRODUCTS (full catalogue)  (GET /offline-products)"
    Invoke-Api GET "/offline-products" -Head 2

    Write-Section 23 "SEARCH  (GET /search?q=protein)"
    Invoke-Api GET "/search?q=protein" -Head 3

    Write-Section "23a" "CATALOGUE COMPLETENESS  (GET /search?limit=300)  ->  every curated product"
    Write-Host "# Regression guard: /search used to default to limit=10 and hard-cap at 50, so a" -ForegroundColor DarkGray
    Write-Host "# client that did not paginate could only ever show the first page - which looked" -ForegroundColor DarkGray
    Write-Host "# like 'most products are missing' even though the catalogue was complete." -ForegroundColor DarkGray
    Invoke-Api GET "/product-count"
    $curated = 0
    try { $curated = ($script:LastBody | ConvertFrom-Json).curated_count } catch { $curated = -1 }
    Invoke-Api GET "/search?limit=300" -Head 2
    $searchAll = Get-ResultCount
    Write-Host "curated_count = $curated   /search?limit=300 returned = $searchAll" -ForegroundColor Blue
    Assert-Equal "/search?limit=300 returns the whole catalogue" $searchAll $curated

    Invoke-Api GET "/search" -Head 2
    $searchDef = Get-ResultCount
    Write-Host "/search default page size = $searchDef  (expected 50, was 10)" -ForegroundColor Blue
    Assert-Equal "/search default limit is 50" $searchDef 50

    Write-Section "23b" "SEARCH PAGINATION METADATA  (GET /search?meta=true)  ->  total / has_more"
    Write-Host "# meta=true returns an envelope so the client can tell 'this is everything' apart" -ForegroundColor DarkGray
    Write-Host "# from 'this is page 1 of N'." -ForegroundColor DarkGray
    Invoke-Api GET "/search?meta=true&limit=25" -Head 2
    try {
        $m = $script:LastBody | ConvertFrom-Json
        Write-Host "total=$($m.total) count=$($m.count) has_more=$($m.has_more)" -ForegroundColor Blue
        Assert-Equal "meta total matches curated_count" $m.total   $curated
        Assert-Equal "meta count honours limit=25"      $m.count   25
        Assert-Equal "meta has_more is true"            $m.has_more $true
    } catch {
        Write-Host "  FAIL  meta envelope unparseable" -ForegroundColor Red; $script:Fail++
    }

    Write-Section 24 "REPORT MISSING PRODUCT  (POST /report-missing)  [auth]  ->  writes missing_reports"
    $rmBody = @{ barcode = "0000000000000"; product_name = "Mystery Snack"; comment = "Not in DB, please add" } | ConvertTo-Json -Compress
    Invoke-Api POST "/report-missing" $rmBody -Auth

    Write-Section 25 "AI NUTRITIONIST - general question  (POST /chat)  [uses OpenRouter key]"
    $chat1 = @{ question = "Is a diet high in saturated fat bad for my heart?" } | ConvertTo-Json -Compress
    Invoke-Api POST "/chat" $chat1
    try { $chatSource = ($script:LastBody | ConvertFrom-Json).source } catch { $chatSource = "unknown" }
    Write-Host "chat source = $chatSource  (openrouter = real AI, fallback = rule-based)" -ForegroundColor Blue

    Write-Section 26 "AI NUTRITIONIST - with product context  (POST /chat + barcode)"
    $chat2 = @{ question = "Should I eat this often?"; barcode = $BcUnhealthy } | ConvertTo-Json -Compress
    Invoke-Api POST "/chat" $chat2

    Write-Section 27 "AI NUTRITIONIST - ingredient substitution  (POST /chat)  -> substitutions[]"
    $chat3 = @{ question = "What can I use instead of sugar in baking?" } | ConvertTo-Json -Compress
    Invoke-Api POST "/chat" $chat3

    Write-Section "27a" "AI CHAT - greeting fast-path (Task 1)  (POST /chat 'hi')  ->  source 'fast-path', instant"
    Write-Host "   A bare greeting must NOT hit the LLM (no ~25s wait). Expect source=fast-path and" -ForegroundColor DarkGray
    Write-Host "   a sub-second response." -ForegroundColor DarkGray
    Invoke-Api POST "/chat" (@{ question = "hi" } | ConvertTo-Json -Compress)
    try { $fp = ($script:LastBody | ConvertFrom-Json).source } catch { $fp = "unknown" }
    Write-Host "fast-path source = $fp  (expected: fast-path)" -ForegroundColor Blue

    Write-Section "27b" "AI CHAT - structured top picks (Task 4)  (POST /chat)  ->  top_picks[] via 7+ rule"
    Write-Host "   Must return a structured top_picks[] array (score/grade/recommended/category) built" -ForegroundColor DarkGray
    Write-Host "   from the real scored catalogue - not a generic paragraph." -ForegroundColor DarkGray
    Invoke-Api POST "/chat" (@{ question = "what are the top picks from all products" } | ConvertTo-Json -Compress)

    Write-Section "27c" "AI CHAT - top picks by category (Task 4)  (POST /chat 'best chocolates')"
    Invoke-Api POST "/chat" (@{ question = "what are the best chocolates" } | ConvertTo-Json -Compress)

    Write-Section "27d" "AI CHAT - app/commerce question  ('can we buy products from this website?')"
    Write-Host "# Regression guard for the reported bug: the client attaches the last-scanned" -ForegroundColor DarkGray
    Write-Host "# barcode to EVERY message, and the prompt told the model to ground every claim in" -ForegroundColor DarkGray
    Write-Host "# that product - so this question was answered with the attached cola's score." -ForegroundColor DarkGray
    Write-Host "# Expect source=fast-path, a sub-second reply, and no product talk." -ForegroundColor DarkGray
    $buyBody = @{ question = "can we buy products from this website?"; barcode = $BcCola } | ConvertTo-Json -Compress
    Invoke-Api POST "/chat" $buyBody
    try { $buySrc = ($script:LastBody | ConvertFrom-Json).source } catch { $buySrc = '' }
    Assert-Equal "commerce question is fast-pathed" $buySrc "fast-path"
    Assert-NoProductLeak "commerce answer"

    Write-Section "27e" "AI CHAT - out-of-scope guardrail  ('what is the capital of France?')"
    Write-Host "# A general-knowledge question must be declined politely rather than answered, and" -ForegroundColor DarkGray
    Write-Host "# must NOT be answered by talking about the attached product either." -ForegroundColor DarkGray
    $offBody = @{ question = "what is the capital of France?"; barcode = $BcCola } | ConvertTo-Json -Compress
    Invoke-Api POST "/chat" $offBody
    try { $offResp = ("" + ($script:LastBody | ConvertFrom-Json).response).ToLower() } catch { $offResp = '' }
    if ($offResp.Contains('paris')) {
        Write-Host "  FAIL  model answered the trivia question (said 'Paris')" -ForegroundColor Red
        $script:Fail++
    } else {
        Write-Host "  PASS  model declined the out-of-scope question" -ForegroundColor Green
        $script:Pass++
    }
    Write-Host "  NOTE  needs a live AI key; with no key this is the rule-based fallback." -ForegroundColor DarkGray

    Write-Section "27f" "AI CHAT - commerce keywords must not hijack real questions"
    Write-Host "# The fast-path matches single keywords on word boundaries, so 'ship' inside" -ForegroundColor DarkGray
    Write-Host "# 'relationship', 'order' inside 'in order to' and 'cart' inside 'carton' must NOT" -ForegroundColor DarkGray
    Write-Host "# divert a genuine nutrition question into the canned shopping answer." -ForegroundColor DarkGray
    Invoke-Api POST "/chat" (@{ question = "what is the relationship between sugar and diabetes?" } | ConvertTo-Json -Compress)
    try { $relSrc = ($script:LastBody | ConvertFrom-Json).source } catch { $relSrc = '' }
    Write-Host "source = $relSrc  (must NOT be fast-path)" -ForegroundColor Blue
    if ($relSrc -eq 'fast-path') {
        Write-Host "  FAIL  nutrition question was wrongly fast-pathed" -ForegroundColor Red
        $script:Fail++
    } else {
        Write-Host "  PASS  nutrition question reached the AI/fallback path" -ForegroundColor Green
        $script:Pass++
    }

    Write-Section "27g" "AI CHAT - latency budget  (POST /chat, real question)"
    Write-Host "# The whole provider failover chain shares one wall-clock budget (CHAT_BUDGET," -ForegroundColor DarkGray
    Write-Host "# default 12s). Without it the chain could stack to ~48s, which is what produced" -ForegroundColor DarkGray
    Write-Host "# the reported 15-20s replies. Measure the round-trip below." -ForegroundColor DarkGray
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $latBody = @{ question = "is this high in sugar?"; barcode = $BcUnhealthy } | ConvertTo-Json -Compress
    Invoke-Api POST "/chat" $latBody
    $sw.Stop()
    $ms = [int]$sw.Elapsed.TotalMilliseconds
    Write-Host "/chat round-trip = $ms ms" -ForegroundColor Blue
    if ($ms -le 20000) {
        Write-Host "  PASS  within the 20s ceiling (budget 12s + network/cold start)" -ForegroundColor Green
        $script:Pass++
    } else {
        Write-Host "  FAIL  exceeded 20s - check CHAT_BUDGET and provider timeouts" -ForegroundColor Red
        $script:Fail++
    }

    # =========================================================================
    #  TASK 1 - CROWDSOURCED PRODUCT RATINGS
    # =========================================================================
    Write-Section 28 "SUBMIT RATING  (POST /rate-product)  [auth]  ->  writes product_ratings"
    $rate1 = @{ barcode = $BcHealthy; taste_rating = 5; quality_rating = 4; value_rating = 4 } | ConvertTo-Json -Compress
    Invoke-Api POST "/rate-product" $rate1 -Auth

    Write-Section 29 "SUBMIT RATING - 2nd product  (POST /rate-product)  [auth]"
    $rate2 = @{ barcode = $BcUnhealthy; taste_rating = 3; quality_rating = 2; value_rating = 3 } | ConvertTo-Json -Compress
    Invoke-Api POST "/rate-product" $rate2 -Auth

    Write-Section 30 "UPDATE RATING - re-rate same product  (POST /rate-product)  [auth]  ->  'Rating updated'"
    Write-Host "# Re-rating $BcHealthy overwrites the previous rating (never double-counts)" -ForegroundColor DarkGray
    $rate3 = @{ barcode = $BcHealthy; taste_rating = 4; quality_rating = 5; value_rating = 5 } | ConvertTo-Json -Compress
    Invoke-Api POST "/rate-product" $rate3 -Auth

    Write-Section 31 "RATING VALIDATION - star out of range  (POST /rate-product)  [auth]  ->  expect HTTP 400"
    Write-Host "# taste_rating=9 is invalid (must be 1-5) - the endpoint should reject it" -ForegroundColor DarkGray
    $rateBad = @{ barcode = $BcHealthy; taste_rating = 9; quality_rating = 3; value_rating = 3 } | ConvertTo-Json -Compress
    Invoke-Api POST "/rate-product" $rateBad -Auth -Expect '4'

    Write-Section 32 "PRODUCT AVERAGE RATINGS  (GET /product/{barcode}/ratings)  [public]"
    Invoke-Api GET "/product/$BcHealthy/ratings"

    Write-Section 33 "USER'S OWN RATINGS  (GET /user/ratings)  [auth]  <- proves ratings from 28-30 saved"
    Invoke-Api GET "/user/ratings" -Auth

    # =========================================================================
    #  TASK 2 - AI-POWERED PRODUCT RECOMMENDATIONS
    # =========================================================================
    Write-Section 34 "RECOMMENDATIONS - personalized  (GET /recommendations)  [auth]"
    Write-Host "# Uses this user's scan history, preferences, comparisons + community ratings" -ForegroundColor DarkGray
    Invoke-Api GET "/recommendations" -Auth -Head 3
    Invoke-Api GET "/recommendations?limit=5" -Auth -Head 3

    Write-Section 35 "RECOMMENDATIONS - generic popular  (GET /recommendations)  [anonymous]"
    Invoke-Api GET "/recommendations" -Head 3

    # =========================================================================
    #  TASK 3 - SHAREABLE SCORE CARD
    # =========================================================================
    Write-Section 36 "SHARE CARD  (GET /share/{barcode})  [local product]"
    Invoke-Api GET "/share/$BcUnhealthy"

    Write-Section 37 "SHARE CARD  (GET /share/{barcode})  [Open Food Facts fallback -> has image_url]"
    Invoke-Api GET "/share/$BcOff"

    # =========================================================================
    #  TASK - PRODUCT BARCODE VALIDATION & CORRECTION
    # =========================================================================
    Write-Section 38 "VALIDATE BARCODE - valid EAN-13  (GET /validate-barcode/{barcode})"
    Invoke-Api GET "/validate-barcode/$BcUnhealthy"

    Write-Section 39 "VALIDATE BARCODE - invalid check digit  (GET /validate-barcode/{barcode})  -> suggestion"
    Write-Host "# 8901491101830 has a wrong check digit; the API suggests 8901491101837" -ForegroundColor DarkGray
    Invoke-Api GET "/validate-barcode/8901491101830"

    Write-Section 40 "VALIDATE BARCODE - non-numeric  (GET /validate-barcode/{barcode})"
    Invoke-Api GET "/validate-barcode/abc123"

    Write-Section 41 "SEARCH BY BARCODE - auto-corrects a mistyped check digit  (GET /search?q=)"
    Write-Host "# q is a barcode with a bad check digit; search still finds the product" -ForegroundColor DarkGray
    Invoke-Api GET "/search?q=8901491101830"

    Write-Section 42 "PRODUCT LOOKUP - unknown malformed barcode  (GET /product/{barcode})  -> 404 + suggestion"
    Invoke-Api GET "/product/9999999999998" -Expect '4'

    # =========================================================================
    #  TASK - USER ACTIVITY LOGGING
    # =========================================================================
    Write-Section 43 "LOG ACTIVITY  (POST /activity)  [auth]  ->  writes user_activity"
    $actBody = @{ action_type = "scan"; barcode = $BcUnhealthy; metadata = @{ src = "test-suite" } } | ConvertTo-Json -Compress
    Invoke-Api POST "/activity" $actBody -Auth

    Write-Section 44 "LOG ACTIVITY - invalid action_type  (POST /activity)  [auth]  ->  expect HTTP 400"
    $actBad = @{ action_type = "teleport" } | ConvertTo-Json -Compress
    Invoke-Api POST "/activity" $actBad -Auth -Expect '4'

    Write-Section 45 "USER ACTIVITY HISTORY  (GET /activity/user/{user_id})  <- scans/compare/rate/favorite/share auto-logged above"
    Invoke-Api GET "/activity/user/$($script:UserId)" -Head 5

    Write-Section 46 "ACTIVITY TRENDS (overall)  (GET /activity/trends)"
    Invoke-Api GET "/activity/trends"

    # =========================================================================
    #  TASK - DAILY DIGEST / NOTIFICATION
    # =========================================================================
    Write-Section 47 "DAILY DIGEST  (GET /digest/{user_id})  <- summarises today's scans, notification/email ready"
    Invoke-Api GET "/digest/$($script:UserId)"

    # =========================================================================
    #  TASK 1 - WEEKLY CHALLENGES & LEADERBOARD
    # =========================================================================
    Write-Section 48 "LIST CHALLENGES  (GET /challenges)  [anonymous]  -> 4 active weekly challenges"
    Invoke-Api GET "/challenges"

    Write-Section 49 "JOIN CHALLENGE - 'Scan 20 products this week'  (POST /challenges/1/join)  [auth]  ->  writes challenge_participants"
    Invoke-Api POST "/challenges/1/join" -Auth

    Write-Section 50 "JOIN CHALLENGE - 'Compare 10 products'  (POST /challenges/3/join)  [auth]"
    Invoke-Api POST "/challenges/3/join" -Auth

    Write-Section 51 "JOIN CHALLENGE - 'Rate 15 products'  (POST /challenges/4/join)  [auth]"
    Invoke-Api POST "/challenges/4/join" -Auth

    Write-Section 52 "RE-JOIN (idempotent)  (POST /challenges/1/join)  [auth]  ->  'Already joined'"
    Invoke-Api POST "/challenges/1/join" -Auth

    Write-Section 53 "CHALLENGE PROGRESS  (GET /challenges/1/progress)  [auth]  <- counts the scans from step 4"
    Invoke-Api GET "/challenges/1/progress" -Auth

    Write-Section 54 "LIST CHALLENGES with my progress  (GET /challenges)  [auth]  -> joined + progress per challenge"
    Invoke-Api GET "/challenges" -Auth

    Write-Section 55 "JOIN UNKNOWN CHALLENGE  (POST /challenges/999/join)  [auth]  ->  expect HTTP 404"
    Invoke-Api POST "/challenges/999/join" -Auth -Expect '4'

    Write-Section 56 "LEADERBOARD - weekly  (GET /leaderboard?period=weekly)  -> rank, username, score, badges"
    Invoke-Api GET "/leaderboard?period=weekly&limit=10" -Head 5

    Write-Section 57 "LEADERBOARD - monthly  (GET /leaderboard?period=monthly)"
    Invoke-Api GET "/leaderboard?period=monthly&limit=5" -Head 5

    Write-Section 58 "LEADERBOARD - all-time  (GET /leaderboard?period=all-time)"
    Invoke-Api GET "/leaderboard?period=all-time&limit=5" -Head 5

    Write-Section 59 "LEADERBOARD - invalid period  (GET /leaderboard?period=daily)  ->  expect HTTP 400"
    Invoke-Api GET "/leaderboard?period=daily" -Expect '4'

    # =========================================================================
    #  TASK 2 - SMART CART / SHOPPING LIST OPTIMIZATION
    # =========================================================================
    Write-Section 60 "CREATE SHOPPING LIST  (POST /shopping-list)  [auth]  ->  writes shopping_lists + items"
    $slBody = @{ name = "Weekly Groceries"; items = @($BcBar, $BcBar2, $BcUnhealthy) } | ConvertTo-Json -Compress
    Invoke-Api POST "/shopping-list" $slBody -Auth
    try { $script:ListId = ($script:LastBody | ConvertFrom-Json).id } catch { $script:ListId = 0 }
    Write-Host "shopping list id = $($script:ListId)" -ForegroundColor Blue

    Write-Section 61 "GET SHOPPING LIST  (GET /shopping-list/{id})  <- each item scored"
    Invoke-Api GET "/shopping-list/$($script:ListId)"

    Write-Section 62 "OPTIMIZE SHOPPING LIST  (GET /shopping-list/{id}/optimize)  <- original + top 2 healthier alternatives"
    Invoke-Api GET "/shopping-list/$($script:ListId)/optimize" -Auth

    Write-Section 63 "REPLACE AN ITEM  (POST /shopping-list/{id}/replace)  <- swap Chocobar for the healthy protein bar"
    $replBody = @{ old_barcode = $BcBar; new_barcode = $BcHealthy } | ConvertTo-Json -Compress
    Invoke-Api POST "/shopping-list/$($script:ListId)/replace" $replBody -Auth

    Write-Section 64 "GET UNKNOWN SHOPPING LIST  (GET /shopping-list/999999)  ->  expect HTTP 404"
    Invoke-Api GET "/shopping-list/999999" -Expect '4'

    Write-Section 65 "CREATE + DELETE a throwaway list  (POST then DELETE /shopping-list/{id})"
    $slTmp = @{ items = @($BcHealthy) } | ConvertTo-Json -Compress
    Invoke-Api POST "/shopping-list" $slTmp -Auth
    try { $tmpListId = ($script:LastBody | ConvertFrom-Json).id } catch { $tmpListId = 0 }
    Invoke-Api DELETE "/shopping-list/$tmpListId" -Auth

    # =========================================================================
    #  TASK 3 - COMMUNITY REVIEWS & DISCUSSIONS
    # =========================================================================
    Write-Section 66 "SUBMIT REVIEW  (POST /reviews)  [auth]  ->  writes reviews (text + 1-5 stars)"
    $revBody = @{ barcode = $BcUnhealthy; rating = 4; review_text = "Great crunch but way too salty for daily snacking." } | ConvertTo-Json -Compress
    Invoke-Api POST "/reviews" $revBody -Auth
    try { $script:ReviewId = ($script:LastBody | ConvertFrom-Json).review.id } catch { $script:ReviewId = 0 }
    Write-Host "review id = $($script:ReviewId)" -ForegroundColor Blue

    Write-Section 67 "REVIEW VALIDATION - rating out of range  (POST /reviews)  [auth]  ->  expect HTTP 400"
    $revBad = @{ barcode = $BcUnhealthy; rating = 9; review_text = "bad" } | ConvertTo-Json -Compress
    Invoke-Api POST "/reviews" $revBad -Auth -Expect '4'

    Write-Section 68 "UPVOTE A REVIEW  (POST /reviews/{id}/vote)  [auth]  ->  writes review_votes"
    Invoke-Api POST "/reviews/$($script:ReviewId)/vote" '{"vote":"up"}' -Auth

    Write-Section 69 "REPLY TO A REVIEW  (POST /reviews/{id}/replies)  [auth]  ->  writes review_replies"
    Invoke-Api POST "/reviews/$($script:ReviewId)/replies" '{"reply_text":"Agreed - the sodium is the main downside here."}' -Auth

    Write-Section 70 "GET SINGLE REVIEW  (GET /reviews/{id})  <- with vote counts + replies"
    Invoke-Api GET "/reviews/$($script:ReviewId)"

    Write-Section 71 "GET ALL REVIEWS FOR A PRODUCT  (GET /product/{barcode}/reviews)  <- with average rating"
    Invoke-Api GET "/product/$BcUnhealthy/reviews"

    Write-Section 72 "DELETE SOMEONE ELSE'S REVIEW  (DELETE /reviews/{id})  <- 2nd user  ->  expect HTTP 403"
    Write-Host "# register a 2nd user and try to delete user 1's review - the API must forbid it" -ForegroundColor DarkGray
    $stamp2 = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $reg2 = @{ email = "tester2_$stamp2@example.com"; username = "tester2_$stamp2"; password = $Password } | ConvertTo-Json -Compress
    Invoke-Api POST "/register" $reg2
    $login2 = @{ email = "tester2_$stamp2@example.com"; password = $Password } | ConvertTo-Json -Compress
    Invoke-Api POST "/login" $login2
    try { $token2 = ($script:LastBody | ConvertFrom-Json).access_token } catch { $token2 = '' }
    $oldToken = $script:Token; $script:Token = $token2
    Invoke-Api DELETE "/reviews/$($script:ReviewId)" -Auth -Expect '4'
    $script:Token = $oldToken

    Write-Section 73 "CREATE + DELETE own review  (POST then DELETE /reviews/{id})  [auth]  ->  'Review deleted'"
    $revTmp = @{ barcode = $BcHealthy; rating = 5; review_text = "Clean ingredients, will buy again." } | ConvertTo-Json -Compress
    Invoke-Api POST "/reviews" $revTmp -Auth
    try { $tmpReviewId = ($script:LastBody | ConvertFrom-Json).review.id } catch { $tmpReviewId = 0 }
    Invoke-Api DELETE "/reviews/$tmpReviewId" -Auth

    # =========================================================================
    #  TASK 1 - PERSONALIZED HOME FEED
    # =========================================================================
    Write-Section 74 "HOME FEED - personalized  (GET /home-feed)  [auth]  <- recently_scanned + recommendations + challenge_progress + badges_earned"
    Write-Host "# Task 3 shape: recently_scanned[{...,score,grade,image_url}], recommendations[{...,score,reason,image_url}]," -ForegroundColor DarkGray
    Write-Host "#             challenge_progress{challenge_name,progress,target}, badges_earned[{name,icon,earned_at}]" -ForegroundColor DarkGray
    Invoke-Api GET "/home-feed" -Auth

    Write-Section 75 "HOME FEED - via explicit user_id  (GET /home-feed?user_id=)  [public]"
    Invoke-Api GET "/home-feed?user_id=$($script:UserId)"

    Write-Section 76 "HOME FEED - generic fallback  (GET /home-feed)  [anonymous]  -> popular recommendations, preview challenge (progress 0), no badges"
    Invoke-Api GET "/home-feed"

    # =========================================================================
    #  TASK 2 - SMART SEARCH WITH AUTOCOMPLETE
    # =========================================================================
    Write-Section 77 "AUTOCOMPLETE  (GET /search/autocomplete?q=pro)  -> name + brand + barcode suggestions"
    Invoke-Api GET "/search/autocomplete?q=pro&limit=5"

    Write-Section 78 "AUTOCOMPLETE - blank query  (GET /search/autocomplete?q=)  -> empty suggestions (still 200)"
    Invoke-Api GET "/search/autocomplete?q="

    Write-Section "78a" "AUTOCOMPLETE reaches OPEN FOOD FACTS  (q=nutella - a product NOT in our catalogue)"
    # Regression guard: the search box only ever calls /search/autocomplete, so
    # while this was a DB-only lookup the UI could not see anything beyond our
    # ~250 curated rows - "nutella" returned 0 suggestions while /search had 20.
    Invoke-Api GET "/search/autocomplete?q=nutella&limit=8"
    try { $ac = $script:LastBody | ConvertFrom-Json } catch { $ac = $null }
    $offRows = @($ac.suggestions | Where-Object { $_.source -eq 'openfoodfacts' })
    Assert-Equal "autocomplete surfaces Open Food Facts products" ($offRows.Count -gt 0) $true
    $offNamed = @($offRows | Where-Object { -not [string]::IsNullOrWhiteSpace($_.product_name) }).Count
    Assert-Equal "every OFF suggestion carries a product_name" $offNamed $offRows.Count

    Write-Section "78b" "AUTOCOMPLETE - external=false keeps the curated-only behaviour"
    Invoke-Api GET "/search/autocomplete?q=nutella&limit=8&external=false"
    try { $ac2 = $script:LastBody | ConvertFrom-Json } catch { $ac2 = $null }
    $offCount2 = @($ac2.suggestions | Where-Object { $_.source -eq 'openfoodfacts' }).Count
    Assert-Equal "external=false returns no OFF rows" $offCount2 0

    Write-Section 79 "SEARCH - enhanced filtering  (GET /search?q=protein&sort=score_desc&limit=5)"
    Invoke-Api GET "/search?q=protein&sort=score_desc&limit=5" -Head 3

    Write-Section 80 "SEARCH - filter by category  (GET /search?category=chips)"
    Invoke-Api GET "/search?category=chips&limit=5" -Head 5

    # =========================================================================
    #  TASK 3 - "SWAPIFY RECOMMENDED" BADGE
    # =========================================================================
    Write-Section 81 "PRODUCT BADGE  (GET /product/{barcode}/badge)  <- criteria: score>7, no high-risk, no artificial colors"
    Invoke-Api GET "/product/$BcHealthy/badge"

    Write-Section 82 "PRODUCT BADGE - unhealthy product  (GET /product/{barcode}/badge)  -> is_recommended false + failing_criteria"
    Invoke-Api GET "/product/$BcUnhealthy/badge"

    Write-Section 83 "BADGE INTEGRATED IN /product  (GET /product/{barcode})  <- response now carries is_recommended + recommended_badge"
    Invoke-Api GET "/product/$BcHealthy"

    Write-Section 84 "PRODUCT BADGE - unknown barcode  (GET /product/{barcode}/badge)  ->  expect HTTP 404"
    Invoke-Api GET "/product/0000000000000/badge" -Expect '4'

    # =========================================================================
    #  TASK 1 - API PERFORMANCE  (pagination, gzip compression)
    # =========================================================================
    Write-Section 85 "SEARCH PAGINATION - page 1  (GET /search?...&limit=3&offset=0)  (Task 1B)"
    Invoke-Api GET "/search?q=&sort=name&limit=3&offset=0" -Head 3

    Write-Section 86 "SEARCH PAGINATION - page 2  (GET /search?...&limit=3&offset=3)  <- different products than page 1"
    Invoke-Api GET "/search?q=&sort=name&limit=3&offset=3" -Head 3

    Write-Section 87 "GZIP COMPRESSION  (GET /search with Accept-Encoding: gzip)  -> Content-Encoding: gzip  (Task 1D)"
    Test-Gzip "/search?q=&limit=50&sort=name"

    # =========================================================================
    #  TASK 2 - PRODUCT IMAGES  (image_url in responses + crowdsourced upload)
    # =========================================================================
    New-TestImages

    Write-Section 88 "IMAGE URL IN /search  (GET /search?q=protein)  <- every result carries image_url (placeholder when none)  (Task 2B)"
    Invoke-Api GET "/search?q=protein&limit=5" -Head 5
    Test-ImageUrl -Mode array

    Write-Section 89 "IMAGE URL IN /similar  (GET /similar/{barcode})  <- every alternative carries image_url  (Task 2B)"
    Invoke-Api GET "/similar/$BcBar" -Head 3
    Test-ImageUrl -Mode array

    Write-Section 90 "UPLOAD PRODUCT IMAGE - valid PNG  (POST /product/image)  [auth]  ->  stores reference, updates products.image_url  (Task 2C)"
    Invoke-Upload "/product/image" $BcUnhealthy (Join-Path $script:ImgDir 'valid.png') "image/png" -Auth

    Write-Section 91 "PRODUCT NOW RETURNS THE UPLOADED image_url  (GET /product/{barcode})  <- cache invalidated on upload"
    Invoke-Api GET "/product/$BcUnhealthy"
    if ($script:LastBody -match "/product-images/$BcUnhealthy\.") {
        Write-Host "image_url now points at the uploaded file (cache was invalidated on upload)" -ForegroundColor Green
        $script:Pass++
    } else {
        Write-Host "image_url did not update to the uploaded file" -ForegroundColor Red
        $script:Fail++
    }

    Write-Section 92 "UPLOAD - reject non-image  (POST /product/image with a text file)  ->  expect HTTP 400  (Task 2C validation)"
    Invoke-Upload "/product/image" $BcUnhealthy (Join-Path $script:ImgDir 'not_image.png') "image/png" -Auth -Expect '4'

    Write-Section 93 "UPLOAD - reject file > 2 MB  (POST /product/image with a 2.1 MB file)  ->  expect HTTP 413  (Task 2C validation)"
    Invoke-Upload "/product/image" $BcUnhealthy (Join-Path $script:ImgDir 'too_big.png') "image/png" -Auth -Expect '4'

    # =========================================================================
    #  TASK 6 - OCR LABEL SCANNER (Proof of Concept)
    # =========================================================================
    Write-Section 94 "OCR AVAILABILITY  (GET /ocr/health)  -> reports whether Tesseract is installed"
    Invoke-Api GET "/ocr/health"
    $ocrAvailable = $false
    try { $ocrAvailable = [bool]($script:LastBody | ConvertFrom-Json).ocr_available } catch { $ocrAvailable = $false }
    Write-Host "OCR available = $ocrAvailable  (true -> scan-label returns 200; false -> 503)" -ForegroundColor Blue

    Write-Section 95 "OCR SCAN LABEL  (POST /ocr/scan-label)  <- extracts text/ingredients, scores via the engine"
    # Expected status depends on whether the Tesseract engine is installed on this host.
    $ocrExpect = if ($ocrAvailable) { '2' } else { '5' }
    Invoke-Upload "/ocr/scan-label" $BcUnhealthy (Join-Path $script:ImgDir 'valid.png') "image/png" -Expect $ocrExpect

    # =========================================================================
    Write-Banner "22 JULY - FIXES & NEW FEATURES"

    Write-Section 96 "FIX 1 - NUTRITION PER 100g  (GET /product/{barcode})  <- response carries nutrition_per_100g"
    Write-Host "# Frooti has a 200 ml serving, so per-100g sugar should be HALF the per-serving 31.2g (~15.6g)" -ForegroundColor DarkGray
    Invoke-Api GET "/product/8902579100025"
    Write-Host "nutrition_per_100g.sugar should be ~15.6 (per-serving sugar_g_per_serving is 31.2)" -ForegroundColor Blue

    Write-Section 97 "FIX 2 - SCORE CONSISTENCY  (GET /score + GET /v2/score for the same product)  <- one engine"
    Invoke-Api GET "/score/$BcHealthy"
    Invoke-Api GET "/v2/score/$BcHealthy"

    Write-Section 98 "FIX 3 - AI CHAT PRODUCT LOOKUP BY NAME  (POST /chat {question:'Frooti score'})  <- product_in_database:true"
    Invoke-Api POST "/chat" '{"question":"Frooti score"}'
    Write-Host "response should be structured Markdown; product_in_database = true, resolved_by = name" -ForegroundColor Blue

    Write-Section "98b" "FIX 3 - AI CHAT UNKNOWN PRODUCT -> scan guidance  (POST /chat {question:'score of ZZZ mystery bar'})"
    Invoke-Api POST "/chat" '{"question":"what is the score of ZZZ mystery bar"}'
    Write-Host "response should guide the user to scan the barcode; product_in_database = false" -ForegroundColor Blue

    Write-Section 99 "FEATURE 1 - AVAILABLE PREFERENCES  (GET /preferences/available)  <- lists scoring + clean-label prefs"
    Invoke-Api GET "/preferences/available"

    Write-Section "99b" "FEATURE 1 - CLEAN-LABEL FILTER  (GET /search?q=lay&no_palm_oil=true)  <- palm-oil products removed"
    Invoke-Api GET "/search?q=lay&no_palm_oil=true&limit=10" -Head 10
    Write-Host "'Lay's Classic Salted' (palm oil in its ingredients) must be filtered OUT vs the same query without the flag" -ForegroundColor Blue

    Write-Section "99c" "FEATURE 1 - SAVE NEW PREFERENCES  (POST /preferences {clean_label:true})  [auth]"
    Invoke-Api POST "/preferences" '{"preferences":{"clean_label":true,"no_palm_oil":true}}' -Auth

    Write-Section 100 "FEATURE 2 - BETTER FOR YOU BADGE  (GET /product/{barcode})  <- is_better_for_you flag"
    Invoke-Api GET "/product/$BcHealthy"
    Write-Host "response carries is_better_for_you (true when score >= 7) and better_for_you_badge" -ForegroundColor Blue

    Write-Section 101 "FEATURE 4 - LIST CATEGORIES  (GET /products/categories)  <- DB + Open Food Facts counts"
    Invoke-Api GET "/products/categories"
    try { $c = $script:LastBody | ConvertFrom-Json } catch { $c = $null }
    $catsTotal = if ($c) { [int]$c.total_products } else { 0 }
    $catsDb    = if ($c) { [int]$c.db_products } else { 0 }
    $catsExt   = if ($c) { [int]$c.external_products } else { 0 }
    $catsPending = if ($c) { [int]$c.counts_pending } else { 0 }
    Write-Host "  total_products=$catsTotal  (db=$catsDb + external=$catsExt, counts_pending=$catsPending)" -ForegroundColor Blue
    Assert-Equal "categories status 200" $script:LastCode 200
    # July 28: each tile is db_count + external_count, so the grid reflects what is
    # browsable rather than the size of our seed catalogue (the "stuck at 367" bug).
    $tiles = if ($c) { @($c.categories) } else { @() }
    $tileShape = $false
    if ($tiles.Count -gt 0) {
        $names = @($tiles[0].PSObject.Properties.Name)
        $missing = @('category', 'label', 'count', 'db_count', 'external_count') |
            Where-Object { $names -notcontains $_ }
        $tileShape = (@($missing).Count -eq 0)
    }
    Assert-Equal "each tile carries db_count + external_count" $tileShape $true
    $sumOk = $false
    if ($tiles.Count -gt 0) {
        $bad = @($tiles | Where-Object { [int]$_.count -ne ([int]$_.db_count + [int]$_.external_count) }).Count
        $sumOk = ($bad -eq 0) -and ($catsTotal -eq ($catsDb + $catsExt))
    }
    Assert-Equal "count == db_count + external_count everywhere" $sumOk $true
    # July 30 (Task 3): external_count is Open Food Facts' REAL count for the category,
    # not our fetch cap. It used to be SWAPIFY_CATEGORY_EXTERNAL_LIMIT (200), which is
    # how the section reported a few hundred products for a database of millions.
    $known = @($tiles | Where-Object { $_.external_count_known })
    if ($known.Count -gt 0 -and (@($known | ForEach-Object { [int]$_.external_count } |
            Measure-Object -Maximum).Maximum -gt 1000)) {
        Assert-Equal "a category reports thousands of OFF products (not a 200 cap)" $true $true
    } else {
        Write-Host "  (no OFF counts resolved - network/OFF may be unavailable; not failing the run)" -ForegroundColor Yellow
    }

    Write-Section "101a" "FEATURE 4 - CURATED-ONLY COUNTS  (GET /products/categories?external=false)  <- opt out of OFF"
    Invoke-Api GET "/products/categories?external=false"
    try { $c2 = $script:LastBody | ConvertFrom-Json } catch { $c2 = $null }
    $dbOnlyTotal = if ($c2) { [int]$c2.total_products } else { 0 }
    $dbOnlyExt   = if ($c2) { [int]$c2.external_products } else { -1 }
    Write-Host "  external=false -> total_products=$dbOnlyTotal (external_products=$dbOnlyExt)" -ForegroundColor Blue
    Assert-Equal "external=false reports no external products" $dbOnlyExt 0
    if ($catsTotal -gt $dbOnlyTotal) {
        Assert-Equal "combined counts exceed curated-only counts" $true $true
    } else {
        Write-Host "  (no OFF products merged - network/OFF may be unavailable; not failing the run)" -ForegroundColor Yellow
    }

    Write-Section "101b" "FEATURE 4 - PRODUCTS BY CATEGORY  (GET /products/by-category/{category})  <- paginated + scored"
    Invoke-Api GET "/products/by-category/protein_bar?limit=3"

    Write-Section "101c" "TASK 3 - CATEGORY PAGING IS UNCAPPED  (GET /products/by-category/biscuit?offset=...)  <- pages into OFF"
    Invoke-Api GET "/products/by-category/biscuit?limit=5"
    try { $bc = $script:LastBody | ConvertFrom-Json } catch { $bc = $null }
    $bcTotal = if ($bc) { [int]$bc.total } else { 0 }
    $bcDb    = if ($bc) { [int]$bc.db_total } else { 0 }
    Write-Host "  biscuit: total=$bcTotal (our rows: $bcDb) - total is db + Open Food Facts' real count" -ForegroundColor Blue
    Assert-Equal "by-category status 200" $script:LastCode 200
    # An offset far past our curated rows must fetch the corresponding page from Open
    # Food Facts on demand. Before Task 3 browsing stopped at the 200 products we had
    # pre-fetched, so a deep offset returned an empty page.
    Invoke-Api GET "/products/by-category/biscuit?limit=5&offset=1000"
    try { $deep = $script:LastBody | ConvertFrom-Json } catch { $deep = $null }
    if ($deep -and [int]$deep.count -gt 0 -and [int]$deep.external_count -gt 0) {
        Assert-Equal "offset=1000 still returns Open Food Facts products" $true $true
    } else {
        Write-Host "  (deep page empty - network/OFF may be unavailable; not failing the run)" -ForegroundColor Yellow
    }

    Write-Section "101d" "TASK 1 - LOOK UP A PRODUCT BY NAME  (GET /product/by-name/{name})  <- auto-fill without a barcode"
    Invoke-Api GET "/product/by-name/Nutella"
    Assert-Equal "by-name status 200" $script:LastCode 200
    try { $byName = $script:LastBody | ConvertFrom-Json } catch { $byName = $null }
    $byNameOk = ($byName -and $null -ne $byName.score -and
                 $byName.resolution -and @($byName.resolution.sources_tried).Count -gt 0)
    Assert-Equal "by-name returns a scored product with a resolution trail" $byNameOk $true

    Write-Section "101e" "TASK 1 - FORCE A RE-RESOLVE  (GET /product/{barcode}?refresh=true)  <- bypasses caches + cooldown"
    Invoke-Api GET "/product/$BcHealthy`?refresh=true"
    Assert-Equal "refresh=true still returns the product" $script:LastCode 200

    Write-Section 102 "FEATURE 3 - WEEKLY DIGEST PREVIEW  (GET /weekly-digest/{user_id})  <- data + rendered email"
    Invoke-Api GET "/weekly-digest/$($script:UserId)" -Auth

    Write-Section "102b" "FEATURE 3 - EMAIL PREFERENCES  (GET/POST /email-preferences)  [auth]"
    Invoke-Api GET "/email-preferences" -Auth
    Invoke-Api POST "/email-preferences" '{"weekly_digest":true}' -Auth

    Write-Section "102c" "FEATURE 3 - SEND WEEKLY DIGEST NOW  (POST /weekly-digest/{user_id}/send)  <- writes to outbox by default"
    Invoke-Api POST "/weekly-digest/$($script:UserId)/send" -Auth

    Write-Section "102d" "FEATURE 3 - ADMIN BATCH SEND  (POST /admin/send-weekly-digests)  <- requires X-Admin-Token"
    $adminToken = if ($env:ADMIN_TOKEN) { $env:ADMIN_TOKEN } else { 'swapify-admin-dev' }
    Invoke-Admin POST "/admin/send-weekly-digests?limit=2" -AdminToken $adminToken

    Write-Section "102e" "FEATURE 3 - ADMIN BATCH WITHOUT TOKEN -> 403  (POST /admin/send-weekly-digests)"
    Invoke-Api POST "/admin/send-weekly-digests" -Expect '4'

    Write-Section "102f" "FEATURE 3 - UNSUBSCRIBE BAD TOKEN -> 400  (GET /unsubscribe?token=garbage)"
    Invoke-Api GET "/unsubscribe?token=garbage" -Expect '4'

    # =========================================================================
    #  AUTH: forgot password / reset password / Google OAuth
    # =========================================================================
    # Uses its own throwaway account so changing a password can't disturb the
    # rest of the run ($script:Token is a JWT and stays valid regardless).
    $resetEmail   = "resettester_$stamp@example.com"
    $resetPassOld = 'Passw0rd!'
    $resetPassNew = 'N3wPassw0rd!'

    Write-Section "105a" "AUTH - EMAIL DELIVERY STATUS  (GET /auth/email/status)  <- which provider is live"
    Invoke-Api GET "/auth/email/status"
    try { $mail = $script:LastBody | ConvertFrom-Json } catch { $mail = $null }
    $mailProvider = if ($mail) { $mail.provider } else { 'unknown' }
    Write-Host "  email provider = $mailProvider" -ForegroundColor Blue
    if ($mailProvider -eq 'outbox') {
        Write-Host "  No mail provider configured - reset mail goes to outbox/*.eml (dry run)." -ForegroundColor Yellow
    }

    Write-Section "105b" "AUTH - FORGOT PASSWORD, UNKNOWN EMAIL  (POST /forgot-password)  <- must NOT reveal that"
    Invoke-Api POST "/forgot-password" (@{ email = "definitely-not-registered-$stamp@example.com" } | ConvertTo-Json -Compress)
    try { $genericMsg = ($script:LastBody | ConvertFrom-Json).message } catch { $genericMsg = '' }

    Write-Section "105c" "AUTH - FORGOT PASSWORD, MALFORMED EMAIL -> 400"
    Invoke-Api POST "/forgot-password" '{"email":"not-an-email"}' -Expect '4'

    Write-Section "105d" "AUTH - FORGOT PASSWORD, REAL ACCOUNT  (POST /forgot-password)"
    Invoke-Api POST "/register" (@{ email = $resetEmail; username = "resettester_$stamp"; password = $resetPassOld } | ConvertTo-Json -Compress)
    Invoke-Api POST "/forgot-password" (@{ email = $resetEmail } | ConvertTo-Json -Compress)
    try { $forgot = $script:LastBody | ConvertFrom-Json } catch { $forgot = $null }
    $knownMsg = if ($forgot) { $forgot.message } else { '' }
    # Account-enumeration guard: registered and unregistered addresses must get
    # the byte-identical response.
    Assert-Equal "known and unknown emails get the identical response" ($knownMsg -eq $genericMsg -and $genericMsg) $true
    # The token comes back only when no mail provider is configured
    # (PASSWORD_RESET_EXPOSE_TOKEN=auto); with SMTP live it is in the inbox, so
    # the end-to-end steps are skipped rather than failed.
    $resetToken = ''
    if ($forgot -and $forgot.PSObject.Properties.Name -contains 'debug' -and $forgot.debug) {
        $resetToken = $forgot.debug.token
    }

    if ($resetToken) {
        Write-Section "105e" "AUTH - VALIDATE THE EMAILED LINK  (GET /reset-password/validate?token=...)"
        Invoke-Api GET "/reset-password/validate?token=$resetToken"
        try { $v = $script:LastBody | ConvertFrom-Json } catch { $v = $null }
        Assert-Equal "the emailed token validates" ($v -and $v.valid) $true
        Assert-Equal "the email is masked in the response" ($v -and "$($v.email)".Contains('*')) $true

        Write-Section "105f" "AUTH - RESET REJECTS A TOO-SHORT PASSWORD -> 400"
        Invoke-Api POST "/reset-password" (@{ token = $resetToken; new_password = 'abc' } | ConvertTo-Json -Compress) -Expect '4'

        Write-Section "105g" "AUTH - RESET PASSWORD  (POST /reset-password)"
        Invoke-Api POST "/reset-password" (@{ token = $resetToken; new_password = $resetPassNew } | ConvertTo-Json -Compress)

        Write-Section "105h" "AUTH - THE NEW PASSWORD WORKS, THE OLD ONE DOESN'T"
        Invoke-Api POST "/login" (@{ email = $resetEmail; password = $resetPassNew } | ConvertTo-Json -Compress)
        Assert-Equal "login with the NEW password" $script:LastCode 200
        Invoke-Api POST "/login" (@{ email = $resetEmail; password = $resetPassOld } | ConvertTo-Json -Compress) -Expect '4'
        Assert-Equal "login with the OLD password is refused" $script:LastCode 401

        Write-Section "105i" "AUTH - THE RESET LINK IS SINGLE-USE  (replaying it -> 400)"
        Invoke-Api POST "/reset-password" (@{ token = $resetToken; new_password = 'Another1!' } | ConvertTo-Json -Compress) -Expect '4'
    } else {
        Write-Host "  (skipping 105e-105i: a mail provider is configured, so the token is only in the inbox)" -ForegroundColor Yellow
    }

    Write-Section "105j" "AUTH - RESET WITH A GARBAGE TOKEN -> 400"
    Invoke-Api POST "/reset-password" '{"token":"garbage-token","new_password":"Whatever1!"}' -Expect '4'

    Write-Section "105k" "AUTH - THE RESET PAGE THE EMAIL LINKS TO  (GET /reset-password)"
    Invoke-Api GET "/reset-password"
    Assert-Equal "reset page served" $script:LastCode 200

    Write-Section "105l" "AUTH - GOOGLE OAUTH CONFIG  (GET /auth/google/config)  <- no secrets, drives the button"
    Invoke-Api GET "/auth/google/config"
    try { $gcfg = $script:LastBody | ConvertFrom-Json } catch { $gcfg = $null }
    $googleConfigured = ($gcfg -and $gcfg.configured)
    Write-Host "  google oauth configured = $googleConfigured" -ForegroundColor Blue
    if ($gcfg) {
        Assert-Equal "config never returns the client secret" ($gcfg.PSObject.Properties.Name -contains 'client_secret') $false
    }

    if ($googleConfigured) {
        Write-Section "105m" "AUTH - GOOGLE SIGN-IN REDIRECTS TO GOOGLE  (GET /auth/google/login -> 302)"
        # The 302 itself must be observed, not followed. Invoke-WebRequest cannot do
        # this reliably: on Windows PowerShell 5.1 `-MaximumRedirection 0` raises a
        # PSInvalidOperationException ("Windows PowerShell is in NonInteractive mode")
        # instead of returning the response, so $_.Exception.Response is null and the
        # whole section reported HTTP 0 and three failures on every non-interactive
        # run. HttpWebRequest.AllowAutoRedirect never prompts and exposes both the
        # status and the Location header on 5.1 and 7.
        $loc = ''; $code = 0
        try {
            $req = [System.Net.HttpWebRequest]::Create("$Base/auth/google/login")
            $req.AllowAutoRedirect = $false
            $req.Timeout = 30000
            $resp = $req.GetResponse()
            $code = [int]$resp.StatusCode; $loc = "" + $resp.Headers['Location']
            $resp.Close()
        } catch [System.Net.WebException] {
            if ($_.Exception.Response) {
                $code = [int]$_.Exception.Response.StatusCode
                $loc  = "" + $_.Exception.Response.Headers['Location']
            }
        }
        Write-Host "  HTTP $code -> $($loc.Substring(0, [Math]::Min(110, $loc.Length)))" -ForegroundColor DarkGray
        Assert-Equal "302 to Google's consent screen" ($code -ge 300 -and $code -lt 400 -and $loc.StartsWith('https://accounts.google.com/')) $true
        Assert-Equal "carries a signed state (CSRF guard)" ($loc -match '[?&]state=') $true
        Assert-Equal "asks for the openid scope" ($loc -match 'scope=openid') $true
    } else {
        Write-Host "  (skipping 105m: set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET to test the redirect)" -ForegroundColor Yellow
    }

    Write-Section "105n" "AUTH - GOOGLE CALLBACK WITH A FORGED STATE -> 400  (CSRF guard)"
    Invoke-Api GET "/auth/google/callback?code=fake&state=forged" -Expect '4'

    Write-Section "105o" "AUTH - GOOGLE ID TOKEN THAT ISN'T ONE -> 401  (POST /auth/google/token)"
    Invoke-Api POST "/auth/google/token" '{"credential":"not.a.real.jwt"}' -Expect '4'

    Write-Section 103 "SCORING SPEC COMPLIANCE  (python test_scoring_spec.py)  <- engine matches ScoringLogic_Swapify.md"
    $specScript = Join-Path $ServerDir 'test_scoring_spec.py'
    Write-Host ">> $Py test_scoring_spec.py" -ForegroundColor DarkGray
    $specOut = & $Py $specScript 2>&1
    $specOk  = ($LASTEXITCODE -eq 0)
    ($specOut | Select-Object -Last 3) | ForEach-Object { Write-Host "   $_" }
    if ($specOk) {
        Write-Host "Scoring engine matches ScoringLogic_Swapify.md (all assertions passed)." -ForegroundColor Green
        $script:Pass++
    } else {
        Write-Host "Spec compliance FAILED." -ForegroundColor Red
        $script:Fail++
    }

    Write-Section "103b" "SCORING SPEC - Example A over HTTP  (GET /score/{Maggi})  <- noodles ingredients -> very low score"
    Write-Host "# Maggi's stored ingredients ARE spec section 6 Example A (maida, palm oil, MSG, TBHQ, ...) -> expect grade F" -ForegroundColor DarkGray
    Invoke-Api GET "/score/8901058005783"
    try { $specGrade = ($script:LastBody | ConvertFrom-Json).grade } catch { $specGrade = '?' }
    Write-Host "Example A grade = $specGrade  (spec: very low, D/F)" -ForegroundColor Blue

    # =========================================================================
    Write-Banner "REVIEWER FEEDBACK VERIFICATION  (the 'What Needs to be Fixed' checklist)"

    # Named products the reviewer reported as "Product Not Found" — resolved by
    # barcode (a scan), which is how the app actually looks them up.
    $BcRagabites   = "8908002984590"   # "Tata Soulful Ragi Bite" == Soulfull Ragabites choco
    $BcSlurrpRagi  = "8908006217465"   # "Slurrp Farm Ragi & Banana Cereal"
    $BcKulfi       = "8901262176477"   # Amul mava malai kulfi (reviewer said 50g)
    $BcChocobar    = "8901262176224"   # Amul Chocobar (reviewer said 44g)
    $BcWtCranberry = "8906123100028"   # Whole Truth cranberry bar (reviewer said 52g)
    $BcParleG      = "8901719113345"   # Parle G (complete data)
    $BcChanna      = "8906161390719"   # Let's Try roasted channa (scores 7+)

    Write-Section "104a" "FIX #1 - ALL PRODUCTS LOADED  (named 'not found' products are scannable)"
    foreach ($bc in @($BcRagabites, $BcSlurrpRagi)) {
        Invoke-Api GET "/product/$bc"
        try { $name = ($script:LastBody | ConvertFrom-Json).product_name } catch { $name = '' }
        Assert-Equal "product $bc resolves (status 200)" $script:LastCode 200
        Assert-Equal "product $bc has a name" ([bool]$name) $true
        Write-Host "  -> $name" -ForegroundColor Blue
    }

    Write-Section "104b" "FIX #2 - NUTRITION NORMALIZED TO 100g  (serving_size_g == 100 for the reported products)"
    foreach ($bc in @($BcKulfi, $BcChocobar, $BcWtCranberry)) {
        Invoke-Api GET "/product/$bc"
        try { $o = $script:LastBody | ConvertFrom-Json } catch { $o = $null }
        $serv  = if ($o) { [double]$o.serving_size_g } else { -1 }
        $basis = if ($o -and $o.nutrition_per_100g) { $o.nutrition_per_100g.basis } else { '' }
        Assert-Equal "serving_size_g==100 for $bc" ([math]::Abs($serv - 100) -lt 0.01) $true
        Assert-Equal "nutrition basis is per_100g for $bc" $basis 'per_100g'
    }

    Write-Section "104c" "FIX #4 - PER-100g SCORING + CONFIDENCE  (Parle G: DB-first, complete -> Very High)"
    Invoke-Api GET "/product/$BcParleG"
    try { $o = $script:LastBody | ConvertFrom-Json } catch { $o = $null }
    Assert-Equal "Parle G resolves from our DATABASE first (Fix #1)" $(if ($o) { $o.source } else { '' }) 'database'
    Assert-Equal "Parle G confidence == Very High"                   $(if ($o) { $o.confidence } else { '' }) 'Very High'

    Write-Section "104d" "FIX #5 - BETTER FOR YOU BADGE  (a product scores 7+ and is flagged)"
    Invoke-Api GET "/product/$BcChanna"
    try { $o = $script:LastBody | ConvertFrom-Json } catch { $o = $null }
    $chScore = if ($o) { [double]$o.score } else { 0 }
    Write-Host "  roasted channa score=$chScore  is_better_for_you=$($o.is_better_for_you)" -ForegroundColor Blue
    Assert-Equal "roasted channa is_better_for_you == True" $(if ($o) { [bool]$o.is_better_for_you } else { $false }) $true
    Assert-Equal "roasted channa score >= 7" ($chScore -ge 7) $true

    Write-Section "104e" "FIX #3 - AI CHAT IS FAST  (deterministic fast-path, expected < 5s)"
    foreach ($q in @('Hi', 'What is the score of Frooti?')) {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        Invoke-Api POST "/chat" (@{ question = $q } | ConvertTo-Json -Compress)
        $sw.Stop()
        try { $src = ($script:LastBody | ConvertFrom-Json).source } catch { $src = '?' }
        Write-Host "  chat '$q' -> $($sw.ElapsedMilliseconds)ms  source=$src" -ForegroundColor Blue
        Assert-Equal "chat '$q' status 200" $script:LastCode 200
        Assert-Equal "chat '$q' under 5s"   ($sw.ElapsedMilliseconds -lt 5000) $true
    }

    Write-Section "104f" "FIX #10 - AUTOCOMPLETE IS FAST  (single-letter query returns instantly)"
    foreach ($q in @('L', 'Li')) {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        Invoke-Api GET "/search/autocomplete?q=$q&limit=8"
        $sw.Stop()
        Write-Host "  autocomplete '$q' -> $($sw.ElapsedMilliseconds)ms" -ForegroundColor Blue
        Assert-Equal "autocomplete '$q' status 200" $script:LastCode 200
    }

    # =========================================================================
    Write-Banner "27 JULY REVIEWER FIXES  (scanner speed, OFF search/categories, autofill, chat, report-missing)"
    $bcFiveStar = "7622210622211"   # Five star chocolate (in our DB) - fast, in-DB scan
    $bcNutella  = "3017620422003"   # Nutella - NOT in our DB, resolvable via Open Food Facts

    Write-Section "127a" "FIX #1 - SCANNER IS FAST  (in-DB product resolves well under 5s)"
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    Invoke-Api GET "/product/$bcFiveStar"
    $sw.Stop()
    try { $ds = ($script:LastBody | ConvertFrom-Json).data_source } catch { $ds = '?' }
    Write-Host "  /product/$bcFiveStar -> $($sw.ElapsedMilliseconds)ms  source=$ds" -ForegroundColor Blue
    Assert-Equal "in-DB scan status 200" $script:LastCode 200
    Assert-Equal "in-DB scan under 5s"   ($sw.ElapsedMilliseconds -lt 5000) $true

    Write-Section "127b" "FIX #2/#3 - OFF PRODUCT: real name + auto-filled nutrition  (never 'Unknown Product')"
    Invoke-Api GET "/product/$bcNutella"
    try { $o = $script:LastBody | ConvertFrom-Json } catch { $o = $null }
    $nutName = if ($o) { "" + $o.product_name } else { '' }
    $nutSrc  = if ($o) { "" + $o.data_source } else { '' }
    $nutHasCal = if ($o -and $null -ne $o.calories_kcal_per_serving) { $true } else { $false }
    Write-Host "  -> name='$nutName'  data_source=$nutSrc  has_calories=$nutHasCal" -ForegroundColor Blue
    if ($script:LastCode -eq 200) {
        Assert-Equal "OFF product has a real (non-placeholder) name" (($nutName.Length -gt 0) -and ($nutName -ne 'Unknown Product')) $true
        Assert-Equal "OFF product carries auto-filled calories" $nutHasCal $true
    } else {
        Write-Host "  (Open Food Facts unreachable - skipping name/nutrition asserts)" -ForegroundColor Yellow
    }

    Write-Section "127c" "FIX #5 - AI CHAT USES THE DATABASE  (a DB product answers with its own score, not 'no data')"
    Invoke-Api POST "/chat" (@{ question = "is Kellogg's Multigrain Chocos healthy?" } | ConvertTo-Json -Compress)
    try { $o = $script:LastBody | ConvertFrom-Json } catch { $o = $null }
    $chIndb = if ($o) { [bool]$o.product_in_database } else { $false }
    $chName = if ($o) { "" + $o.product_name } else { '' }
    Write-Host "  -> product_in_database=$chIndb  product_name='$chName'  score=$(if($o){$o.score})" -ForegroundColor Blue
    Assert-Equal "chat resolved the product from the DB" $chIndb $true
    Assert-Equal "chat resolved to the 'Chocos' product (not an unrelated match)" ($chName -match '(?i)chocos') $true

    Write-Section "127d" "FIX #7 - SEARCH INCLUDES OPEN FOOD FACTS  (results carry a 'source'; global products appear)"
    # external=false is deterministic: our curated catalogue only, every result labelled "database".
    Invoke-Api GET "/search?q=Frooti&external=false&meta=true&limit=5"
    try { $r = @(($script:LastBody | ConvertFrom-Json).results) } catch { $r = @() }
    $src0 = if ($r.Count -gt 0) { "" + $r[0].source } else { '' }
    Assert-Equal "curated-only results are labelled source=database" $src0 "database"
    # A product NOT in our DB ("Mother Dairy") should still appear, sourced from Open Food Facts.
    Invoke-Api GET "/search?q=Mother%20Dairy&meta=true&limit=10"
    try { $m = $script:LastBody | ConvertFrom-Json } catch { $m = $null }
    $mdTotal = if ($m) { $m.total } else { 0 }
    $mdOff = if ($m) { @($m.results | Where-Object { $_.source -eq 'openfoodfacts' }).Count } else { 0 }
    Write-Host "  'Mother Dairy' -> total=$mdTotal  (openfoodfacts results: $mdOff)" -ForegroundColor Blue
    Assert-Equal "'Mother Dairy' search status 200" $script:LastCode 200
    if ($mdOff -gt 0) {
        Assert-Equal "'Mother Dairy' returns Open Food Facts products" $true $true
    } else {
        Write-Host "  (no OFF results - network/OFF may be unavailable; not failing the run)" -ForegroundColor Yellow
    }

    Write-Section "127e" "FIX #6 - CATEGORIES INCLUDE OPEN FOOD FACTS  (external_count reported)"
    Invoke-Api GET "/products/by-category/chocolate?limit=80"
    try { $o = $script:LastBody | ConvertFrom-Json } catch { $o = $null }
    $catTotal = if ($o) { $o.total } else { 0 }
    $hasExt = if ($o -and ($o.PSObject.Properties.Name -contains 'external_count')) { $true } else { $false }
    $catExt = if ($hasExt) { $o.external_count } else { 0 }
    Write-Host "  chocolate -> total=$catTotal  external_count=$catExt" -ForegroundColor Blue
    Assert-Equal "category endpoint status 200" $script:LastCode 200
    Assert-Equal "category response reports external_count" $hasExt $true

    Write-Section "127e2" "JULY 28 - CATEGORY BROWSING GOES DEEP INTO OFF  (was capped at the 20-hit search default)"
    Write-Host "  a category page should offer hundreds of products, not our rows + 20" -ForegroundColor Blue
    if ([int]$catExt -gt 20) {
        Assert-Equal "chocolate merges more than the old 20-product cap" $true $true
    } else {
        Write-Host "  (external_count=$catExt - OFF unavailable or SWAPIFY_CATEGORY_EXTERNAL_LIMIT lowered; not failing)" -ForegroundColor Yellow
    }
    # Deep pagination has to reach past our curated rows into the OFF half.
    Invoke-Api GET "/products/by-category/chocolate?limit=5&offset=100"
    try { $o = $script:LastBody | ConvertFrom-Json } catch { $o = $null }
    $deepCount = if ($o) { [int]$o.count } else { 0 }
    $deepSrc = if ($o) { @($o.products | Where-Object { $_.source -eq 'openfoodfacts' }).Count -gt 0 } else { $false }
    Write-Host "  offset=100 -> count=$deepCount  (openfoodfacts rows present: $deepSrc)" -ForegroundColor Blue
    Assert-Equal "deep offset still returns a page" $script:LastCode 200
    if ([int]$catExt -gt 20) {
        Assert-Equal "page past our curated rows is served from OFF" $deepSrc $true
    }

    Write-Section "127e3" "JULY 28 - CURATED-ONLY CATEGORY PAGE  (?external=false)  <- our rows, no OFF"
    Invoke-Api GET "/products/by-category/chocolate?limit=80&external=false"
    try { $o = $script:LastBody | ConvertFrom-Json } catch { $o = $null }
    $curTotal = if ($o) { [int]$o.total } else { 0 }
    $curExt   = if ($o) { [int]$o.external_count } else { -1 }
    $curProducts = if ($o) { @($o.products) } else { @() }
    $onlyDb = ($curProducts.Count -gt 0) -and
              (@($curProducts | Where-Object { $_.source -and $_.source -ne 'database' }).Count -eq 0)
    Write-Host "  external=false -> total=$curTotal external_count=$curExt" -ForegroundColor Blue
    Assert-Equal "external=false merges no OFF products" $curExt 0
    Assert-Equal "external=false returns only our own rows" $onlyDb $true

    Write-Section "127f" "FIX #8 - REPORT MISSING PRODUCT WORKS ANONYMOUSLY  (no auth required)"
    Invoke-Api POST "/report-missing" (@{ barcode = "0000000000027"; product_name = "Anon Test" } | ConvertTo-Json -Compress)
    try { $o = $script:LastBody | ConvertFrom-Json } catch { $o = $null }
    $rmStatus = if ($o) { "" + $o.status } else { '' }
    $rmAuth   = if ($o) { [bool]$o.authenticated } else { $true }
    Assert-Equal "anonymous report accepted (status 200)" $script:LastCode 200
    Assert-Equal "report status == reported" $rmStatus "reported"
    Assert-Equal "report flagged as unauthenticated" $rmAuth $false

    Write-Host "  FIX #9 (sodium vs salt): a frontend-only change - bare 'Salt' is no longer flagged" -ForegroundColor DarkGray
    Write-Host "  when the sodium panel reads 0mg (static/script.js calculateScore). Not an API check." -ForegroundColor DarkGray

    # =========================================================================
    Write-Banner "2 AUGUST REVIEWER FIXES  (auto-fill coverage, safety-net honesty, cross-brand guard)"

    Write-Section "128a" "AUTO-FILL SOURCE HEALTH  (GET /autofill/status)  <- why a product came back empty"
    Write-Host "  A product the chain cannot resolve returns a plain 404, identical whether the product" -ForegroundColor DarkGray
    Write-Host "  genuinely has no published data or every search provider is refusing us. This endpoint" -ForegroundColor DarkGray
    Write-Host "  is what tells those two apart, so it must always answer." -ForegroundColor DarkGray
    Invoke-Api GET "/autofill/status"
    try { $af = $script:LastBody | ConvertFrom-Json } catch { $af = $null }
    $afOff = if ($af) { [bool]$af.sources.openfoodfacts.available } else { $false }
    $afDep = if ($af) { $af.has_dependable_search } else { $null }
    Write-Host "  openfoodfacts.available=$afOff   has_dependable_search=$afDep" -ForegroundColor Blue
    Assert-Equal "auto-fill status reports Open Food Facts as a source" $afOff $true
    # has_dependable_search is False until GOOGLE_API_KEY + GOOGLE_CSE_ID are set; the
    # assertion is that the field is present and boolean, not which way it points.
    Assert-Equal "has_dependable_search is reported as a boolean" ($afDep -is [bool]) $true
    if ($afDep -eq $false) {
        $afWarn = if ($af -and $af.warning) { ("" + $af.warning).Trim().Length -gt 0 } else { $false }
        Assert-Equal "no keyed provider -> a warning explains the consequence" $afWarn $true
    }

    Write-Section "128b" "PROVIDER PROBE IS ADMIN-GATED -> 403  (GET /autofill/status?probe=true)"
    Write-Host "  The probe opens real pages and exposes provider error text, so it needs the shared secret." -ForegroundColor DarkGray
    Invoke-Api GET "/autofill/status?probe=true" -Expect '4'

    Write-Section "128c" "PROVIDER PROBE MEASURES USEFULNESS, NOT HITS  (?probe=true with X-Admin-Token)"
    Write-Host "  A provider returning five results is not a working safety net: Bing's keyless RSS answers" -ForegroundColor DarkGray
    Write-Host "  every packaged-product query with the brand's HOME PAGE, which has no nutrition panel on" -ForegroundColor DarkGray
    Write-Host "  it, and it ignores 'site:' filters entirely. So the probe opens the top hits and tries to" -ForegroundColor DarkGray
    Write-Host "  extract nutrition - 'ok' must mean that succeeded, never merely that results came back." -ForegroundColor DarkGray
    $adminToken2 = if ($env:ADMIN_TOKEN) { $env:ADMIN_TOKEN } else { 'swapify-admin-dev' }
    Invoke-Admin GET "/autofill/status?probe=true" -AdminToken $adminToken2
    try { $pr = @(($script:LastBody | ConvertFrom-Json).probe) } catch { $pr = @() }
    $live = @($pr | Where-Object { -not $_.PSObject.Properties['skipped'] -and -not $_.PSObject.Properties['error'] })
    $fieldOk = ($live.Count -gt 0) -and (@($live | Where-Object { -not $_.PSObject.Properties['nutrition_parsed'] }).Count -eq 0)
    # ok must never be true while nothing could be parsed - that is the exact way the
    # old count-based probe reported a dead provider as healthy.
    $okHonest = (@($live | Where-Object { [bool]$_.ok -ne [bool]$_.nutrition_parsed }).Count -eq 0)
    Write-Host "  probed $($pr.Count) providers ($($live.Count) live)" -ForegroundColor Blue
    Assert-Equal "probe reports every live provider" ($pr.Count -gt 0) $true
    Assert-Equal "each probed provider reports nutrition_parsed" $fieldOk $true
    Assert-Equal "'ok' means nutrition was extracted, not just that hits came back" $okHonest $true

    Write-Section "128d" "CROSS-BRAND GUARD - a branded query is never answered by a generic product"
    Write-Host "  Open Food Facts holds Kapiva's Amla Juice (8901207034145) with an EMPTY nutriments object," -ForegroundColor DarkGray
    Write-Host "  and its search returns unrelated amla juices that do carry panels. 'juice' is a stopword," -ForegroundColor DarkGray
    Write-Host "  so a generic 'Amla Juice' shared exactly half of the query's identifying words and cleared" -ForegroundColor DarkGray
    Write-Host "  the old '>= 0.5' relevance bar - filling Kapiva with another product's numbers under a" -ForegroundColor DarkGray
    Write-Host "  confident 'openfoodfacts' label. A strict majority is now required. Empty is the correct" -ForegroundColor DarkGray
    Write-Host "  answer here; wrong data would be worse than none." -ForegroundColor DarkGray
    # 404 is a legitimate PASS here - "no source publishes this" is the honest answer.
    Invoke-Api GET "/product/by-name/Kapiva%20Amla%20Juice" -Expect '[24]'
    try { $kp = $script:LastBody | ConvertFrom-Json } catch { $kp = $null }
    if (-not $kp -or $kp.error) {
        $kapClean = $true                     # 404 = honest "no data", which is fine
    } else {
        # Judge the BRAND, not the name: the by-name resolver echoes the query back as
        # the product_name, so 'Kapiva' is present even when the panel came from someone
        # else. The production symptom was exactly that - product_name 'Kapiva Amla
        # Juice' with brand 'Dabur' - so a name check would pass the very bug it must catch.
        $kapBrand = ("" + $kp.brand).Trim().ToLower()
        $kapClean = ([string]::IsNullOrWhiteSpace($kapBrand) -or 'kapiva amla juice'.Contains($kapBrand))
    }
    Write-Host "  HTTP $($script:LastCode) -> not-another-brand: $kapClean" -ForegroundColor Blue
    Assert-Equal "Kapiva query is answered by Kapiva or by nothing" $kapClean $true

    Write-Section "128e" "OFF EMPTY-ROW FALLBACK - a nutrition-less barcode row no longer ends the lookup"
    Write-Host "  OFF lists many Indian packs as name+image with no nutriments, and the SAME pack again under" -ForegroundColor DarkGray
    Write-Host "  a neighbouring GTIN with the full panel (Amul Butter: ...0153 is empty, ...0030 is complete)." -ForegroundColor DarkGray
    Write-Host "  The old code returned the empty row and stopped, so the source 'succeeded' with no data." -ForegroundColor DarkGray
    Invoke-Api GET "/product/by-name/Amul%20Butter"
    try { $ab = $script:LastBody | ConvertFrom-Json } catch { $ab = $null }
    $abCal = if ($ab -and $null -ne $ab.calories_kcal_per_serving) { $true } else { $false }
    Write-Host "  HTTP $($script:LastCode)  has_calories=$abCal" -ForegroundColor Blue
    if ($script:LastCode -eq 200) {
        Assert-Equal "a widely-stocked pack resolves with real calories" $abCal $true
    } else {
        Write-Host "  (Open Food Facts unreachable - skipping the auto-fill assert)" -ForegroundColor Yellow
    }

    # =========================================================================
    Write-Banner "DATABASE VERIFICATION  (proving the writes actually persisted)"

    Write-Host ""
    Write-Host "users  (baseline had $baseUsers rows)" -ForegroundColor White
    Invoke-DbQuery "SELECT id, username, email, created_at FROM users WHERE email='$Email'"
    $nowUsers = Get-Count 'users'
    Write-Host "   users: $baseUsers -> $nowUsers" -ForegroundColor Green

    Write-Host ""
    Write-Host "user_preferences  (from steps 9 & 11)" -ForegroundColor White
    Invoke-DbQuery "SELECT user_id, preferences FROM user_preferences WHERE user_id=$($script:UserId)"

    Write-Host ""
    Write-Host "scan_history  (from step 4)" -ForegroundColor White
    Invoke-DbQuery "SELECT id, barcode, scanned_at FROM scan_history WHERE user_id=$($script:UserId) ORDER BY id DESC LIMIT 5"
    $nowScans = Get-Count 'scan_history'
    Write-Host "   scan_history total: $baseScans -> $nowScans" -ForegroundColor Green

    Write-Host ""
    Write-Host "favorites  (added in 13, deleted in 15 -> expect none for this user)" -ForegroundColor White
    Invoke-DbQuery "SELECT user_id, barcode, added_at FROM favorites WHERE user_id=$($script:UserId)"

    Write-Host ""
    Write-Host "missing_reports  (from step 24)" -ForegroundColor White
    Invoke-DbQuery "SELECT id, barcode, product_name, user_comment FROM missing_reports ORDER BY id DESC LIMIT 3"
    $nowReports = Get-Count 'missing_reports'
    Write-Host "   missing_reports total: $baseReports -> $nowReports" -ForegroundColor Green

    Write-Host ""
    Write-Host "product_ratings  (from steps 28-30; the re-rating in 30 UPDATED, didn't stack)" -ForegroundColor White
    Invoke-DbQuery "SELECT id, barcode, taste_rating, quality_rating, value_rating, rated_at FROM product_ratings WHERE user_id=$($script:UserId) ORDER BY id DESC LIMIT 5"

    Write-Host ""
    Write-Host "comparison_history  (from step 21; feeds the /recommendations engine)" -ForegroundColor White
    Invoke-DbQuery "SELECT id, barcode, compared_at FROM comparison_history WHERE user_id=$($script:UserId) ORDER BY id DESC LIMIT 5"

    Write-Host ""
    Write-Host "user_activity  (auto-logged scans/compare/rate/favorite/share + POST /activity from steps 43-47)" -ForegroundColor White
    Invoke-DbQuery "SELECT id, action_type, barcode, created_at FROM user_activity WHERE user_id=$($script:UserId) ORDER BY id DESC LIMIT 8"

    Write-Host ""
    Write-Host "challenge_participants  (from steps 49-52; the joins this user made)" -ForegroundColor White
    Invoke-DbQuery "SELECT challenge_id, user_id, joined_at, completed_at FROM challenge_participants WHERE user_id=$($script:UserId) ORDER BY challenge_id"

    Write-Host ""
    Write-Host "shopping_lists + items  (from steps 60-63; item swapped in step 63)" -ForegroundColor White
    Invoke-DbQuery "SELECT id, name, user_id, created_at FROM shopping_lists WHERE user_id=$($script:UserId) ORDER BY id DESC LIMIT 3"
    Invoke-DbQuery "SELECT list_id, barcode FROM shopping_list_items WHERE list_id=$($script:ListId) ORDER BY id"

    Write-Host ""
    Write-Host "reviews + votes + replies  (from steps 66-70; review $($script:ReviewId) kept, votes/replies attached)" -ForegroundColor White
    Invoke-DbQuery "SELECT id, barcode, rating, review_text, created_at FROM reviews WHERE user_id=$($script:UserId) ORDER BY id DESC LIMIT 3"
    Invoke-DbQuery "SELECT review_id, user_id, vote FROM review_votes WHERE review_id=$($script:ReviewId)"
    Invoke-DbQuery "SELECT review_id, user_id, reply_text FROM review_replies WHERE review_id=$($script:ReviewId)"

    Write-Host ""
    Write-Host "product_images  (Task 2C; from the image upload in step 90 -> reference stored, products.image_url updated)" -ForegroundColor White
    Invoke-DbQuery "SELECT id, barcode, image_url, content_type, file_size FROM product_images ORDER BY id DESC LIMIT 3"
    Invoke-DbQuery "SELECT barcode, image_url FROM products WHERE barcode='$BcUnhealthy'"

    Write-Host ""
    Write-Host "product indexes  (Task 1A; created at startup)" -ForegroundColor White
    Invoke-DbQuery "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='products' AND name LIKE 'idx_%' ORDER BY name"

    Write-Host ""
    Write-Host "password_reset_tokens  (from steps 105d-105i; note token_hash - the raw token is never stored)" -ForegroundColor White
    Invoke-DbQuery "SELECT id, user_id, substr(token_hash,1,16) AS token_hash_prefix, expires_at, used_at FROM password_reset_tokens ORDER BY id DESC LIMIT 5"

    Write-Host ""
    Write-Host "users - Google sign-in columns  (google_id stays NULL until an account signs in with Google)" -ForegroundColor White
    Invoke-DbQuery "SELECT id, username, auth_provider, google_id FROM users ORDER BY id DESC LIMIT 5"

    # =========================================================================
    Write-Banner "SUMMARY"
    $total = $script:Pass + $script:Fail
    Write-Host "  Passed: $($script:Pass)   Failed: $($script:Fail)   Total requests: $total   (pass = status matched the expected code)"
    Write-Host "  AI /chat source: $chatSource"
    if ($chatSource -eq 'openrouter') {
        Write-Host "  OpenRouter API key is working - real AI answers." -ForegroundColor Green
    } else {
        Write-Host "  /chat used the rule-based fallback (key missing, model offline, or rate-limited)." -ForegroundColor Yellow
        Write-Host "  Check '$ErrLog' or the fallback_reason field above." -ForegroundColor Yellow
    }
    Write-Host ""
    Write-Host "Done." -ForegroundColor White
}
finally {
    if ($script:ServerProc) {
        Write-Host ""
        Write-Host "Stopping test server (pid $($script:ServerProc.Id))..." -ForegroundColor DarkGray
        Stop-Process -Id $script:ServerProc.Id -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path $script:DbqScript) { Remove-Item $script:DbqScript -Force -ErrorAction SilentlyContinue }
    if (Test-Path $script:ImgDir)    { Remove-Item $script:ImgDir -Recurse -Force -ErrorAction SilentlyContinue }
}
