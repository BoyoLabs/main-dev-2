#!/usr/bin/env python3
"""GCode Sender — send a .gcode file to a 3D printer over USB serial."""

import glob
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import serial
import serial.tools.list_ports
import cv2
from PIL import Image, ImageTk


BAUD_RATES = ["115200", "250000", "230400", "57600", "38400", "19200", "9600"]
COMMON_PRINTER_VID_PIDS = {
    (0x1A86, 0x7523),  # CH340 (many Chinese boards)
    (0x2341, 0x0042),  # Arduino Mega
    (0x2341, 0x0010),  # Arduino Mega (old bootloader)
    (0x0403, 0x6001),  # FTDI FT232R
    (0x0403, 0x6015),  # FTDI FT230X
}

WEBCAM_W = 640
WEBCAM_H = 480


def list_ports():
    ports = serial.tools.list_ports.comports()
    def priority(p):
        vid_pid = (p.vid, p.pid) if p.vid and p.pid else None
        return 0 if vid_pid in COMMON_PRINTER_VID_PIDS else 1
    return sorted(ports, key=priority)


class GCodeSender(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("GCode Sender")
        self.resizable(True, True)
        self._serial = None
        self._send_thread = None
        self._cancel_flag = threading.Event()
        self._cap = None
        self._webcam_job = None
        self._baud_detect_thread = None
        self._build_ui()
        self._refresh_ports()

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # --- Port row ---
        port_frame = ttk.LabelFrame(self, text="Serial Port")
        port_frame.grid(row=0, column=0, sticky="ew", **pad)

        self._port_var = tk.StringVar()
        self._port_cb = ttk.Combobox(port_frame, textvariable=self._port_var,
                                     state="readonly", width=30)
        self._port_cb.grid(row=0, column=0, padx=4, pady=4)

        ttk.Button(port_frame, text="Refresh", command=self._refresh_ports
                   ).grid(row=0, column=1, padx=4, pady=4)

        self._baud_var = tk.StringVar(value="115200")
        ttk.Label(port_frame, text="Baud:").grid(row=0, column=2, padx=(8, 0))
        ttk.Combobox(port_frame, textvariable=self._baud_var,
                     values=BAUD_RATES, state="readonly", width=8
                     ).grid(row=0, column=3, padx=4, pady=4)

        self._port_cb.bind("<<ComboboxSelected>>", lambda _: self._auto_detect_baud())

        # --- File row ---
        file_frame = ttk.LabelFrame(self, text="GCode File")
        file_frame.grid(row=1, column=0, sticky="ew", **pad)

        self._file_var = tk.StringVar()
        ttk.Entry(file_frame, textvariable=self._file_var, width=38,
                  state="readonly").grid(row=0, column=0, padx=4, pady=4)
        ttk.Button(file_frame, text="Browse…", command=self._browse
                   ).grid(row=0, column=1, padx=4, pady=4)

        # --- Progress ---
        prog_frame = ttk.Frame(self)
        prog_frame.grid(row=2, column=0, sticky="ew", **pad)

        self._progress = ttk.Progressbar(prog_frame, length=380, mode="determinate")
        self._progress.grid(row=0, column=0, padx=4, pady=4)

        self._status_var = tk.StringVar(value="Ready")
        ttk.Label(prog_frame, textvariable=self._status_var, width=14,
                  anchor="w").grid(row=0, column=1, padx=4)

        # --- Buttons ---
        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=3, column=0, **pad)

        self._send_btn = ttk.Button(btn_frame, text="Send", width=14,
                                    command=self._start_send)
        self._send_btn.grid(row=0, column=0, padx=6)

        self._cancel_btn = ttk.Button(btn_frame, text="Cancel", width=14,
                                      command=self._cancel, state="disabled")
        self._cancel_btn.grid(row=0, column=1, padx=6)

        # --- Log ---
        log_frame = ttk.LabelFrame(self, text="Log")
        log_frame.grid(row=4, column=0, sticky="nsew", **pad)

        self._log = scrolledtext.ScrolledText(log_frame, width=58, height=14,
                                              state="disabled",
                                              font=("Monospace", 9))
        self._log.grid(row=0, column=0, padx=4, pady=4)

        # --- Webcam ---
        cam_frame = ttk.LabelFrame(self, text="Webcam")
        cam_frame.grid(row=5, column=0, sticky="ew", **pad)

        cam_ctrl = ttk.Frame(cam_frame)
        cam_ctrl.grid(row=0, column=0, sticky="w", padx=4, pady=(4, 0))

        ttk.Label(cam_ctrl, text="Camera:").grid(row=0, column=0, padx=(0, 4))
        self._cam_index_var = tk.StringVar(value="0")
        ttk.Combobox(cam_ctrl, textvariable=self._cam_index_var,
                     values=["0", "1", "2", "3"], state="readonly", width=4
                     ).grid(row=0, column=1, padx=(0, 8))

        self._cam_btn = ttk.Button(cam_ctrl, text="Start Webcam",
                                   command=self._toggle_webcam)
        self._cam_btn.grid(row=0, column=2, padx=4)

        self._cam_label = ttk.Label(cam_frame, background="black",
                                    width=WEBCAM_W, anchor="center")
        self._cam_label.grid(row=1, column=0, padx=4, pady=4)
        self._cam_label.grid_remove()  # hidden until started

        # --- Copyright ---
        ttk.Label(self, text="by Boyo Labs, 2026", anchor="center"
                  ).grid(row=6, column=0, sticky="ew", pady=(2, 6))

    # ---------------------------------------------------------------- Logic --

    def _refresh_ports(self):
        ports = list_ports()
        names = [f"{p.device}  ({p.description})" for p in ports]
        self._port_cb["values"] = names
        self._port_map = {n: p.device for n, p in zip(names, ports)}
        if names:
            self._port_cb.current(0)
            self._auto_detect_baud()

    def _auto_detect_baud(self):
        port_label = self._port_var.get()
        if not port_label:
            return
        # Don't run a second detection while one is already in progress
        if self._baud_detect_thread and self._baud_detect_thread.is_alive():
            return
        device = self._port_map.get(port_label)
        if not device:
            return
        self._send_btn.configure(state="disabled")
        self._status_var.set("Detecting…")
        self._baud_detect_thread = threading.Thread(
            target=self._detect_baud_worker, args=(device,), daemon=True
        )
        self._baud_detect_thread.start()

    def _detect_baud_worker(self, device):
        import time
        detected = None
        for rate in BAUD_RATES:
            try:
                with serial.Serial(device, int(rate), timeout=1) as ser:
                    ser.write(b"\r\nM115\r\n")
                    time.sleep(1.2)
                    response = ser.read(ser.in_waiting).decode(errors="replace").lower()
                    if any(kw in response for kw in ("ok", "firmware", "marlin", "echo", "start")):
                        detected = rate
                        break
            except serial.SerialException:
                break
        if detected:
            self.after(0, self._on_baud_detected, detected)
        else:
            self.after(0, self._on_baud_detected, None)

    def _on_baud_detected(self, rate):
        if rate:
            self._baud_var.set(rate)
            self._status_var.set(f"Baud: {rate}")
            self._log_line(f"Auto-detected baud rate: {rate}")
        else:
            self._status_var.set("Ready")
            self._log_line("Could not auto-detect baud rate — defaulting to 115200.")
        self._send_btn.configure(state="normal")

    def _browse(self):
        path = filedialog.askopenfilename(
            title="Select GCode file",
            filetypes=[("GCode files", "*.gcode *.gco *.g *.nc"), ("All files", "*.*")],
            initialdir=os.path.expanduser("~"),
        )
        if path:
            self._file_var.set(path)

    def _log_line(self, text):
        self._log.configure(state="normal")
        self._log.insert("end", text + "\n")
        self._log.see("end")
        self._log.configure(state="disabled")

    def _set_sending(self, active):
        state_send = "disabled" if active else "normal"
        state_cancel = "normal" if active else "disabled"
        self._send_btn.configure(state=state_send)
        self._cancel_btn.configure(state=state_cancel)

    def _start_send(self):
        port_label = self._port_var.get()
        if not port_label:
            messagebox.showerror("No port", "Please select a serial port.")
            return
        filepath = self._file_var.get()
        if not filepath:
            messagebox.showerror("No file", "Please select a GCode file.")
            return

        device = self._port_map[port_label]
        baud = int(self._baud_var.get())

        self._cancel_flag.clear()
        self._set_sending(True)
        self._progress["value"] = 0
        self._log_line(f"Connecting to {device} @ {baud} baud…")

        self._send_thread = threading.Thread(
            target=self._send_worker, args=(device, baud, filepath), daemon=True
        )
        self._send_thread.start()

    def _cancel(self):
        self._cancel_flag.set()
        self._log_line("Cancelling…")

    def _send_worker(self, device, baud, filepath):
        try:
            with serial.Serial(device, baud, timeout=10) as ser:
                self._serial = ser
                self.after(0, self._log_line, "Connected. Waiting for printer ready…")
                ser.write(b"\r\n\r\n")
                import time; time.sleep(2)
                ser.reset_input_buffer()

                with open(filepath, "r", errors="replace") as f:
                    lines = f.readlines()

                total = len(lines)
                sent = 0

                for raw in lines:
                    if self._cancel_flag.is_set():
                        self.after(0, self._log_line, "Cancelled.")
                        break

                    line = raw.strip()
                    cmd = line.split(";")[0].strip()
                    if not cmd:
                        sent += 1
                        continue

                    ser.write((cmd + "\n").encode())

                    while True:
                        if self._cancel_flag.is_set():
                            break
                        resp = ser.readline().decode(errors="replace").strip()
                        if resp:
                            self.after(0, self._log_line, f"  < {resp}")
                        if resp.lower().startswith("ok"):
                            break
                        if resp.lower().startswith("error"):
                            self.after(0, messagebox.showerror,
                                       "Printer error", f"Printer reported: {resp}")
                            self._cancel_flag.set()
                            break

                    sent += 1
                    pct = int(sent / total * 100)
                    status = f"{pct}%  ({sent}/{total})"
                    self.after(0, self._update_progress, pct, status)

        except serial.SerialException as e:
            self.after(0, messagebox.showerror, "Serial error", str(e))
            self.after(0, self._log_line, f"Error: {e}")
        finally:
            self._serial = None
            self.after(0, self._finish)

    def _update_progress(self, pct, status):
        self._progress["value"] = pct
        self._status_var.set(status)

    def _finish(self):
        self._set_sending(False)
        if not self._cancel_flag.is_set():
            self._progress["value"] = 100
            self._status_var.set("Done!")
            self._log_line("File sent successfully.")
        else:
            self._status_var.set("Cancelled")

    # -------------------------------------------------------------- Webcam --

    def _toggle_webcam(self):
        if self._cap is None:
            self._start_webcam()
        else:
            self._stop_webcam()

    def _start_webcam(self):
        idx = int(self._cam_index_var.get())
        cap = cv2.VideoCapture(idx)
        if not cap.isOpened():
            messagebox.showerror("Webcam error",
                                 f"Could not open camera {idx}. Try a different index.")
            cap.release()
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, WEBCAM_W)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, WEBCAM_H)
        self._cap = cap
        self._cam_btn.configure(text="Stop Webcam")
        self._cam_label.grid()
        self._poll_webcam()

    def _poll_webcam(self):
        if self._cap is None:
            return
        ret, frame = self._cap.read()
        if ret:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(frame)
            img.thumbnail((WEBCAM_W, WEBCAM_H), Image.NEAREST)
            photo = ImageTk.PhotoImage(img)
            self._cam_label.configure(image=photo)
            self._cam_label._photo = photo  # keep reference
        self._webcam_job = self.after(33, self._poll_webcam)  # ~30 fps

    def _stop_webcam(self):
        if self._webcam_job is not None:
            self.after_cancel(self._webcam_job)
            self._webcam_job = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        self._cam_label.configure(image="")
        self._cam_label.grid_remove()
        self._cam_btn.configure(text="Start Webcam")

    def destroy(self):
        self._stop_webcam()
        super().destroy()


if __name__ == "__main__":
    app = GCodeSender()
    app.mainloop()
