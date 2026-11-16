[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$namespace = "house-finder"
$secretName = "house-finder-secrets"
$deploymentName = "house-finder"

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw "No se encontro kubectl. Instala y configura kubectl antes de continuar."
}

$context = & kubectl config current-context
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($context | Out-String))) {
    throw "No se pudo determinar el contexto actual de kubectl."
}
$context = ($context | Out-String).Trim()

& kubectl -n $namespace get secret $secretName -o name | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "No se encontro el Secret $secretName en el namespace $namespace."
}

& kubectl -n $namespace get deployment $deploymentName -o name | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "No se encontro el Deployment $deploymentName en el namespace $namespace."
}

Write-Host "Contexto: $context"
Write-Host "Se actualizara unicamente DEEPSEEK_API_KEY en $namespace/$secretName."
$confirmation = Read-Host "Continuar? [y/N]"
if ($confirmation -notin @("y", "Y", "yes", "YES", "s", "S", "si", "SI")) {
    Write-Host "Operacion cancelada; no se modifico ningun recurso."
    return
}

$secureKey = Read-Host "Nueva clave API de DeepSeek" -AsSecureString
if ($secureKey.Length -eq 0) {
    throw "La clave API no puede estar vacia."
}

$bstr = [IntPtr]::Zero
$keyBytes = $null
$keyText = $null
$patchFile = $null
try {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    $keyText = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    $keyBytes = [Text.Encoding]::UTF8.GetBytes($keyText)
    $patch = @{
        data = @{
            DEEPSEEK_API_KEY = [Convert]::ToBase64String($keyBytes)
        }
    } | ConvertTo-Json -Compress -Depth 3

    $patchFile = Join-Path ([IO.Path]::GetTempPath()) ("house-finder-deepseek-" + [guid]::NewGuid().ToString("N") + ".json")
    [IO.File]::WriteAllText($patchFile, $patch, (New-Object Text.UTF8Encoding($false)))

    & kubectl -n $namespace patch secret $secretName --type=merge "--patch-file=$patchFile"
    if ($LASTEXITCODE -ne 0) {
        throw "kubectl no pudo actualizar DEEPSEEK_API_KEY."
    }

    & kubectl -n $namespace rollout restart "deployment/$deploymentName"
    if ($LASTEXITCODE -ne 0) {
        throw "La clave se actualizo, pero no se pudo reiniciar el Deployment."
    }

    & kubectl -n $namespace rollout status "deployment/$deploymentName" --timeout=180s
    if ($LASTEXITCODE -ne 0) {
        throw "La clave se actualizo, pero el rollout del Deployment no finalizo correctamente."
    }

    Write-Host "DEEPSEEK_API_KEY se actualizo y el Deployment esta listo."
}
finally {
    if ($patchFile -and (Test-Path -LiteralPath $patchFile)) {
        Remove-Item -LiteralPath $patchFile -Force
    }
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    if ($null -ne $keyBytes) {
        [Array]::Clear($keyBytes, 0, $keyBytes.Length)
    }
    $keyText = $null
    if ($null -ne $secureKey) {
        $secureKey.Dispose()
    }
}
