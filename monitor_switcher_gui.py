"""
ASUS Monitor 輸入源切換器 - GUI 版 (跨平台: Windows / Linux / macOS)
====================================================================
Windows / Linux 需要:
    pip install --user monitorcontrol

macOS (Apple Silicon, M1/M2/...) 需要:
    brew install m1ddc
    (macOS 唔使 pip 裝 monitorcontrol；如果喺呢個 project folder 內執行，
     會自動借用 monitorcontrol 嘅 get_input_name 去顯示輸入源名，
     借唔到就用內置對照表，一樣顯示到。)

執行:
    python3 monitor_switcher_gui.py
    (Windows 想無黑色 console 窗，可將 file 改名做 .pyw 再用 pythonw.exe 開)

macOS 注意事項:
  - m1ddc 只支援 Apple Silicon，唔支援 Intel Mac，亦唔支援 Mac 機身
    內置 HDMI 及內置螢幕 (淨係外接顯示器 via USB-C / DisplayPort / HDMI)。
  - m1ddc 冇官方「讀取目前 input」功能；本工具會嘗試讀，讀唔到就顯示「—」，
    但「切換 (set input)」係正常支援嘅。
"""

import os
import re
import sys
import json
import shutil
import platform
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox

# ------------------------------------------------------------------
#  輸入源名稱 (純 Python，任何 OS 都行，唔掂硬件)
# ------------------------------------------------------------------
try:
    # monitorcontrol.get_input_name 只係 enum 查表，跨平台可用 (macOS 都 import 到)
    from monitorcontrol import get_input_name as _mc_input_name
except Exception:
    # 借唔到就用內置對照表 (標準 DDC/CI input 代碼)
    _STD_INPUTS = {
        0x01: "ANALOG1", 0x03: "DVI1", 0x04: "DVI2",
        0x0F: "DP1", 0x10: "DP2",
        0x11: "HDMI1", 0x12: "HDMI2",
    }

    def _mc_input_name(code):
        return _STD_INPUTS.get(code, f"UNKNOWN (code {code:#04x})")

# m1ddc / 部分 USB-C 顯示器用嘅額外代碼，補返個易睇嘅名
_EXTRA_INPUTS = {27: "USB-C", 0x1B: "USB-C"}


def input_label(code):
    """將 input 代碼變成人睇得明嘅名，例如 17 -> 'HDMI1 (17)'。"""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return str(code)
    name = _EXTRA_INPUTS.get(code) or _mc_input_name(code)
    return f"{name} ({code})"


CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".monitor_switcher_config.json")

DEFAULT_CONFIG = {
    "pc_code": 18,   # PC  = HDMI-2
    "mac_code": 15,  # Mac = DisplayPort (經 Belkin dock)
}

ALL_TARGET = "__ALL__"  # 代表「全部顯示器」


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {**DEFAULT_CONFIG, **data}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ==================================================================
#  平台後端 (backend): 收埋 Windows/Linux vs macOS 嘅差異
#  每個後端提供:
#    available() -> (bool, str)      # 有冇裝好，未裝好就講點裝
#    snapshot()  -> [ {label,target,input}, ... ]   # 偵測
#    set_input(target, code)         # 切換，失敗會 raise
# ==================================================================
class Backend:
    display_name = "?"

    def available(self):
        return True, ""

    def snapshot(self):
        raise NotImplementedError

    def set_input(self, target, code):
        raise NotImplementedError


class MonitorControlBackend(Backend):
    """Windows / Linux — 用 monitorcontrol library。"""

    display_name = "monitorcontrol"

    def available(self):
        try:
            from monitorcontrol import get_monitors  # noqa: F401
        except ImportError:
            return False, "請先安裝: pip install --user monitorcontrol"
        return True, ""

    @staticmethod
    def _monitors():
        from monitorcontrol import get_monitors
        return get_monitors()

    def snapshot(self):
        out = []
        for i, mon in enumerate(self._monitors()):
            model = None
            inp = None
            try:
                with mon:
                    try:
                        caps = mon.get_vcp_capabilities()
                        model = (caps.get("model") or "").strip() or None
                    except Exception:
                        pass
                    try:
                        inp = mon.get_input_source()
                    except Exception:
                        pass
            except Exception:
                pass
            label = f"Monitor {i}" + (f" — {model}" if model else "")
            out.append({"label": label, "target": i, "input": inp})
        return out

    def set_input(self, target, code):
        mons = self._monitors()
        idxs = range(len(mons)) if target in (None, ALL_TARGET) else [int(target)]
        for i in idxs:
            with mons[i] as mon:
                mon.set_input_source(code)


