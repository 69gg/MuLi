# Fixed, read-only bridge. No source compilation, external commands or UI actions.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$PSModuleAutoLoadingPreference = 'None'
Set-StrictMode -Version Latest

$encoding = [System.Text.UTF8Encoding]::new($false, $true)
[Console]::InputEncoding = $encoding
$writer = [System.IO.StreamWriter]::new([Console]::OpenStandardOutput(), $encoding)
$writer.NewLine = [string][char]10
$writer.AutoFlush = $true
$script:ready = $false
$script:references = @{}
$reasonOrder = @('node_budget', 'depth_limit', 'children_limit', 'provider_error', 'output_budget')

function Stop-Bridge([string]$code) {
    $failure = [System.InvalidOperationException]::new('UIA bridge failed')
    $failure.Data['muli_code'] = $code
    throw $failure
}

function Initialize-Uia {
    if ($script:ready) { return }
    if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) {
        Stop-Bridge 'dependency_missing'
    }
    if ([Threading.Thread]::CurrentThread.GetApartmentState().ToString() -ne 'MTA') {
        Stop-Bridge 'dependency_missing'
    }
    try {
        Microsoft.PowerShell.Utility\Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
        if (-not [System.Windows.Automation.AutomationElement].Assembly.GlobalAssemblyCache -or
            -not [System.Windows.Automation.ControlType].Assembly.GlobalAssemblyCache) {
            Stop-Bridge 'dependency_missing'
        }
    } catch {
        Stop-Bridge 'dependency_missing'
    }
    $script:ready = $true
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

function Add-Omission([string]$reason) {
    [void]$script:reasons.Add($reason)
}

function Read-Node([object]$element, [object]$parent, [int]$depth, [string]$kind) {
    Check-Deadline
    if ($script:visited -ge $script:maxNodes) {
        Add-Omission 'node_budget'
        return $null
    }
    $script:visited++
    try {
        # Never query ValuePattern, TextPattern, or even Name on a password node.
        $current = $element.Current
        $protected = [bool]$current.IsPassword
        $name = ''
        if (-not $protected) { $name = [string]$current.Name }
        if ($name.Length -gt 2048) {
            Add-Omission 'output_budget'
            return $null
        }
        return [ordered]@{
            ref_id = [Guid]::NewGuid().ToString('N')
            parent_ref_id = $parent
            depth = $depth
            kind = $kind
            name = $name
            role = [string]$current.ControlType.ProgrammaticName
            actions = @()
            editable = $false
            read_only = $true
            protected = $protected
            enabled = [bool]$current.IsEnabled
        }
    } catch {
        Add-Omission 'provider_error'
        return $null
    }
}

function Window-Stamp([object]$element) {
    Check-Deadline
    $current = $element.Current
    $processId = [int]$current.ProcessId
    $handle = [int]$current.NativeWindowHandle
    if ($processId -le 0 -or $handle -eq 0) { Stop-Bridge 'element_unavailable' }
    $process = [Diagnostics.Process]::GetProcessById($processId)
    try {
        $started = $process.StartTime.ToUniversalTime().Ticks
    } finally {
        $process.Dispose()
    }
    return @{ process = $processId; handle = $handle; started = $started }
}

function Same-Window([object]$element, [object]$expected) {
    $actual = Window-Stamp $element
    if ($actual.process -ne $expected.process -or $actual.handle -ne $expected.handle -or
        $actual.started -ne $expected.started) {
        Stop-Bridge 'target_changed'
    }
}

function Read-Children([object]$element, [string]$parent, [int]$depth) {
    Check-Deadline
    if ($depth -gt 12) {
        Add-Omission 'depth_limit'
        return
    }
    if ($script:visited -ge $script:maxNodes) {
        Add-Omission 'node_budget'
        return
    }
    try {
        $child = $script:walker.GetFirstChild($element)
        $count = 0
        while ($null -ne $child) {
            Check-Deadline
            if ($count -ge 100) {
                Add-Omission 'children_limit'
                return
            }
            if ($script:visited -ge $script:maxNodes) {
                Add-Omission 'node_budget'
                return
            }
            $node = Read-Node $child $parent $depth 'node'
            if ($null -ne $node) {
                $script:rows.Add($node)
                Read-Children $child $node.ref_id ($depth + 1)
            }
            $count++
            Check-Deadline
            $child = $script:walker.GetNextSibling($child)
        }
    } catch {
        if ($_.Exception.Data['muli_code'] -eq 'read_timeout') { throw }
        Add-Omission 'provider_error'
    }
}

function Read-Targets {
    $script:references = @{}
    Check-Deadline
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $script:visited++ # The desktop root consumes work, but is not a public target.
    $element = $script:walker.GetFirstChild($root)
    $count = 0
    while ($null -ne $element) {
        Check-Deadline
        if ($count -ge 100) {
            Add-Omission 'children_limit'
            return
        }
        if ($script:visited -ge $script:maxNodes) {
            Add-Omission 'node_budget'
            return
        }
        $node = Read-Node $element $null 0 'window'
        if ($null -ne $node) {
            try {
                $stamp = Window-Stamp $element
                $script:rows.Add($node)
                $script:references[$node.ref_id] = @{ element = $element; stamp = $stamp }
            } catch {
                if ($_.Exception.Data['muli_code'] -eq 'read_timeout') { throw }
                Add-Omission 'provider_error'
            }
        }
        $count++
        Check-Deadline
        $element = $script:walker.GetNextSibling($element)
    }
}

