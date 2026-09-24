"""Optional persistent touch transport using the installed scrcpy protocol.

Own a control-only connection and write due packets directly from the executor.
There is no adapter packet FIFO containing obsolete future pointer positions.
This transport requires device calibration; ADB remains the default.
"""
import time


class ScrcpyDragTransport:
    def __init__(self, device=None, *, connection=None, size=None,
                 clock=time.monotonic, sleep=time.sleep):
        from mysc.core.control import ControlAdapter
        from mysc.core.connection import Connection
        from mysc.utils.keys import EnumAction
        self.packet = ControlAdapter.packet__touch
        self.down, self.move_action, self.up = EnumAction.DOWN, EnumAction.MOVE, EnumAction.UP
        self.clock, self.sleep = clock, sleep
        self.width, self.height = size or device.window_size()
        self.connection = connection or Connection.connect(device, {'control': True, 'clipboard_autosync': False})
        if self.connection is None:
            raise RuntimeError('Could not connect scrcpy control socket')
        # Bound stalled socket writes; sending a packet is not a device ACK.
        if hasattr(self.connection, 'conn'):
            self.connection.conn.settimeout(.2)
        self.held = False
        self.x = self.y = 0.
        self.pointer = 0x1234

    def _send(self, action, x, y):
        packet = self.packet(action, round(max(0., min(1., x))*(self.width-1)),
            round(max(0., min(1., y))*(self.height-1)), self.width, self.height, self.pointer)
        if hasattr(self.connection, 'conn'):
            self.connection.conn.sendall(packet)
        else:
            self.connection.send(packet)
        self.x, self.y = x, y

    def move(self, action, cancel):
        if cancel.is_set():
            return False
        if not self.held:
            self._send(self.down, action.source_x, action.y)
            self.held = True
        source = self.x
        start = self.clock()
        while True:
            if cancel.is_set():
                self.release()
                return False
            fraction = min(1., (self.clock()-start)/max(action.duration, .001))
            self._send(self.move_action, source+(action.target_x-source)*fraction, action.y)
            if fraction >= 1.:
                return True
            self.sleep(min(1/60, max(.001, start+action.duration-self.clock())))

    def release(self):
        if self.held:
            try:
                self._send(self.up, self.x, self.y)
            finally:
                self.held = False

    def close(self):
        try:
            self.release()
        finally:
            self.connection.disconnect()
