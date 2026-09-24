"""PC threat & health scan: Defender, firewall, suspicious processes/startup items,
open ports, updates, disk, resources. Produces a spoken summary and an HTML report."""
import datetime as dt
import html
import json
import os
import subprocess
import threading
import time
import webbrowser
from pathlib import Path

import psutil

from .util import DATA, NO_WINDOW, log, run_ps

_PS = r"""
$r = [ordered]@{}
try { $m = Get-MpComputerStatus -ErrorAction Stop
  $r.defender = @{ av=$m.AntivirusEnabled; realtime=$m.RealTimeProtectionEnabled; sigAge=$m.AntivirusSignatureAge;
                   quickAge=$m.QuickScanAge; fullAge=$m.FullScanAge; tamper=$m.IsTamperProtected } } catch { $r.defender = $null }
try { $r.threats = @(Get-MpThreat -ErrorAction Stop | Select-Object -First 15 | ForEach-Object {
  @{ name=$_.ThreatName; severity=[int]$_.SeverityID; active=$_.IsActive; resources=(($_.Resources | Select-Object -First 2) -join '; ') } }) } catch { $r.threats = @() }
try { $r.firewall = @(Get-NetFirewallProfile -ErrorAction Stop | ForEach-Object { @{ name=$_.Name; on=[bool]$_.Enabled } }) } catch {}
try { $r.uac = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System').EnableLUA } catch {}
try { $r.rdpOpen = ((Get-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server').fDenyTSConnections -eq 0) } catch {}
try { $r.startup = @(Get-CimInstance Win32_StartupCommand -ErrorAction Stop | ForEach-Object { @{ name=$_.Name; cmd=$_.Command } }) } catch {}
try { $h = Get-HotFix | Where-Object InstalledOn | Sort-Object InstalledOn -Descending | Select-Object -First 1
      $r.lastUpdate = $h.InstalledOn.ToString('yyyy-MM-dd') } catch {}
$r.rebootPending = (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') -or
                   (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired')
try { $r.listen = @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalAddress -in '0.0.0.0','::' } |
      ForEach-Object { @{ port=$_.LocalPort; proc=(Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName } }) } catch {}
$r | ConvertTo-Json -Depth 4 -Compress
"""

_RISKY_PORTS = {21: "FTP", 23: "Telnet", 445: "SMB file sharing", 3389: "Remote Desktop", 5900: "VNC", 135: "RPC",
                139: "NetBIOS"}
_SUSPICIOUS_DIRS = ("\\appdata\\local\\temp\\", "\\windows\\temp\\", "\\downloads\\", "\\users\\public\\",
                    "\\appdata\\roaming\\microsoft\\windows\\start menu\\")


def _signatures(paths):
    if not paths:
        return {}
    arr = ",".join("'" + p.replace("'", "''") + "'" for p in paths)
    _, out, _ = run_ps(f"@({arr}) | ForEach-Object {{ $s = Get-AuthenticodeSignature -LiteralPath $_; "
                       f"@{{ p=$_; st=[string]$s.Status; who=[string]$s.SignerCertificate.Subject }} }} | "
                       f"ConvertTo-Json -Compress", 60)
    try:
        data = json.loads(out)
        data = [data] if isinstance(data, dict) else data
        return {d["p"].lower(): d for d in data}
    except Exception:
        return {}


