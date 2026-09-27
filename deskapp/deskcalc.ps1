# DeskCalc: the deterministic desktop target for the Windows UI Automation
# surface. What mockapp is to the web adapter, this is to the desktop one: a
# small line-of-business window with the traits that make desktop automation
# hard, switchable per launch with -Inject.
#
#   (none)          two unnamed number fields labelled by text beside them, an
#                   operation combo box, a checkbox, Calculate, a read-only
#                   result, Record (a commit: it appends to the ledger file)
#   renamed_button  Calculate is labelled "Compute"
#   ambiguous       a second "Calculate" button, in a "Legacy" group
#   disabled        Calculate is disabled
#   slow            the result appears 2.5 s after Calculate
#   modal           Calculate raises a native message box instead of a result
#
# Deterministic: fixed position and size, no clock, no randomness. The ledger
# (DESKCALC_LEDGER, default %TEMP%\deskcalc-ledger.txt) is the commit oracle:
# one line per Record.
#
# Run: powershell -NoProfile -ExecutionPolicy Bypass -File deskapp\deskcalc.ps1 [-Inject name]

param([string]$Inject = "")

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$Invariant = [System.Globalization.CultureInfo]::InvariantCulture
$Ledger = if ($env:DESKCALC_LEDGER) { $env:DESKCALC_LEDGER } else { Join-Path $env:TEMP "deskcalc-ledger.txt" }

$form = New-Object System.Windows.Forms.Form
$form.Text = "DeskCalc"
$form.StartPosition = "Manual"
$form.Location = New-Object System.Drawing.Point(120, 120)
$form.ClientSize = New-Object System.Drawing.Size(460, 430)
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox = $false

function Add-Control($control, [int]$x, [int]$y, [int]$w, [int]$h) {
    $control.Location = New-Object System.Drawing.Point($x, $y)
    $control.Size = New-Object System.Drawing.Size($w, $h)
    $form.Controls.Add($control)
    return $control
}

function New-Label([string]$text, [int]$x, [int]$y) {
    $label = New-Object System.Windows.Forms.Label
    $label.Text = $text
    Add-Control $label $x $y 120 20 | Out-Null
}

# -- menu ---------------------------------------------------------------------
$menu = New-Object System.Windows.Forms.MenuStrip
$file = New-Object System.Windows.Forms.ToolStripMenuItem("File")
$clear = New-Object System.Windows.Forms.ToolStripMenuItem("Clear history")
$exit = New-Object System.Windows.Forms.ToolStripMenuItem("Exit")
$help = New-Object System.Windows.Forms.ToolStripMenuItem("Help")
$about = New-Object System.Windows.Forms.ToolStripMenuItem("About DeskCalc")
[void]$file.DropDownItems.Add($clear)
[void]$file.DropDownItems.Add($exit)
[void]$help.DropDownItems.Add($about)
[void]$menu.Items.Add($file)
[void]$menu.Items.Add($help)
$form.MainMenuStrip = $menu
$form.Controls.Add($menu)

# -- inputs -------------------------------------------------------------------
# The number fields carry no accessible name: like the legacy web app, a
# control is known by the label a person reads beside it.
New-Label "First number" 20 43
$first = Add-Control (New-Object System.Windows.Forms.TextBox) 150 40 160 22
New-Label "Second number" 20 78
$second = Add-Control (New-Object System.Windows.Forms.TextBox) 150 75 160 22
New-Label "Operation" 20 113
$operation = New-Object System.Windows.Forms.ComboBox
$operation.DropDownStyle = "DropDownList"
[void]$operation.Items.AddRange(@("Add", "Subtract", "Multiply", "Divide"))
$operation.SelectedIndex = 0
Add-Control $operation 150 110 160 22 | Out-Null

$round = New-Object System.Windows.Forms.CheckBox
$round.Text = "Round to cents"
Add-Control $round 150 143 160 22 | Out-Null

$calculate = New-Object System.Windows.Forms.Button
$calculate.Text = if ($Inject -eq "renamed_button") { "Compute" } else { "Calculate" }
$calculate.Enabled = $Inject -ne "disabled"
Add-Control $calculate 150 175 100 28 | Out-Null
$form.AcceptButton = $calculate

