#Requires -Version 7.5
[CmdletBinding()]
param([Parameter(Mandatory)][string]$ConfigPath,[string]$PythonPath,[switch]$SelfTest,[string]$CapturePath,[switch]$ReaderCheck)
$ErrorActionPreference='Stop'
if(-not $PythonPath){$PythonPath=(Get-Command python -CommandType Application -ErrorAction Stop).Source}
$ConfigPath=(Resolve-Path -LiteralPath $ConfigPath).Path
$settings=Get-Content -LiteralPath $ConfigPath -Raw -Encoding utf8 | ConvertFrom-Json -DateKind String
$collectorPath=Join-Path $PSScriptRoot 'development_status.py'
if($ReaderCheck){& $pythonPath -X utf8 -B $collectorPath --config $ConfigPath --once;exit $LASTEXITCODE}
Add-Type -AssemblyName PresentationFramework,PresentationCore,WindowsBase
[xml]$xaml=@"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml" Title="开发状态" Width="1320" Height="900" MinWidth="1000" MinHeight="650" WindowStartupLocation="CenterScreen" Background="#F3F5F9" FontFamily="Microsoft YaHei UI" FontSize="14" UseLayoutRounding="True" ShowActivated="False">
 <Window.Resources>
  <Style TargetType="TextBlock"><Setter Property="TextWrapping" Value="Wrap"/><Setter Property="Foreground" Value="#25374B"/></Style>
  <Style TargetType="DataGrid">
   <Setter Property="AutoGenerateColumns" Value="False"/><Setter Property="IsReadOnly" Value="True"/><Setter Property="CanUserAddRows" Value="False"/><Setter Property="CanUserDeleteRows" Value="False"/>
   <Setter Property="HeadersVisibility" Value="Column"/><Setter Property="GridLinesVisibility" Value="Horizontal"/><Setter Property="HorizontalGridLinesBrush" Value="#E4EAF2"/>
   <Setter Property="BorderBrush" Value="#D7E0ED"/><Setter Property="Background" Value="White"/><Setter Property="RowBackground" Value="White"/><Setter Property="AlternatingRowBackground" Value="#F8FAFD"/>
   <Setter Property="SelectionMode" Value="Single"/><Setter Property="SelectionUnit" Value="FullRow"/><Setter Property="RowHeaderWidth" Value="0"/><Setter Property="CanUserReorderColumns" Value="False"/>
  </Style>
  <Style TargetType="DataGridColumnHeader"><Setter Property="Background" Value="#E8F0FA"/><Setter Property="Foreground" Value="#294E76"/><Setter Property="FontWeight" Value="SemiBold"/><Setter Property="Padding" Value="12,12"/><Setter Property="BorderThickness" Value="0"/></Style>
  <Style x:Key="CellText" TargetType="TextBlock"><Setter Property="TextWrapping" Value="Wrap"/><Setter Property="Padding" Value="12,13"/><Setter Property="LineHeight" Value="24"/><Setter Property="VerticalAlignment" Value="Center"/></Style>
 </Window.Resources>
 <Grid Margin="24,20,24,16">
  <Grid.RowDefinitions><RowDefinition Height="Auto"/><RowDefinition Height="Auto"/><RowDefinition Height="*"/><RowDefinition Height="Auto"/><RowDefinition Height="Auto"/><RowDefinition Height="Auto"/></Grid.RowDefinitions>
  <Grid>
   <Grid.ColumnDefinitions><ColumnDefinition Width="*"/><ColumnDefinition Width="Auto"/></Grid.ColumnDefinitions>
   <StackPanel>
    <TextBlock x:Name="DashboardTitle" Text="开发分工" FontSize="25" FontWeight="SemiBold" Foreground="#102C4C"/>
    <TextBlock Text="每个人做什么、对应计划哪一项、现在进行到哪一步" Foreground="#65748B" Margin="0,7,0,0" FontSize="15"/>
   </StackPanel>
   <StackPanel Grid.Column="1" Orientation="Horizontal" VerticalAlignment="Center">
    <CheckBox x:Name="Pin" Content="置顶" VerticalAlignment="Center" Margin="0,0,20,0"/>
    <Button x:Name="Refresh" Content="刷新" Padding="18,7" Background="White" BorderBrush="#CBD5E1"/>
   </StackPanel>
  </Grid>
  <Border Grid.Row="1" Background="#E8F0FA" CornerRadius="7" Padding="14,10" Margin="0,16,0,14">
   <TextBlock x:Name="Notice" Text="正在读取分工记录…" Foreground="#38618C" LineHeight="23"/>
  </Border>
  <DataGrid Grid.Row="2" x:Name="Current" FontSize="14" MinHeight="170" EnableRowVirtualization="True">
   <DataGrid.Columns>
    <DataGridTextColumn Header="谁在负责" Binding="{Binding display_name}" Width="165" ElementStyle="{StaticResource CellText}"/>
    <DataGridTextColumn Header="对应开发计划" Binding="{Binding plan}" Width="265" ElementStyle="{StaticResource CellText}"/>
    <DataGridTextColumn Header="最近在做什么" Binding="{Binding work}" Width="*" MinWidth="240" ElementStyle="{StaticResource CellText}"/>
    <DataGridTemplateColumn Header="当前记录状态" Width="160"><DataGridTemplateColumn.CellTemplate><DataTemplate><TextBlock Text="{Binding status}" Foreground="{Binding color}" TextWrapping="Wrap" Padding="12,13" LineHeight="24" FontWeight="SemiBold" VerticalAlignment="Center"/></DataTemplate></DataGridTemplateColumn.CellTemplate></DataGridTemplateColumn>
    <DataGridTextColumn Header="说明更新时间" Binding="{Binding updated}" Width="132" ElementStyle="{StaticResource CellText}"/>
   </DataGrid.Columns>
  </DataGrid>
  <Expander Grid.Row="3" x:Name="HistoryExpander" Header="历史子智能体" FontSize="13" Margin="0,12,0,0" Foreground="#65748B" IsExpanded="False">
   <DataGrid x:Name="History" FontSize="12" Height="230" Margin="0,8,0,0" EnableRowVirtualization="True">
    <DataGrid.Columns>
     <DataGridTextColumn Header="智能体 / 所属对话" Binding="{Binding display_name}" Width="220" ElementStyle="{StaticResource CellText}"/>
     <DataGridTextColumn Header="关联计划" Binding="{Binding plan}" Width="265" ElementStyle="{StaticResource CellText}"/>
     <DataGridTextColumn Header="最后工作记录" Binding="{Binding work}" Width="*" ElementStyle="{StaticResource CellText}"/>
     <DataGridTextColumn Header="记录状态" Binding="{Binding status}" Width="160" ElementStyle="{StaticResource CellText}"/>
     <DataGridTextColumn Header="更新时间" Binding="{Binding updated}" Width="132" ElementStyle="{StaticResource CellText}"/>
    </DataGrid.Columns>
   </DataGrid>
  </Expander>
  <Border Grid.Row="4" Background="White" BorderBrush="#D7E0ED" BorderThickness="1" CornerRadius="7" Padding="16,12" Margin="0,14,0,0">
   <StackPanel>
    <TextBlock x:Name="SelectedTitle" Text="点击一行，查看计划关联和验证范围" FontSize="16" FontWeight="SemiBold" Foreground="#244F7D"/>
    <TextBlock x:Name="SelectedValidation" Margin="0,7,0,0" LineHeight="24"/>
    <TextBlock x:Name="SelectedPlan" Margin="0,6,0,0" FontSize="12" Foreground="#65748B" LineHeight="21"/>
    <Expander Header="查看这项工作的原始说明" FontSize="12" Foreground="#687A91" Margin="0,8,0,0" IsExpanded="False">
     <TextBox x:Name="Raw" IsReadOnly="True" TextWrapping="Wrap" VerticalScrollBarVisibility="Auto" MaxHeight="140" Padding="10" FontSize="12" BorderBrush="#D9E0EB" Margin="0,8,0,0"/>
    </Expander>
   </StackPanel>
  </Border>
  <TextBlock Grid.Row="5" x:Name="Footer" FontSize="12" Foreground="#687A91" Margin="0,11,0,0" LineHeight="21"/>
 </Grid>
