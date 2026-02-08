# core/controller.py
# Copyright © 2025 Sharkbia
# MIT License - See LICENSE for details
import time
import math
from threading import Thread, Lock
from core.protocols import PelcoDProtocol, GS232BProtocol
from hardware.interfaces import SerialHandler, TCPHandler


class RotationManager:
    """管理真角度、限位和旋转逻辑"""
    def __init__(self, config):
        self.config = config
        
        # --- [修改] 从配置读取软限位 ---
        limits = config["pelco"].get("limits", {})
        self.min_az = limits.get("min_az", -360) # 默认 -360
        self.max_az = limits.get("max_az", 360)  # 默认 360
        # -----------------------------

        self.offset = config["pelco"]["angle_correction"].get("azimuth_offset", 0)
        
        self.current_true_az = 0.0
        self.last_raw_az = 0.0
        self.initialized = False

    def update_raw_angle(self, raw_az):
        """更新原始角度并计算真角度（处理过圈）"""
        corrected_raw = (raw_az + self.offset) % 360
        
        if not self.initialized:
            self.last_raw_az = corrected_raw
            self.current_true_az = corrected_raw
            self.initialized = True
            return

        diff = corrected_raw - self.last_raw_az
        if diff < -180: diff += 360
        elif diff > 180: diff -= 360
            
        self.current_true_az += diff
        self.last_raw_az = corrected_raw

    def get_target_plan(self, target_req_az):
        target_norm = target_req_az % 360
        current_norm = self.current_true_az % 360
        diff = (target_norm - current_norm) % 360
        distance = 0
        direction = ""

        if math.isclose(diff, 180, abs_tol=0.1):
            if target_norm > current_norm: direction = 'right'
            else: direction = 'left'
            distance = 180
        elif diff < 180:
            direction = 'right'; distance = diff
        else:
            direction = 'left'; distance = 360 - diff

        if direction == 'right': target_true = self.current_true_az + distance
        else: target_true = self.current_true_az - distance

        is_safe = self.min_az <= target_true <= self.max_az
        return target_true, direction, is_safe

    def set_true_angle(self, angle):
        self.current_true_az = angle

    # --- [新增] 圈数校准方法 ---
    def calibrate_turns(self, turns):
        """校准圈数：增加或减少 turns * 360 度"""
        self.current_true_az += (turns * 360.0)
    # -------------------------


