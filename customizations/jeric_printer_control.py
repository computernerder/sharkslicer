# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Jeric Printer Control"
# description = "Personal Klipper dashboard, preheat, filament safety, macros, and maintenance controls."
# author = "Jeric Dante"
# version = "2.1.0"
# ///

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import orca


DEFAULT_CONFIG = {
    "prusa_url": "http://prusa.lan",
    "voron_url": "http://voron.lan",
    "confirm_before_heating": True,
    "cooldown_timeout_minutes": 15,
    "request_timeout_seconds": 5,
}

MATERIALS = {
    "PLA": (215, 60),
    "PLAPLUS": (220, 60),
    "PETG": (240, 80),
    "ABS": (260, 105),
    "ASA": (260, 105),
    "TPU": (225, 55),
}


def _config(capability):
    config = dict(DEFAULT_CONFIG)
    try:
        stored = json.loads(capability.get_config() or "{}")
        if isinstance(stored, dict):
            config.update(stored)
    except (TypeError, ValueError):
        pass
    return config


def _bundle_value(key):
    try:
        return orca.host.preset_bundle().full_config_value(key)
    except (RuntimeError, TypeError, ValueError):
        return None


def _first(value):
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return value


def _number(value):
    try:
        return int(round(float(_first(value))))
    except (TypeError, ValueError):
        return None


def _material(value):
    text = str(_first(value) or "").upper().replace(" ", "").replace("_", "")
    if "PLA+" in text or "PLAPLUS" in text:
        return "PLAPLUS"
    for name in ("PETG", "TPU", "ASA", "ABS", "PLA"):
        if name in text:
            return name
    return text or "UNKNOWN"


def _active_material():
    return _material(_bundle_value("filament_type"))


def _active_printer(config):
    """Read the edited printer preset every time so an open panel follows it."""
    name = ""
    host = ""
    try:
        preset = orca.host.preset_bundle().current_printer_preset()
        name = str(getattr(preset, "name", "") or "").strip()
        host = str(_first(preset.config_value("print_host")) or "").strip()
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass

    if not name:
        name = str(_first(_bundle_value("printer_settings_id")) or "Active printer").strip()
    if not host:
        host = str(_first(_bundle_value("print_host")) or "").strip()
    if host:
        if not host.startswith(("http://", "https://")):
            host = "http://" + host
        url = host.rstrip("/")
    else:
        url = str(config["voron_url"] if "voron" in name.lower() else config["prusa_url"]).rstrip("/")
    return name, url


def _active_url(config):
    return _active_printer(config)[1]


def _request(config, path, payload=None, base_url=None):
    url = (base_url or _active_url(config)) + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if data is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=float(config["request_timeout_seconds"])) as response:
        return json.loads(response.read() or b"{}")


def _gcode(config, script):
    return _request(config, "/printer/gcode/script", {"script": script})


def _status(config):
    printer, base_url = _active_printer(config)
    query = (
        "/printer/objects/query?heater_bed&extruder&print_stats&idle_timeout"
        "&toolhead&save_variables&virtual_sdcard"
    )
    body = _request(config, query, base_url=base_url)
    status = body.get("result", {}).get("status", {})
    extruder = status.get("extruder", {})
    bed = status.get("heater_bed", {})
    stats = status.get("print_stats", {})
    saved = status.get("save_variables", {}).get("variables", {})
    return {
        "url": base_url,
        "printer": printer,
        "printer_key": f"{printer}|{base_url}",
        "state": stats.get("state") or status.get("idle_timeout", {}).get("state", "unknown"),
        "filename": stats.get("filename", ""),
        "progress": round(float(status.get("virtual_sdcard", {}).get("progress", 0)) * 100, 1),
        "nozzle": round(float(extruder.get("temperature", 0)), 1),
        "nozzle_target": round(float(extruder.get("target", 0)), 1),
        "bed": round(float(bed.get("temperature", 0)), 1),
        "bed_target": round(float(bed.get("target", 0)), 1),
        "homed": status.get("toolhead", {}).get("homed_axes", ""),
        "loaded": _material(saved.get("filament_loaded", "UNKNOWN")),
        "selected": _active_material(),
        "revo_position": str(saved.get("revo_filament_position", "NOT TRACKED")).upper(),
    }