</Window>
"@
if($SelfTest){$xaml.Window.SetAttribute('WindowStartupLocation','Manual');$xaml.Window.SetAttribute('Left','-10000');$xaml.Window.SetAttribute('Top','-10000');$xaml.Window.SetAttribute('ShowInTaskbar','False')}
$reader=New-Object System.Xml.XmlNodeReader $xaml
$script:window=[Windows.Markup.XamlReader]::Load($reader)
$script:controls=@{}
foreach($name in @('DashboardTitle','Pin','Refresh','Notice','Current','History','HistoryExpander','SelectedTitle','SelectedValidation','SelectedPlan','Raw','Footer')){$script:controls[$name]=$script:window.FindName($name)}
if($settings.title){$script:window.Title=[string]$settings.title;$script:controls.DashboardTitle.Text=[string]$settings.title}
$startInfo=New-Object Diagnostics.ProcessStartInfo
$startInfo.FileName=$pythonPath
foreach($arg in @('-X','utf8','-B','-u',$collectorPath,'--config',$ConfigPath)){[void]$startInfo.ArgumentList.Add($arg)}
$startInfo.UseShellExecute=$false;$startInfo.CreateNoWindow=$true
$startInfo.RedirectStandardInput=$true;$startInfo.RedirectStandardOutput=$true;$startInfo.RedirectStandardError=$true
$startInfo.StandardInputEncoding=[Text.Encoding]::UTF8;$startInfo.StandardOutputEncoding=[Text.Encoding]::UTF8
$script:collector=New-Object Diagnostics.Process
$script:collector.StartInfo=$startInfo
[void]$script:collector.Start()
$script:latestSnapshot=$null

