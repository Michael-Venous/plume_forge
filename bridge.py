import os
import queue
import subprocess
import threading

from .protocol import (
    CANCEL,
    CANCELLED,
    FAILED,
    FRAME,
    SESSION_BEGIN,
    SESSION_END,
    SESSION_RESET,
    read_message,
    write_message,
)
from .utils import process_environment


class BridgeWorker:
    def __init__(self, executable, session, *, keep_alive=False):
        self.responses = queue.Queue()
        self._requests = queue.Queue(maxsize=2)
        self._executable = executable
        self._session = session
        self._keep_alive = keep_alive
        self._process = None
        self._stderr = []
        self._cancel_requested = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def send_frame(self, frame):
        self._requests.put_nowait((FRAME, frame.header, frame.payload))

    def end_session(self):
        self._requests.put_nowait((SESSION_END, {}, b""))

    def reset_session(self, session):
        self._session = session
        self._requests.put_nowait((SESSION_RESET, session, b""))

    def cancel(self):
        self._cancel_requested.set()
        try:
            self._requests.put_nowait((CANCEL, {}, b""))
        except queue.Full:
            pass
        process = self._process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def close(self, timeout=0.5):
        self.cancel()
        self._thread.join(timeout=timeout)
        process = self._process
        if self._thread.is_alive() and process and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        self._thread.join(timeout=timeout)

    def poll(self):
        responses = []
        while True:
            try:
                responses.append(self.responses.get_nowait())
            except queue.Empty:
                return responses

    def stderr(self):
        return "".join(self._stderr).strip()

    def _run(self):
        try:
            self._process = subprocess.Popen(
                [self._executable],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=process_environment(),
                **_popen_options(),
            )
            threading.Thread(target=self._drain_stderr, daemon=True).start()

            self._receive()
            write_message(self._process.stdin, SESSION_BEGIN, self._session)
            self._receive()

            while True:
                message_type, data, payload = self._requests.get()
                if self._process.poll() is not None:
                    raise RuntimeError("Bridge exited before the next frame was sent")
                write_message(self._process.stdin, message_type, data, payload)
                self._receive()
                if (
                    message_type == FRAME
                    and int(data["frame"]) >= self._session["end_frame"]
                    and not self._keep_alive
                ):
                    write_message(self._process.stdin, SESSION_END, {})
                    self._receive()
                    break
                if message_type in {SESSION_BEGIN, SESSION_RESET}:
                    continue
                if message_type in {CANCEL, SESSION_END} and not self._keep_alive:
                    break
        except Exception as error:
            message_type = (
                CANCELLED if self._cancel_requested.is_set() else FAILED
            )
            self.responses.put((
                message_type,
                {
                    "message": (
                        "Bake stopped"
                        if message_type == CANCELLED
                        else str(error)
                    ),
                    "stderr": self.stderr(),
                },
                b"",
            ))
        finally:
            self._close_process()

    def _receive(self):
        response = read_message(self._process.stdout)
        if response[0] == FAILED:
            raise RuntimeError(response[1].get("message", "Bridge failed"))
        self.responses.put(response)

    def _drain_stderr(self):
        while True:
            chunk = self._process.stderr.readline()
            if not chunk:
                return
            self._stderr.append(chunk.decode("utf-8", errors="replace"))
            if len(self._stderr) > 200:
                del self._stderr[:50]

    def _close_process(self):
        process = self._process
        if not process:
            return
        if process.stdin:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _popen_options():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}