def _active_temperatures():
    nozzle = None
    bed = None
    for key in ("nozzle_temperature_initial_layer", "first_layer_temperature"):
        nozzle = _number(_bundle_value(key))
        if nozzle and nozzle > 0:
            break
    for key in (
        "bed_temperature_initial_layer_single",
        "first_layer_bed_temperature",
        "hot_plate_temp_initial_layer",
        "textured_plate_temp_initial_layer",
        "eng_plate_temp_initial_layer",
        "cool_plate_temp_initial_layer",
        "supertack_plate_temp_initial_layer",
    ):
        bed = _number(_bundle_value(key))
        if bed and bed > 0:
            break
    return nozzle, bed


def _result_error(prefix, error):
    return orca.ExecutionResult.failure(
        orca.PluginResult.RecoverableError, f"{prefix}: {error}"
    )


def _confirm(text, title="Jeric Dante Slicer"):
    return orca.host.ui.message(text, title=title, buttons="yes_no", icon="question") == "yes"


class JericCapability(orca.script.ScriptPluginCapabilityBase):
    def get_default_config(self):
        return dict(DEFAULT_CONFIG)

    def cfg(self):
        return _config(self)


class PreheatActive(JericCapability):
    cooldown_timer = None

    def get_name(self):
        return "Preheat from active filament"

    def execute(self):
        nozzle, bed = _active_temperatures()
        if not nozzle or not bed or not (0 < nozzle <= 320 and 0 < bed <= 130):
            return _result_error("Preheat unavailable", "active first-layer temperatures are missing or unsafe")
        config = self.cfg()
        if config["confirm_before_heating"] and not _confirm(
            f"Preheat {_active_url(config)}?\n\nNozzle: {nozzle} C\nBed: {bed} C"
        ):
            return orca.ExecutionResult.skipped("Preheat cancelled.")
        try:
            _gcode(config, f"M104 S{nozzle}\nM140 S{bed}")
            _arm_cooldown(config)
            return orca.ExecutionResult.success(
                f"Preheating {nozzle} C / {bed} C. Safety cooldown armed."
            )
        except (urllib.error.URLError, OSError, ValueError) as error:
            return _result_error("Moonraker preheat failed", error)


class CancelPreheat(JericCapability):
    def get_name(self):
        return "Cancel preheat"

    def execute(self):
        try:
            _gcode(self.cfg(), "TURN_OFF_HEATERS")
            _cancel_cooldown()
            return orca.ExecutionResult.success("Nozzle and bed heaters turned off.")
        except (urllib.error.URLError, OSError, ValueError) as error:
            return _result_error("Cooldown failed", error)


class MaterialPreheat(JericCapability):
    material = "PLA"

    def get_name(self):
        return f"Preheat {self.material}"

    def execute(self):
        nozzle, bed = MATERIALS[self.material]
        config = self.cfg()
        if not _confirm(f"Preheat {_active_url(config)} for {self.material}?\n\nNozzle: {nozzle} C\nBed: {bed} C"):
            return orca.ExecutionResult.skipped("Preheat cancelled.")
        try:
            _gcode(config, f"M104 S{nozzle}\nM140 S{bed}")
            _arm_cooldown(config)
            return orca.ExecutionResult.success(f"{self.material} preheat started.")
        except (urllib.error.URLError, OSError, ValueError) as error:
            return _result_error("Moonraker preheat failed", error)


def _material_class(name):
    return type(f"Preheat{name}", (MaterialPreheat,), {"material": name})


PreheatPLA = _material_class("PLA")
PreheatPETG = _material_class("PETG")
PreheatABS = _material_class("ABS")
PreheatASA = _material_class("ASA")
PreheatTPU = _material_class("TPU")