class M1ddcBackend(Backend):
    """macOS (Apple Silicon) — shell out 去 m1ddc。"""

    display_name = "m1ddc"

    def __init__(self):
        self.bin = shutil.which("m1ddc")
        if not self.bin:
            for p in ("/opt/homebrew/bin/m1ddc", "/usr/local/bin/m1ddc"):
                if os.path.exists(p):
                    self.bin = p
                    break

    def available(self):
        if not self.bin:
            hint = "搵唔到 m1ddc，請先安裝:\n    brew install m1ddc"
            if platform.machine() != "arm64":
                hint += ("\n(注意: m1ddc 只支援 Apple Silicon；"
                         "Intel Mac 請改用 BetterDisplay 或 ddcctl)")
            return False, hint
        return True, ""

    def _run(self, args):
        return subprocess.run(
            [self.bin] + args,
            capture_output=True, text=True, timeout=15,
        )

    def _list_displays(self):
        """回傳 [(target_str, name), ...]，解析唔到就 []。"""
        displays = []
        try:
            r = self._run(["display", "list"])
            for line in r.stdout.splitlines():
                m = re.match(r"\s*\[(\d+)\]\s*(.+)", line)  # 格式: [1] Name (UUID)
                if not m:
                    continue
                idx = m.group(1)
                rest = m.group(2).strip()
                name = re.sub(r"\s*\([^()]*\)\s*$", "", rest).strip() or rest
                displays.append((idx, name))
        except Exception:
            pass
        return displays

    def _read_input(self, target):
        # m1ddc 冇正式 get input；試吓，失敗就 None
        args = (["display", str(target)] if target not in (None, ALL_TARGET) else [])
        args += ["get", "input"]
        try:
            r = self._run(args)
            if r.returncode == 0:
                return int(r.stdout.strip())
        except Exception:
            pass
        return None

    def snapshot(self):
        # 注意: m1ddc 冇官方 get input，而經某啲 dock 讀返嘅值係垃圾 (實測全 -66)，
        # 所以 macOS 側一律唔顯示「目前 input」(顯示 —)。切換 (set input) 唔需要讀取，照 work。
        displays = self._list_displays()
        if not displays:
            # 解析唔到就當一部「預設外接顯示器」
            return [{"label": "預設外接顯示器 (default)", "target": None, "input": None}]
        return [{"label": f"[{idx}] {name}", "target": idx, "input": None}
                for idx, name in displays]

    def set_input(self, target, code):
        if target in (None, ALL_TARGET):
            displays = self._list_displays()
            targets = [idx for idx, _ in displays] or [None]
        else:
            targets = [target]

        ok = False
        errs = []
        for t in targets:
            args = (["display", str(t)] if t not in (None, ALL_TARGET) else [])
            args += ["set", "input", str(code)]
            r = self._run(args)
            if r.returncode == 0:
                ok = True
            else:
                msg = (r.stderr or r.stdout).strip()
                if msg:
                    errs.append(msg)
        if not ok:
            raise RuntimeError("; ".join(errs) or "m1ddc set input 失敗")


def make_backend():
    if sys.platform == "darwin":
        return M1ddcBackend()
    return MonitorControlBackend()  # win32 / linux


BACKEND = make_backend()