function Set-Selection($Row){
 if($null -eq $Row){return}
 $script:controls.SelectedTitle.Text=if($Row.kind -eq '子智能体'){$Row.name+' · 属于'+$Row.group}else{$Row.name}
 $script:controls.SelectedValidation.Text='验证范围：'+$Row.validation
 $statuses=@{'planned'='待开始';'in-progress'='进行中';'complete'='已完成'}
 $planLines=New-Object System.Collections.Generic.List[string]
 foreach($p in @($Row.plan_entries)){
  $s=if($statuses.ContainsKey([string]$p.status)){$statuses[[string]$p.status]}else{[string]$p.status}
  [void]$planLines.Add(($p.id+' · '+$p.original_title+'（原计划状态：'+$s+'）'))
 }
 $script:controls.SelectedPlan.Text=($planLines -join "`n")+"`n"+$Row.association
 $script:controls.Raw.Text=$Row.raw
}
function Prepare-Rows($Rows){
 $list=New-Object Collections.Generic.List[object]
 foreach($row in @($Rows)){
  $display=if($row.kind -eq '子智能体'){$row.name+"`n"+'属于'+$row.group}else{$row.name}
  $color=switch($row.state){'working'{'#1A6B9D'}'paused'{'#9A6700'}'delivered'{'#526CB0'}'ended'{'#526170'}default{'#795E48'}}
  $row|Add-Member -NotePropertyName display_name -NotePropertyValue $display -Force
  $row|Add-Member -NotePropertyName color -NotePropertyValue $color -Force
  [void]$list.Add($row)
 }
 return ,$list
}
function Refresh-Window{
 try{
  if($script:collector.HasExited){throw 'Collector exited'}
  $script:collector.StandardInput.WriteLine('snapshot');$script:collector.StandardInput.Flush()
  $line=$script:collector.StandardOutput.ReadLine()
  if(-not $line){throw 'No snapshot'}
  $data=$line|ConvertFrom-Json -DateKind String
  if($data.error){throw 'Snapshot failed'}
  $selected=$script:controls.Current.SelectedItem
  $selectedId=if($selected){$selected.id}else{''}
  $script:latestSnapshot=$data
  $script:controls.Current.ItemsSource=Prepare-Rows $data.rows
  $script:controls.History.ItemsSource=Prepare-Rows $data.history
  $script:controls.HistoryExpander.Header='历史子智能体（'+$data.history_count+'个，默认收起）'
  $script:controls.Notice.Text='“已交付”“已暂停”“本轮结束”分别显示。计划按实际工作内容关联，原计划状态保持原样。'
  if(@($data.warnings).Count){$script:controls.Notice.Text=$data.warnings -join ' '}
  $script:controls.Footer.Text=([string]$data.known_count)+'个开发对话 · 本日'+$data.agent_count+'个子智能体有记录 · 每5秒读取新进展；新智能体名单每30秒更新。状态以最新记录为准。'
  $target=@($script:controls.Current.ItemsSource|Where-Object {$_.id -eq $selectedId})|Select-Object -First 1
  if($target){$script:controls.Current.SelectedItem=$target}else{$script:controls.Current.SelectedIndex=0}
  Set-Selection $script:controls.Current.SelectedItem
 }catch{
  $script:controls.Notice.Text='暂时无法读取新的分工记录，保留上次内容。'
 }
}
$script:controls.Current.Add_SelectionChanged({Set-Selection $script:controls.Current.SelectedItem})
$script:controls.History.Add_SelectionChanged({Set-Selection $script:controls.History.SelectedItem})
$script:controls.Pin.Add_Checked({$script:window.Topmost=$true})
$script:controls.Pin.Add_Unchecked({$script:window.Topmost=$false})
$script:controls.Refresh.Add_Click({Refresh-Window})
$script:timer=New-Object Windows.Threading.DispatcherTimer
$script:timer.Interval=[TimeSpan]::FromSeconds(5)
$script:timer.Add_Tick({Refresh-Window})
$script:window.Add_ContentRendered({
 Refresh-Window
 $script:timer.Start()
 if($SelfTest){
  if(-not $CapturePath){throw 'SelfTest requires CapturePath'}
  if($null -eq $script:latestSnapshot){throw 'No valid snapshot for rendering'}
  $script:window.UpdateLayout()
  $bitmap=New-Object Windows.Media.Imaging.RenderTargetBitmap ([int]$script:window.ActualWidth),([int]$script:window.ActualHeight),96,96,([Windows.Media.PixelFormats]::Pbgra32)
  $bitmap.Render($script:window)
  $encoder=New-Object Windows.Media.Imaging.PngBitmapEncoder
  $encoder.Frames.Add([Windows.Media.Imaging.BitmapFrame]::Create($bitmap))
  $file=[IO.File]::Create($CapturePath)
  try{$encoder.Save($file)}finally{$file.Dispose()}
  $proof=@{title=$script:window.Title;rows=$script:latestSnapshot.rows;historyCount=$script:latestSnapshot.history_count;warnings=$script:latestSnapshot.warnings;capture=$CapturePath}
  [IO.File]::WriteAllText(($CapturePath+'.json'),($proof|ConvertTo-Json -Depth 9),(New-Object Text.UTF8Encoding($false)))
  $script:window.Close()
 }
})
$script:window.Add_Closed({
 $script:timer.Stop()
 try{if(-not $script:collector.HasExited){$script:collector.StandardInput.WriteLine('quit');$script:collector.StandardInput.Flush();if(-not $script:collector.WaitForExit(2000)){$script:collector.Kill();[void]$script:collector.WaitForExit(2000)}}}catch{}
 $script:collector.Dispose()
})
$application=New-Object Windows.Application
[void]$application.Run($script:window)