class MacroCapability(JericCapability):
    label = "Macro"
    script = ""
    confirm = False

    def get_name(self):
        return self.label

    def execute(self):
        config = self.cfg()
        if self.confirm and not _confirm(f"Run on {_active_url(config)}?\n\n{self.script}"):
            return orca.ExecutionResult.skipped("Command cancelled.")
        try:
            _gcode(config, self.script)
            return orca.ExecutionResult.success(f"Sent: {self.script}")
        except (urllib.error.URLError, OSError, ValueError) as error:
            return _result_error(f"{self.label} failed", error)


class HomePrinter(MacroCapability):
    label, script, confirm = "Home printer", "G28", True


class BedMesh(MacroCapability):
    label, script, confirm = "Run bed mesh", "G29", True


class SafeCancel(MacroCapability):
    label, script, confirm = "Safe cancel print", "CANCEL_PRINT", True


class EmergencyStop(JericCapability):
    def get_name(self):
        return "Emergency stop"

    def execute(self):
        config = self.cfg()
        if not _confirm(
            "EMERGENCY STOP immediately disables the printer.\n\n"
            "The Revo cannot retract and its position may need recovery on the next print.\n\n"
            "Use Safe cancel unless there is an immediate hazard. Continue?",
            title="Emergency stop",
        ):
            return orca.ExecutionResult.skipped("Emergency stop cancelled.")
        try:
            _request(config, "/printer/emergency_stop", {})
            return orca.ExecutionResult.success("Emergency stop sent.")
        except (urllib.error.URLError, OSError, ValueError) as error:
            return _result_error("Emergency stop failed", error)


class LoadFilament(JericCapability):
    def get_name(self):
        return "Load filament"

    def execute(self):
        material = _active_material()
        if material not in ("PLA", "PLAPLUS", "PETG", "TPU", "ABS", "ASA"):
            return _result_error("Load filament", f"unsupported active material {material}")
        script = f"LOAD_FILAMENT TYPE={material}"
        config = self.cfg()
        if not _confirm(f"Load {material} on {_active_url(config)}?"):
            return orca.ExecutionResult.skipped("Load cancelled.")
        try:
            _gcode(config, script)
            return orca.ExecutionResult.success(f"Loading {material}; Klipper will remember it.")
        except (urllib.error.URLError, OSError, ValueError) as error:
            return _result_error("Load filament failed", error)


class UnloadFilament(MacroCapability):
    label, script, confirm = "Unload filament", "UNLOAD_FILAMENT", True


class FilamentStatus(MacroCapability):
    label, script = "Filament status", "FILAMENT_STATUS"


class SetActiveFilament(JericCapability):
    def get_name(self):
        return "Set loaded filament from active profile"

    def execute(self):
        material = _active_material()
        if material not in ("PLA", "PLAPLUS", "TPU", "ABS", "ASA"):
            return _result_error("Filament not set", f"unsupported active material {material}")
        try:
            _gcode(self.cfg(), f"SET_FILAMENT TYPE={material}")
            return orca.ExecutionResult.success(f"Klipper loaded filament set to {material}.")
        except (urllib.error.URLError, OSError, ValueError) as error:
            return _result_error("SET_FILAMENT failed", error)


class PrinterPreflight(JericCapability):
    def get_name(self):
        return "Printer preflight"

    def execute(self):
        try:
            state = _status(self.cfg())
            if state["state"] in ("printing", "paused"):
                return _result_error("Preflight blocked", f"printer is {state['state']}")
            if state["loaded"] != "UNKNOWN" and state["selected"] != "UNKNOWN" and state["loaded"] != state["selected"]:
                return _result_error(
                    "Filament mismatch",
                    f"Orca selected {state['selected']}, but Klipper says {state['loaded']} is loaded",
                )
            return orca.ExecutionResult.success(
                f"Ready: {state['printer']}; filament {state['selected']}; nozzle {state['nozzle']} C; bed {state['bed']} C."
            )
        except (urllib.error.URLError, OSError, ValueError) as error:
            return _result_error("Preflight connection failed", error)