# ==================================================================
#  GUI
# ==================================================================
class MonitorSwitcherApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ASUS Monitor 輸入源切換器")
        self.geometry("500x520")
        self.resizable(False, False)

        self.config_data = load_config()
        self._target_map = {"全部 (all)": ALL_TARGET}  # combobox label -> target
        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        title = ttk.Label(self, text="Monitor Input 一鍵切換", font=("Segoe UI", 14, "bold"))
        title.pack(**pad)

        ttk.Label(self, text=f"後端: {BACKEND.display_name}  ({sys.platform})",
                  foreground="gray").pack()

        # --- 1. 偵測 ---
        detect_frame = ttk.LabelFrame(self, text="1. 偵測目前狀態 (Detect)")
        detect_frame.pack(fill="x", **pad)

        self.detect_btn = ttk.Button(detect_frame, text="偵測 / Detect", command=self.on_detect)
        self.detect_btn.pack(side="left", padx=8, pady=8)

        self.detect_output = tk.Text(detect_frame, height=8, width=54, state="disabled", wrap="word")
        self.detect_output.pack(padx=8, pady=8)

        # --- 2. 設定代碼 ---
        config_frame = ttk.LabelFrame(self, text="2. 設定 Input 代碼")
        config_frame.pack(fill="x", **pad)

        ttk.Label(config_frame, text="PC 代碼 (例如 17 或 0x11):").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self.pc_var = tk.StringVar(value=str(self.config_data["pc_code"]))
        ttk.Entry(config_frame, textvariable=self.pc_var, width=12).grid(row=0, column=1, padx=8, pady=4)

        ttk.Label(config_frame, text="Mac 代碼 (例如 18 或 0x12):").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        self.mac_var = tk.StringVar(value=str(self.config_data["mac_code"]))
        ttk.Entry(config_frame, textvariable=self.mac_var, width=12).grid(row=1, column=1, padx=8, pady=4)

        ttk.Button(config_frame, text="儲存設定 / Save", command=self.on_save).grid(
            row=2, column=0, columnspan=2, pady=8
        )

        # --- 3. 一鍵切換 ---
        switch_frame = ttk.LabelFrame(self, text="3. 一鍵切換 (Switch)")
        switch_frame.pack(fill="x", **pad)

        target_row = ttk.Frame(switch_frame)
        target_row.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Label(target_row, text="目標顯示器:").pack(side="left")
        self.target_var = tk.StringVar(value="全部 (all)")
        self.target_combo = ttk.Combobox(
            target_row, textvariable=self.target_var, state="readonly",
            values=list(self._target_map.keys()), width=34,
        )
        self.target_combo.pack(side="left", padx=8)

        btn_row = ttk.Frame(switch_frame)
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="切換到 PC", command=lambda: self.on_switch("pc")).pack(
            side="left", expand=True, fill="x", padx=8, pady=10
        )
        ttk.Button(btn_row, text="切換到 Mac", command=lambda: self.on_switch("mac")).pack(
            side="left", expand=True, fill="x", padx=8, pady=10
        )

        self.status_var = tk.StringVar(value="準備就緒")
        ttk.Label(self, textvariable=self.status_var, foreground="gray").pack(**pad)

    def _parse_code(self, raw):
        raw = raw.strip()
        if raw.lower().startswith("0x"):
            return int(raw, 16)
        return int(raw)

    def _current_target(self):
        return self._target_map.get(self.target_var.get(), ALL_TARGET)

    # -------- 偵測 --------
    def on_detect(self):
        self.status_var.set("偵測緊...")
        self.detect_btn.config(state="disabled")
        threading.Thread(target=self._detect_worker, daemon=True).start()

    def _detect_worker(self):
        lines = []
        target_map = {"全部 (all)": ALL_TARGET}

        ok, msg = BACKEND.available()
        if not ok:
            lines.append(f"[{BACKEND.display_name}] 未就緒:\n{msg}")
            self.after(0, self._detect_done, "\n".join(lines), target_map)
            return

        try:
            snap = BACKEND.snapshot()
            lines.append(f"搵到 {len(snap)} 部顯示器\n")
            for s in snap:
                lines.append(s["label"])
                if s["input"] is None:
                    extra = " (macOS 唔支援讀取)" if sys.platform == "darwin" else ""
                    lines.append(f"  目前 input: —{extra}")
                else:
                    lines.append(f"  目前 input: {input_label(s['input'])}")
                target_map[s["label"]] = s["target"]
        except Exception as e:
            lines.append(f"偵測失敗: {e}")

        self.after(0, self._detect_done, "\n".join(lines), target_map)

    def _detect_done(self, text, target_map):
        self.detect_output.config(state="normal")
        self.detect_output.delete("1.0", "end")
        self.detect_output.insert("1.0", text)
        self.detect_output.config(state="disabled")
        self.detect_btn.config(state="normal")

        self._target_map = target_map
        keep = self.target_var.get()
        self.target_combo["values"] = list(target_map.keys())
        if keep not in target_map:
            self.target_var.set("全部 (all)")
        self.status_var.set("偵測完成")

    # -------- 儲存設定 --------
    def on_save(self):
        try:
            self.config_data["pc_code"] = self._parse_code(self.pc_var.get())
            self.config_data["mac_code"] = self._parse_code(self.mac_var.get())
            save_config(self.config_data)
            self.status_var.set("設定已儲存")
        except ValueError:
            messagebox.showerror("格式錯誤", "代碼要係數字，例如 17 或 0x11")

    # -------- 切換 --------
    def on_switch(self, kind):
        code = self.config_data.get(f"{kind}_code")
        if code is None:
            messagebox.showerror("未設定", "請先響上面填代碼並儲存")
            return

        target = self._current_target()
        self.status_var.set(f"切換緊去 {kind}...")
        threading.Thread(target=self._switch_worker, args=(kind, code, target), daemon=True).start()

    def _switch_worker(self, kind, code, target):
        ok, msg = BACKEND.available()
        if not ok:
            self.after(0, self.status_var.set, f"未就緒: {msg}")
            return
        try:
            BACKEND.set_input(target, code)
            self.after(0, self.status_var.set, f"已切換到 {kind}: {input_label(code)}")
        except Exception as e:
            self.after(0, self.status_var.set, f"切換失敗: {e}")


if __name__ == "__main__":
    app = MonitorSwitcherApp()
    app.mainloop()
