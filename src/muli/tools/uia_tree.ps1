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
        $node = [ordered]@{
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
        if ($kind -eq 'node' -and -not $protected -and $node.enabled -and
            $current.ControlType -eq [System.Windows.Automation.ControlType]::Button -and
            $null -ne $script:selected -and
            $current.ProcessId -eq $script:selected.stamp.process) {
            $pattern = $null
            if ($element.TryGetCurrentPattern(
                    [System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern) -and
                $pattern -is [System.Windows.Automation.InvokePattern]) {
                $node.actions = @('invoke')
                $node.read_only = $false
                $script:references[$node.ref_id] = @{
                    kind = 'node'; element = $element; record = $node
                    window = $script:selected.element; stamp = $script:selected.stamp
                }
            }
        }
        return $node
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
                $script:references[$node.ref_id] = @{
                    kind = 'target'; element = $element; stamp = $stamp
                }
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
    if ($selected.kind -ne 'target') { Stop-Bridge 'target_changed' }
    $script:selected = $selected
    Same-Window $selected.element $selected.stamp
    $node = Read-Node $selected.element $null 0 'window'
    if ($null -eq $node) { Stop-Bridge 'element_unavailable' }
    $script:rows.Add($node)
    Read-Children $selected.element $node.ref_id 1
    Same-Window $selected.element $selected.stamp
}

function Invoke-Node([string]$reference) {
    # Every attempt consumes the entire private snapshot, even a bad reference.
    $captured = $script:references
    $script:references = @{}
    if (-not $captured.ContainsKey($reference)) { Stop-Bridge 'stale_node' }
    $selected = $captured[$reference]
    if ($selected.kind -ne 'node') { Stop-Bridge 'stale_node' }
    Check-Deadline
    $current = $selected.element.Current
    # IsPassword is always the first provider property gate. Never ask for a
    # protected Name, Value, TextPattern or InvokePattern.
    if ($current.IsPassword) { Stop-Bridge 'protected_element' }
    if ($current.ControlType -ne [System.Windows.Automation.ControlType]::Button -or
        $current.ProcessId -ne $selected.stamp.process -or
        [string]$current.Name -cne $selected.record.name) {
        Stop-Bridge 'stale_node'
    }
    if (-not $current.IsEnabled) { Stop-Bridge 'element_disabled' }
    Same-Window $selected.window $selected.stamp
    $cursor = $selected.element
    $owned = $false
    for ($depth = 0; $depth -le 12 -and $null -ne $cursor; $depth++) {
        Check-Deadline
        if ([System.Windows.Automation.Automation]::Compare($cursor, $selected.window)) {
            $owned = $true
            break
        }
        if ($depth -eq 12) { break }
        $cursor = $script:walker.GetParent($cursor)
    }
    if (-not $owned) { Stop-Bridge 'target_changed' }
    $pattern = $null
    if (-not $selected.element.TryGetCurrentPattern(
            [System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern) -or
        $pattern -isnot [System.Windows.Automation.InvokePattern]) {
        Stop-Bridge 'unsupported_operation'
    }
    Check-Deadline
    # An exception after entering a provider is not proof of zero effects.
    $script:nativeEntered = $true
    $pattern.Invoke()
    return @{ ok = $true; outcome = 'accepted'; effect_verified = $false }
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

function Fits-Frame([string]$text) {
    return (($text.Length + 1 -le $script:maxChars) -and
        ($encoding.GetByteCount($text) + 1 -le 250000))
}

function Keep-References([object[]]$rows) {
    $kept = @{}
    foreach ($row in $rows) {
        if ($script:references.ContainsKey($row.ref_id)) {
            $kept[$row.ref_id] = $script:references[$row.ref_id]
        }
    }
    $script:references = $kept
}

function Tree-Frame([string]$requestId, [string]$command) {
    $all = $script:rows.ToArray()
    $json = Encode-Result $requestId (Tree-Result $command $all)
    if (Fits-Frame $json) {
        Keep-References $all
        return $json
    }
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
    Keep-References $kept
    return $json
}
