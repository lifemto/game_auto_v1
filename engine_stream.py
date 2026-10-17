# -*- coding: utf-8 -*-
import os
import time
import socket
import shutil
import threading
import subprocess
from pathlib import Path

import av
import psutil


SERVER_VERSION = "4.1"
LOCAL_PORT = 27199
REMOTE_SOCKET = "scrcpy"
REMOTE_SERVER = "/data/local/tmp/scrcpy-server-auto.jar"
RECV_CHUNK = 64 * 1024


def find_file(candidates, which_name=None):
    if which_name:
        p = shutil.which(which_name)
        if p:
            candidates.append(Path(p))
    for p in candidates:
        p = Path(p)
        try:
            p = p.resolve()
        except Exception:
            pass
        if p.is_file():
            return str(p)
    return None


def run_cmd(args, timeout=20, check=False):
    p = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=timeout,
        check=False,
    )
    if check and p.returncode != 0:
        raise RuntimeError(
            f"命令失败：{' '.join(args)}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}"
        )
    return p


def adb_run(adb, serial, args, timeout=20, check=False):
    return run_cmd([adb, "-s", serial] + list(args), timeout=timeout, check=check)


def get_devices(adb):
    p = run_cmd([adb, "devices"], timeout=8)
    out = []
    for line in p.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            out.append(parts[0])
    return out


def remove_forward(adb, serial):
    try:
        adb_run(adb, serial, ["forward", "--remove", f"tcp:{LOCAL_PORT}"], timeout=5)
    except Exception:
        pass


def terminate_process(proc):
    if proc is None:
        return
    try:
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        for c in children:
            try:
                c.terminate()
            except Exception:
                pass
        try:
            parent.terminate()
        except Exception:
            pass
        _, alive = psutil.wait_procs([parent] + children, timeout=2)
        for p in alive:
            try:
                p.kill()
            except Exception:
                pass
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


class H264Stream:
    """
    scrcpy-server -> ADB forward -> H.264 -> PyAV -> latest BGR frame.
    始终只保留最新帧，不积压旧帧。
    """

    def __init__(
        self,
        adb_path,
        server_path,
        serial,
        max_fps=60,
        max_size=1600,
        bitrate=8_000_000,
        log_path=None,
    ):
        self.adb = adb_path
        self.server_path = server_path
        self.serial = serial
        self.max_fps = int(max_fps)
        self.max_size = int(max_size)
        self.bitrate = int(bitrate)
        self.log_path = Path(log_path) if log_path else Path("scrcpy_server.log")

        self.proc = None
        self.log_file = None
        self.sock = None
        self.thread = None
        self.stop_event = threading.Event()
        self.first_frame_event = threading.Event()
        self.lock = threading.Lock()

        self.latest_frame = None
        self.frame_seq = 0
        self.error = None

    def _push_server(self):
        adb_run(
            self.adb,
            self.serial,
            ["push", self.server_path, REMOTE_SERVER],
            timeout=20,
            check=True,
        )

    def _setup_forward(self):
        remove_forward(self.adb, self.serial)
        adb_run(
            self.adb,
            self.serial,
            ["forward", f"tcp:{LOCAL_PORT}", f"localabstract:{REMOTE_SOCKET}"],
            timeout=8,
            check=True,
        )

    def _start_server(self):
        args = [
            f"CLASSPATH={REMOTE_SERVER}",
            "app_process",
            "/",
            "com.genymobile.scrcpy.Server",
            SERVER_VERSION,
            "tunnel_forward=true",
            "audio=false",
            "control=false",
            "cleanup=false",
            "send_device_meta=false",
            "send_frame_meta=false",
            "send_stream_meta=false",
            "send_dummy_byte=true",
            "video_codec=h264",
            f"video_bit_rate={self.bitrate}",
            f"max_size={self.max_size}",
            f"max_fps={self.max_fps}",
            "log_level=info",
        ]
        cmd = [self.adb, "-s", self.serial, "shell"] + args

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = open(self.log_path, "w", encoding="utf-8", buffering=1)

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW

        self.proc = subprocess.Popen(
            cmd,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            creationflags=creationflags,
        )

    def _connect_ready(self, timeout=15):
        deadline = time.perf_counter() + timeout
        attempts = 0
        last_error = None

        while time.perf_counter() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"scrcpy-server 提前退出，returncode={self.proc.returncode}"
                )

            attempts += 1
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(0.6)

            try:
                s.connect(("127.0.0.1", LOCAL_PORT))
                b = s.recv(1)
                if b == b"\x00":
                    s.settimeout(0.5)
                    self.sock = s
                    return
                last_error = "recv EOF" if b == b"" else repr(b)
            except Exception as e:
                last_error = repr(e)

            try:
                s.close()
            except Exception:
                pass

            time.sleep(0.08)

        raise RuntimeError(f"15秒内未连上 scrcpy-server：{last_error}")

    def _worker(self):
        codec = av.CodecContext.create("h264", "r")

        try:
            while not self.stop_event.is_set():
                try:
                    data = self.sock.recv(RECV_CHUNK)
                except socket.timeout:
                    continue

                if data == b"":
                    if not self.stop_event.is_set():
                        raise RuntimeError("H.264 TCP 被对端关闭")
                    break

                if not data:
                    continue

                packets = codec.parse(data)

                for packet in packets:
                    decoded = codec.decode(packet)

                    for frame in decoded:
                        image = frame.to_ndarray(format="bgr24")
                        with self.lock:
                            self.latest_frame = image
                            self.frame_seq += 1
                        self.first_frame_event.set()

        except Exception as e:
            with self.lock:
                self.error = repr(e)
            self.first_frame_event.set()

    def start(self):
        self._push_server()
        self._setup_forward()
        self._start_server()
        self._connect_ready()

        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

        if not self.first_frame_event.wait(15):
            raise RuntimeError("等待第一张 H.264 帧超时")

        if self.error:
            raise RuntimeError(f"H.264 解码失败：{self.error}")

        if self.get_frame(copy=False) is None:
            raise RuntimeError("没有取得首帧")

    def get_frame(self, copy=True):
        with self.lock:
            if self.latest_frame is None:
                return None
            return self.latest_frame.copy() if copy else self.latest_frame

    def get_frame_with_seq(self):
        with self.lock:
            if self.latest_frame is None:
                return None, self.frame_seq
            return self.latest_frame.copy(), self.frame_seq

    def stop(self):
        self.stop_event.set()

        try:
            if self.sock:
                self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass

        if self.thread:
            try:
                self.thread.join(timeout=2)
            except Exception:
                pass

        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass

        terminate_process(self.proc)

        if self.log_file:
            try:
                self.log_file.flush()
                self.log_file.close()
            except Exception:
                pass

        remove_forward(self.adb, self.serial)