def scan() -> dict:
    t0 = time.time()
    findings = []   # (severity, title, detail, fix)

    def add(sev, title, detail="", fix=""):
        findings.append({"sev": sev, "title": title, "detail": detail, "fix": fix})

    info = {}
    try:
        _, out, _ = run_ps(_PS, 90)
        info = json.loads(out) if out else {}
    except Exception as e:
        log.warning("security ps failed: %s", e)

    d = info.get("defender")
    if d:
        if not d.get("av"):
            add("high", "Microsoft Defender antivirus is OFF", "", "Turn it on in Windows Security → Virus & threat protection.")
        if not d.get("realtime"):
            add("high", "Real-time protection is OFF", "", "Enable real-time protection in Windows Security.")
        if (d.get("sigAge") or 0) > 3:
            add("medium", f"Virus definitions are {d['sigAge']} days old", "", "Say 'Miles, update virus definitions' or run Windows Update.")
        if (d.get("quickAge") or 0) > 14:
            add("low", f"No quick scan for {d['quickAge']} days", "", "Say 'Miles, run a quick virus scan'.")
        if d.get("tamper") is False:
            add("low", "Tamper protection is off", "", "Turn on Tamper Protection in Windows Security.")
    else:
        add("info", "Couldn't read Microsoft Defender status", "A third-party antivirus may be in use.", "")

    for t in info.get("threats") or []:
        sev = "high" if t.get("active") or (t.get("severity") or 0) >= 4 else "medium"
        add(sev, f"Threat detected: {t.get('name')}", t.get("resources", ""),
            "Open Windows Security → Protection history and remove/quarantine it.")

    for fw in info.get("firewall") or []:
        if not fw.get("on"):
            add("high", f"Firewall is OFF for the {fw['name']} network profile", "",
                "Turn the firewall on in Windows Security → Firewall & network protection.")
    if info.get("uac") == 0:
        add("high", "User Account Control (UAC) is disabled", "Any program can silently get admin rights.",
            "Re-enable UAC in Control Panel → User Accounts.")
    if info.get("rdpOpen"):
        add("medium", "Remote Desktop connections are allowed", "", "Disable Remote Desktop in Settings if you don't use it.")
    if info.get("rebootPending"):
        add("medium", "A restart is pending to finish installing updates", "", "Restart your PC when convenient.")
    lu = info.get("lastUpdate")
    if lu:
        try:
            age = (dt.date.today() - dt.date.fromisoformat(lu)).days
            if age > 40:
                add("medium", f"Last Windows update was installed {age} days ago", "", "Open Settings → Windows Update and install updates.")
        except ValueError:
            pass

    seen_ports = set()
    for l in info.get("listen") or []:
        p = l.get("port")
        if p in _RISKY_PORTS and p not in seen_ports:
            seen_ports.add(p)
            add("low" if p in (135, 139, 445) else "medium", f"Port {p} ({_RISKY_PORTS[p]}) is open to the network",
                f"Process: {l.get('proc')}", "Close it in the firewall if you don't need it.")

    # processes & startup items running from unusual places
    sus = {}
    for pr in psutil.process_iter(["name", "exe"]):
        exe = (pr.info.get("exe") or "")
        if exe and any(s in exe.lower() for s in _SUSPICIOUS_DIRS):
            sus[exe.lower()] = (pr.info["name"], exe)
    for s in info.get("startup") or []:
        cmd = (s.get("cmd") or "").strip('"')
        exe = cmd.split('"')[0].split(" -")[0].strip()
        if exe.lower().endswith(".exe") and any(x in exe.lower() for x in _SUSPICIOUS_DIRS + ("\\appdata\\roaming\\",)):
            sus.setdefault(exe.lower(), (s.get("name"), exe))
    sigs = _signatures([v[1] for v in list(sus.values())[:25] if os.path.exists(v[1])])
    for key, (name, exe) in sus.items():
        sg = sigs.get(key, {})
        if sg.get("st") == "Valid":
            continue
        add("medium", f"Unsigned program running from an unusual folder: {name}", exe,
            "If you don't recognise it, end it in Task Manager and scan it with Defender.")

    # health
    for part in psutil.disk_partitions():
        try:
            u = psutil.disk_usage(part.mountpoint)
            if u.percent > 90:
                add("medium", f"Drive {part.device} is {u.percent:.0f}% full", f"{u.free / 1e9:.1f} GB free",
                    "Say 'Miles, clean temp files' or empty the recycle bin.")
        except Exception:
            pass
    mem = psutil.virtual_memory()
    if mem.percent > 88:
        add("low", f"Memory usage is high ({mem.percent:.0f}%)", "", "Close apps you aren't using.")
    up_days = (time.time() - psutil.boot_time()) / 86400
    if up_days > 10:
        add("low", f"PC hasn't restarted in {up_days:.0f} days", "", "A restart applies updates and frees memory.")

    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    findings.sort(key=lambda f: order[f["sev"]])
    result = {"findings": findings, "info": info, "seconds": round(time.time() - t0, 1),
              "time": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
    result["report"] = str(_write_report(result))
    return result


def summary(result: dict) -> str:
    f = [x for x in result["findings"] if x["sev"] != "info"]
    high = [x for x in f if x["sev"] == "high"]
    med = [x for x in f if x["sev"] == "medium"]
    clean = "No active threats found, Defender and the firewall are running."
    if not f:
        return f"Scan complete. {clean} Your system looks clean."
    if not high and not med:
        return (f"Scan complete. {clean} Just {len(f)} minor note{'s' if len(f) > 1 else ''}, "
                f"such as: {f[0]['title']}. Details are in the report on your screen.")
    parts = [f"Scan complete. I found {len(high)} serious and {len(med)} moderate issue{'s' if len(med) != 1 else ''}."]
    for x in (high + med)[:4]:
        parts.append(f"{x['title']}. {x['fix']}")
    parts.append("The full report is on your screen.")
    return " ".join(parts)


def _write_report(r: dict) -> Path:
    colors = {"high": "#ff5252", "medium": "#ffb74d", "low": "#4fc3f7", "info": "#90a4ae"}
    rows = "".join(
        f"<div class='f'><span class='b' style='background:{colors[x['sev']]}'>{x['sev'].upper()}</span>"
        f"<div><b>{html.escape(x['title'])}</b><div class='d'>{html.escape(x['detail'])}</div>"
        f"<div class='x'>{html.escape(x['fix'])}</div></div></div>" for x in r["findings"]) or \
        "<div class='f'><b>No problems found. System looks clean.</b></div>"
    d = r["info"].get("defender") or {}
    page = f"""<!doctype html><meta charset=utf-8><title>Miles Security Report</title>
<style>body{{background:#060b10;color:#cfe8f5;font:15px Segoe UI,sans-serif;max-width:860px;margin:40px auto;padding:0 16px}}
h1{{color:#00e5ff;letter-spacing:.2em;font:600 22px Consolas,monospace}}.s{{color:#6b8a99;font-family:Consolas}}
.f{{display:flex;gap:14px;padding:14px;border:1px solid #11394a;border-radius:10px;margin:10px 0;background:#0a141c}}
.b{{font:700 11px Consolas;padding:3px 8px;border-radius:4px;color:#000;height:fit-content}}.d{{color:#7fa3b5;font-size:13px}}
.x{{color:#00e5ff;font-size:13px;margin-top:4px}}.g{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:18px 0}}
.k{{border:1px solid #11394a;border-radius:10px;padding:12px;background:#0a141c}}.k b{{display:block;font-size:20px;color:#00e5ff}}</style>
<h1>MILES // SECURITY REPORT</h1><div class=s>{r['time']} · scan took {r['seconds']}s</div>
<div class=g><div class=k><b>{'ON' if d.get('realtime') else 'OFF'}</b>Real-time protection</div>
<div class=k><b>{d.get('sigAge', '?')}d</b>Definitions age</div>
<div class=k><b>{len(r['info'].get('threats') or [])}</b>Threats in history</div>
<div class=k><b>{len([x for x in r['findings'] if x['sev'] in ('high', 'medium')])}</b>Issues to fix</div></div>
{rows}"""
    path = DATA / "security_report.html"
    path.write_text(page, encoding="utf-8")
    return path


def open_report(path):
    webbrowser.open(Path(path).as_uri())


_MPCMD = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Windows Defender" / "MpCmdRun.exe"


def virus_scan(kind: str, announce):
    """Start a Defender scan in the background and announce when finished."""
    if not _MPCMD.exists():
        return "Microsoft Defender's scanner isn't available on this PC."
    scan_type = "2" if kind == "full" else "1"

    def run():
        t0 = time.time()
        try:
            p = subprocess.run([str(_MPCMD), "-Scan", "-ScanType", scan_type], capture_output=True,
                               creationflags=NO_WINDOW, timeout=6 * 3600)
            out = p.stdout.decode("utf-8", "replace")
            mins = (time.time() - t0) / 60
            if "found no threats" in out.lower() or p.returncode == 0:
                announce(f"The {kind} virus scan finished after {mins:.0f} minutes. No threats found.")
            else:
                announce(f"The {kind} virus scan finished and found possible threats. Please check Windows Security.")
        except Exception as e:
            announce(f"The virus scan failed: {e}")
    threading.Thread(target=run, daemon=True).start()
    return f"{kind.capitalize()} virus scan started in the background. I'll tell you when it's done."


def update_definitions():
    if not _MPCMD.exists():
        return "Defender isn't available."
    p = subprocess.run([str(_MPCMD), "-SignatureUpdate"], capture_output=True, creationflags=NO_WINDOW, timeout=600)
    return "Virus definitions updated." if p.returncode == 0 else "Couldn't update definitions (may need admin or internet)."