_cooldown_lock = threading.Lock()
_cooldown_timer = None


def _cancel_cooldown():
    global _cooldown_timer
    with _cooldown_lock:
        if _cooldown_timer is not None:
            _cooldown_timer.cancel()
            _cooldown_timer = None


def _cooldown_if_idle(config):
    global _cooldown_timer
    try:
        if _status(config)["state"] not in ("printing", "paused"):
            _gcode(config, "TURN_OFF_HEATERS")
    finally:
        with _cooldown_lock:
            _cooldown_timer = None


def _arm_cooldown(config):
    global _cooldown_timer
    _cancel_cooldown()
    delay = max(1, int(config["cooldown_timeout_minutes"])) * 60
    with _cooldown_lock:
        _cooldown_timer = threading.Timer(delay, _cooldown_if_idle, args=(dict(config),))
        _cooldown_timer.daemon = True
        _cooldown_timer.start()


class ArmCooldown(JericCapability):
    def get_name(self):
        return "Arm safety cooldown"

    def execute(self):
        config = self.cfg()
        _arm_cooldown(config)
        return orca.ExecutionResult.success(
            f"Heaters will turn off after {config['cooldown_timeout_minutes']} idle minutes."
        )


DASHBOARD = r"""
<style>
  :root { color-scheme: dark; }
  body { margin:0; padding:14px; background:#0b0b0d; color:#f4f4f4; font:13px sans-serif; }
  h2 { margin:0; color:#ff3344; } h3 { margin:16px 0 7px; color:#fff; }
  .muted { color:#aaa; } .card { background:#16161a; border:1px solid #34343a; border-left:4px solid #d71920; border-radius:7px; padding:11px; margin-top:10px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:7px; }
  button { background:#d71920; color:white; border:1px solid #ff4650; border-radius:5px; padding:8px; cursor:pointer; }
  button.secondary { background:#25252b; border-color:#555; } button.danger { background:#680b10; }
  input { box-sizing:border-box; width:100%; background:#09090b; color:white; border:1px solid #555; border-radius:4px; padding:7px; }
  .temps { font-size:20px; font-weight:bold; display:flex; gap:20px; margin:8px 0; }
  .ok { color:#57d77b; } .warn { color:#ffcc55; } .bad { color:#ff5360; }
  #macros { max-height:260px; overflow:auto; margin-top:7px; }
  .macro { display:grid; grid-template-columns:1fr auto; gap:6px; margin:5px 0; }
</style>
<h2>Jeric Dante Printer Control</h2><div class="muted" id="printer">Connecting…</div>
<div class="card"><div id="state">Waiting for Moonraker…</div><div class="temps"><span id="nozzle">Nozzle —</span><span id="bed">Bed —</span></div><div id="filament"></div><div id="revo"></div><div id="file" class="muted"></div></div>
<h3>Quick actions</h3><div class="grid">
 <button data-action="preheat_active">Preheat active</button><button class="danger" data-action="cooldown">Heaters off</button>
 <button class="danger" data-action="safe_cancel">Safe cancel</button><button class="danger" data-action="emergency_stop">EMERGENCY STOP</button>
 <button data-action="set_filament">Set active filament</button><button class="secondary" data-action="filament_status">Filament status</button>
 <button class="secondary" data-action="load">Load</button><button class="secondary" data-action="unload">Unload</button>
 <button class="secondary" data-action="home">Home</button><button class="secondary" data-action="mesh">Bed mesh</button>
 <button class="secondary" data-action="park">Park</button><button class="secondary" data-action="preflight">Preflight</button>
</div>
<h3>Klipper macros</h3><input id="filter" placeholder="Search macros"><input id="params" placeholder="Optional parameters, e.g. TYPE=ABS" style="margin-top:6px"><div id="macros"></div>
<div class="muted" id="message" style="margin-top:10px"></div>
<script>
(function(){
 const $=id=>document.getElementById(id); let all=[], activePrinter='';
 function esc(v){const s=document.createElement('span');s.textContent=String(v);return s.innerHTML;}
 function renderMacros(){const q=$('filter').value.toLowerCase();$('macros').innerHTML=all.filter(x=>x.toLowerCase().includes(q)).map(x=>'<div class="macro"><span>'+esc(x)+'</span><button class="secondary" data-macro="'+esc(x)+'">Run</button></div>').join(''); document.querySelectorAll('[data-macro]').forEach(b=>b.onclick=()=>orca.postMessage({command:'macro',name:b.dataset.macro,params:$('params').value}));}
 orca.onMessage(m=>{if(!m)return;if(m.command==='status'){if(m.error){$('state').textContent=m.error;return;}const d=m.data;const changed=activePrinter&&activePrinter!==d.printer_key;activePrinter=d.printer_key;if(changed){all=[];renderMacros();$('message').textContent='Printer changed — refreshing macros…';orca.postMessage({command:'macros'});}$('printer').textContent=d.printer+' · '+d.url;$('state').textContent=String(d.state).toUpperCase()+(d.progress?' · '+d.progress+'%':'');$('nozzle').textContent='Nozzle '+d.nozzle+' / '+d.nozzle_target+' °C';$('bed').textContent='Bed '+d.bed+' / '+d.bed_target+' °C';$('filament').innerHTML='Selected: <b>'+esc(d.selected)+'</b> · Loaded: <b>'+esc(d.loaded)+'</b>';$('filament').className=(d.loaded!=='UNKNOWN'&&d.selected!==d.loaded)?'bad':'ok';$('revo').innerHTML='Revo position: <b>'+esc(d.revo_position)+'</b>';$('revo').className=(d.revo_position==='UNKNOWN')?'warn':'muted';$('file').textContent=d.filename||'';}else if(m.command==='macros'){if(m.printer_key&&activePrinter&&m.printer_key!==activePrinter)return;all=m.items||[];renderMacros();$('message').textContent='';}else if(m.command==='result'){$('message').textContent=m.message||m.error||'';orca.postMessage({command:'refresh'});}});
 document.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>orca.postMessage({command:'action',action:b.dataset.action}));$('filter').oninput=renderMacros;
 orca.postMessage({command:'refresh'});orca.postMessage({command:'macros'});setInterval(()=>orca.postMessage({command:'refresh'}),2000);
})();
</script>
"""


