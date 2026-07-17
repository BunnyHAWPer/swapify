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
$Db        = Join-Path $ServerDir 'swapify.db'
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
    # deliberately expect a client error pass -Expect '4'.
    if (("$code").StartsWith($Expect)) { $script:Pass++ } else { $script:Fail++ }
    $script:LastBody = $content
    if ($Delay -gt 0) { Start-Sleep -Seconds $Delay }
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
