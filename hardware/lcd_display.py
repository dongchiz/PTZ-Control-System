# hardware/lcd_display.py
import time
from threading import Lock

class LCDHandler:
    def __init__(self, config, log_callback):
        self.config = config
        self.log = log_callback
        self.lcd = None
        self._lock = Lock()
        
    def init_display(self):
        """初始化 LCD (懒加载模式)"""
        try:
            # [关键修改] 只有在需要时才导入库
            # 如果没安装库，或者系统不支持 smbus，这里会直接抛出 ImportError
            from RPLCD.i2c import CharLCD
            
            # 解析地址
            addr_conf = self.config.get("address", "0x27")
            addr = int(addr_conf, 16) if isinstance(addr_conf, str) else int(addr_conf)

            # 初始化硬件
            self.lcd = CharLCD(i2c_expander='PCF8574', address=addr, port=1,
                               cols=16, rows=2, dotsize=8)
            
            self.lcd.clear()
            self._show_welcome()
            return True # 初始化成功

        except ImportError:
            raise RuntimeError("未安装 RPLCD 或 smbus2 库，无法驱动 LCD")
        except Exception as e:
            # 可能是 I2C 地址错误或接线问题
            self.lcd = None 
            raise RuntimeError(f"LCD 硬件初始化失败: {str(e)}")

    def _show_welcome(self):
        with self._lock:
            if self.lcd:
                try:
                    self.lcd.cursor_pos = (0, 0)
                    self.lcd.write_string("PTZ System Ready")
                except: pass

    def update_display(self, az, el):
        if not self.lcd:
            return
            
        with self._lock:
            try:
                self.lcd.cursor_pos = (0, 0)
                self.lcd.write_string("Range: -360~+360")
                self.lcd.cursor_pos = (1, 0)
                # 使用 format 确保长度固定，覆盖旧字符
                self.lcd.write_string(f"AZ:{int(az):03d} EL:{int(el):03d}   "[:16])
            except Exception as e:
                self.log(f"[LCD警告] 通信中断: {str(e)}")

    def close(self):
        with self._lock:
            if self.lcd:
                try:
                    self.lcd.clear()
                    self.lcd.backlight_enabled = False
                    self.lcd.close(clear=True)
                except: pass
                self.lcd = None