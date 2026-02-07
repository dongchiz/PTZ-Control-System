# core/controller.py
# Copyright © 2025 Sharkbia
# MIT License - See LICENSE for details
from threading import Thread, Lock
from core.protocols import PelcoDProtocol, GS232BProtocol
from hardware.interfaces import SerialHandler, TCPHandler
from hardware.lcd_display import LCDHandler  # 确保导入了 LCD 模块

class ControlSystem:
    def __init__(self, config, log_callback):
        self.thread = Thread(target=self._run, daemon=True)
        self.config = config
        self.log = log_callback
        self.running = False
        self._connection_lock = Lock()
        self.gs232b = None
        self.pelco = None
        self.lcd = None  # 初始化 LCD 变量

        try:
            self._init_connections()
            self._init_lcd()  # 初始化 LCD
            self.log("[系统] 硬件初始化成功")
        except Exception as e:
            self.log(f"[错误] 初始化失败: {str(e)}")
            raise

    def _init_lcd(self):
        """初始化 LCD 显示屏"""
        lcd_config = self.config.get("lcd", {})
        if lcd_config.get("enabled", False):
            try:
                self.lcd = LCDHandler(lcd_config, self.log)
                self.lcd.init_display()
                self.log(f"[LCD] 显示屏已启动 (地址: {lcd_config.get('address')})")
            except Exception as e:
                self.log(f"[错误] LCD 启动失败: {str(e)}")

    def _init_connections(self):
        with self._connection_lock:
            try:
                self._close_all_connections()

                # 初始化 GS-232B
                self.gs232b = self._create_handler("gs232b")
                self.log("[连接] GS-232B连接已建立")

                # 初始化 Pelco-D
                pelco_hw = self._create_handler("pelco")
                self.pelco = PelcoDProtocol(pelco_hw, self.config["pelco"])
                self.log("[连接] Pelco-D连接已建立")
            except Exception as e:
                self.log(f"[错误] 连接初始化失败: {str(e)}")
                self._close_all_connections()
                raise

    def _close_all_connections(self):
        if self.gs232b:
            try: self.gs232b.close()
            except: pass
            self.gs232b = None
        if self.pelco:
            try:
                if hasattr(self.pelco, 'hw') and self.pelco.hw:
                    self.pelco.hw.close()
            except: pass
            self.pelco = None

    def _create_handler(self, device):
        config = self.config[device]
        protocol = config["protocol"]
        handlers = {
            "serial": SerialHandler,
            "tcp": TCPHandler
        }
        handler = handlers[protocol](config, self.log)
        if not handler.connect():
            raise ConnectionError(f"{device} 连接失败")
        return handler

    def start(self):
        if not self.running:
            self.running = True
            self.thread.start()
            self.log("[系统] 系统已启动")

    def _run(self):
        while self.running:
            try:
                data = self.gs232b.recv(1024, timeout=1.0)
                if data:
                    cmd = GS232BProtocol.parse_command(data)
                    self.log(f"[命令] 收到命令: {cmd}")
                    response = self._process_command(cmd)
                    if response:
                        # self.log(f"[系统] 返回：{response.strip()}")
                        self.gs232b.send(response.encode())
            except Exception as e:
                self.log(f"[错误] 处理错误: {str(e)}")

    def _process_command(self, cmd: str) -> str:
        command_handlers = {
            'C2': self._handle_c2,
            'W': self._handle_w,
            r'\set_pos': self._handle_setpos
        }
        if cmd.startswith("C2W") or (cmd.startswith("W") and cmd.endswith("C2")):
            return self._handle_combined_command(cmd)
        for prefix, handler in command_handlers.items():
            if cmd.startswith(prefix):
                return handler(cmd[len(prefix):].strip())
        return ""

    def _handle_combined_command(self, cmd: str) -> str:
        self.log("[命令] 接收到 C2 和 W 命令")
        w_cmd = cmd[3:] if cmd.startswith("C2W") else cmd[1:-2]
        w_result = self._execute_angle_control_command(w_cmd)
        return self._execute_angle_query_command() if w_result else ""

    def _handle_c2(self, _) -> str:
        return self._execute_angle_query_command()

    def _handle_w(self, cmd: str) -> str:
        return self._execute_angle_control_command(cmd)

    def _handle_setpos(self, cmd: str) -> str:
        parts = cmd.split()
        if len(parts) != 2: return ""
        try:
            azi, ele = map(float, parts)
            if ele < 0: return ""
            self.log(f"[命令] 收到 look4sat 角度指令: 方位 {azi} 俯仰 {ele}")
            return self._execute_angle_control_command(f"{round(azi)} {round(ele)}")
        except ValueError:
            return ""

    def _execute_angle_control_command(self, w_cmd: str) -> str:
        try:
            parts = w_cmd.split()
            azi = float(parts[0])
            ele = float(parts[1])
            success = (
                    self.pelco.set_angle(azi, 0x4B) and
                    self.pelco.set_angle(ele, 0x4D)
            )
            return "ACK\r\n" if success else ""
        except (IndexError, ValueError):
            self.log("[命令] W 命令参数解析失败")
            return ""

    def _execute_angle_query_command(self) -> str:
        """执行 C2 命令，并更新 LCD"""
        # self.log("[命令] 处理 C2 查询")
        azimuth = self.pelco.query_angle(0x51)
        elevation = self.pelco.query_angle(0x53)

        if azimuth is not None and elevation is not None:
            azimuth_deg = azimuth // 100
            elevation_deg = elevation // 100
            
            # [关键] 更新 LCD 显示
            if self.lcd:
                self.lcd.update_display(azimuth_deg, elevation_deg)
                
            log_msg = f"水平角度 {azimuth_deg} 俯仰角度 {elevation_deg}"
            self.log(log_msg)
            return f"AZ={azimuth_deg:03d} EL={elevation_deg:03d}\r\n"
        return ""

    def select_angle(self, angle, set_cmd):
        self.log(f"[命令] 选择角度 {angle} 并执行 {'水平' if set_cmd == 0x4D else '俯仰'}调整")
        self.pelco.set_angle(angle, set_cmd)

    def stop(self):
        with self._connection_lock:
            self.running = False
            self._close_all_connections()
            
            # 关闭 LCD
            if self.lcd:
                try: self.lcd.close()
                except: pass
                self.lcd = None
                
            if self.thread.is_alive():
                self.thread.join(timeout=5)
            self.log("[系统] 系统已停止")