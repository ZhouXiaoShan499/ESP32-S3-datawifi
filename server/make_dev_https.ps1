param(
    [string]$LanIp = "",
    [string]$CertDir = (Join-Path $PSScriptRoot "certs")
)

$ErrorActionPreference = "Stop"
$openssl = (Get-Command openssl -ErrorAction SilentlyContinue).Source
if (-not $openssl) {
    throw "OpenSSL was not found. Install OpenSSL or use a trusted reverse-proxy certificate."
}

if (-not $LanIp) {
    $LanIp = Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object {
            $_.IPAddress -notlike "127.*" -and
            $_.IPAddress -notlike "169.254.*" -and
            $_.PrefixOrigin -ne "WellKnown"
        } |
        Sort-Object InterfaceMetric |
        Select-Object -First 1 -ExpandProperty IPAddress
}
if (-not $LanIp) {
    throw "No LAN IPv4 address was detected. Pass one with -LanIp."
}

New-Item -ItemType Directory -Force -Path $CertDir | Out-Null
$certFile = Join-Path $CertDir "dev-cert.pem"
$keyFile = Join-Path $CertDir "dev-key.pem"
$caCertFile = Join-Path $CertDir "dev-ca-cert.pem"
$caKeyFile = Join-Path $CertDir "dev-ca-key.pem"
$csrFile = Join-Path $CertDir "dev-server.csr"
$leafFile = Join-Path $CertDir "dev-server-cert.pem"
$caConfigFile = Join-Path $CertDir "openssl-ca.cnf"
$serverConfigFile = Join-Path $CertDir "openssl-server.cnf"
$san = "DNS:localhost,IP:127.0.0.1,IP:$LanIp"
@"
[req]
distinguished_name = subject
prompt = no

[subject]
CN = ESP32-S3 Local Development CA
O = ESP32-S3 Local Development

[v3_ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
"@ | Set-Content -LiteralPath $caConfigFile -Encoding Ascii
@"
[req]
distinguished_name = subject
prompt = no

[subject]
CN = $LanIp
O = ESP32-S3 Local Development

[v3_server]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
authorityKeyIdentifier = keyid,issuer
subjectAltName = $san
"@ | Set-Content -LiteralPath $serverConfigFile -Encoding Ascii

try {
    & $openssl req -x509 -nodes -newkey rsa:3072 -sha256 -days 3650 `
        -keyout $caKeyFile -out $caCertFile -config $caConfigFile `
        -extensions v3_ca
    if ($LASTEXITCODE -ne 0) {
        throw "OpenSSL failed to create the local CA (exit $LASTEXITCODE)."
    }
    & $openssl req -new -nodes -newkey rsa:2048 -sha256 `
        -keyout $keyFile -out $csrFile -config $serverConfigFile
    if ($LASTEXITCODE -ne 0) {
        throw "OpenSSL failed to create the server CSR (exit $LASTEXITCODE)."
    }
    & $openssl x509 -req -in $csrFile -CA $caCertFile -CAkey $caKeyFile `
        -CAcreateserial -out $leafFile -days 825 -sha256 `
        -extfile $serverConfigFile -extensions v3_server
    if ($LASTEXITCODE -ne 0) {
        throw "OpenSSL failed to sign the server certificate (exit $LASTEXITCODE)."
    }
    # Browsers need the server certificate followed by the local CA chain.
    @((Get-Content -Raw -LiteralPath $leafFile),
      (Get-Content -Raw -LiteralPath $caCertFile)) |
        Set-Content -LiteralPath $certFile -Encoding Ascii
} finally {
    Remove-Item -LiteralPath $caConfigFile,$serverConfigFile,$csrFile `
        -Force -ErrorAction SilentlyContinue
}

Write-Host "Development certificates created:"
Write-Host "  Server chain: $certFile"
Write-Host "  Server key:   $keyFile"
Write-Host "  Local CA:     $caCertFile"
Write-Host "Open: https://${LanIp}:8443/ui/"
Write-Host "Trust dev-ca-cert.pem on controlled client devices before first use."
