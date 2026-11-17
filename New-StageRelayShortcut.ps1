# Run this from PowerShell on Windows with the path to a packaged Stage Relay.exe.
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$ExePath,

    [string]$DataDir
)

$ErrorActionPreference = 'Stop'

$exe = Get-Item -LiteralPath $ExePath
if ($exe.PSIsContainer -or $exe.Name -ne 'Stage Relay.exe') {
    throw 'Select the packaged Stage Relay.exe.'
}

$launchArguments = ''
if ($PSBoundParameters.ContainsKey('DataDir')) {
    $store = Get-Item -LiteralPath $DataDir
    if (-not $store.PSIsContainer -or
        -not (Test-Path -LiteralPath (Join-Path $store.FullName 'workspace.sqlite3') -PathType Leaf)) {
        throw 'DataDir must be an existing Stage Relay data directory containing workspace.sqlite3.'
    }
    $launchArguments = '--data-dir "' + $store.FullName.TrimEnd('\') + '"'
}

$desktop = [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
if (-not $desktop -or -not (Test-Path -LiteralPath $desktop -PathType Container)) {
    throw 'The current Windows user has no accessible desktop directory.'
}

$linkPath = Join-Path $desktop 'Stage Relay.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($linkPath)
$shortcut.TargetPath = $exe.FullName
$shortcut.WorkingDirectory = $exe.DirectoryName
$shortcut.Arguments = $launchArguments
$shortcut.IconLocation = $exe.FullName + ',0'
$shortcut.Description = 'Open Stage Relay'
$shortcut.Hotkey = 'CTRL+ALT+R'
$shortcut.Save()

$linkPath