class ControlSystem:
    def __init__(self, config, log_callback, status_callback=None):
        self.thread = Thread(target=self._run, daemon=True)
        self.config = config
        self.log = log_callback
        self.status_callback = status_callback
        self.running = False
        self._connection_lock = Lock()
        self.pelco_lock = Lock()
        
        self.gs232b = None
        self.pelco = None

        self.rotator = RotationManager(config)
        self.last_action_time = time.time()
        self.auto_return_timeout = 4 * 3600
        self.manual_move_expire = 0
        self.is_returning = False

        try:
            self._init_connections()
            self.log("[系统] 硬件初始化成功")
        except Exception as e:
            self.log(f"[错误] 初始化失败: {str(e)}")
            raise

    def _init_connections(self):
        with self._connection_lock:
            try:
                if self.gs232b: self.gs232b.close()
                if self.pelco and self.pelco.hw: self.pelco.hw.close()

                self.gs232b = self._create_handler("gs232b")
                self.log("[连接] GS-232B连接已建立")

                pelco_hw = self._create_handler("pelco")
                self.pelco = PelcoDProtocol(pelco_hw, self.config["pelco"])
                self.log("[连接] Pelco-D连接已建立")
                
                self._update_initial_status()
            except Exception as e:
                self.log(f"[错误] 连接初始化失败: {str(e)}")
                raise

    def _update_initial_status(self):
        try:
            self._execute_angle_query_command(log=False)
        except Exception as e:
            self.log(f"[警告] 初始角度读取失败: {e}")

    def _create_handler(self, device):
        config = self.config[device]
        protocol = config["protocol"]
        handlers = {"serial": SerialHandler, "tcp": TCPHandler}
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
        """主控制循环"""
        while self.running:
            current_time = time.time()
            
            try:
                data = self.gs232b.recv(1024, timeout=0.1)
                if data:
                    self.last_action_time = current_time
                    self.is_returning = False
                    
                    cmd = GS232BProtocol.parse_command(data)
                    if cmd:
                        # self.log(f"[命令] 收到命令: {cmd}") 
                        response = self._process_command(cmd)
                        if response:
                            self.gs232b.send(response.encode())
            except Exception as e:
                self.log(f"[错误] 主循环异常: {str(e)}")

            if self.manual_move_expire > 0 and current_time > self.manual_move_expire:
                self.log("[保护] 手动移动心跳超时，停止云台")
                with self.pelco_lock:
                    if self.pelco: self.pelco.move('stop')
                self.manual_move_expire = 0

            if not self.is_returning and (current_time - self.last_action_time > self.auto_return_timeout):
                self._perform_auto_return()
                self.last_action_time = time.time()

    def _process_command(self, cmd: str) -> str:
        command_handlers = {
            'C2': self._handle_c2,
            'W': self._handle_w,
            'S': self._handle_stop,
            r'\set_pos': self._handle_setpos
        }
        
        if cmd.startswith("M_"): return self._handle_manual_gui_command(cmd)
        if cmd.startswith("C2W") or (cmd.startswith("W") and cmd.endswith("C2")):
            return self._handle_combined_command(cmd)

        for prefix, handler in command_handlers.items():
            if cmd.startswith(prefix):
                return handler(cmd[len(prefix):].strip())
        
        return "" 
        
    def _handle_stop(self, _) -> str:
        with self.pelco_lock:
            if self.pelco: self.pelco.move('stop')
        self.manual_move_expire = 0
        self.is_returning = False
        return "\r\n"

    def _handle_manual_gui_command(self, cmd: str) -> str:
        try:
            action = cmd.split('_')[1].lower()
            with self.pelco_lock:
                if action == 'stop':
                    self.pelco.move('stop')
                    self.manual_move_expire = 0
                else:
                    self.pelco.move(action)
                    self.manual_move_expire = time.time() + 0.5
            return "ACK"
        except Exception as e:
            self.log(f"[错误] 手动指令异常: {e}")
            return ""

    def _handle_combined_command(self, cmd: str) -> str:
        self.log("[命令] 接收到 C2 和 W 命令")
        w_cmd = cmd[3:] if cmd.startswith("C2W") else cmd[1:-2]
        w_result = self._execute_angle_control_command(w_cmd)
        return self._execute_angle_query_command() if w_result else ""

    def _handle_c2(self, _) -> str:
        # --- [修改] 增加日志打印 ---
        self.log("[命令] 收到 C2 查询指令")
        # -------------------------
        return self._execute_angle_query_command()

    def _handle_w(self, cmd: str) -> str:
        return self._execute_angle_control_command(cmd)

    def _handle_setpos(self, cmd: str) -> str:
        try:
            parts = cmd.split(); azi, ele = map(float, parts)
            self.log(f"[命令] 收到 look4sat 角度指令: {azi} {ele}")
            return self._execute_angle_control_command(f"{round(azi)} {round(ele)}")
        except ValueError: return ""

    def _execute_angle_control_command(self, w_cmd: str) -> str:
        try:
            parts = w_cmd.split()
            target_az_req = float(parts[0]); target_el_req = float(parts[1])

            # --- 第一阶段：查询与计算 ---
            with self.pelco_lock:
                # 1. 查询当前角度
                curr_az_raw = self.pelco.query_angle(0x51)
                
                if curr_az_raw is not None: 
                    self.rotator.update_raw_angle(curr_az_raw / 100.0)
                    # self.log(f"[命令] W 指令目标: AZ={target_az_req} EL={target_el_req}")
                else:
                    self.log("[警告] 无法读取当前角度，为安全起见放弃 W 指令")
                    return "" 

                # 2. 检查限位
                target_true, direction, is_safe = self.rotator.get_target_plan(target_az_req)
                if not is_safe:
                    self.log(f"[拒绝] 目标真角度 {target_true:.1f} 超出限位")
                    return "?> \r\n"

            # 延时移出锁外
            time.sleep(0.15) 

            # --- 第二阶段：执行水平旋转 ---
            with self.pelco_lock:
                success_az = self.pelco.set_angle(target_az_req % 360, 0x4B)
            
            time.sleep(0.1) 
            
            # --- 第三阶段：执行俯仰旋转 ---
            with self.pelco_lock:
                success_el = self.pelco.set_angle(target_el_req, 0x4D)

            if success_az and success_el:
                return "ACK\r\n"
            else:
                self.log("[警告] 部分旋转指令发送失败")
                return "ACK\r\n"
                    
        except (IndexError, ValueError) as e:
            self.log(f"[命令] W 命令参数解析失败: {e}")
            return ""

    def _execute_angle_query_command(self, log=True) -> str:
        with self.pelco_lock:
            azimuth = self.pelco.query_angle(0x51)
            elevation = self.pelco.query_angle(0x53)

        if azimuth is not None and elevation is not None:
            self.rotator.update_raw_angle(azimuth / 100.0)
            azimuth_deg = azimuth // 100
            elevation_deg = elevation // 100
            
            if self.status_callback:
                self.status_callback(self.rotator.current_true_az, elevation / 100.0)

            if log:
                # self.log(f"[查询] AZ={azimuth_deg} EL={elevation_deg}")
                pass

            return f"AZ={azimuth_deg:03d} EL={elevation_deg:03d}\r\n"
        else:
            if log: self.log("[警告] C2查询失败：无法读取角度")
            return ""

    def select_angle(self, angle, set_cmd):
        """UI手动设置角度"""
        def _worker():
            try:
                self.log(f"[命令] UI 请求设置角度 {angle}")
                with self.pelco_lock:
                    self.pelco.set_angle(angle, set_cmd)
                
                if set_cmd == 0x4B:
                    time.sleep(0.2)
                    self._execute_angle_query_command(log=False)
                    
            except Exception as e:
                self.log(f"[错误] 手动设置角度失败: {e}")

        Thread(target=_worker, daemon=True).start()

    # --- [新增] 校准接口 ---
    def calibrate_turns(self, turns):
        """执行圈数校准"""
        with self.pelco_lock:
            self.rotator.calibrate_turns(turns)
            self.log(f"[校准] 手动调整 {turns:+d} 圈")
        # 校准后刷新界面显示
        self._execute_angle_query_command(log=False)
    # --------------------

    def _perform_auto_return(self):
        self.is_returning = True
        self.log("[系统] 自动回中...")
        try:
            with self.pelco_lock:
                current_raw = self.pelco.query_angle(0x51)
            if current_raw is None:
                self.log("[系统] 无法获取角度，回中中止")
                return
            start_deg = current_raw / 100.0
            targets = [(start_deg - 120) % 360, (start_deg - 240) % 360, (start_deg - 360) % 360]
            
            for t in targets:
                if not self.is_returning: return
                self.log(f"[回中] 正在旋转至 {t:.1f}...")
                with self.pelco_lock:
                    self.pelco.set_angle(t, 0x4B)
                wait_count = 0
                while wait_count < 15:
                    time.sleep(1); wait_count += 1
                    if not self.is_returning: return
            
            if self.is_returning:
                self.log("[回中] 调整俯仰角至 80...")
                with self.pelco_lock:
                    self.pelco.set_angle(80, 0x4D)
            self.log("[系统] 自动回中完成")
        except Exception as e:
            self.log(f"[错误] 自动回中异常: {e}")
        finally:
            self.is_returning = False

    def stop(self):
        with self._connection_lock:
            self.running = False
            if self.gs232b: self.gs232b.close()
            with self.pelco_lock:
                if self.pelco and self.pelco.hw: self.pelco.hw.close()
            if self.thread.is_alive():
                self.thread.join(timeout=5)