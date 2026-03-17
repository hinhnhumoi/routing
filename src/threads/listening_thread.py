"""
Listening Thread - reads from STDIN and handles incoming socket messages.
Processes UPDATE packets and dynamic commands.
"""
import threading
import sys
import socket

from command_handler import CommandHandler


class ListeningThread(threading.Thread):
    def __init__(self, node):
        super().__init__()
        self.node = node
        self.command_handler = CommandHandler(node)

    def run(self):
        # Start socket listener in a separate thread
        socket_thread = threading.Thread(target=self._listen_socket, daemon=True)
        socket_thread.start()

        # Read from STDIN
        self._listen_stdin()

    def _listen_stdin(self):
        """Continuously read commands from STDIN."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            self._process_message(line)

    def _listen_socket(self):
        """Listen for incoming UDP messages from other nodes."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(('localhost', self.node.port))
        except OSError as e:
            print(f"Error: Could not bind to port {self.node.port}: {e}", flush=True)
            return
        while True:
            data, addr = sock.recvfrom(65535)
            message = data.decode('utf-8').strip()
            if message:
                self._process_message(message, from_port=addr[1])

    def _process_message(self, message, from_port=None):
        """Route message to appropriate handler."""
        with self.node.lock:
            if message.startswith("LSA "):
                self.command_handler.handle_lsa(message, from_port)
                return
            elif message.startswith("UPDATE "):
                # Record heartbeat from source node
                parts = message.split()
                if len(parts) >= 2:
                    self.node.record_heartbeat(parts[1])
                self.command_handler.handle_update(message)
            elif self._looks_like_update(message):
                # Malformed update packet (e.g. "UPD8 Source A:2.3:6000")
                self.command_handler.handle_update(message)
            else:
                self.command_handler.handle_command(message)

    @staticmethod
    def _looks_like_update(message):
        """Check if message looks like a malformed update packet.
        Detects messages like 'UPD8 Source A:2.3:6000' - wrong keyword but update structure.
        Only triggers if the first word is NOT a known command keyword.
        """
        parts = message.split()
        if len(parts) < 3:
            return False
        # Known command keywords — these should go through handle_command, not handle_update
        known_commands = {"CHANGE", "FAIL", "RECOVER", "QUERY", "RESET",
                          "BATCH", "CYCLE", "MERGE", "SPLIT", "SHOW"}
        if parts[0].upper() in known_commands:
            return False
        # Check if the last part contains colon-separated entries like "A:2.3:6000"
        for entry in parts[-1].split(','):
            tokens = entry.split(':')
            if len(tokens) == 3:
                return True
        return False
