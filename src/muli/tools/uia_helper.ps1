# Fixed MTA bridge. Only captured ordinary Buttons may be invoked, once.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$PSModuleAutoLoadingPreference = 'None'
Set-StrictMode -Version Latest

$script:traceMask = 0
$script:traceProbeSeen = $false
# Preserve historical stage indices; probe no longer initializes the tree walker.
$script:traceStages = @(
    'muli-uia-stage=1:script_entered',
    'muli-uia-stage=1:utility_importing',
    'muli-uia-stage=1:utility_ready',
    'muli-uia-stage=1:probe_received',
    'muli-uia-stage=1:uia_initializing',
    'muli-uia-stage=1:uia_ready',
    'muli-uia-stage=1:probe_encoded',
    'muli-uia-stage=1:probe_write_returned',
    'muli-uia-stage=1:input_waiting',
    'muli-uia-stage=1:input_line_received',
    'muli-uia-stage=1:request_decoded',
    'muli-uia-stage=1:walker_initializing',
    'muli-uia-stage=1:walker_ready',
    'muli-uia-stage=1:probe_process_ready',
    'muli-uia-stage=1:uia_host_validated',
    'muli-uia-stage=1:uia_add_type_returned',
    'muli-uia-stage=1:watch_starting',
    'muli-uia-stage=1:watch_ready'
)

function Write-Stage([int]$index) {
    try {
        if ($index -lt 0 -or $index -ge $script:traceStages.Length) { return }
        $bit = 1 -shl $index
        if (($script:traceMask -band $bit) -ne 0) { return }
        $script:traceMask = $script:traceMask -bor $bit
        [Console]::Error.WriteLine($script:traceStages[$index])
        [Console]::Error.Flush()
    } catch { }
}
Write-Stage 0

$encoding = [System.Text.UTF8Encoding]::new($false, $true)
[Console]::InputEncoding = $encoding
$writer = [System.IO.StreamWriter]::new([Console]::OpenStandardOutput(), $encoding)
$writer.NewLine = [string][char]10
$writer.AutoFlush = $true
$script:ready = $false
$script:treeLoaded = $false
$script:references = @{}
$script:selected = $null
$reasonOrder = @('node_budget', 'depth_limit', 'children_limit', 'provider_error', 'output_budget')

function Stop-Bridge([string]$code) {
    $failure = [System.InvalidOperationException]::new('UIA bridge failed')
    $failure.Data['muli_code'] = $code
    throw $failure
}

function Initialize-Uia {
    if ($script:ready) { return }
    Write-Stage 4
    if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) {
        Stop-Bridge 'dependency_missing'
    }
    if ([Threading.Thread]::CurrentThread.GetApartmentState().ToString() -ne 'MTA') {
        Stop-Bridge 'dependency_missing'
    }
    Write-Stage 14
    try {
        Microsoft.PowerShell.Utility\Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
        Write-Stage 15
        if (-not [System.Windows.Automation.AutomationElement].Assembly.GlobalAssemblyCache -or
            -not [System.Windows.Automation.ControlType].Assembly.GlobalAssemblyCache) {
            Stop-Bridge 'dependency_missing'
        }
    } catch {
        Stop-Bridge 'dependency_missing'
    }
    $script:ready = $true
    Write-Stage 5
}

function Check-Deadline {
    if ($script:watch.Elapsed.TotalSeconds -ge $script:seconds) {
        Stop-Bridge 'read_timeout'
    }
}

function Read-BoundedLine {
    $buffer = [Text.StringBuilder]::new()
    while ($true) {
        $character = [Console]::In.Read()
        if ($character -eq -1) {
            if ($buffer.Length -eq 0) { return $null }
            Stop-Bridge 'protocol_error'
        }
        if ($character -eq 10) { return $buffer.ToString() }
        if ($buffer.Length -ge 65536) { Stop-Bridge 'protocol_error' }
        [void]$buffer.Append([char]$character)
    }
}

function Encode-Result([string]$requestId, [object]$result) {
    return Microsoft.PowerShell.Utility\ConvertTo-Json -InputObject @{
        v = 1; request_id = $requestId; result = $result
    } -Depth 8 -Compress
}

