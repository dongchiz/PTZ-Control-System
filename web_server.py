# web_server.py
import json
import os
import queue
import time
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response
from core.controller import ControlSystem
import serial.tools.list_ports

# 配置 Flask
app = Flask(__name__, template_folder='web/templates', static_folder='web/static')

# 全局状态
class WebState:
    def __init__(self):
        self.control_system = None
        self.log_queue = queue.Queue()
        self.config_file = self._get_config_path()
        self.running = False

    def _get_config_path(self) -> str:
        """获取配置文件路径 (保持与原版兼容)"""
        if os.name == 'nt':
            config_dir = Path(os.getenv('APPDATA')) / 'PTZ_Controller'
        else:
            config_dir = Path.home() / '.config' / 'PTZ_Controller'
        config_dir.mkdir(parents=True, exist_ok=True)
        return str(config_dir / 'config.json')






    def log(self, message):
        """日志回调"""
        timestamp = time.strftime("%H:%M:%S", time.localtime())
        self.log_queue.put(f"[{timestamp}] {message}")

    def get_default_config(self):
        return {
            "gs232b": {"protocol": "serial", "serial": {"port": "/dev/ttyUSB0", "baudrate": 9600}},
            "pelco": {
                "protocol": "serial", 
                "serial": {"port": "/dev/ttyUSB1", "baudrate": 9600},
                "angle_correction": {"min_elevation": 0, "max_elevation": 90, "azimuth_offset": 0, "initial_azimuth": 0}
            },
            "lcd": {"enabled": False, "address": "0x27"}, # 新增 LCD 配置
            "ui": {"topmost": False}
        }

state = WebState()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    if request.method == 'GET':
        if os.path.exists(state.config_file):
            with open(state.config_file, 'r') as f:
                return jsonify(json.load(f))
        return jsonify(state.get_default_config())
    
    if request.method == 'POST':
        new_config = request.json
        with open(state.config_file, 'w') as f:
            json.dump(new_config, f, indent=2)
        state.log("[系统] 配置已通过 Web 保存")
        return jsonify({"status": "ok"})

@app.route('/api/serial_ports')
def get_ports():
    ports = [p.device for p in serial.tools.list_ports.comports()]
    return jsonify(ports)

@app.route('/api/system/toggle', methods=['POST'])
def toggle_system():
    action = request.json.get('action')
    try:
        if action == 'start' and not state.running:
            # 加载当前配置
            with open(state.config_file, 'r') as f:
                config = json.load(f)
            state.control_system = ControlSystem(config, state.log)
            state.control_system.start()
            state.running = True
            return jsonify({"status": "started"})
        
        elif action == 'stop' and state.running:
            if state.control_system:
                state.control_system.stop()
                state.control_system = None
            state.running = False
            state.log("[系统] 系统已停止")
            return jsonify({"status": "stopped"})
            
    except Exception as e:
        state.log(f"[错误] 操作失败: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify({"status": "no_change"})



@app.route('/api/system/status', methods=['GET'])
def get_system_status():
    """获取当前系统运行状态"""
    return jsonify({"running": state.running})



@app.route('/api/logs')
def stream_logs():
    def generate():
        while True:
            try:
                # 阻塞等待日志，避免空转
                msg = state.log_queue.get(timeout=1)
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield ": keep-alive\n\n" # 发送心跳包防止断开
    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/control/set_angle', methods=['POST'])
def set_angle():
    if not state.running or not state.control_system:
        return jsonify({"status": "error", "message": "系统未启动"}), 400
        
    data = request.json
    angle = data.get('angle')
    cmd_type = int(data.get('type')) # 0x4B or 0x4D
    
    state.control_system.select_angle(float(angle), cmd_type)
    return jsonify({"status": "ok"})

@app.route('/api/control/clear_log', methods=['POST'])
def clear_log():
    # 清空队列
    while not state.log_queue.empty():
        state.log_queue.get()
    return jsonify({"status": "ok"})


@app.route('/api/lcd/toggle', methods=['POST'])
def toggle_lcd():
    """专门处理 LCD 的动态开关"""
    data = request.json
    enabled = data.get('enabled', False)
    address = data.get('address', '0x27')

    # 1. 更新全局配置对象中的 LCD 设置 (为了下次启动记忆)
    # 读取最新配置 -> 修改 -> 保存
    try:
        if os.path.exists(state.config_file):
            with open(state.config_file, 'r') as f:
                config = json.load(f)
        else:
            config = state.get_default_config()

        config.setdefault('lcd', {})
        config['lcd']['enabled'] = enabled
        config['lcd']['address'] = address
        
        with open(state.config_file, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        state.log(f"[错误] 配置文件保存失败: {e}")

    # 2. 如果系统正在运行，立即应用硬件变更
    if state.running and state.control_system:
        # 实时更新控制系统的配置
        state.control_system.config['lcd']['address'] = address
        
        # 尝试切换状态
        success = state.control_system.set_lcd_state(enabled)
        
        if enabled and not success:
            # 如果用户想开，但是硬件初始化失败了
            # 这里的 enabled=False 是为了让前端把勾选框取消掉
            return jsonify({"status": "error", "message": "LCD 初始化失败，请检查日志", "enabled": False}), 500
            
    return jsonify({"status": "ok", "enabled": enabled})






if __name__ == '__main__':
    # 自动创建默认配置
    if not os.path.exists(state.config_file):
        with open(state.config_file, 'w') as f:
            json.dump(state.get_default_config(), f, indent=2)
            
    app.run(host='0.0.0.0', port=8000, threaded=True)