function Read-Snapshot([string]$reference) {
    if (-not $script:references.ContainsKey($reference)) {
        Stop-Bridge 'target_changed'
    }
    $selected = $script:references[$reference]
    $script:references = @{}
    Same-Window $selected.element $selected.stamp
    $node = Read-Node $selected.element $null 0 'window'
    if ($null -eq $node) { Stop-Bridge 'element_unavailable' }
    $script:rows.Add($node)
    Read-Children $selected.element $node.ref_id 1
    Same-Window $selected.element $selected.stamp
}

function Tree-Result([string]$command, [object[]]$rows) {
    $omissions = @()
    $reasons = @()
    foreach ($reason in $reasonOrder) {
        if ($script:reasons.Contains($reason)) {
            $reasons += $reason
            $omissions += @{ reason = $reason; omitted_direct_children = $null }
        }
    }
    $result = [ordered]@{
        scope = 'window'
        complete = ($reasons.Count -eq 0)
        truncated = ($reasons.Count -ne 0)
        visited = $script:visited
        omissions = @($omissions)
        omission_reasons = @($reasons)
    }
    if ($command -eq 'targets') {
        $result.scope = 'desktop_windows'
        $result['targets'] = @($rows)
    } else {
        $result['nodes'] = @($rows)
    }
    return $result
}

function Encode-Result([string]$requestId, [object]$result) {
    return Microsoft.PowerShell.Utility\ConvertTo-Json -InputObject @{
        v = 1; request_id = $requestId; result = $result
    } -Depth 8 -Compress
}

function Fits-Frame([string]$text) {
    return (($text.Length + 1 -le $script:maxChars) -and
        ($encoding.GetByteCount($text) + 1 -le 250000))
}

function Tree-Frame([string]$requestId, [string]$command) {
    $all = $script:rows.ToArray()
    $json = Encode-Result $requestId (Tree-Result $command $all)
    if (Fits-Frame $json) { return $json }
    Add-Omission 'output_budget'
    $low = 0
    $high = $all.Length
    while ($low -lt $high) {
        Check-Deadline
        $middle = [int][Math]::Ceiling(($low + $high) / 2.0)
        $prefix = @()
        if ($middle -gt 0) { $prefix = @($all[0..($middle - 1)]) }
        $candidate = Encode-Result $requestId (Tree-Result $command $prefix)
        if (Fits-Frame $candidate) { $low = $middle } else { $high = $middle - 1 }
    }
    $kept = @()
    if ($low -gt 0) { $kept = @($all[0..($low - 1)]) }
    $json = Encode-Result $requestId (Tree-Result $command $kept)
    if (-not (Fits-Frame $json)) { Stop-Bridge 'node_budget' }
    return $json
}

try {
    # Explicit system module only: never search user PSModulePath or enable autoload.
    $utilityPath = [IO.Path]::Combine($PSHOME, 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1')
    Microsoft.PowerShell.Core\Import-Module -Name $utilityPath -Cmdlet Add-Type, ConvertFrom-Json, ConvertTo-Json
    $utility = Microsoft.PowerShell.Core\Get-Module -Name Microsoft.PowerShell.Utility
    if ($null -eq $utility -or $utility.Path -ne $utilityPath) { exit 1 }
    for ($requestCount = 0; $requestCount -lt 16; $requestCount++) {
        $line = Read-BoundedLine
        if ($null -eq $line) { break }
        $request = Microsoft.PowerShell.Utility\ConvertFrom-Json -InputObject $line
        if ($request.v -ne 1 -or $request.request_id -cnotmatch '^[a-f0-9]{32}$') {
            Stop-Bridge 'protocol_error'
        }
        $requestId = [string]$request.request_id
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
            $script:watch = [Diagnostics.Stopwatch]::StartNew()
            Initialize-Uia
            Check-Deadline
            $script:walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
            $script:rows = [Collections.Generic.List[object]]::new()
            $script:reasons = [Collections.Generic.HashSet[string]]::new()
            $script:visited = 0
            switch -CaseSensitive ([string]$request.command) {
                'probe' {
                    $process = [Diagnostics.Process]::GetCurrentProcess()
                    try { $hasWindow = ($process.MainWindowHandle -ne [IntPtr]::Zero) }
                    finally { $process.Dispose() }
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
                default { Stop-Bridge 'unsupported_operation' }
            }
            Check-Deadline
        } catch {
            $code = $_.Exception.Data['muli_code']
            if ($code -notin @('dependency_missing', 'protocol_error', 'read_timeout',
                'element_unavailable', 'target_changed', 'node_budget', 'unsupported_operation')) {
                $code = 'element_unavailable'
            }
            $json = Microsoft.PowerShell.Utility\ConvertTo-Json -InputObject @{
                v = 1
                request_id = $requestId
                error = @{ code = $code; message = 'UIA read unavailable'; outcome = 'not_started' }
            } -Depth 4 -Compress
            $script:references = @{}
        }
        $writer.WriteLine($json)
    }
} catch {
    # Malformed transport never prints PowerShell exception text or provider data.
    exit 1
} finally {
    $script:references = @{}
    $writer.Dispose()
}