class PrinterDashboard(JericCapability):
    panel = None

    def get_name(self):
        return "Open printer dashboard"

    def execute(self):
        if self.panel is not None and self.panel.is_open():
            self.panel.show()
            return orca.ExecutionResult.success("Printer dashboard is already open.")
        self.panel = orca.host.ui.create_dock_panel(
            html=DASHBOARD,
            title="Jeric Dante Printer Control",
            width=390,
            height=700,
            dock="right",
            on_message=self.on_message,
            on_close=self.on_close,
        )
        return orca.ExecutionResult.success("Printer dashboard opened.")

    def on_close(self):
        self.panel = None

    def post(self, payload):
        if self.panel is not None and self.panel.is_open():
            self.panel.post(payload)

    def on_message(self, message):
        message = message or {}
        config = self.cfg()
        try:
            if message.get("command") == "refresh":
                self.post({"command": "status", "data": _status(config)})
            elif message.get("command") == "macros":
                printer, base_url = _active_printer(config)
                body = _request(config, "/printer/objects/list", base_url=base_url)
                objects = body.get("result", {}).get("objects", [])
                macros = sorted(x[12:] for x in objects if x.startswith("gcode_macro ") and not x[12:].startswith("_"))
                self.post({"command": "macros", "items": macros, "printer_key": f"{printer}|{base_url}"})
            elif message.get("command") == "macro":
                name = str(message.get("name", "")).strip()
                params = str(message.get("params", "")).strip()
                if not name or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for c in name):
                    raise ValueError("Invalid macro name")
                script = name + (" " + params if params else "")
                if _confirm(f"Run macro on {_active_url(config)}?\n\n{script}"):
                    _gcode(config, script)
                    self.post({"command": "result", "message": f"Sent {script}"})
            elif message.get("command") == "action":
                self.run_action(str(message.get("action", "")), config)
        except Exception as error:
            self.post({"command": "result", "error": str(error)})

    def run_action(self, action, config):
        scripts = {
            "cooldown": "TURN_OFF_HEATERS",
            "safe_cancel": "CANCEL_PRINT",
            "set_filament": f"SET_FILAMENT TYPE={_active_material()}",
            "filament_status": "FILAMENT_STATUS",
            "load": f"LOAD_FILAMENT TYPE={_active_material()}",
            "unload": "UNLOAD_FILAMENT",
            "home": "G28",
            "mesh": "G29",
        }
        if action == "preheat_active":
            nozzle, bed = _active_temperatures()
            if not nozzle or not bed:
                raise ValueError("Active first-layer temperatures are unavailable")
            if _confirm(f"Preheat?\n\nNozzle: {nozzle} C\nBed: {bed} C"):
                _gcode(config, f"M104 S{nozzle}\nM140 S{bed}")
                _arm_cooldown(config)
                self.post({"command": "result", "message": "Preheat started; safety cooldown armed."})
            return
        if action == "emergency_stop":
            if _confirm(
                "EMERGENCY STOP immediately disables the printer. The Revo cannot retract.\n\n"
                "Use Safe cancel unless there is an immediate hazard. Continue?",
                title="Emergency stop",
            ):
                _request(config, "/printer/emergency_stop", {})
                self.post({"command": "result", "message": "Emergency stop sent."})
            return
        if action == "preflight":
            state = _status(config)
            mismatch = state["loaded"] != "UNKNOWN" and state["selected"] != state["loaded"]
            self.post({"command": "result", "message": "FILAMENT MISMATCH" if mismatch else "Preflight passed."})
            return
        if action == "park":
            state = _status(config)
            if "xyz" not in state["homed"]:
                raise ValueError("Printer must be homed before parking")
            body = _request(config, "/printer/objects/query?toolhead&gcode_move")
            raw = body.get("result", {}).get("status", {})
            position = raw.get("gcode_move", {}).get("gcode_position", [0, 0, 0, 0])
            axis_max = raw.get("toolhead", {}).get("axis_maximum", [180, 180, 180, 0])
            z = min(float(position[2]) + 10, float(axis_max[2]) - 1)
            scripts[action] = f"G90\nG1 Z{z:.1f} F900\nG1 X{float(axis_max[0])/2:.1f} Y{float(axis_max[1])-5:.1f} F6000"
        script = scripts.get(action)
        if not script:
            raise ValueError(f"Unknown action {action}")
        moving = action in ("safe_cancel", "load", "unload", "home", "mesh", "park")
        if not moving or _confirm(f"Run on {_active_url(config)}?\n\n{script}"):
            _gcode(config, script)
            if action == "cooldown":
                _cancel_cooldown()
            self.post({"command": "result", "message": f"Sent {script}"})


@orca.plugin
class JericPrinterControlPlugin(orca.base):
    def register_capabilities(self):
        for capability in (
            PrinterDashboard,
            PrinterPreflight,
            PreheatActive,
            PreheatPLA,
            PreheatPETG,
            PreheatABS,
            PreheatASA,
            PreheatTPU,
            CancelPreheat,
            ArmCooldown,
            SetActiveFilament,
            FilamentStatus,
            LoadFilament,
            UnloadFilament,
            HomePrinter,
            BedMesh,
            SafeCancel,
            EmergencyStop,
        ):
            orca.register_capability(capability)
