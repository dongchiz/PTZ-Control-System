# ui/main_window.py
# Copyright © 2025 Sharkbia
# MIT License - See LICENSE for details
import json
import os
import queue
import win32api
import win32con
import win32gui
import win32print
import tkinter as tk
from pathlib import Path
from threading import Lock, Thread
import ttkbootstrap as ttkb
from tkinter import messagebox
import serial.tools.list_ports
from ttkbootstrap.constants import *
from core.controller import ControlSystem


class MainWindow:
    def __init__(self):
        # 宽高自适应系统缩放
        scale = self._get_scaling()
        base_width = 380  # 稍微加宽一点以适应新按钮
        base_height = 950
        scaled_width = int(base_width * scale)
        scaled_height = int(base_height * scale)

        self.root = ttkb.Window()
        self.root.title("PTZ 云台控制系统")
        self.root.geometry(f"{scaled_width}x{scaled_height}")
        self.root.attributes('-topmost', True)
        self.root.resizable(False, False)

        # 设置网格行列权重
        self.root.rowconfigure(0, weight=0)
        self.root.rowconfigure(1, weight=1)
        self.root.columnconfigure(0, weight=1)

        self.control_system = None
        self.running = False
        self.config_file = self._get_config_path()
        self.log_queue = queue.Queue()
        self._connection_lock = Lock()
        self.manual_panel_visible = True # 手动面板默认展开

        self._init_config()
        self._init_ui()
        self.root.after(100, self._process_log_queue)

    def _get_config_path(self) -> str:
        if os.name == 'nt':
            config_dir = Path(os.getenv('APPDATA')) / 'PTZ_Controller'
        else:
            config_dir = Path.home() / '.config' / 'PTZ_Controller'
        config_dir.mkdir(parents=True, exist_ok=True)
        return str(config_dir / 'config.json')

    def _get_default_config(self) -> dict:
        return {
            "gs232b": {
                "protocol": "serial",
                "serial": {"port": "COM1", "baudrate": 9600}
            },
            "pelco": {
                "protocol": "serial",
                "serial": {"port": "COM2", "baudrate": 9600},
                "angle_correction": {
                    "min_elevation": 0,
                    "max_elevation": 90,
                    "azimuth_offset": 0,
                    "initial_azimuth": 0
                },
                # --- [新增] 默认软限位 ---
                "limits": {
                    "min_az": -360,
                    "max_az": 360
                }
            },
            "ui": {"topmost": True}
        }

    def _init_config(self):
        if not os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'w') as f:
                    json.dump(self._get_default_config(), f, indent=2)
                self.log("[系统] 已创建默认配置文件")
            except Exception as e:
                messagebox.showerror("错误", f"创建配置文件失败: {str(e)}")

    def _init_ui(self):
        main_frame = ttkb.Frame(self.root)
        main_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        main_frame.columnconfigure(0, weight=1) # 确保列宽充满

        # 设备配置区域
        config_frame = ttkb.Labelframe(main_frame, text="设备配置", bootstyle=INFO)
        config_frame.grid(row=0, column=0, sticky="ew", padx=5, pady=5)
        config_frame.columnconfigure(0, weight=1)

        self._create_device_panel(config_frame, "gs232b", 0)
        self._create_device_panel(config_frame, "pelco", 1)
        self._create_device_panel(config_frame, "AZ/EL", 2)

        # --- [修改] 手动控制区域（可折叠） ---
        self._create_manual_control_panel(main_frame)

        # 控制按钮区域
        btn_frame = ttkb.Frame(main_frame)
        btn_frame.grid(row=2, column=0, sticky="ew", padx=5, pady=5)
        btn_frame.columnconfigure(0, weight=1)
        btn_frame.columnconfigure(5, weight=1)

        self.start_btn = ttkb.Button(btn_frame, text="启动系统", command=self.toggle_system,
                                     bootstyle=(SUCCESS, OUTLINE))
        self.start_btn.grid(row=0, column=1, padx=5)

        clear_btn = ttkb.Button(btn_frame, text="清除日志", command=self.clear_log,
                                bootstyle=(WARNING, OUTLINE))
        clear_btn.grid(row=0, column=2, padx=5)

        save_btn = ttkb.Button(btn_frame, text="保存配置", command=self._save_config,
                               bootstyle=(PRIMARY, OUTLINE))
        save_btn.grid(row=0, column=3, padx=5)

        self.topmost_btn = ttkb.Button(btn_frame, text="窗口置顶", command=self.toggle_topmost,
                                       bootstyle=(PRIMARY, OUTLINE))
        self.topmost_btn.grid(row=0, column=4, padx=5)

        # 初始化置顶状态
        config = self._load_config()
        if config.get('ui', {}).get('topmost', True):
            self.root.attributes('-topmost', True)
            self.topmost_btn.config(bootstyle=(PRIMARY, OUTLINE))
        else:
            self.root.attributes('-topmost', False)
            self.topmost_btn.config(bootstyle=(SECONDARY, OUTLINE))

        # 日志区域
        log_frame = ttkb.Labelframe(self.root, text="系统日志", bootstyle=INFO)
        log_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.root.rowconfigure(1, weight=1)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log_area = tk.Text(log_frame, state=tk.DISABLED, font=('微软雅黑', 10))
        scrollbar = ttkb.Scrollbar(log_frame, command=self.log_area.yview)
        self.log_area.configure(yscrollcommand=scrollbar.set)
        self.log_area.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self._load_config_to_ui()

    # --- [修改] 手动控制面板（支持折叠和校准） ---
    def _create_manual_control_panel(self, parent):
        """创建可折叠的手动控制面板"""
        container = ttkb.Frame(parent)
        container.grid(row=1, column=0, sticky="ew", padx=5, pady=5)
        container.columnconfigure(0, weight=1)

        # 折叠切换按钮
        self.toggle_btn = ttkb.Button(container, text="▼ 手动控制 & 校准", 
                                      command=self._toggle_manual_panel,
                                      bootstyle="link")
        self.toggle_btn.pack(fill="x")

        # 内容框架
        self.manual_content = ttkb.Frame(container)
        self.manual_content.pack(fill="x", expand=True)

        # 边框装饰
        panel = ttkb.Labelframe(self.manual_content, text="长按旋转", bootstyle="info")
        panel.pack(fill="x", padx=5, pady=5)
        
        # 布局配置
        panel.columnconfigure(0, weight=1)
        panel.columnconfigure(1, weight=1)
        panel.columnconfigure(2, weight=1)

        # 辅助函数
        def bind_btn(btn, cmd):
            btn.bind('<ButtonPress-1>', lambda e: self._start_manual_move(cmd))
            btn.bind('<ButtonRelease-1>', lambda e: self._stop_manual_move())

        # 方向控制按钮
        btn_up = ttkb.Button(panel, text="▲", bootstyle="secondary")
        btn_up.grid(row=0, column=1, padx=2, pady=2)
        bind_btn(btn_up, "up")

        btn_left = ttkb.Button(panel, text="◀", bootstyle="secondary")
        btn_left.grid(row=1, column=0, padx=2, pady=2)
        bind_btn(btn_left, "left")

        btn_stop = ttkb.Button(panel, text="■", bootstyle="danger")
        btn_stop.grid(row=1, column=1, padx=2, pady=2)
        btn_stop.configure(command=self._stop_manual_move)

        btn_right = ttkb.Button(panel, text="▶", bootstyle="secondary")
        btn_right.grid(row=1, column=2, padx=2, pady=2)
        bind_btn(btn_right, "right")

        btn_down = ttkb.Button(panel, text="▼", bootstyle="secondary")
        btn_down.grid(row=2, column=1, padx=2, pady=2)
        bind_btn(btn_down, "down")

        # --- [新增] 校准按钮 ---
        cali_frame = ttkb.Frame(panel)
        cali_frame.grid(row=3, column=0, columnspan=3, pady=5)
        
        ttkb.Label(cali_frame, text="圈数校准:").pack(side="left", padx=5)
        
        btn_sub = ttkb.Button(cali_frame, text="-1 圈", bootstyle="warning-outline", width=6,
                             command=lambda: self._calibrate_turns(-1))
        btn_sub.pack(side="left", padx=2)
        
        btn_add = ttkb.Button(cali_frame, text="+1 圈", bootstyle="warning-outline", width=6,
                             command=lambda: self._calibrate_turns(1))
        btn_add.pack(side="left", padx=2)

    def _toggle_manual_panel(self):
        """切换手动面板的显示/隐藏"""
        if self.manual_panel_visible:
            self.manual_content.pack_forget()
            self.toggle_btn.configure(text="▶ 手动控制 & 校准")
            self.manual_panel_visible = False
        else:
            self.manual_content.pack(fill="x", expand=True)
            self.toggle_btn.configure(text="▼ 手动控制 & 校准")
            self.manual_panel_visible = True

    def _calibrate_turns(self, turns):
        """调用后端进行校准"""
        if self.running and self.control_system:
            # 弹窗确认
            direction = "增加" if turns > 0 else "减少"
            if messagebox.askyesno("确认校准", f"确定要将当前真角度{direction} 360度吗？"):
                self.control_system.calibrate_turns(turns)
        else:
            messagebox.showwarning("提示", "请先启动系统")

    # --- [保持不变的辅助方法] ---
    def _create_device_panel(self, parent, device, row):
        frame = ttkb.Labelframe(parent, text=f"{device.upper()}", bootstyle=INFO)
        frame.grid(row=row, column=0, sticky="ew", padx=5, pady=5)
        frame.columnconfigure(1, weight=1)

        if device == "AZ/EL":
            self._create_az_el_panel(frame)
        else:
            ttkb.Label(frame, text="通信协议:").grid(row=0, column=0, sticky="w", padx=5, pady=2)
            protocol = ttkb.Combobox(frame, values=["串口", "TCP"], state="readonly", width=8)
            protocol.grid(row=0, column=1, sticky="ew", padx=5, pady=2)
            protocol.set("串口")
            setattr(self, f"{device}_protocol", protocol)
            protocol.bind("<<ComboboxSelected>>", lambda e, dev=device: self._on_protocol_changed(dev))
            self._create_settings_notebook(frame, device)

    def _create_az_el_panel(self, parent):
        ttkb.Label(parent, text="水平角度:").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        azimuth_entry = ttkb.Entry(parent)
        azimuth_entry.grid(row=0, column=1, sticky="ew", padx=5, pady=2)
        azimuth_btn = ttkb.Button(parent, text="执行",
                                  command=lambda: self.set_az_el(azimuth_entry.get(), 0x4B))
        azimuth_btn.grid(row=0, column=2, padx=5)

        ttkb.Label(parent, text="俯仰角度:").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        elevation_entry = ttkb.Entry(parent)
        elevation_entry.grid(row=1, column=1, sticky="ew", padx=5, pady=2)
        elevation_btn = ttkb.Button(parent, text="执行",
                                    command=lambda: self.set_az_el(elevation_entry.get(), 0x4D))
        elevation_btn.grid(row=1, column=2, padx=5)
        
        self.lbl_status = ttkb.Label(parent, text="状态: 真AZ= -- | EL= --", bootstyle="secondary")
        self.lbl_status.grid(row=2, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        
        c2_btn = ttkb.Button(parent, text="查询C2", bootstyle="info-outline", width=8,
                             command=self._manual_query_c2)
        c2_btn.grid(row=2, column=2, padx=5, pady=5)

    def update_status_display(self, true_az, el):
        def _update():
            if hasattr(self, 'lbl_status'):
                self.lbl_status.config(text=f"状态: 真AZ={true_az:.2f} | EL={el:.2f}")
        self.root.after(0, _update)

    def _manual_query_c2(self):
        if self.running and self.control_system:
            Thread(target=self.control_system._execute_angle_query_command, daemon=True).start()
        else:
            self.log("[警告] 系统未启动")

    def _create_settings_notebook(self, parent, device):
        notebook = ttkb.Notebook(parent, bootstyle=INFO)
        notebook.grid(row=1, column=0, columnspan=2, sticky="ew", padx=5, pady=5)
        parent.columnconfigure(1, weight=1)
        setattr(self, f"{device}_notebook", notebook)

        serial_frame = ttkb.Frame(notebook)
        serial_frame.columnconfigure(1, weight=1)
        ttkb.Label(serial_frame, text="串口号:").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        serial_port = ttkb.Combobox(serial_frame, state="readonly")
        serial_port.bind("<Button-1>", lambda e: self._refresh_ports(serial_port))
        serial_port.grid(row=0, column=1, sticky="ew", padx=5, pady=2)
        ttkb.Label(serial_frame, text="波特率:").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        baudrate = ttkb.Combobox(serial_frame, values=["2400", "9600", "19200", "38400", "115200"])
        baudrate.grid(row=1, column=1, sticky="ew", padx=5, pady=2)
        notebook.add(serial_frame, text="串口参数")
        setattr(self, f"{device}_serial", (serial_port, baudrate))

        tcp_frame = ttkb.Frame(notebook)
        tcp_frame.columnconfigure(1, weight=1)
        ttkb.Label(tcp_frame, text="IP地址:").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        tcp_host = ttkb.Entry(tcp_frame)
        tcp_host.grid(row=0, column=1, sticky="ew", padx=5, pady=2)
        ttkb.Label(tcp_frame, text="端口号:").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        tcp_port = ttkb.Entry(tcp_frame)
        tcp_port.grid(row=1, column=1, sticky="ew", padx=5, pady=2)
        notebook.add(tcp_frame, text="TCP参数")
        setattr(self, f"{device}_tcp", (tcp_host, tcp_port))

        # --- [修改] 增加限位配置 ---
        if device == "pelco":
            angle_frame = ttkb.Frame(notebook)
            angle_frame.columnconfigure(1, weight=1)

            # 角度修正
            fields = [
                ("最小俯仰角:", "min_elevation"),
                ("最大俯仰角:", "max_elevation"),
                ("方位角偏移:", "azimuth_offset"),
                ("初始水平角:", "initial_azimuth")
            ]
            entries = []
            for i, (label, _) in enumerate(fields):
                ttkb.Label(angle_frame, text=label).grid(row=i, column=0, padx=5, pady=2, sticky="w")
                entry = ttkb.Entry(angle_frame)
                entry.grid(row=i, column=1, padx=5, pady=2, sticky="ew")
                entries.append(entry)
            
            # 分隔线
            ttkb.Separator(angle_frame).grid(row=len(fields), column=0, columnspan=2, sticky="ew", pady=5)
            
            # 软限位设置
            limit_fields = [
                ("软限位下限:", "min_az"),
                ("软限位上限:", "max_az")
            ]
            limit_entries = []
            start_row = len(fields) + 1
            for i, (label, _) in enumerate(limit_fields):
                ttkb.Label(angle_frame, text=label).grid(row=start_row+i, column=0, padx=5, pady=2, sticky="w")
                entry = ttkb.Entry(angle_frame)
                entry.grid(row=start_row+i, column=1, padx=5, pady=2, sticky="ew")
                limit_entries.append(entry)

            notebook.add(angle_frame, text="角度与限位")
            # 将普通修正和限位修正合并存入 pelco_angle 列表，前4个是普通，后2个是限位
            setattr(self, f"{device}_angle", entries + limit_entries)

    def _on_protocol_changed(self, device):
        proto = getattr(self, f"{device}_protocol").get()
        notebook = getattr(self, f"{device}_notebook")
        if proto == "串口": notebook.select(0)
        else: notebook.select(1)

    def _save_config(self):
        try:
            config = {
                "gs232b": self._build_device_config("gs232b"),
                "pelco": self._build_device_config("pelco")
            }
            # UI settings
            config["ui"] = {"topmost": self.root.attributes("-topmost")}
            
            self._validate_config(config)
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=2)
            self.log("[系统] 配置已保存")
        except Exception as e:
            messagebox.showerror("错误", f"保存失败: {e}")

    def _build_device_config(self, device):
        proto = getattr(self, f"{device}_protocol").get()
        config = {"protocol": "serial" if proto == "串口" else "tcp"}

        if proto == "串口":
            port, baud = getattr(self, f"{device}_serial")
            config["serial"] = {
                "port": port.get().strip(),
                "baudrate": int(baud.get().strip())
            }
        else:
            host, port = getattr(self, f"{device}_tcp")
            config["tcp"] = {
                "host": host.get().strip(),
                "port": int(port.get().strip())
            }

        if device == "pelco":
            entries = getattr(self, f"{device}_angle")
            # 前4个是基本修正
            config["angle_correction"] = {
                "min_elevation": float(entries[0].get()),
                "max_elevation": float(entries[1].get()),
                "azimuth_offset": float(entries[2].get()),
                "initial_azimuth": float(entries[3].get())
            }
            # 后2个是限位
            config["limits"] = {
                "min_az": float(entries[4].get()),
                "max_az": float(entries[5].get())
            }
        return config

    def _validate_config(self, config):
        required_keys = ["gs232b", "pelco"]
        for k in required_keys:
            if k not in config: raise ValueError(f"缺少配置段 {k}")

        pelco_limits = config["pelco"].get("limits", {})
        if pelco_limits.get("min_az", 0) > pelco_limits.get("max_az", 0):
             raise ValueError("软限位错误：下限不能大于上限")

    def _load_config_to_ui(self):
        config = self._load_config()
        self._load_protocol_config("gs232b", config["gs232b"])
        self._load_protocol_config("pelco", config["pelco"])
        
        # 加载Pelco参数
        if "angle_correction" in config["pelco"]:
            entries = getattr(self, "pelco_angle")
            correction = config["pelco"]["angle_correction"]
            limits = config["pelco"].get("limits", {"min_az": -360, "max_az": 360})
            
            # 按顺序填充：前4个修正，后2个限位
            values = [
                correction.get("min_elevation", 0),
                correction.get("max_elevation", 90),
                correction.get("azimuth_offset", 0),
                correction.get("initial_azimuth", 0),
                limits.get("min_az", -360),
                limits.get("max_az", 360)
            ]
            
            for entry, value in zip(entries, values):
                entry.delete(0, tk.END)
                entry.insert(0, str(value))

    def _load_protocol_config(self, device, config):
        proto = config["protocol"]
        getattr(self, f"{device}_protocol").set("串口" if proto == "serial" else "TCP")
        notebook = getattr(self, f"{device}_notebook")
        if proto == "serial":
            serial_port, baudrate = getattr(self, f"{device}_serial")
            serial_port.set(config["serial"]["port"])
            baudrate.set(str(config["serial"]["baudrate"]))
            notebook.select(0)
        else:
            host, port = getattr(self, f"{device}_tcp")
            host.delete(0, tk.END); host.insert(0, config["tcp"]["host"])
            port.delete(0, tk.END); port.insert(0, str(config["tcp"]["port"]))
            notebook.select(1)

    def log(self, message: str):
        self.log_queue.put(message)

    def _process_log_queue(self):
        while not self.log_queue.empty():
            msg = self.log_queue.get()
            self.log_area.configure(state=tk.NORMAL)
            tag = "info"
            if "[错误]" in msg: tag = "error"; self.log_area.tag_config("error", foreground="red")
            elif "[警告]" in msg: tag = "warning"; self.log_area.tag_config("warning", foreground="orange")
            else: self.log_area.tag_config("info", foreground="green")
            self.log_area.insert(tk.END, f">> {msg}\n", tag)
            self.log_area.configure(state=tk.DISABLED)
            self.log_area.see(tk.END)
        self.root.after(100, self._process_log_queue)

    def clear_log(self):
        self.log_area.configure(state=tk.NORMAL)
        self.log_area.delete(1.0, tk.END)
        self.log_area.configure(state=tk.DISABLED)

    def toggle_system(self):
        if not self.running:
            try:
                config = self._load_config()
                self.control_system = ControlSystem(config, self.log, self.update_status_display)
                self.control_system.start()
                self.running = True
                self.start_btn.config(text="停止系统")
                self.log("[系统] 系统启动成功")
            except Exception as e:
                messagebox.showerror("错误", f"系统启动失败：{str(e)}")
        else:
            with self._connection_lock:
                self.running = False
                if self.control_system:
                    self.control_system.stop()
                    self.control_system = None
                self.start_btn.config(text="启动系统")
                self.log("[系统] 系统已安全停止")

    def _load_config(self):
        try:
            with open(self.config_file, 'r') as f:
                config = json.load(f)
            return config
        except Exception:
            return self._get_default_config()

    def _refresh_ports(self, combobox):
        try:
            ports = [port.device for port in serial.tools.list_ports.comports()]
            combobox['values'] = ports
            if ports and not combobox.get(): combobox.set(ports[0])
        except Exception: pass

    def _get_scaling(self):
        try:
            return round(win32print.GetDeviceCaps(win32gui.GetDC(0), win32con.DESKTOPHORZRES) / win32api.GetSystemMetrics(0), 2)
        except: return 1.0

    def toggle_topmost(self):
        current = self.root.attributes("-topmost")
        self.root.attributes("-topmost", not current)
        self.topmost_btn.config(text="取消置顶" if not current else "窗口置顶",
                                bootstyle=(PRIMARY, OUTLINE) if not current else (SECONDARY, OUTLINE))

    def set_az_el(self, angle, set_cmd):
        if self.running:
            if angle == '': return
            self.control_system.select_angle(float(angle), set_cmd)
        else:
            self.log("[错误] 系统未启动")

    def _start_manual_move(self, direction):
        if not self.running or not self.control_system: return
        self._current_manual_cmd = direction
        self._sending_manual = True
        self._manual_loop()

    def _manual_loop(self):
        if self._sending_manual and self.running:
            if hasattr(self.control_system, 'pelco') and self.control_system.pelco:
                 self.control_system.pelco.move(self._current_manual_cmd)
            self.root.after(200, self._manual_loop)

    def _stop_manual_move(self, event=None):
        self._sending_manual = False
        if self.running and self.control_system:
             if hasattr(self.control_system, 'pelco') and self.control_system.pelco:
                self.control_system.pelco.move("stop")

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    app = MainWindow()
    app.run()