try {
    # Explicit system module only: never search user PSModulePath or enable autoload.
    $utilityPath = [IO.Path]::Combine($PSHOME, 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1')
    Write-Stage 1
    Microsoft.PowerShell.Core\Import-Module -Name $utilityPath -Cmdlet Add-Type, ConvertFrom-Json, ConvertTo-Json
    $utility = Microsoft.PowerShell.Core\Get-Module -Name Microsoft.PowerShell.Utility
    if ($null -eq $utility -or $utility.Path -ne $utilityPath) { exit 1 }
    Write-Stage 2
    for ($requestCount = 0; $requestCount -lt 16; $requestCount++) {
        if ($requestCount -eq 0) { Write-Stage 8 }
        $line = Read-BoundedLine
        if ($null -eq $line) { break }
        if ($requestCount -eq 0) { Write-Stage 9 }
        $request = Microsoft.PowerShell.Utility\ConvertFrom-Json -InputObject $line
        if ($requestCount -eq 0) { Write-Stage 10 }
        if ($request.v -ne 1 -or $request.request_id -cnotmatch '^[a-f0-9]{32}$') {
            Stop-Bridge 'protocol_error'
        }
        $requestId = [string]$request.request_id
        $script:nativeEntered = $false
        $script:selected = $null
        $traceProbe = $false
        $probeSucceeded = $false
        try {
            $script:seconds = [double]$request.limits.timeout_seconds
            $script:maxNodes = [int]$request.limits.max_nodes
            $script:maxChars = [int]$request.limits.max_response_chars
            if ([double]::IsNaN($script:seconds) -or [double]::IsInfinity($script:seconds) -or
                $script:seconds -le 0 -or $script:seconds -gt 120 -or
                $script:maxNodes -lt 1 -or $script:maxNodes -gt 1000 -or
                $script:maxChars -lt 512 -or $script:maxChars -gt 60000) {
                Stop-Bridge 'protocol_error'
            }
            if ([string]$request.command -ceq 'probe') {
                if (-not $script:traceProbeSeen) {
                    $script:traceProbeSeen = $true
                    $traceProbe = $true
                    Write-Stage 3
                }
            }
            if ($traceProbe) { Write-Stage 16 }
            $script:watch = [Diagnostics.Stopwatch]::StartNew()
            if ($traceProbe) { Write-Stage 17 }
            Initialize-Uia
            Check-Deadline
            if ([string]$request.command -cne 'probe') {
                if (-not $script:treeLoaded) {
                    # Dot-source here so later requests share this script's functions.
                    . ([System.IO.Path]::Combine($PSScriptRoot, 'uia_tree.ps1'))
                    $script:treeLoaded = $true
                    Check-Deadline
                }
                $script:walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
                $script:rows = [Collections.Generic.List[object]]::new()
                $script:reasons = [Collections.Generic.HashSet[string]]::new()
                $script:visited = 0
            }
            switch -CaseSensitive ([string]$request.command) {
                'probe' {
                    $process = [Diagnostics.Process]::GetCurrentProcess()
                    try { $hasWindow = ($process.MainWindowHandle -ne [IntPtr]::Zero) }
                    finally { $process.Dispose() }
                    if ($traceProbe) { Write-Stage 13 }
                    $json = Encode-Result $requestId @{
                        backend = 'uia'
                        apartment = 'MTA'
                        dependency_available = $true
                        session_accessible = $null
                        discovery_scope = 'desktop_windows'
                        helper_pid = $PID
                        utility_path = $utility.Path
                        powershell_home = $PSHOME
                        module_autoloading = [string]$PSModuleAutoLoadingPreference
                        no_profile = ([Environment]::GetCommandLineArgs() -icontains '-NoProfile')
                        non_interactive = ([Environment]::GetCommandLineArgs() -icontains '-NonInteractive')
                        has_window = $hasWindow
                    }
                    if ($traceProbe) {
                        $probeSucceeded = $true
                        Write-Stage 6
                    }
                }
                'targets' {
                    if (@($request.payload.PSObject.Properties).Count -ne 0) {
                        Stop-Bridge 'protocol_error'
                    }
                    Read-Targets
                    $json = Tree-Frame $requestId 'targets'
                }
                'snapshot' {
                    if (@($request.payload.PSObject.Properties.Name).Count -ne 1 -or
                        $request.payload.target_ref_id -cnotmatch '^[a-f0-9]{32}$') {
                        Stop-Bridge 'protocol_error'
                    }
                    Read-Snapshot ([string]$request.payload.target_ref_id)
                    $json = Tree-Frame $requestId 'snapshot'
                }
                'action' {
                    $properties = @($request.payload.PSObject.Properties.Name)
                    if ($properties.Count -ne 2 -or 'ref_id' -cnotin $properties -or
                        'action' -cnotin $properties -or
                        $request.payload.ref_id -cnotmatch '^[a-f0-9]{32}$' -or
                        $request.payload.action -cne 'invoke') {
                        $script:references = @{}
                        Stop-Bridge 'unsupported_operation'
                    }
                    $json = Encode-Result $requestId (Invoke-Node ([string]$request.payload.ref_id))
                }
                default { Stop-Bridge 'unsupported_operation' }
            }
            Check-Deadline
        } catch {
            $probeSucceeded = $false
            $code = $_.Exception.Data['muli_code']
            if ($code -notin @('dependency_missing', 'protocol_error', 'read_timeout',
                'element_unavailable', 'target_changed', 'node_budget', 'unsupported_operation',
                'stale_node', 'protected_element', 'element_disabled')) {
                $code = 'element_unavailable'
            }
            $outcome = 'not_started'
            if ($script:nativeEntered) {
                $code = 'action_unknown_outcome'
                $outcome = 'unknown'
            }
            $json = Microsoft.PowerShell.Utility\ConvertTo-Json -InputObject @{
                v = 1
                request_id = $requestId
                error = @{ code = $code; message = 'UIA operation unavailable'; outcome = $outcome }
            } -Depth 4 -Compress
            $script:references = @{}
        }
        $writer.WriteLine($json)
        if ($traceProbe -and $probeSucceeded) { Write-Stage 7 }
    }
} catch {
    # Malformed transport never prints PowerShell exception text or provider data.
    exit 1
} finally {
    $script:references = @{}
    $script:selected = $null
    $writer.Dispose()
}
