#requires -Version 5.1

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$powerShellPath = 'UNKNOWN'
try {
    $powerShellPath = (Get-Process -Id $PID -ErrorAction Stop).Path
} catch {
    # Diagnostics must not block the probe.
}
Write-Output "POWERSHELL_PATH=$powerShellPath"
Write-Output "POWERSHELL_VERSION=$($PSVersionTable.PSVersion)"
Write-Output "POWERSHELL_EDITION=$($PSVersionTable.PSEdition)"

if ($null -eq ('SmartAssistant.Acceptance.A05.CertificateValidationCallbackFactory' -as [type])) {
    Add-Type -TypeDefinition @'
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;

namespace SmartAssistant.Acceptance.A05
{
    public static class CertificateValidationCallbackFactory
    {
        private static bool Accept(
            object sender,
            X509Certificate certificate,
            X509Chain chain,
            SslPolicyErrors sslPolicyErrors)
        {
            return true;
        }

        public static RemoteCertificateValidationCallback Create()
        {
            return new RemoteCertificateValidationCallback(Accept);
        }
    }
}
'@ | Out-Null
}

try {
    $processes = @(Get-CimInstance -ClassName Win32_Process)
    $electronProcesses = @(
        $processes |
            Where-Object { $_.Name -ieq 'SmartAssistant.exe' }
    )
    $electronIds = @($electronProcesses | ForEach-Object { [uint32]$_.ProcessId })
    $backendProcesses = @(
        $processes |
            Where-Object {
                $_.Name -ieq 'smart-assistant-backend.exe' -and
                $electronIds -contains [uint32]$_.ParentProcessId
            }
    )
    $backend = $backendProcesses | Sort-Object ProcessId | Select-Object -First 1
    if ($null -eq $backend) {
        throw 'BACKEND_PROCESS_NOT_FOUND'
    }

    $listener = @(
        Get-NetTCPConnection -State Listen -OwningProcess $backend.ProcessId -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -in @('127.0.0.1', '::1') } |
            Sort-Object LocalPort
    ) | Select-Object -First 1
    if ($null -eq $listener) {
        throw 'BACKEND_LISTENER_NOT_FOUND'
    }

    $listenerHost = if ($listener.LocalAddress -eq '::1') { '[::1]' } else { '127.0.0.1' }
    $uri = "https://${listenerHost}:$($listener.LocalPort)/api/version"
    Write-Output "REQUEST_URL=$uri"
    $previousValidationCallback = [Net.ServicePointManager]::ServerCertificateValidationCallback
    $response = $null
    try {
        [Net.ServicePointManager]::ServerCertificateValidationCallback =
            [SmartAssistant.Acceptance.A05.CertificateValidationCallbackFactory]::Create()
        $request = [Net.HttpWebRequest]::Create($uri)
        $request.Method = 'GET'
        $request.AllowAutoRedirect = $false
        $request.Timeout = 10000
        try {
            $requestProxy = $request.Proxy
            Write-Output "PROXY_IS_NULL=$($null -eq $requestProxy)"
            if ($null -ne $requestProxy) {
                Write-Output "PROXY_BYPASSED=$($requestProxy.IsBypassed($uri))"
                $proxyUri = $requestProxy.GetProxy($uri)
                if ($null -eq $proxyUri) {
                    Write-Output 'PROXY_GET_PROXY=NULL'
                } else {
                    $safeProxyUri = [UriBuilder]::new($proxyUri)
                    $safeProxyUri.UserName = ''
                    $safeProxyUri.Password = ''
                    $safeProxyUri.Query = ''
                    $safeProxyUri.Fragment = ''
                    Write-Output "PROXY_GET_PROXY=$($safeProxyUri.Uri.AbsoluteUri)"
                }
            }
        } catch {
            Write-Output "PROXY_DIAGNOSTIC_ERROR=$($_.Exception.GetType().FullName)"
        }
        try {
            $response = $request.GetResponse()
        } catch [Net.WebException] {
            if ($null -eq $_.Exception.Response) {
                throw
            }
            $response = $_.Exception.Response
        }

        $statusCode = [int]$response.StatusCode
        $stream = $response.GetResponseStream()
        $reader = [IO.StreamReader]::new($stream)
        try {
            $body = $reader.ReadToEnd()
        } finally {
            $reader.Dispose()
            $stream.Dispose()
        }
    } finally {
        if ($null -ne $response) {
            $response.Dispose()
        }
        [Net.ServicePointManager]::ServerCertificateValidationCallback = $previousValidationCallback
    }

    if ($statusCode -ne 401 -or $body.Length -ne 0) {
        Write-Output 'PERIMETER_DEFENSE=FAIL'
        Write-Output "HTTP_STATUS=$statusCode"
        Write-Output "BODY_LENGTH=$($body.Length)"
        exit 1
    }

    Write-Output 'PERIMETER_DEFENSE=PASS'
    Write-Output 'HTTP_STATUS=401'
    exit 0
} catch {
    $exception = $_.Exception
    $depth = 0
    while ($null -ne $exception) {
        Write-Output "EXCEPTION[$depth].TYPE=$($exception.GetType().FullName)"
        Write-Output "EXCEPTION[$depth].MESSAGE=$($exception.Message)"
        Write-Output "EXCEPTION[$depth].HRESULT=$($exception.HResult)"
        $exception = $exception.InnerException
        $depth++
    }
    Write-Error $_.Exception.Message
    exit 1
}