if ($Inject -eq "ambiguous") {
    $legacy = New-Object System.Windows.Forms.GroupBox
    $legacy.Text = "Legacy"
    Add-Control $legacy 320 160 120 50 | Out-Null
    $twin = New-Object System.Windows.Forms.Button
    $twin.Text = "Calculate"
    $twin.Location = New-Object System.Drawing.Point(10, 18)
    $twin.Size = New-Object System.Drawing.Size(100, 26)
    $legacy.Controls.Add($twin)
}

# -- outputs ------------------------------------------------------------------
New-Label "Result" 20 223
$result = Add-Control (New-Object System.Windows.Forms.TextBox) 150 220 160 22
$result.ReadOnly = $true

$problem = New-Object System.Windows.Forms.Label
$problem.ForeColor = [System.Drawing.Color]::DarkRed
Add-Control $problem 150 248 290 20 | Out-Null

$record = New-Object System.Windows.Forms.Button
$record.Text = "Record"
Add-Control $record 150 272 100 28 | Out-Null

New-Label "History" 20 313
$history = Add-Control (New-Object System.Windows.Forms.ListBox) 150 310 290 80

$status = New-Object System.Windows.Forms.StatusStrip
$statusText = New-Object System.Windows.Forms.ToolStripStatusLabel("Ready")
[void]$status.Items.Add($statusText)
$form.Controls.Add($status)

# -- behaviour ----------------------------------------------------------------
function Read-Number([string]$text, [string]$what) {
    $value = [decimal]0
    $style = [System.Globalization.NumberStyles]::Number
    if (-not [decimal]::TryParse($text.Trim(), $style, $Invariant, [ref]$value)) {
        throw "$what is not a number"
    }
    return $value
}

function Show-Result {
    try {
        $a = Read-Number $first.Text "First number"
        $b = Read-Number $second.Text "Second number"
        switch ($operation.SelectedItem) {
            "Add" { $value = $a + $b }
            "Subtract" { $value = $a - $b }
            "Multiply" { $value = $a * $b }
            "Divide" {
                if ($b -eq 0) { throw "Cannot divide by zero" }
                $value = $a / $b
            }
        }
        if ($round.Checked) {
            $value = [decimal]::Round($value, 2, [System.MidpointRounding]::AwayFromZero)
            $result.Text = $value.ToString("0.00", $Invariant)
        } else {
            $result.Text = $value.ToString($Invariant)
        }
        $problem.Text = ""
        $statusText.Text = "Ready"
    } catch {
        $result.Text = ""
        $problem.Text = $_.Exception.Message
        $statusText.Text = "Ready"
    }
}

$slowTimer = New-Object System.Windows.Forms.Timer
$slowTimer.Interval = 2500
$slowTimer.Add_Tick({ $slowTimer.Stop(); Show-Result })

$calculate.Add_Click({
    if ($Inject -eq "modal") {
        [void][System.Windows.Forms.MessageBox]::Show(
            $form, "The rate service is unavailable.", "DeskCalc", "OK", "Warning")
        return
    }
    $result.Text = ""
    $problem.Text = ""
    if ($Inject -eq "slow") {
        $statusText.Text = "Working..."
        $slowTimer.Start()
    } else {
        Show-Result
    }
})

$record.Add_Click({
    if (-not $result.Text) {
        $problem.Text = "Nothing to record"
        return
    }
    $line = "{0} {1} {2} = {3}" -f $first.Text.Trim(), $operation.SelectedItem, $second.Text.Trim(), $result.Text
    Add-Content -Path $Ledger -Value $line -Encoding UTF8
    [void]$history.Items.Add($line)
    $statusText.Text = "Recorded entry " + $history.Items.Count
})

$clear.Add_Click({ $history.Items.Clear() })
$exit.Add_Click({ $form.Close() })
$about.Add_Click({
    [void][System.Windows.Forms.MessageBox]::Show($form, "DeskCalc 1.0", "About DeskCalc", "OK", "Information")
})

[void]$form.ShowDialog()
